"""WebM -> MP4 conversion based on the mp4_merger.py v4.5 strategy.

Key ideas ported from the reference implementation:

* probe duration, codecs, pixel format and bit depth;
* verify GPU encoders with a one-second test encode before using them;
* automatically fall back to CPU when a GPU encoder is missing or fails;
* high-quality defaults (CRF 16, AAC 320k), 8-bit 4:2:0 compatibility unless
  the source is genuinely 10-bit;
* optional stream-copy fast path;
* temporary output plus atomic rename, input never modified.

Only local executables are called, via argument lists with ``shell=False`` and
the ``file`` protocol whitelist.  No network access is performed.
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

HARDWARE_CHOICES = ("auto", "cpu", "nvidia", "intel", "amd", "vaapi")
_HARDWARE_ALIASES = {
    "none": "cpu",
    "nvenc": "nvidia",
    "qsv": "intel",
    "amf": "amd",
    "automatic": "auto",
}
SUPPORTED_OUTPUT_SUFFIXES = {".mp4"}
DEFAULT_CRF = 16
DEFAULT_AUDIO_BITRATE = "320k"
_GPU_CANDIDATES = (
    ("nvidia", "h264_nvenc"),
    ("intel", "h264_qsv"),
    ("amd", "h264_amf"),
)


class MediaError(RuntimeError):
    """Raised for invalid media inputs or FFmpeg failures."""


def normalize_hardware(value: str) -> str:
    key = str(value or "auto").strip().casefold()
    key = _HARDWARE_ALIASES.get(key, key)
    if key not in HARDWARE_CHOICES:
        raise MediaError(f"不支持的硬件编码器: {value}")
    return key


@dataclass(frozen=True, slots=True)
class MediaInfo:
    """The subset of ffprobe output used by the converter."""

    path: Path
    format_name: str
    duration: float | None
    video_codec: str | None
    audio_codec: str | None
    width: int | None
    height: int | None
    pix_fmt: str = ""
    bit_depth: int = 8
    size: int = 0
    has_video: bool = False
    has_audio: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "format_name": self.format_name,
            "duration": self.duration,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "width": self.width,
            "height": self.height,
            "pix_fmt": self.pix_fmt,
            "bit_depth": self.bit_depth,
            "size": self.size,
            "has_video": self.has_video,
            "has_audio": self.has_audio,
        }


@dataclass(frozen=True, slots=True)
class FfmpegOptions:
    """Conversion settings; all values are validated before a command runs."""

    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    hardware: str = "auto"
    crf: int = DEFAULT_CRF
    audio_bitrate: str = DEFAULT_AUDIO_BITRATE
    timeout: float = 3600.0
    overwrite: bool = False
    stream_copy_first: bool = False
    force_8bit: bool = False
    fast: bool = False
    vaapi_device: str = "/dev/dri/renderD128"

    def __post_init__(self) -> None:
        normalize_hardware(self.hardware)
        if not 0 <= self.crf <= 51:
            raise MediaError("crf 必须在 0..51 之间")
        if self.timeout <= 0:
            raise MediaError("timeout 必须大于 0")
        if not re.fullmatch(r"\d+(?:\.\d+)?[kKmM]?", self.audio_bitrate):
            raise MediaError(f"音频码率格式非法: {self.audio_bitrate}")
        if not self.vaapi_device or any(ord(char) < 32 for char in self.vaapi_device):
            raise MediaError("vaapi_device 路径非法")

    @property
    def hardware_key(self) -> str:
        return normalize_hardware(self.hardware)


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
        for encoding in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
            try:
                return value.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
        return value.decode("utf-8", "replace")
    return value


def _resolve_binary(name: str) -> str:
    candidate = Path(name)
    if candidate.is_file():
        return str(candidate)
    found = shutil.which(name)
    if not found:
        raise MediaError(
            f"未找到 {name}；请先安装 FFmpeg 并确保它在 PATH 中，或显式传入路径"
        )
    return found


def _windows_creationflags() -> int:
    if os.name == "nt":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


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
            creationflags=_windows_creationflags(),
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"FFmpeg 命令超时（{timeout} 秒）") from exc
    except OSError as exc:
        raise MediaError(f"无法启动本地媒体命令: {exc}") from exc


def _tail(text: str, limit: int = 4000) -> str:
    return text[-limit:] if len(text) > limit else text


def _parse_bit_depth(pix_fmt: str) -> int:
    match = re.search(r"p(\d{1,2})(?:le|be)?$", pix_fmt or "")
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            pass
    return 8


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
    video_stream = None
    audio_stream = None
    for item in streams:
        if not isinstance(item, dict):
            continue
        if item.get("codec_type") == "video" and video_stream is None:
            disposition = item.get("disposition") or {}
            if not disposition.get("attached_pic"):
                video_stream = item
        elif item.get("codec_type") == "audio" and audio_stream is None:
            audio_stream = item
    format_info = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    try:
        duration = (
            float(format_info.get("duration"))
            if format_info.get("duration") is not None
            else None
        )
    except (TypeError, ValueError):
        duration = None
    width = height = None
    pix_fmt = ""
    if video_stream:
        try:
            width = int(video_stream["width"]) if video_stream.get("width") is not None else None
            height = int(video_stream["height"]) if video_stream.get("height") is not None else None
        except (TypeError, ValueError):
            width = height = None
        pix_fmt = str(video_stream.get("pix_fmt") or "")
    return MediaInfo(
        path=media_path,
        format_name=str(format_info.get("format_name") or ""),
        duration=duration,
        video_codec=(video_stream or {}).get("codec_name"),
        audio_codec=(audio_stream or {}).get("codec_name"),
        width=width,
        height=height,
        pix_fmt=pix_fmt,
        bit_depth=_parse_bit_depth(pix_fmt),
        size=media_path.stat().st_size,
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


def _copy_command(
    ffmpeg: str, input_path: Path, output_path: Path, info: MediaInfo, overwrite: bool
) -> list[str]:
    command = _base_command(ffmpeg, input_path, output_path, overwrite)
    command.extend(["-map", "0:v:0", "-map", "0:a:0?", "-c", "copy"])
    command.extend(["-movflags", "+faststart", str(output_path)])
    return command


def _decide_pix_args(info: MediaInfo, options: FfmpegOptions) -> list[str]:
    """Port of mp4_merger's 8-bit compatibility decision for one source."""

    if info.bit_depth >= 10 and not options.force_8bit:
        return []
    return ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p"]


