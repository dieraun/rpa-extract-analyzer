"""Tests for static .pyo header inspection and decompiler hardening."""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.decompiler import pyo_decompiler as module
from src.decompiler.pyo_decompiler import (
    DecompileResult,
    PyoFormatError,
    decompile_pyo,
    inspect_pyo_header,
    normalize_python2_source,
    save_source,
)

PY27_MAGIC = b"\x03\xf3\r\n"
UNKNOWN_MAGIC = b"\x00\x00\x00\x00"


class PyoHeaderTests(unittest.TestCase):
    def test_detects_python27_magic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "loader.pyo"
            path.write_bytes(PY27_MAGIC + b"\x00\x00\x00\x00" + b"bytecode")
            header = inspect_pyo_header(path)
            self.assertTrue(header.is_python2)
            self.assertIn("2.7", header.python_versions)
            self.assertEqual(header.magic, PY27_MAGIC)

    def test_short_and_missing_files_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            short = Path(tmp) / "short.pyo"
            short.write_bytes(b"abc")
            with self.assertRaises(PyoFormatError):
                inspect_pyo_header(short)
            with self.assertRaises(PyoFormatError):
                inspect_pyo_header(Path(tmp) / "missing.pyo")

    def test_unknown_magic_is_reported_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "loader.pyo"
            path.write_bytes(UNKNOWN_MAGIC + b"data")
            header = inspect_pyo_header(path)
            self.assertEqual(header.python_versions, ())
            self.assertFalse(header.is_python2)


class NormalizePython2Tests(unittest.TestCase):
    def test_common_python2_constructs_become_parseable(self) -> None:
        source = (
            "import sys\n"
            "try:\n"
            "    value = 1\n"
            "except Exception, e:\n"
            "    print >>sys.stderr, 'failed', e\n"
            "print `value`\n"
            "for i in xrange(3):\n"
            "    print i,\n"
        )
        normalized, warnings = normalize_python2_source(source)
        ast.parse(normalized)
        self.assertIn("except Exception as e:", normalized)
        self.assertIn("print('failed', e, file=sys.stderr)", normalized)
        self.assertIn("repr(value)", normalized)
        self.assertIn("range(3)", normalized)
        self.assertTrue(warnings)

    def test_already_valid_python3_is_not_changed_materially(self) -> None:
        source = "def f(x):\n    print(x)\n    return x ^ 0x10\n"
        normalized, warnings = normalize_python2_source(source)
        ast.parse(normalized)
        self.assertEqual(normalized, source)
        self.assertFalse(warnings)

    def test_print_assignment_not_rewritten(self) -> None:
        source = "print = 1\nprint(print)\n"
        normalized, _ = normalize_python2_source(source)
        self.assertIn("print = 1", normalized)
        self.assertIn("print(print)", normalized)


class DecompileFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.pyo = self.root / "renpy" / "loader.pyo"
        self.pyo.parent.mkdir()
        self.pyo.write_bytes(PY27_MAGIC + b"\x00\x00\x00\x00" + b"bytecode")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _fake_decompile(self, source: str):
        def _inner(path: str, outstream) -> None:
            self.assertEqual(Path(path), self.pyo)
            outstream.write(source)

        return _inner

    def test_successful_decompile_returns_source_without_executing(self) -> None:
        source = "key = 0xDEADBEEF\nvalue = raw ^ key\n"
        with mock.patch.object(module, "_decompile_file", self._fake_decompile(source)):
            result = decompile_pyo(self.pyo)
        self.assertTrue(result.ok)
        self.assertIn("0xDEADBEEF", result.source or "")
        self.assertEqual(result.python_version, "2.7")
        # The recovered text is data only; no side effect occurred.
        self.assertNotIn("print", result.to_dict())

    def test_decompile_exception_is_captured(self) -> None:
        def _boom(path: str, outstream) -> None:
            raise RuntimeError("synthetic failure")

        with mock.patch.object(module, "_decompile_file", _boom):
            result = decompile_pyo(self.pyo)
        self.assertEqual(result.status, "failed")
        self.assertIn("synthetic failure", result.message)
        self.assertFalse(result.ok)

    def test_empty_output_is_a_friendly_failure(self) -> None:
        with mock.patch.object(module, "_decompile_file", self._fake_decompile("  \n")):
            result = decompile_pyo(self.pyo)
        self.assertEqual(result.status, "failed")
        self.assertIn("未产生源码", result.message)

    def test_unknown_magic_returns_unsupported(self) -> None:
        self.pyo.write_bytes(UNKNOWN_MAGIC + b"data")
        result = decompile_pyo(self.pyo)
        self.assertEqual(result.status, "unsupported")
        self.assertIn("magic", result.message)

    def test_output_is_written_and_not_overwritten(self) -> None:
        source = "key = 0x1234\n"
        out_dir = self.root / "output"
        with mock.patch.object(module, "_decompile_file", self._fake_decompile(source)):
            first = decompile_pyo(self.pyo, output_dir=out_dir)
            self.assertTrue(first.ok)
            self.assertIsNotNone(first.source_path)
            self.assertEqual(first.source_path.read_text(encoding="utf-8"), source)
            second = decompile_pyo(self.pyo, output_dir=out_dir)
        self.assertEqual(second.status, "failed")
        self.assertIn("已存在", second.message)

    def test_save_source_rejects_original_and_path_traversal(self) -> None:
        result = DecompileResult(
            path=self.pyo,
            status="success",
            message="ok",
            source="x = 1\n",
        )
        with self.assertRaises(ValueError):
            save_source(result, self.pyo.parent, filename="loader.pyo")
        with self.assertRaises(ValueError):
            save_source(result, self.root / "output", filename="../escape.py")

    def test_save_source_without_source_raises(self) -> None:
        result = DecompileResult(path=self.pyo, status="failed", message="failed")
        with self.assertRaises(ValueError):
            save_source(result, self.root / "output")


if __name__ == "__main__":
    unittest.main()
