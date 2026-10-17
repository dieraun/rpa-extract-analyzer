"""Optional real-FFmpeg integration test.

Skipped automatically when ffmpeg/ffprobe or the required encoders are absent.
No network access and no game files are used.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from src.media_convert import FfmpegOptions, convert_webm, probe_media


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "本地未安装 ffmpeg/ffprobe",
)
class FfmpegIntegrationTests(unittest.TestCase):
    def test_real_webm_to_mp4(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            webm = root / "sample.webm"
            mp4 = root / "sample.mp4"
            generate = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=128x96:rate=10",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000",
                "-t",
                "0.4",
                "-c:v",
                "libvpx-vp9",
                "-c:a",
                "libopus",
                "-shortest",
                str(webm),
            ]
            generated = subprocess.run(
                generate,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=60,
                check=False,
            )
            if generated.returncode != 0 or not webm.is_file():
                self.skipTest(
                    "当前 FFmpeg 构建缺少 libvpx-vp9/libopus: "
                    + (generated.stderr or "")[-300:]
                )

            result = convert_webm(
                webm,
                mp4,
                options=FfmpegOptions(timeout=120),
            )
            self.assertTrue(result.success, result.message)
            self.assertTrue(mp4.is_file())
            self.assertGreater(mp4.stat().st_size, 0)
            info = probe_media(mp4)
            self.assertTrue(info.has_video)

            reencoded = root / "sample-reencoded.mp4"
            second = convert_webm(
                webm,
                reencoded,
                options=FfmpegOptions(timeout=120, force_reencode=True),
            )
            self.assertTrue(second.success, second.message)
            self.assertFalse(second.stream_copy)
            second_info = probe_media(reencoded)
            self.assertEqual(second_info.video_codec, "h264")
            self.assertEqual(second_info.audio_codec, "aac")


if __name__ == "__main__":
    unittest.main()