def _cpu_preset(options: FfmpegOptions) -> str:
    cores = os.cpu_count() or 4
    return "veryfast" if (options.fast or cores <= 4) else "slow"


def _video_args(hardware: str, options: FfmpegOptions) -> tuple[str, list[str]]:
    if hardware == "nvidia":
        preset = "p1" if options.fast else "p4"
        return (
            "h264_nvenc",
            ["-c:v", "h264_nvenc", "-preset", preset, "-rc", "vbr",
             "-cq", str(options.crf), "-b:v", "0"],
        )
    if hardware == "intel":
        preset = "veryfast" if options.fast else "medium"
        return (
            "h264_qsv",
            ["-c:v", "h264_qsv", "-preset", preset,
             "-global_quality", str(options.crf)],
        )
    if hardware == "amd":
        quality = "speed" if options.fast else "balanced"
        return (
            "h264_amf",
            ["-c:v", "h264_amf", "-quality", quality,
             "-qp_i", str(options.crf), "-qp_p", str(options.crf)],
        )
    if hardware == "vaapi":
        return (
            "h264_vaapi",
            ["-c:v", "h264_vaapi", "-qp", str(options.crf)],
        )
    return (
        "libx264",
        ["-c:v", "libx264", "-preset", _cpu_preset(options),
         "-crf", str(options.crf)],
    )


def _transcode_command(
    ffmpeg: str,
    input_path: Path,
    output_path: Path,
    info: MediaInfo,
    options: FfmpegOptions,
    hardware: str,
) -> list[str]:
    if hardware == "vaapi":
        command = [
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file",
            "-vaapi_device",
            options.vaapi_device,
            "-y" if options.overwrite else "-n",
            "-i",
            str(input_path),
        ]
    else:
        command = _base_command(ffmpeg, input_path, output_path, options.overwrite)
    command.extend(["-map", "0:v:0", "-map", "0:a:0?"])
    if hardware == "vaapi":
        command.extend(["-vf", "format=nv12,hwupload"])
    else:
        command.extend(_decide_pix_args(info, options))
    _encoder, video = _video_args(hardware, options)
    command.extend(video)
    if info.has_audio:
        command.extend(
            ["-c:a", "aac", "-b:a", options.audio_bitrate,
             "-ar", "48000", "-ac", "2"]
        )
    else:
        command.append("-an")
    command.extend(
        [
            "-movflags",
            "+faststart",
            "-avoid_negative_ts",
            "make_zero",
            "-max_muxing_queue_size",
            "9999",
            str(output_path),
        ]
    )
    return command


