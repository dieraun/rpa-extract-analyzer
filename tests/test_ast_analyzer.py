"""Tests for static XOR extraction and rule normalisation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.ast_analyzer.dataflow_tracker import analyze_file, analyze_source
from src.ast_analyzer import analyze_loader
from src.ast_analyzer.node_finder import infer_target, parse_source


class XorExtractionTests(unittest.TestCase):
    def test_literal_key_rules_for_offset_and_size(self) -> None:
        source = (
            "def load(index):\n"
            "    offset = raw_offset ^ 0xDEADBEEF\n"
            "    size = raw_size ^ 0xDEADBEEF\n"
            "    return offset, size\n"
        )
        report = analyze_source(source, "loader.py")
        self.assertTrue(report["parse"]["ok"])
        self.assertEqual(len(report["rules"]), 2)
        by_target = {rule["target"]: rule for rule in report["rules"]}
        self.assertEqual(by_target["offset"]["key_int"], 0xDEADBEEF)
        self.assertEqual(by_target["size"]["key_int"], 0xDEADBEEF)
        self.assertEqual(report["stats"]["xor_nodes"], 2)
        json.dumps(report)

    def test_variable_key_is_resolved_through_assignment(self) -> None:
        source = (
            "KEY = 0x1234ABCD\n"
            "def load(raw_offset):\n"
            "    return raw_offset ^ KEY\n"
        )
        report = analyze_source(source)
        self.assertEqual(report["rules"][0]["key_int"], 0x1234ABCD)
        self.assertIn("KEY", report["rules"][0]["occurrences"][0]["key_sources"])

    def test_nested_xor_constants_are_composed_once(self) -> None:
        source = "def load(raw_offset):\n    return (raw_offset ^ 0x00AA) ^ 0x00BB\n"
        report = analyze_source(source)
        self.assertEqual(len(report["rules"]), 1)
        self.assertEqual(report["rules"][0]["key_int"], 0x00AA ^ 0x00BB)
        self.assertEqual(report["stats"]["xor_nodes"], 2)
        self.assertEqual(report["stats"]["rule_count"], 1)

    def test_unresolved_key_produces_findings_but_no_rule(self) -> None:
        source = "def load(raw_offset, key):\n    return raw_offset ^ key\n"
        report = analyze_source(source)
        self.assertEqual(report["rules"], [])
        self.assertEqual(report["stats"]["xor_nodes"], 1)
        self.assertFalse(report["findings"][0]["nested_in_xor"])

    def test_int_and_ord_calls_are_folded(self) -> None:
        source = (
            "KEY = int('0xCAFE', 16)\n"
            "MASK = ord('\\x5a')\n"
            "def load(offset, data):\n"
            "    return (offset ^ KEY), (data ^ MASK)\n"
        )
        report = analyze_source(source)
        keys = {rule["key_int"] for rule in report["rules"]}
        self.assertEqual(keys, {0xCAFE, 0x5A})

    def test_bytes_key_is_preserved(self) -> None:
        source = 'KEY = b"\\x12\\x34"\ndef load(raw_data):\n    return raw_data ^ KEY\n'
        report = analyze_source(source)
        self.assertEqual(len(report["rules"]), 1)
        self.assertEqual(report["rules"][0]["key_bytes_hex"], "1234")
        self.assertEqual(report["rules"][0]["xor_key"], "0x1234")

    def test_python2_source_is_normalised_with_line_context(self) -> None:
        source = (
            "try:\n"
            "    pass\n"
            "except Exception, e:\n"
            "    print 'failed', e\n"
            "key = 0x55\n"
            "offset = raw_offset ^ key\n"
        )
        report = analyze_source(source)
        self.assertTrue(report["parse"]["ok"])
        self.assertEqual(report["parse"]["mode"], "python2-normalized")
        self.assertEqual(report["rules"][0]["key_int"], 0x55)

    def test_source_is_never_executed(self) -> None:
        source = (
            "raise RuntimeError('this must not run')\n"
            "key = 0x77\n"
            "offset = raw ^ key\n"
        )
        # Parsing and walking an AST cannot execute the top-level raise.
        report = analyze_source(source)
        self.assertTrue(report["parse"]["ok"])
        self.assertEqual(report["rules"][0]["key_int"], 0x77)

    def test_syntax_error_is_reported_not_raised(self) -> None:
        report = analyze_source("def broken(:\n    pass\n")
        self.assertFalse(report["parse"]["ok"])
        self.assertTrue(report["warnings"])
        self.assertEqual(report["rules"], [])

    def test_ambiguous_assignments_are_flagged(self) -> None:
        source = (
            "def load(flag, raw_offset):\n"
            "    if flag:\n"
            "        key = 0x11\n"
            "    else:\n"
            "        key = 0x22\n"
            "    offset = raw_offset ^ key\n"
        )
        report = analyze_source(source)
        self.assertEqual(len(report["rules"]), 1)
        self.assertTrue(report["rules"][0]["occurrences"][0]["ambiguous_assignment"])
        self.assertLess(report["rules"][0]["confidence"], 0.8)

    def test_hex_literal_and_target_inference(self) -> None:
        self.assertEqual(infer_target(["raw_offset"]), "offset")
        self.assertEqual(infer_target(["dlen"]), "size")
        self.assertEqual(infer_target(["payload"]), "data")
        self.assertEqual(infer_target(["something"]), "unknown")

    def test_parse_tree_can_be_supplied(self) -> None:
        source = "KEY = 0x10\nx = raw ^ KEY\n"
        tree, mode, warnings = parse_source(source)
        from src.ast_analyzer.dataflow_tracker import build_rule_report

        report = build_rule_report(source, "inline", tree=tree, parse_mode=mode, parse_warnings=warnings)
        self.assertTrue(report["parse"]["ok"])
        self.assertEqual(report["rules"][0]["key_int"], 0x10)

    def test_analyze_file_reads_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "loader.py"
            path.write_text("key = 0x99\nx = raw ^ key\n", encoding="utf-8")
            report = analyze_file(str(path))
            self.assertTrue(report["parse"]["ok"])
            self.assertEqual(report["rules"][0]["key_int"], 0x99)

    def test_analyze_file_missing_returns_report(self) -> None:
        report = analyze_file("does-not-exist.py")
        self.assertFalse(report["parse"]["ok"])
        self.assertIn("无法读取源码", report["warnings"][0])

    def test_analyze_existing_loader_py(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "loader.py"
            path.write_text("KEY = 0xAB\noffset = raw ^ KEY\n", encoding="utf-8")
            report = analyze_loader(path)
            self.assertTrue(report["parse"]["ok"])
            self.assertEqual(report["rules"][0]["key_int"], 0xAB)
            self.assertNotIn("decompiler", report)


if __name__ == "__main__":
    unittest.main()
