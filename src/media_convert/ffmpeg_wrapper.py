"""Minimal local FFmpeg wrapper for WebM -> MP4 conversion.

The wrapper only invokes local ``ffmpeg``/``ffprobe`` executables through an
argument list with ``shell=False``.  It tries a stream copy first and falls
back to a controlled re-encode.  Inputs are never modified and outputs are
written atomically through a temporary file.

FFmpeg is an external, explicitly user-invoked tool.  This module never
downloads binaries and never accepts a URL as an input.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

logger = logging.getLogger(__name__)

HARDWARE_CHOICES = ("none", "nvenc", "qsv", "vaapi")
VIDEO_CODEC_CHOICES = ("h264", "hevc")
SUPPORTED_OUTPUT_SUFFIXES = {".mp4", ".mkv", ".webm"}


class MediaError(RuntimeError):
    """Raised for invalid media inputs or FFmpeg failures."""


@dataclass(frozen=True, slots=True)
class MediaInfo:
    """The small subset of ffprobe output this tool needs."""

    path: Path
    format_name: str
    duration: float | None
    video_codec: str | None
    audio_codec: str | None
    width: int | None
    height: int | None
    has_video: bool
    has_audio: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "format_name": self.format_name,
            "duration": self.duration,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "width": self.width,
            "height": self.height,
            "has_video": self.has_video,
            "has_audio": self.has_audio,
        }


@dataclass(frozen=True, slots=True)
class FfmpegOptions:
    """Conversion settings; all values are validated before a command runs."""

    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    hardware: str = "none"
    hardware_codec: str = "h264"
    crf: int = 23
    preset: str = "medium"
    audio_bitrate: str = "192k"
    timeout: float = 3600.0
    overwrite: bool = False
    vaapi_device: str = "/dev/dri/renderD128"
    force_reencode: bool = False

    def __post_init__(self) -> None:
        if self.hardware not in HARDWARE_CHOICES:
            raise MediaError(f"不支持的硬件编码器: {self.hardware}")
        if self.hardware_codec not in VIDEO_CODEC_CHOICES:
            raise MediaError(f"不支持的视频编码: {self.hardware_codec}")
        if not 0 <= self.crf <= 51:
            raise MediaError("crf 必须在 0..51 之间")
        if self.timeout <= 0:
            raise MediaError("timeout 必须大于 0")
        if not re.fullmatch(r"\d+(?:\.\d+)?[kKmM]?", self.audio_bitrate):
            raise MediaError(f"音频码率格式非法: {self.audio_bitrate}")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", self.preset):
            raise MediaError(f"preset 格式非法: {self.preset!r}")
        if not self.vaapi_device or any(ord(char) < 32 for char in self.vaapi_device):
            raise MediaError("vaapi_device 路径非法")


@dataclass(slots=True)
class ConversionResult:
    """Outcome of one conversion attempt, including fallback diagnostics."""

    success: bool
    input_path: Path
    output_path: Path
    media: MediaInfo
    stream_copy: bool
    hardware: str
    commands: list[list[str]] = field(default_factory=list)
    attempts: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    message: str = ""
    stderr_tail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "input": str(self.input_path),
            "output": str(self.output_path),
            "media": self.media.to_dict(),
            "stream_copy": self.stream_copy,
            "hardware": self.hardware,
            "commands": self.commands,
            "attempts": self.attempts,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "message": self.message,
            "stderr_tail": self.stderr_tail,
        }


def is_webm(path: str | Path, info: MediaInfo | None = None) -> bool:
    """Identify WebM by extension, container name or VPx/Opus stream hints."""

    media_path = Path(path)
    if media_path.suffix.casefold() == ".webm":
        return True
    if info is not None:
        format_name = info.format_name.casefold()
        if "webm" in format_name:
            return True
        video = (info.video_codec or "").casefold()
        audio = (info.audio_codec or "").casefold()
        if video in {"vp8", "vp9", "av1"} and audio in {"opus", "vorbis", ""}:
            return True
    return False


def _decode_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _resolve_binary(name: str) -> str:
    candidate = Path(name)
    if candidate.is_file():
        return str(candidate)
    found = shutil.which(name)
    if not found:
        raise MediaError(
            f"未找到 {name}；请先安装 FFmpeg 并确保它在 PATH 中，或显式传入可执行文件路径"
        )
    return found


def _run(
    command: Sequence[str],
    *,
    timeout: float,
    runner: Callable[..., Any] | None = None,
) -> Any:
    """Run a local command without a shell and with bounded output."""

    logger.debug("执行本地媒体命令: %s", command)
    runner = runner or subprocess.run
    try:
        return runner(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"FFmpeg 命令超时（{timeout} 秒）") from exc
    except OSError as exc:
        raise MediaError(f"无法启动本地媒体命令: {exc}") from exc


def _tail(text: str, limit: int = 4000) -> str:
    return text[-limit:] if len(text) > limit else text


def probe_media(
    path: str | Path,
    *,
    options: FfmpegOptions | None = None,
    runner: Callable[..., Any] | None = None,
) -> MediaInfo:
    """Run local ``ffprobe`` and return a small media summary."""

    options = options or FfmpegOptions()
    media_path = Path(path).expanduser()
    try:
        media_path = media_path.resolve(strict=True)
    except OSError as exc:
        raise MediaError(f"无法访问媒体文件 {media_path}: {exc}") from exc
    if not media_path.is_file():
        raise MediaError(f"媒体路径不是文件: {media_path}")
    ffprobe = _resolve_binary(options.ffprobe_path)
    command = [
        ffprobe,
        "-v",
        "error",
        "-protocol_whitelist",
        "file",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(media_path),
    ]
    completed = _run(command, timeout=min(options.timeout, 120.0), runner=runner)
    stderr = _decode_output(getattr(completed, "stderr", None))
    if getattr(completed, "returncode", 1) != 0:
        raise MediaError(f"ffprobe 读取失败: {_tail(stderr) or '未知错误'}")
    try:
        payload = json.loads(_decode_output(getattr(completed, "stdout", "")) or "{}")
    except json.JSONDecodeError as exc:
        raise MediaError(f"ffprobe 输出不是合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise MediaError("ffprobe 输出结构非法")

    streams = payload.get("streams") or []
    if not isinstance(streams, list):
        streams = []
    video_stream = next(
        (item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"),
        None,
    )
    audio_stream = next(
        (item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"),
        None,
    )
    format_info = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    duration: float | None
    try:
        duration = float(format_info.get("duration")) if format_info.get("duration") is not None else None
    except (TypeError, ValueError):
        duration = None
    width = height = None
    if video_stream:
        try:
            width = int(video_stream["width"]) if video_stream.get("width") is not None else None
            height = int(video_stream["height"]) if video_stream.get("height") is not None else None
        except (TypeError, ValueError):
            width = height = None
    return MediaInfo(
        path=media_path,
        format_name=str(format_info.get("format_name") or ""),
        duration=duration,
        video_codec=(video_stream or {}).get("codec_name"),
        audio_codec=(audio_stream or {}).get("codec_name"),
        width=width,
        height=height,
        has_video=video_stream is not None,
        has_audio=audio_stream is not None,
    )


def _base_command(ffmpeg: str, input_path: Path, output_path: Path, overwrite: bool) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-protocol_whitelist",
        "file",
        "-y" if overwrite else "-n",
        "-i",
        str(input_path),
    ]


def _output_arguments(output_path: Path, info: MediaInfo) -> list[str]:
    args = ["-map", "0:v:0?", "-map", "0:a:0?"]
    if output_path.suffix.casefold() == ".mp4":
        args.extend(["-movflags", "+faststart"])
    return args


def _copy_command(
    ffmpeg: str, input_path: Path, output_path: Path, info: MediaInfo, overwrite: bool
) -> list[str]:
    command = _base_command(ffmpeg, input_path, output_path, overwrite)
    command.extend(_output_arguments(output_path, info))
    command.extend(["-c", "copy", str(output_path)])
    return command


def _video_encoder(options: FfmpegOptions, output_path: Path) -> str:
    suffix = output_path.suffix.casefold()
    if suffix == ".webm":
        if options.hardware != "none":
            raise MediaError("WebM 输出不支持所选硬件编码器")
        return "libvpx-vp9"
    if options.hardware == "nvenc":
        return "hevc_nvenc" if options.hardware_codec == "hevc" else "h264_nvenc"
    if options.hardware == "qsv":
        return "hevc_qsv" if options.hardware_codec == "hevc" else "h264_qsv"
    if options.hardware == "vaapi":
        return "hevc_vaapi" if options.hardware_codec == "hevc" else "h264_vaapi"
    return "libx265" if options.hardware_codec == "hevc" else "libx264"


def _reencode_command(
    ffmpeg: str,
    input_path: Path,
    output_path: Path,
    info: MediaInfo,
    options: FfmpegOptions,
) -> list[str]:
    suffix = output_path.suffix.casefold()
    encoder = _video_encoder(options, output_path)
    command: list[str] = []
    if options.hardware == "vaapi":
        command.extend([ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
                        "-protocol_whitelist", "file", "-vaapi_device", options.vaapi_device])
    else:
        command = _base_command(ffmpeg, input_path, output_path, options.overwrite)
    if options.hardware == "vaapi":
        command.extend(["-y" if options.overwrite else "-n", "-i", str(input_path)])

    command.extend(_output_arguments(output_path, info))
    command.extend(["-c:v", encoder])
    if suffix == ".webm":
        command.extend(["-crf", str(options.crf), "-b:v", "0"])
    elif options.hardware == "nvenc":
        command.extend(["-preset", "p5", "-cq", str(options.crf), "-b:v", "0"])
    elif options.hardware == "qsv":
        command.extend(["-preset", options.preset, "-global_quality", str(options.crf)])
    elif options.hardware == "vaapi":
        command.extend(["-vf", "format=nv12,hwupload", "-qp", str(options.crf)])
    else:
        command.extend(["-preset", options.preset, "-crf", str(options.crf), "-pix_fmt", "yuv420p"])

    if info.has_audio:
        if suffix == ".webm":
            command.extend(["-c:a", "libopus", "-b:a", options.audio_bitrate])
        else:
            command.extend(["-c:a", "aac", "-b:a", options.audio_bitrate])
    else:
        command.append("-an")
    command.append(str(output_path))
    return command


def _temporary_output(output_path: Path) -> Path:
    return output_path.with_name(
        f".{output_path.stem}.{uuid.uuid4().hex}.part{output_path.suffix}"
    )


def convert_webm(
    input_path: str | Path,
    output_path: str | Path,
    *,
    options: FfmpegOptions | None = None,
    runner: Callable[..., Any] | None = None,
) -> ConversionResult:
    """Convert a local WebM file, preferring stream copy and falling back.

    The output is written to a temporary file in the destination directory and
    atomically renamed only after FFmpeg succeeds.  The input is never modified.
    """

    started = time.monotonic()
    options = options or FfmpegOptions()
    source = Path(input_path).expanduser()
    destination = Path(output_path).expanduser()
    try:
        source_resolved = source.resolve(strict=True)
    except OSError as exc:
        raise MediaError(f"无法访问输入媒体 {source}: {exc}") from exc
    if not source_resolved.is_file():
        raise MediaError(f"输入路径不是文件: {source_resolved}")
    if destination.suffix.casefold() not in SUPPORTED_OUTPUT_SUFFIXES:
        raise MediaError(
            f"不支持的输出格式 {destination.suffix!r}；"
            f"可选 {', '.join(sorted(SUPPORTED_OUTPUT_SUFFIXES))}"
        )
    try:
        destination_resolved = destination.resolve(strict=False)
    except OSError as exc:
        raise MediaError(f"无法解析输出路径 {destination}: {exc}") from exc
    if destination_resolved == source_resolved:
        raise MediaError("输出路径不能与输入相同")
    try:
        if destination.absolute().is_symlink():
            raise MediaError(f"拒绝使用符号链接作为输出: {destination}")
    except OSError as exc:
        raise MediaError(f"无法检查输出路径 {destination}: {exc}") from exc
    if destination_resolved.exists() and not options.overwrite:
        raise MediaError(f"输出文件已存在（如需覆盖请显式允许）: {destination_resolved}")
    if destination_resolved.is_symlink():
        raise MediaError(f"拒绝使用符号链接作为输出: {destination_resolved}")

    info = probe_media(source_resolved, options=options, runner=runner)
    if not is_webm(source_resolved, info):
        raise MediaError(f"输入不是可识别的 WebM 媒体: {source_resolved}")
    if not info.has_video:
        raise MediaError("输入媒体没有视频流，无法转换")

    try:
        destination_resolved.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MediaError(f"无法创建输出目录 {destination_resolved.parent}: {exc}") from exc

    ffmpeg = _resolve_binary(options.ffmpeg_path)
    result = ConversionResult(
        success=False,
        input_path=source_resolved,
        output_path=destination_resolved,
        media=info,
        stream_copy=False,
        hardware=options.hardware,
    )

    temporary = _temporary_output(destination_resolved)
    if options.force_reencode:
        result.attempts.append("已配置 force_reencode，跳过流复制")
    else:
        copy_command = _copy_command(
            ffmpeg, source_resolved, temporary, info, options.overwrite
        )
        result.commands.append(copy_command)
        try:
            completed = _run(copy_command, timeout=options.timeout, runner=runner)
        except MediaError:
            _cleanup(temporary)
            raise
        copy_error = _decode_output(getattr(completed, "stderr", None))
        if getattr(completed, "returncode", 1) == 0 and temporary.is_file() and temporary.stat().st_size > 0:
            try:
                if destination_resolved.exists() and not options.overwrite:
                    raise MediaError("输出文件在转换期间出现，未覆盖")
                os.replace(temporary, destination_resolved)
            except MediaError:
                _cleanup(temporary)
                raise
            except OSError as exc:
                _cleanup(temporary)
                raise MediaError(f"无法完成输出文件替换: {exc}") from exc
            result.success = True
            result.stream_copy = True
            result.elapsed_seconds = time.monotonic() - started
            result.message = "流复制成功"
            result.stderr_tail = _tail(copy_error)
            return result

        result.attempts.append(f"流复制失败: {_tail(copy_error) or 'ffmpeg 未生成有效输出'}")
        _cleanup(temporary)

    try:
        reencode_command = _reencode_command(
            ffmpeg, source_resolved, temporary, info, options
        )
    except MediaError:
        raise
    result.commands.append(reencode_command)
    try:
        completed = _run(reencode_command, timeout=options.timeout, runner=runner)
    except MediaError:
        _cleanup(temporary)
        raise
    stderr = _decode_output(getattr(completed, "stderr", None))
    if getattr(completed, "returncode", 1) != 0:
        _cleanup(temporary)
        raise MediaError(f"重编码失败: {_tail(stderr) or '未知错误'}")
    if not temporary.is_file() or temporary.stat().st_size == 0:
        _cleanup(temporary)
        raise MediaError("FFmpeg 报告成功但没有生成有效输出文件")
    try:
        if destination_resolved.exists() and not options.overwrite:
            raise MediaError("输出文件在转换期间出现，未覆盖")
        os.replace(temporary, destination_resolved)
    except MediaError:
        _cleanup(temporary)
        raise
    except OSError as exc:
        _cleanup(temporary)
        raise MediaError(f"无法完成输出文件替换: {exc}") from exc

    result.success = True
    result.stream_copy = False
    result.elapsed_seconds = time.monotonic() - started
    result.message = "流复制不可用，已重编码"
    result.stderr_tail = _tail(stderr)
    return result


def _cleanup(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        logger.warning("无法清理媒体临时文件: %s", path)
