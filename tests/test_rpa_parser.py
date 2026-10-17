"""Tests for the read-only RPA header/index parser."""

from __future__ import annotations

import hashlib
import pickle
import tempfile
import unittest
import zlib
from pathlib import Path

from src.rpa_core.rpa_parser import (
    RpaArchive,
    RpaFormatError,
    RpaLimits,
    RpaReadError,
    RpaSecurityError,
    normalize_archive_name,
    parse_archive,
)
from src.rpa_core.rule_applier import RuleSet, XorRule

STATIC_KEY = 0xDEADBEEF


def _build_index_bytes(index: dict, key: int | None) -> bytes:
    if key is None:
        normalised = index
    else:
        normalised = {
            name: [
                (offset ^ key, length ^ key, prefix)
                for offset, length, prefix in parts
            ]
            for name, parts in index.items()
        }
    return zlib.compress(pickle.dumps(normalised, protocol=2))


def _make_index(
    files: list[tuple[str, bytes, bytes]],
    data_start: int,
    key: int | None,
) -> tuple[dict, list[tuple[int, bytes]]]:
    index: dict[str, list[tuple[int, int, bytes]]] = {}
    records: list[tuple[int, bytes]] = []
    cursor = data_start
    for name, data, prefix in files:
        index[name] = [(cursor, len(prefix) + len(data), prefix)]
        records.append((cursor, data))
        cursor += len(data)
    return index, records


def build_rpa(
    path: Path,
    *,
    version: str = "3.0",
    header_key: int = STATIC_KEY,
    index_key: int | None = None,
    files: list[tuple[str, bytes, bytes]] | None = None,
    index_payload: bytes | None = None,
) -> Path:
    """Write a small RPA fixture using the official synthetic-prefix layout."""

    files = files if files is not None else [("images/background.png", b"PNGDATA", b"PREFIX!!")]
    if index_key is None:
        index_key = header_key if version != "2.0" else None
    if version == "2.0":
        header_template = "RPA-2.0 {offset:016X}\n"
        header_key_text = ""
    else:
        header_template = f"RPA-{version} {{offset:016X}} {{key:08X}}\n"
        header_key_text = header_key

    def render(offset: int) -> bytes:
        if version == "2.0":
            return header_template.format(offset=offset).encode("ascii")
        return header_template.format(offset=offset, key=header_key_text).encode("ascii")

    header_len = len(render(0))
    data_start = header_len
    if index_payload is None:
        for _ in range(8):
            index, records = _make_index(files, data_start, index_key)
            compressed = _build_index_bytes(index, index_key)
            new_start = header_len + len(compressed)
            if new_start == data_start:
                break
            data_start = new_start
        else:
            raise AssertionError("fixture index did not converge")
    else:
        compressed = index_payload
        data_start = header_len + len(compressed)
        _, records = _make_index(files, data_start, index_key)

    with path.open("wb") as handle:
        handle.write(render(header_len))
        handle.write(compressed)
        for _offset, data in records:
            assert handle.tell() == _offset
            handle.write(data)
    return path


def build_multipart_rpa(
    path: Path, name: str, segments: list[tuple[bytes, bytes]], *, key: int = STATIC_KEY
) -> Path:
    """Build an RPA-3.0 fixture whose entry contains several index parts."""

    header_len = len(f"RPA-3.0 {0:016X} {key:08X}\n".encode("ascii"))
    data_start = header_len
    records: list[tuple[int, bytes]] = []
    for _ in range(8):
        cursor = data_start
        parts: list[tuple[int, int, bytes]] = []
        records = []
        for data, prefix in segments:
            parts.append((cursor, len(prefix) + len(data), prefix))
            records.append((cursor, data))
            cursor += len(data)
        compressed = _build_index_bytes({name: parts}, key)
        new_start = header_len + len(compressed)
        if new_start == data_start:
            break
        data_start = new_start
    else:
        raise AssertionError("multipart fixture index did not converge")
    header = f"RPA-3.0 {header_len:016X} {key:08X}\n".encode("ascii")
    with path.open("wb") as handle:
        handle.write(header)
        handle.write(compressed)
        for offset, data in records:
            assert handle.tell() == offset
            handle.write(data)
    return path


class RpaParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_rpa3_roundtrip_with_prefix(self) -> None:
        path = build_rpa(self.root / "game.rpa")
        archive = parse_archive(path)
        self.assertEqual(archive.version, "RPA-3.0")
        self.assertEqual(archive.selection, "header-key")
        self.assertEqual(archive.list_names(), ["images/background.png"])
        self.assertEqual(archive.read_entry("images/background.png"), b"PREFIX!!PNGDATA")
        self.assertEqual(archive.applied_offset_key, STATIC_KEY)
        self.assertEqual(archive.header_key, STATIC_KEY)

    def test_rpa2_has_no_key(self) -> None:
        path = build_rpa(
            self.root / "game.rpa",
            version="2.0",
            header_key=0,
            files=[("audio/theme.ogg", b"OGGDATA", b"")],
        )
        archive = parse_archive(path)
        self.assertIsNone(archive.header_key)
        self.assertIsNone(archive.applied_offset_key)
        self.assertEqual(archive.read_entry("audio/theme.ogg"), b"OGGDATA")

    def test_static_rule_overrides_decoy_header_key(self) -> None:
        path = build_rpa(
            self.root / "game.rpa",
            header_key=0x11111111,
            index_key=STATIC_KEY,
        )
        with self.assertRaises(RpaFormatError):
            parse_archive(path)

        rules = RuleSet(
            [
                XorRule(key=STATIC_KEY, target="offset", rule_id="xor-0001"),
                XorRule(key=STATIC_KEY, target="size", rule_id="xor-0002"),
            ]
        )
        archive = parse_archive(path, rules=rules)
        self.assertTrue(archive.selection.startswith("rules"))
        self.assertEqual(archive.read_entry("images/background.png"), b"PREFIX!!PNGDATA")

    def test_rpa32_and_rpa40_headers(self) -> None:
        for version in ("3.2", "4.0"):
            with self.subTest(version=version):
                path = build_rpa(self.root / f"game-{version}.rpa", version=version)
                archive = parse_archive(path)
                self.assertEqual(archive.version, f"RPA-{version}")
                self.assertEqual(archive.read_entry("images/background.png"), b"PREFIX!!PNGDATA")

    def test_multiple_index_parts_are_concatenated(self) -> None:
        path = build_multipart_rpa(
            self.root / "game.rpa",
            "video/intro.webm",
            [(b"PART1", b"P1"), (b"PART2", b"P2")],
        )
        archive = parse_archive(path)
        entry = archive.get_entry("video/intro.webm")
        self.assertEqual(len(entry.parts), 2)
        self.assertEqual(archive.read_entry(entry), b"P1PART1P2PART2")

    def test_malicious_pickle_is_rejected_before_extraction(self) -> None:
        header = b"RPA-2.0 0000000000000019\n"
        malicious = zlib.compress(b"cbuiltins\neval\n(S'1+1'\ntR.")
        path = self.root / "evil.rpa"
        path.write_bytes(header + malicious)
        with self.assertRaises(RpaSecurityError):
            parse_archive(path)

    def test_zlib_bomb_is_rejected(self) -> None:
        path = build_rpa(self.root / "game.rpa")
        limits = RpaLimits(max_index_bytes=16)
        with self.assertRaises(RpaSecurityError):
            parse_archive(path, limits=limits)

    def test_path_traversal_is_rejected(self) -> None:
        path = build_rpa(
            self.root / "game.rpa",
            files=[("../escape.bin", b"BAD", b"")],
        )
        with self.assertRaises(RpaFormatError):
            parse_archive(path)

    def test_absolute_and_windows_paths_are_rejected(self) -> None:
        for name in ("/etc/passwd", "C:/windows/system32/x.dll", "a\\..\\b", "CON", "a:b"):
            with self.subTest(name=name):
                with self.assertRaises(RpaFormatError):
                    normalize_archive_name(name)

    def test_archive_is_not_modified(self) -> None:
        path = build_rpa(self.root / "game.rpa")
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        archive = parse_archive(path)
        archive.read_entry("images/background.png")
        after = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_empty_archive(self) -> None:
        path = build_rpa(self.root / "empty.rpa", files=[])
        archive = parse_archive(path)
        self.assertEqual(archive.entries, ())

    def test_duplicate_normalised_names_are_rejected(self) -> None:
        path = build_rpa(
            self.root / "game.rpa",
            files=[
                ("a\\b.txt", b"one", b""),
                ("a/b.txt", b"two", b""),
            ],
        )
        with self.assertRaises(RpaFormatError):
            parse_archive(path)

    def test_index_xor_with_header_key_is_supported(self) -> None:
        # Build normally, then XOR the compressed index bytes in place.  The
        # header key is reused as the index transform fallback.
        path = build_rpa(self.root / "game.rpa")
        archive = parse_archive(path)
        header_len = len(archive.header_line.encode("ascii")) + 1
        raw = bytearray(path.read_bytes())
        key_bytes = STATIC_KEY.to_bytes(4, "big")
        for index in range(archive.index_offset, archive.index_end):
            raw[index] ^= key_bytes[(index - archive.index_offset) % len(key_bytes)]
        path.write_bytes(bytes(raw))
        archive2 = parse_archive(path)
        self.assertEqual(archive2.read_entry("images/background.png"), b"PREFIX!!PNGDATA")

    def test_to_dict_is_json_ready(self) -> None:
        import json

        path = build_rpa(self.root / "game.rpa")
        archive = parse_archive(path)
        json.dumps(archive.to_dict())
        self.assertEqual(archive.to_dict()["entry_count"], 1)

    def test_read_bounded_entry(self) -> None:
        path = build_rpa(self.root / "game.rpa")
        archive = parse_archive(path)
        with self.assertRaises(RpaReadError):
            archive.read_entry("images/background.png", max_bytes=3)


if __name__ == "__main__":
    unittest.main()
