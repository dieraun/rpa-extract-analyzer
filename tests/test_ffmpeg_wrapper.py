"""Tests for the local FFmpeg wrapper; no real ffmpeg is invoked."""

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
            },
            {"codec_type": "audio", "codec_name": "opus"},
        ],
    }
)


def _which(name: str) -> str:
    return f"/fake/{name}"


class FakeRunner:
    """A deterministic subprocess stand-in that records the exact argv."""

    def __init__(
        self,
        *,
        copy_returncode: int = 0,
        reencode_returncode: int = 0,
        probe_returncode: int = 0,
        create_output: bool = True,
        timeout_on_copy: bool = False,
        probe_json: str = PROBE_JSON,
    ) -> None:
        self.copy_returncode = copy_returncode
        self.reencode_returncode = reencode_returncode
        self.probe_returncode = probe_returncode
        self.create_output = create_output
        self.timeout_on_copy = timeout_on_copy
        self.probe_json = probe_json
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, command, **kwargs):
        command = list(command)
        self.calls.append((command, kwargs))
        if any(part == "-show_format" for part in command):
            if self.probe_returncode != 0:
                return SimpleNamespace(returncode=self.probe_returncode, stdout="", stderr="probe failed")
            return SimpleNamespace(returncode=0, stdout=self.probe_json, stderr="")
        if self.timeout_on_copy and "-c" in command and command[command.index("-c") + 1] == "copy":
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 1))
        if self.create_output:
            Path(command[-1]).parent.mkdir(parents=True, exist_ok=True)
            Path(command[-1]).write_bytes(b"CONVERTED")
        if "-c" in command and command[command.index("-c") + 1] == "copy":
            return SimpleNamespace(
                returncode=self.copy_returncode, stdout="", stderr="copy failed" if self.copy_returncode else ""
            )
        return SimpleNamespace(
            returncode=self.reencode_returncode,
            stdout="",
            stderr="reencode failed" if self.reencode_returncode else "",
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

    def test_probe_media_parses_streams(self) -> None:
        runner = FakeRunner()
        with mock.patch("src.media_convert.ffmpeg_wrapper.shutil.which", side_effect=_which):
            info = probe_media(self.input, runner=runner)
        self.assertTrue(info.has_video)
        self.assertTrue(info.has_audio)
        self.assertEqual(info.video_codec, "vp9")
        self.assertEqual(info.audio_codec, "opus")
        self.assertEqual(info.width, 1280)
        self.assertAlmostEqual(info.duration or 0, 2.5)
        self.assertTrue(is_webm(self.input, info))

    def test_probe_failure_is_friendly(self) -> None:
        runner = FakeRunner(probe_returncode=1)
        with mock.patch("src.media_convert.ffmpeg_wrapper.shutil.which", side_effect=_which):
            with self.assertRaises(MediaError):
                probe_media(self.input, runner=runner)

    def test_stream_copy_is_preferred(self) -> None:
        runner = FakeRunner(copy_returncode=0)
        with mock.patch("src.media_convert.ffmpeg_wrapper.shutil.which", side_effect=_which):
            result = convert_webm(self.input, self.output, runner=runner)
        self.assertTrue(result.success)
        self.assertTrue(result.stream_copy)
        self.assertTrue(self.output.is_file())
        copy_commands = [
            cmd for cmd, _kwargs in runner.calls if "-c" in cmd and cmd[cmd.index("-c") + 1] == "copy"
        ]
        self.assertEqual(len(copy_commands), 1)
        self.assertNotIn("libx264", copy_commands[0])
        self.assertTrue(all(kwargs["shell"] is False for _cmd, kwargs in runner.calls))

    def test_failed_copy_falls_back_to_reencode(self) -> None:
        runner = FakeRunner(copy_returncode=1, reencode_returncode=0)
        with mock.patch("src.media_convert.ffmpeg_wrapper.shutil.which", side_effect=_which):
            result = convert_webm(self.input, self.output, runner=runner)
        self.assertTrue(result.success)
        self.assertFalse(result.stream_copy)
        self.assertEqual(self.output.read_bytes(), b"CONVERTED")
        reencode = [
            cmd for cmd, _kwargs in runner.calls if "libx264" in cmd
        ]
        self.assertEqual(len(reencode), 1)
        self.assertIn("-crf", reencode[0])
        self.assertIn("aac", reencode[0])

    def test_force_reencode_skips_copy(self) -> None:
        runner = FakeRunner(copy_returncode=0, reencode_returncode=0)
        options = FfmpegOptions(force_reencode=True)
        with mock.patch("src.media_convert.ffmpeg_wrapper.shutil.which", side_effect=_which):
            result = convert_webm(self.input, self.output, options=options, runner=runner)
        self.assertTrue(result.success)
        self.assertFalse(result.stream_copy)
        self.assertFalse(
            any("-c" in cmd and "copy" in cmd for cmd, _kwargs in runner.calls)
        )
        self.assertTrue(any("libx264" in cmd for cmd, _kwargs in runner.calls))
        self.assertTrue(any("force_reencode" in attempt for attempt in result.attempts))

    def test_nvenc_hardware_encoder(self) -> None:
        runner = FakeRunner(copy_returncode=1, reencode_returncode=0)
        options = FfmpegOptions(hardware="nvenc", hardware_codec="hevc")
        with mock.patch("src.media_convert.ffmpeg_wrapper.shutil.which", side_effect=_which):
            result = convert_webm(self.input, self.output, options=options, runner=runner)
        self.assertTrue(result.success)
        self.assertEqual(result.hardware, "nvenc")
        command = result.commands[-1]
        self.assertIn("hevc_nvenc", command)
        self.assertIn("-cq", command)

    def test_qsv_hardware_encoder(self) -> None:
        runner = FakeRunner(copy_returncode=1, reencode_returncode=0)
        options = FfmpegOptions(hardware="qsv", hardware_codec="h264")
        with mock.patch("src.media_convert.ffmpeg_wrapper.shutil.which", side_effect=_which):
            result = convert_webm(self.input, self.output, options=options, runner=runner)
        self.assertIn("h264_qsv", result.commands[-1])
        self.assertIn("-global_quality", result.commands[-1])

    def test_vaapi_hardware_encoder(self) -> None:
        runner = FakeRunner(copy_returncode=1, reencode_returncode=0)
        options = FfmpegOptions(
            hardware="vaapi",
            hardware_codec="h264",
            vaapi_device="/dev/dri/renderD128",
        )
        with mock.patch("src.media_convert.ffmpeg_wrapper.shutil.which", side_effect=_which):
            result = convert_webm(self.input, self.output, options=options, runner=runner)
        command = result.commands[-1]
        self.assertIn("-vaapi_device", command)
        self.assertIn("h264_vaapi", command)
        self.assertIn("format=nv12,hwupload", command)

    def test_webm_output_rejects_hardware(self) -> None:
        runner = FakeRunner(copy_returncode=1, reencode_returncode=0)
        options = FfmpegOptions(hardware="nvenc")
        with mock.patch("src.media_convert.ffmpeg_wrapper.shutil.which", side_effect=_which):
            with self.assertRaises(MediaError):
                convert_webm(self.input, self.root / "out.webm", options=options, runner=runner)

    def test_non_webm_input_is_rejected(self) -> None:
        other = self.root / "video.mp4"
        other.write_bytes(b"MP4")
        mp4_probe = json.dumps(
            {
                "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "1.0"},
                "streams": [
                    {"codec_type": "video", "codec_name": "h264", "width": 640, "height": 360},
                    {"codec_type": "audio", "codec_name": "aac"},
                ],
            }
        )
        runner = FakeRunner(probe_json=mp4_probe)
        with mock.patch("src.media_convert.ffmpeg_wrapper.shutil.which", side_effect=_which):
            with self.assertRaises(MediaError):
                convert_webm(other, self.output, runner=runner)

    def test_existing_output_requires_overwrite(self) -> None:
        self.output.write_bytes(b"OLD")
        runner = FakeRunner()
        with mock.patch("src.media_convert.ffmpeg_wrapper.shutil.which", side_effect=_which):
            with self.assertRaises(MediaError):
                convert_webm(self.input, self.output, runner=runner)
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.output.read_bytes(), b"OLD")

    def test_invalid_output_suffix(self) -> None:
        with mock.patch("src.media_convert.ffmpeg_wrapper.shutil.which", side_effect=_which):
            with self.assertRaises(MediaError):
                convert_webm(self.input, self.root / "out.avi", runner=FakeRunner())

    def test_timeout_cleans_temporary_file(self) -> None:
        runner = FakeRunner(timeout_on_copy=True)
        with mock.patch("src.media_convert.ffmpeg_wrapper.shutil.which", side_effect=_which):
            with self.assertRaises(MediaError):
                convert_webm(self.input, self.output, runner=runner)
        self.assertEqual(list(self.root.glob(".*.part*")), [])
        self.assertFalse(self.output.exists())

    def test_reencode_failure_cleans_temporary_file(self) -> None:
        runner = FakeRunner(copy_returncode=1, reencode_returncode=1)
        with mock.patch("src.media_convert.ffmpeg_wrapper.shutil.which", side_effect=_which):
            with self.assertRaises(MediaError):
                convert_webm(self.input, self.output, runner=runner)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".*.part*")), [])

    def test_input_is_not_modified(self) -> None:
        before = hashlib.sha256(self.input.read_bytes()).hexdigest()
        runner = FakeRunner(copy_returncode=0)
        with mock.patch("src.media_convert.ffmpeg_wrapper.shutil.which", side_effect=_which):
            convert_webm(self.input, self.output, runner=runner)
        after = hashlib.sha256(self.input.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_shell_metacharacters_remain_one_argument(self) -> None:
        weird = self.root / "a;b $(touch nope).webm"
        weird.write_bytes(b"WEBM")
        runner = FakeRunner(copy_returncode=0)
        with mock.patch("src.media_convert.ffmpeg_wrapper.shutil.which", side_effect=_which):
            result = convert_webm(weird, self.output, runner=runner)
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
            FfmpegOptions(preset="-evil")
        with self.assertRaises(MediaError):
            FfmpegOptions(audio_bitrate="192k;rm")

    def test_is_webm_extension_and_format(self) -> None:
        with mock.patch("src.media_convert.ffmpeg_wrapper.shutil.which", side_effect=_which):
            info = probe_media(self.input, runner=FakeRunner())
        self.assertTrue(is_webm(self.input, info))
        self.assertFalse(is_webm(self.root / "x.mp4"))

        fake_info = info.__class__(
            path=Path("x.bin"),
            format_name="matroska,webm",
            duration=None,
            video_codec="vp9",
            audio_codec="opus",
            width=None,
            height=None,
            has_video=True,
            has_audio=True,
        )
        self.assertTrue(is_webm(self.root / "x.bin", fake_info))


if __name__ == "__main__":
    unittest.main()
