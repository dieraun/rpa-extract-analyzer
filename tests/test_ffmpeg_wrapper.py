"""Tests for the mp4_merger-derived FFmpeg wrapper; no real FFmpeg is invoked."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.media_convert.ffmpeg_wrapper import (
    FfmpegOptions,
    MediaError,
    convert_webm,
    is_webm,
    probe_media,
)

PROBE_JSON = json.dumps(
    {
        "format": {"format_name": "matroska,webm", "duration": "2.500000"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "vp9",
                "width": 1280,
                "height": 720,
                "pix_fmt": "yuv420p",
            },
            {"codec_type": "audio", "codec_name": "opus"},
        ],
    }
)

GPU_ENCODERS = " V..... h264_nvenc\n V..... h264_qsv\n V..... h264_amf\n"


def _which(name: str) -> str:
    return f"/fake/{name}"


class FakeRunner:
    """Deterministic stand-in for ffmpeg/ffprobe invocations."""

    def __init__(
        self,
        *,
        probe_json: str = PROBE_JSON,
        probe_returncode: int = 0,
        copy_returncode: int = 0,
        transcode_returncode: int = 0,
        verify_returncode: int = 0,
        encoders: str = "",
        gpu_runtime_fail: bool = False,
        timeout_on_transcode: bool = False,
        create_output: bool = True,
    ) -> None:
        self.probe_json = probe_json
        self.probe_returncode = probe_returncode
        self.copy_returncode = copy_returncode
        self.transcode_returncode = transcode_returncode
        self.verify_returncode = verify_returncode
        self.encoders = encoders
        self.gpu_runtime_fail = gpu_runtime_fail
        self.timeout_on_transcode = timeout_on_transcode
        self.create_output = create_output
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, command, **kwargs):
        command = list(command)
        self.calls.append((command, kwargs))
        if any(part == "-show_format" for part in command):
            if self.probe_returncode != 0:
                return SimpleNamespace(
                    returncode=self.probe_returncode, stdout="", stderr="probe failed"
                )
            return SimpleNamespace(returncode=0, stdout=self.probe_json, stderr="")
        if "-encoders" in command:
            return SimpleNamespace(returncode=0, stdout=self.encoders, stderr="")
        if "testsrc=duration=1:size=320x240:rate=10" in command:
            return SimpleNamespace(returncode=self.verify_returncode, stdout="", stderr="")
        is_copy = "-c" in command and command[command.index("-c") + 1] == "copy"
        if self.timeout_on_transcode and not is_copy:
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 1))
        if self.create_output:
            Path(command[-1]).parent.mkdir(parents=True, exist_ok=True)
            Path(command[-1]).write_bytes(b"CONVERTED")
        if is_copy:
            return SimpleNamespace(
                returncode=self.copy_returncode,
                stdout="",
                stderr="copy failed" if self.copy_returncode else "",
            )
        gpu_fail = self.gpu_runtime_fail and any(
            encoder in command
            for encoder in ("h264_nvenc", "h264_qsv", "h264_amf", "h264_vaapi")
        )
        rc = 1 if gpu_fail else self.transcode_returncode
        return SimpleNamespace(
            returncode=rc,
            stdout="",
            stderr="gpu runtime failed" if gpu_fail else (
                "transcode failed" if rc else ""
            ),
        )


class MediaConversionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.input = self.root / "video.webm"
        self.input.write_bytes(b"WEBM-DATA")
        self.output = self.root / "video.mp4"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, runner: FakeRunner, *, options: FfmpegOptions | None = None):
        options = options or FfmpegOptions(hardware="cpu")
        with mock.patch(
            "src.media_convert.ffmpeg_wrapper.shutil.which", side_effect=_which
        ):
            return convert_webm(
                self.input, self.output, options=options, runner=runner
            )

    def test_probe_media_parses_streams_and_bit_depth(self) -> None:
        runner = FakeRunner()
        with mock.patch(
            "src.media_convert.ffmpeg_wrapper.shutil.which", side_effect=_which
        ):
            info = probe_media(
                self.input, options=FfmpegOptions(hardware="cpu"), runner=runner
            )
        self.assertTrue(info.has_video)
        self.assertTrue(info.has_audio)
        self.assertEqual(info.video_codec, "vp9")
        self.assertEqual(info.audio_codec, "opus")
        self.assertEqual(info.width, 1280)
        self.assertEqual(info.bit_depth, 8)
        self.assertAlmostEqual(info.duration or 0, 2.5)
        self.assertTrue(is_webm(self.input, info))

    def test_probe_failure_is_friendly(self) -> None:
        runner = FakeRunner(probe_returncode=1)
        with mock.patch(
            "src.media_convert.ffmpeg_wrapper.shutil.which", side_effect=_which
        ):
            with self.assertRaises(MediaError):
                probe_media(self.input, runner=runner)

    def test_default_is_high_quality_transcode(self) -> None:
        runner = FakeRunner()
        result = self._run(runner)
        self.assertTrue(result.success)
        self.assertFalse(result.stream_copy)
        self.assertEqual(result.hardware, "cpu")
        command = result.commands[-1]
        self.assertIn("libx264", command)
        self.assertIn("-crf", command)
        self.assertIn("16", command)
        self.assertIn("aac", command)
        self.assertIn("320k", command)
        self.assertTrue(any("format=yuv420p" in part for part in command))
        self.assertNotIn("-c", [part for part in command if part == "copy"])

    def test_copy_first_can_succeed(self) -> None:
        runner = FakeRunner(copy_returncode=0)
        result = self._run(
            runner, options=FfmpegOptions(hardware="cpu", stream_copy_first=True)
        )
        self.assertTrue(result.success)
        self.assertTrue(result.stream_copy)
        self.assertEqual(result.hardware, "copy")
        self.assertTrue(any("-c" in cmd and "copy" in cmd for cmd, _ in runner.calls))

    def test_copy_failure_falls_back_to_transcode(self) -> None:
        runner = FakeRunner(copy_returncode=1)
        result = self._run(
            runner, options=FfmpegOptions(hardware="cpu", stream_copy_first=True)
        )
        self.assertTrue(result.success)
        self.assertFalse(result.stream_copy)
        self.assertTrue(result.attempts)
        self.assertTrue(any("libx264" in cmd for cmd, _ in runner.calls))

    def test_auto_selects_verified_nvenc(self) -> None:
        runner = FakeRunner(encoders=GPU_ENCODERS, verify_returncode=0)
        result = self._run(runner, options=FfmpegOptions(hardware="auto"))
        self.assertTrue(result.success)
        self.assertEqual(result.hardware, "nvidia")
        self.assertTrue(any("h264_nvenc" in cmd for cmd, _ in runner.calls))
        self.assertTrue(any("硬件编码器验证通过" in item for item in result.attempts))

    def test_explicit_gpu_unavailable_falls_back_to_cpu(self) -> None:
        runner = FakeRunner(verify_returncode=1)
        result = self._run(runner, options=FfmpegOptions(hardware="nvidia"))
        self.assertTrue(result.success)
        self.assertEqual(result.hardware, "cpu")
        command = result.commands[-1]
        self.assertIn("libx264", command)
        self.assertNotIn("h264_nvenc", command)
        self.assertTrue(any("不可用" in item for item in result.attempts))

    def test_gpu_runtime_failure_retries_cpu(self) -> None:
        runner = FakeRunner(
            encoders=GPU_ENCODERS,
            verify_returncode=0,
            gpu_runtime_fail=True,
        )
        result = self._run(runner, options=FfmpegOptions(hardware="nvidia"))
        self.assertTrue(result.success)
        self.assertEqual(result.hardware, "cpu")
        self.assertEqual(len(result.commands), 2)
        self.assertIn("h264_nvenc", result.commands[0])
        self.assertIn("libx264", result.commands[1])
        self.assertTrue(any("CPU" in item for item in result.attempts))

    def test_amd_encoder_command(self) -> None:
        runner = FakeRunner()
        result = self._run(runner, options=FfmpegOptions(hardware="amd"))
        command = result.commands[-1]
        self.assertIn("h264_amf", command)
        self.assertIn("-qp_i", command)

    def test_vaapi_encoder_command(self) -> None:
        runner = FakeRunner()
        result = self._run(
            runner,
            options=FfmpegOptions(
                hardware="vaapi", vaapi_device="/dev/dri/renderD128"
            ),
        )
        command = result.commands[-1]
        self.assertIn("-vaapi_device", command)
        self.assertIn("h264_vaapi", command)
        self.assertIn("format=nv12,hwupload", command)

    def test_ten_bit_source_preserves_depth(self) -> None:
        probe = json.loads(PROBE_JSON)
        probe["streams"][0]["pix_fmt"] = "yuv420p10le"
        runner = FakeRunner(probe_json=json.dumps(probe))
        result = self._run(runner)
        self.assertFalse(
            any("format=yuv420p" in part for part in result.commands[-1])
        )
        self.assertEqual(result.media.bit_depth, 10)

    def test_force_8bit_adds_scale_filter(self) -> None:
        probe = json.loads(PROBE_JSON)
        probe["streams"][0]["pix_fmt"] = "yuv420p10le"
        runner = FakeRunner(probe_json=json.dumps(probe))
        result = self._run(
            runner, options=FfmpegOptions(hardware="cpu", force_8bit=True)
        )
        self.assertTrue(
            any("format=yuv420p" in part for part in result.commands[-1])
        )

    def test_non_webm_input_is_rejected(self) -> None:
        other = self.root / "video.mp4"
        other.write_bytes(b"MP4")
        mp4_probe = json.dumps(
            {
                "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "1.0"},
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 640,
                        "height": 360,
                    }
                ],
            }
        )
        runner = FakeRunner(probe_json=mp4_probe)
        with mock.patch(
            "src.media_convert.ffmpeg_wrapper.shutil.which", side_effect=_which
        ):
            with self.assertRaises(MediaError):
                convert_webm(
                    other,
                    self.output,
                    options=FfmpegOptions(hardware="cpu"),
                    runner=runner,
                )

    def test_existing_output_requires_overwrite(self) -> None:
        self.output.write_bytes(b"OLD")
        runner = FakeRunner()
        with mock.patch(
            "src.media_convert.ffmpeg_wrapper.shutil.which", side_effect=_which
        ):
            with self.assertRaises(MediaError):
                convert_webm(
                    self.input,
                    self.output,
                    options=FfmpegOptions(hardware="cpu"),
                    runner=runner,
                )
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.output.read_bytes(), b"OLD")

    def test_invalid_output_suffix(self) -> None:
        with self.assertRaises(MediaError):
            convert_webm(
                self.input,
                self.root / "out.mkv",
                options=FfmpegOptions(hardware="cpu"),
                runner=FakeRunner(),
            )

    def test_timeout_cleans_temporary_file(self) -> None:
        runner = FakeRunner(timeout_on_transcode=True)
        with self.assertRaises(MediaError):
            self._run(runner)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".*.part*")), [])

    def test_reencode_failure_cleans_temporary_file(self) -> None:
        runner = FakeRunner(transcode_returncode=1)
        with self.assertRaises(MediaError):
            self._run(runner)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".*.part*")), [])

    def test_input_is_not_modified(self) -> None:
        before = hashlib.sha256(self.input.read_bytes()).hexdigest()
        self._run(FakeRunner())
        after = hashlib.sha256(self.input.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_shell_metacharacters_remain_one_argument(self) -> None:
        weird = self.root / "a;b $(touch nope).webm"
        weird.write_bytes(b"WEBM")
        runner = FakeRunner()
        with mock.patch(
            "src.media_convert.ffmpeg_wrapper.shutil.which", side_effect=_which
        ):
            result = convert_webm(
                weird,
                self.output,
                options=FfmpegOptions(hardware="cpu"),
                runner=runner,
            )
        self.assertTrue(result.success)
        for command, kwargs in runner.calls:
            self.assertIsInstance(command, list)
            self.assertFalse(kwargs["shell"])
            self.assertIn(str(weird.resolve()), command)

    def test_options_validation(self) -> None:
        with self.assertRaises(MediaError):
            FfmpegOptions(hardware="cuda")
        with self.assertRaises(MediaError):
            FfmpegOptions(crf=100)
        with self.assertRaises(MediaError):
            FfmpegOptions(timeout=0)
        with self.assertRaises(MediaError):
            FfmpegOptions(audio_bitrate="192k;rm")

    def test_normalized_hardware_aliases(self) -> None:
        self.assertEqual(FfmpegOptions(hardware="nvenc").hardware_key, "nvidia")
        self.assertEqual(FfmpegOptions(hardware="qsv").hardware_key, "intel")
        self.assertEqual(FfmpegOptions(hardware="none").hardware_key, "cpu")

    def test_is_webm_extension_and_format(self) -> None:
        with mock.patch(
            "src.media_convert.ffmpeg_wrapper.shutil.which", side_effect=_which
        ):
            info = probe_media(
                self.input,
                options=FfmpegOptions(hardware="cpu"),
                runner=FakeRunner(),
            )
        self.assertTrue(is_webm(self.input, info))
        self.assertFalse(is_webm(self.root / "x.mp4"))
        self.assertTrue(is_webm(self.root / "x.bin", info))


if __name__ == "__main__":
    unittest.main()