def _encoder_list(ffmpeg: str, options: FfmpegOptions, runner: Callable[..., Any] | None) -> str:
    try:
        completed = _run(
            [ffmpeg, "-hide_banner", "-encoders"],
            timeout=min(options.timeout, 30.0),
            runner=runner,
        )
    except MediaError:
        return ""
    if getattr(completed, "returncode", 1) != 0:
        return ""
    return _decode_output(getattr(completed, "stdout", ""))


def _verify_encoder(
    ffmpeg: str,
    encoder: str,
    options: FfmpegOptions,
    runner: Callable[..., Any] | None,
) -> bool:
    """Actually encode one second; an enumerated encoder may lack a driver."""

    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=1:size=320x240:rate=10",
        "-c:v",
        encoder,
        "-f",
        "null",
        "-",
    ]
    try:
        completed = _run(command, timeout=min(options.timeout, 30.0), runner=runner)
    except MediaError:
        return False
    return getattr(completed, "returncode", 1) == 0


def _select_hardware(
    ffmpeg: str,
    options: FfmpegOptions,
    runner: Callable[..., Any] | None,
    attempts: list[str],
) -> str:
    requested = options.hardware_key
    if requested == "cpu":
        return "cpu"
    if requested == "vaapi":
        return "vaapi"
    if requested == "auto":
        encoders = _encoder_list(ffmpeg, options, runner)
        candidates = [
            (key, encoder)
            for key, encoder in _GPU_CANDIDATES
            if encoder in encoders
        ]
        if not candidates:
            attempts.append("未检测到可用硬件编码器，使用 CPU")
            return "cpu"
        for key, encoder in candidates:
            if _verify_encoder(ffmpeg, encoder, options, runner):
                attempts.append(f"硬件编码器验证通过: {encoder}")
                return key
            attempts.append(f"硬件编码器验证失败: {encoder}")
        attempts.append("所有硬件编码器不可用，使用 CPU")
        return "cpu"
    encoder = {"nvidia": "h264_nvenc", "intel": "h264_qsv", "amd": "h264_amf"}[requested]
    if _verify_encoder(ffmpeg, encoder, options, runner):
        attempts.append(f"硬件编码器验证通过: {encoder}")
        return requested
    attempts.append(f"硬件编码器不可用: {encoder}，使用 CPU")
    return "cpu"


def _temporary_output(output_path: Path) -> Path:
    return output_path.with_name(
        f".{output_path.stem}.{uuid.uuid4().hex}.part{output_path.suffix}"
    )


def _cleanup(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        logger.warning("无法清理媒体临时文件: %s", path)


def convert_webm(
    input_path: str | Path,
    output_path: str | Path,
    *,
    options: FfmpegOptions | None = None,
    runner: Callable[..., Any] | None = None,
) -> ConversionResult:
    """Convert a local WebM to MP4 using the mp4_merger-derived pipeline."""

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
        raise MediaError(f"不支持的输出格式 {destination.suffix!r}；当前只支持 .mp4")
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
        hardware="cpu",
    )
    temporary = _temporary_output(destination_resolved)

    if options.stream_copy_first:
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
        if (
            getattr(completed, "returncode", 1) == 0
            and temporary.is_file()
            and temporary.stat().st_size > 0
        ):
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
            result.hardware = "copy"
            result.elapsed_seconds = time.monotonic() - started
            result.message = "流复制成功"
            result.stderr_tail = _tail(copy_error)
            return result
        result.attempts.append(
            f"流复制失败: {_tail(copy_error) or 'ffmpeg 未生成有效输出'}"
        )
        _cleanup(temporary)

    hardware = _select_hardware(ffmpeg, options, runner, result.attempts)
    result.hardware = hardware
    transcode_command = _transcode_command(
        ffmpeg, source_resolved, temporary, info, options, hardware
    )
    result.commands.append(transcode_command)
    try:
        completed = _run(transcode_command, timeout=options.timeout, runner=runner)
    except MediaError:
        _cleanup(temporary)
        raise
    stderr = _decode_output(getattr(completed, "stderr", None))

    if getattr(completed, "returncode", 1) != 0 and hardware != "cpu":
        _cleanup(temporary)
        result.attempts.append(
            f"硬件编码失败({hardware})，自动回退 CPU: {_tail(stderr) or '未知错误'}"
        )
        cpu_command = _transcode_command(
            ffmpeg, source_resolved, temporary, info, options, "cpu"
        )
        result.commands.append(cpu_command)
        try:
            completed = _run(cpu_command, timeout=options.timeout, runner=runner)
        except MediaError:
            _cleanup(temporary)
            raise
        stderr = _decode_output(getattr(completed, "stderr", None))
        hardware = "cpu"
        result.hardware = "cpu"

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
    result.message = "重编码成功"
    result.stderr_tail = _tail(stderr)
    return result
