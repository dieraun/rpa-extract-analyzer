"""Tests for xdis disassembly and bytecode-only XOR extraction."""

from __future__ import annotations

import json
import py_compile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.ast_analyzer import analyze_disassembly, analyze_loader
from src.decompiler import DecompileResult, DisassemblyResult, disassemble_pyo, inspect_pyo_header

CLASSIC = """\
# pydisasm version 6.1.7
# Method Name:       <module>
  1:           0 LOAD_CONST               1 (3735928559)
               2 LOAD_FAST                0 (raw_offset)
               4 BINARY_XOR
               6 STORE_FAST               1 (offset)

# Method Name:       load_size
 10:           0 LOAD_CONST               1 (1234)
               2 LOAD_FAST                0 (raw_size)
               4 BINARY_XOR
               6 STORE_FAST               1 (size)
"""

PY311 = """\
# Method Name:       <module>
  1:           0 LOAD_CONST               1 (4660)
               2 LOAD_FAST                0 (raw_offset)
               4 BINARY_OP               12 (^)
               6 STORE_FAST               1 (offset)
"""

BYTES_KEY = """\
# Method Name:       <module>
  1:           0 LOAD_CONST               1 (b'\\x12\\x34')
               2 LOAD_FAST                0 (raw_data)
               4 BINARY_XOR
               6 STORE_FAST               1 (data)
"""


class DisassemblyAnalyzerTests(unittest.TestCase):
    def test_classic_xor_and_targets(self) -> None:
        report = analyze_disassembly("loader.pyo", CLASSIC)
        self.assertTrue(report["parse"]["ok"])
        self.assertEqual(report["analysis"], "bytecode-disassembly")
        self.assertEqual(report["stats"]["xor_nodes"], 2)
        by_target = {rule["target"]: rule for rule in report["rules"]}
        self.assertEqual(by_target["offset"]["key_int"], 0xDEADBEEF)
        self.assertEqual(by_target["size"]["key_int"], 1234)
        self.assertEqual(by_target["offset"]["id"], "asm-xor-0001")
        json.dumps(report)
        self.assertEqual(report["findings"][0]["code"], "<module>")

    def test_python311_binary_op_xor(self) -> None:
        report = analyze_disassembly("loader.pyc", PY311)
        self.assertEqual(report["rules"][0]["key_int"], 4660)
        self.assertEqual(report["rules"][0]["target"], "offset")

    def test_bytes_key(self) -> None:
        report = analyze_disassembly("loader.pyo", BYTES_KEY)
        self.assertEqual(report["rules"][0]["key_bytes_hex"], "1234")
        self.assertEqual(report["rules"][0]["xor_key"], "0x1234")

    def test_no_xor_is_a_valid_empty_result(self) -> None:
        report = analyze_disassembly("loader.pyo", "# Method Name: <module>\n  1: 0 LOAD_CONST 1 (1)\n")
        self.assertTrue(report["parse"]["ok"])
        self.assertEqual(report["rules"], [])
        self.assertTrue(report["warnings"])

    def test_empty_or_invalid_text(self) -> None:
        for text in ("", "not a disassembly"):
            with self.subTest(text=text):
                report = analyze_disassembly("x", text)
                self.assertFalse(report["parse"]["ok"])
                self.assertEqual(report["rules"], [])

    def test_code_object_scope_is_preserved(self) -> None:
        report = analyze_disassembly("loader.pyo", CLASSIC)
        scopes = {finding["code"] for finding in report["findings"]}
        self.assertIn("<module>", scopes)
        self.assertIn("load_size", scopes)


class RealDisassemblyTests(unittest.TestCase):
    def test_disassemble_generated_pyc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "loader.py"
            source.write_text("KEY = 0x1234\nvalue = raw ^ KEY\n", encoding="utf-8")
            pyc = root / "loader.pyc"
            py_compile.compile(str(source), cfile=str(pyc), doraise=True)
            result = disassemble_pyo(pyc)
            self.assertTrue(result.ok, result.message)
            self.assertGreater(result.instruction_count, 0)
            self.assertGreaterEqual(result.code_objects, 1)
            self.assertIn("LOAD_", result.text or "")
            payload = result.to_dict()
            self.assertNotIn("text", payload)
            self.assertEqual(payload["text_length"], len(result.text or ""))

    def test_disassembly_output_is_written_and_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "loader.py"
            source.write_text("x = 1\n", encoding="utf-8")
            pyc = root / "loader.pyc"
            py_compile.compile(str(source), cfile=str(pyc), doraise=True)
            out = root / "out"
            first = disassemble_pyo(pyc, output_dir=out)
            self.assertTrue(first.ok)
            self.assertTrue(first.output_path and first.output_path.is_file())
            second = disassemble_pyo(pyc, output_dir=out)
            self.assertFalse(second.ok)
            self.assertIn("已存在", second.message)

    def test_unknown_magic_is_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "loader.pyo"
            path.write_bytes(b"\x00\x00\x00\x00" + b"data")
            result = disassemble_pyo(path)
            self.assertFalse(result.ok)
            self.assertEqual(result.status, "unsupported")

    def test_header_is_still_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "loader.pyo"
            path.write_bytes(b"\x03\xf3\r\n\x00\x00\x00\x00" + b"bad")
            header = inspect_pyo_header(path)
            self.assertIn("2.7", header.python_versions)

    def test_analyze_loader_helper_uses_disassembly_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "loader.pyo"
            path.write_bytes(b"\x03\xf3\r\n\x00\x00\x00\x00" + b"bad")
            failed = DecompileResult(
                path=path,
                status="failed",
                message="synthetic decompile failure",
            )
            disassembled = DisassemblyResult(
                path=path,
                status="success",
                message="ok",
                text=CLASSIC,
                code_objects=2,
                instruction_count=8,
            )
            with mock.patch(
                "src.decompiler.decompile_pyo", return_value=failed
            ), mock.patch(
                "src.decompiler.disassemble_pyo", return_value=disassembled
            ):
                report = analyze_loader(path)
            self.assertEqual(report["analysis"], "bytecode-disassembly")
            self.assertIn("decompiler", report)
            self.assertIn("disassembly", report)
            self.assertEqual(report["stats"]["rule_count"], 2)


if __name__ == "__main__":
    unittest.main()
