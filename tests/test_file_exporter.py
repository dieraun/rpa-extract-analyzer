"""Tests for safe, atomic resource export."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from src.exporter.file_exporter import (
    ExportError,
    FileExporter,
    export_archive,
    write_export_report,
)
from src.rpa_core.rpa_parser import RpaArchive, RpaEntry, RpaPart


def make_archive(game_dir: Path) -> RpaArchive:
    game_dir.mkdir(parents=True, exist_ok=True)
    source = game_dir / "data.bin"
    source.write_bytes(b"HELLOWORLD!")
    entries = (
        RpaEntry(
            name="images/pic.bin",
            parts=(RpaPart(offset=0, length=8, prefix=b"PRE"),),
        ),
        RpaEntry(
            name="audio/theme.ogg",
            parts=(RpaPart(offset=5, length=6, prefix=b""),),
        ),
        RpaEntry(
            name="empty.txt",
            parts=(RpaPart(offset=0, length=0, prefix=b""),),
        ),
    )
    return RpaArchive(
        path=source,
        version="RPA-2.0",
        file_size=len(source.read_bytes()),
        index_offset=0,
        index_end=0,
        header_key=None,
        applied_offset_key=None,
        applied_size_key=None,
        selection="plain",
        entries=entries,
    )


class FileExporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.game = self.root / "game"
        self.archive = make_archive(self.game)
        self.out = self.root / "out"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_exports_tree_and_skips_empty(self) -> None:
        result = export_archive(self.archive, self.out)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.exported), 2)
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual((self.out / "images" / "pic.bin").read_bytes(), b"PREHELLO")
        self.assertEqual((self.out / "audio" / "theme.ogg").read_bytes(), b"WORLD!")
        self.assertFalse((self.out / "empty.txt").exists())
        self.assertEqual(result.bytes_written, 14)
        json.dumps(result.to_dict())

    def test_original_archive_is_not_modified(self) -> None:
        before = hashlib.sha256(self.archive.path.read_bytes()).hexdigest()
        result = export_archive(self.archive, self.out)
        self.assertTrue(result.ok)
        after = hashlib.sha256(self.archive.path.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_output_inside_game_root_is_rejected(self) -> None:
        with self.assertRaises(ExportError):
            FileExporter(self.archive, self.game / "exported")

    def test_existing_file_is_skipped_by_default(self) -> None:
        self.out.mkdir()
        target = self.out / "images" / "pic.bin"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"OLD")
        result = export_archive(self.archive, self.out)
        self.assertTrue(result.ok)
        self.assertEqual(target.read_bytes(), b"OLD")
        self.assertTrue(any(item.status == "skipped-existing" for item in result.skipped))

    def test_overwrite_replaces_existing_file(self) -> None:
        self.out.mkdir()
        target = self.out / "images" / "pic.bin"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"OLD")
        result = export_archive(self.archive, self.out, overwrite=True)
        self.assertTrue(result.ok)
        self.assertEqual(target.read_bytes(), b"PREHELLO")

    def test_manual_traversal_name_is_blocked(self) -> None:
        evil = RpaArchive(
            path=self.archive.path,
            version="RPA-2.0",
            file_size=5,
            index_offset=0,
            index_end=0,
            header_key=None,
            applied_offset_key=None,
            applied_size_key=None,
            selection="plain",
            entries=(RpaEntry(name="../escape.bin", parts=(RpaPart(0, 5, b""),)),),
        )
        result = export_archive(evil, self.out)
        self.assertFalse(result.ok)
        self.assertTrue(result.failed)
        self.assertFalse((self.root / "escape.bin").exists())

    def test_read_failure_is_recorded_and_continues(self) -> None:
        bad = RpaArchive(
            path=self.archive.path,
            version="RPA-2.0",
            file_size=11,
            index_offset=0,
            index_end=0,
            header_key=None,
            applied_offset_key=None,
            applied_size_key=None,
            selection="plain",
            entries=(
                RpaEntry(name="bad.bin", parts=(RpaPart(offset=100, length=5, prefix=b""),)),
                RpaEntry(
                    name="good.bin",
                    parts=(RpaPart(offset=5, length=6, prefix=b""),),
                ),
            ),
        )
        result = export_archive(bad, self.out)
        self.assertFalse(result.ok)
        self.assertEqual(len(result.failed), 1)
        self.assertEqual(len(result.exported), 1)
        self.assertFalse((self.out / "bad.bin").exists())
        self.assertEqual((self.out / "good.bin").read_bytes(), b"WORLD!")

    def test_stop_on_first_error(self) -> None:
        bad = RpaArchive(
            path=self.archive.path,
            version="RPA-2.0",
            file_size=11,
            index_offset=0,
            index_end=0,
            header_key=None,
            applied_offset_key=None,
            applied_size_key=None,
            selection="plain",
            entries=(
                RpaEntry(name="bad.bin", parts=(RpaPart(offset=100, length=5, prefix=b""),)),
                RpaEntry(name="good.bin", parts=(RpaPart(offset=5, length=6, prefix=b""),)),
            ),
        )
        result = export_archive(bad, self.out, continue_on_error=False)
        self.assertEqual(len(result.failed), 1)
        self.assertEqual(len(result.exported), 0)

    def test_report_path_inside_output_is_rejected(self) -> None:
        result = export_archive(self.archive, self.out)
        with self.assertRaises(ExportError):
            write_export_report(result, self.out / "report.json")
        report = write_export_report(result, self.root / "report.json")
        self.assertTrue(report.is_file())
        payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(payload["exported_count"], 2)

    def test_progress_callback(self) -> None:
        seen: list[tuple[int, int, str]] = []
        export_archive(
            self.archive,
            self.out,
            progress=lambda current, total, entry: seen.append((current, total, entry.name)),
        )
        self.assertEqual(len(seen), 3)
        self.assertEqual(seen[-1][0], 3)
        self.assertEqual(seen[-1][1], 3)

    def test_output_symlink_component_is_rejected(self) -> None:
        self.out.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        link = self.out / "images"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"当前环境无法创建符号链接: {exc}")
        result = export_archive(self.archive, self.out)
        self.assertFalse(result.ok)
        self.assertFalse((outside / "pic.bin").exists())


if __name__ == "__main__":
    unittest.main()
