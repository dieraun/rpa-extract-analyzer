"""Tests for the read-only game scanner."""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from src.scanner.game_scanner import (
    GameScanError,
    find_archives,
    find_loader,
    scan_game,
)


class GameScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "renpy").mkdir()
        self.loader = self.root / "renpy" / "loader.pyo"
        self.loader.write_bytes(b"fake loader for scanner test")
        (self.root / "game").mkdir()
        (self.root / "game" / "archive.rpa").write_bytes(b"rpa-one")
        (self.root / "game" / "assets").mkdir()
        (self.root / "game" / "assets" / "SECOND.RPA").write_bytes(b"rpa-two")
        (self.root / "game" / "notes.txt").write_text("not an archive", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_finds_loader_and_archives(self) -> None:
        layout = scan_game(self.root)
        self.assertEqual(layout.root, self.root.resolve())
        self.assertEqual(layout.loader, self.loader.resolve())
        self.assertEqual(
            [path.name for path in layout.archives],
            ["archive.rpa", "SECOND.RPA"],
        )
        self.assertEqual(layout.warnings, ())

    def test_convenience_helpers(self) -> None:
        self.assertEqual(find_loader(self.root), self.loader.resolve())
        self.assertEqual(len(find_archives(self.root)), 2)

    def test_scanner_does_not_modify_files(self) -> None:
        before = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        scan_game(self.root)
        after = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)

    def test_missing_root_raises_clear_error(self) -> None:
        with self.assertRaises(GameScanError):
            scan_game(self.root / "does-not-exist")

    def test_file_as_root_raises(self) -> None:
        with self.assertRaises(GameScanError):
            scan_game(self.loader)

    def test_loader_outside_renpy_is_not_selected(self) -> None:
        (self.root / "loader.pyo").write_bytes(b"not in renpy")
        layout = scan_game(self.root)
        self.assertEqual(layout.loader, self.loader.resolve())

    def test_symlink_escape_is_ignored(self) -> None:
        outside = Path(self._tmp.name).parent / f"{self.root.name}-outside.rpa"
        outside.write_bytes(b"outside")
        link = self.root / "game" / "linked.rpa"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"当前环境无法创建符号链接: {exc}")
        try:
            layout = scan_game(self.root)
            self.assertNotIn(outside.resolve(), layout.archives)
            self.assertTrue(any("链接" in warning for warning in layout.warnings))
        finally:
            try:
                link.unlink()
            except OSError:
                pass
            try:
                outside.unlink()
            except OSError:
                pass

    def test_max_entries_truncates(self) -> None:
        for index in range(5):
            (self.root / "game" / f"extra-{index}.rpa").write_bytes(b"x")
        layout = scan_game(self.root, max_entries=2)
        self.assertLessEqual(len(layout.archives), 2)
        self.assertTrue(any("截断" in warning for warning in layout.warnings))

    def test_invalid_limits(self) -> None:
        with self.assertRaises(ValueError):
            scan_game(self.root, max_depth=-1)
        with self.assertRaises(ValueError):
            scan_game(self.root, max_entries=0)

    def test_layout_to_dict_is_json_ready(self) -> None:
        layout = scan_game(self.root)
        payload = layout.to_dict()
        self.assertEqual(payload["archive_count"], 2)
        self.assertIsInstance(payload["archives"], list)
        self.assertEqual(payload["loader"], str(self.loader.resolve()))

    def test_case_insensitive_renpy_directory(self) -> None:
        case_game = self.root / "case-game"
        alt = case_game / "RenPy"
        alt.mkdir(parents=True)
        alt_loader = alt / "LOADER.PYO"
        alt_loader.write_bytes(b"case variant")
        layout = scan_game(case_game)
        self.assertIsNotNone(layout.loader)
        self.assertEqual(layout.loader.name.casefold(), "loader.pyo")
        self.assertEqual(layout.loader.parent.name.casefold(), "renpy")


if __name__ == "__main__":
    unittest.main()
