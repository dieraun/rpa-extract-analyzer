"""Tests for JSON XOR rule loading and application."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.rpa_core.rule_applier import (
    RuleError,
    RuleSet,
    XorRule,
    load_rules,
)


class XorRuleTests(unittest.TestCase):
    def test_int_key_roundtrip(self) -> None:
        rule = XorRule(key=0xDEADBEEF, target="offset", rule_id="xor-0001")
        encoded = 0x11223344
        self.assertEqual(rule.apply_int(encoded), encoded ^ 0xDEADBEEF)
        self.assertEqual(rule.apply_int(rule.apply_int(encoded)), encoded)
        payload = rule.to_dict()
        self.assertEqual(payload["key_int"], 0xDEADBEEF)
        self.assertEqual(payload["key_hex"], "0xDEADBEEF")

    def test_bytes_key_is_repeat_xored(self) -> None:
        rule = XorRule(key=b"\x01\x02")
        self.assertEqual(rule.apply_bytes(b"\x00\x00\x00\x00"), b"\x01\x02\x01\x02")
        self.assertEqual(rule.apply_int(0x0102), 0)

    def test_target_mismatch_is_rejected(self) -> None:
        rule = XorRule(key=1, target="offset")
        with self.assertRaises(RuleError):
            rule.apply_int(2, target="size")

    def test_unknown_target_wildcard(self) -> None:
        rule = XorRule(key=7, target="unknown")
        self.assertEqual(rule.apply_int(0, target="size"), 7)

    def test_from_mapping_rejects_bad_keys(self) -> None:
        for mapping in (
            {},
            {"key_int": -1},
            {"key_int": True},
            {"key_bytes_hex": ""},
            {"key_bytes_hex": "zz"},
            {"key_bytes_hex": "00" * 9},
            {"xor_key": "not-a-number"},
        ):
            with self.subTest(mapping=mapping):
                with self.assertRaises(RuleError):
                    XorRule.from_mapping(mapping)


class RuleSetTests(unittest.TestCase):
    def _report(self) -> dict:
        return {
            "schema": "rpa-loader-analyzer/rules-v1",
            "rules": [
                {
                    "id": "xor-0001",
                    "target": "offset",
                    "key_int": 0xAA,
                    "confidence": 0.9,
                    "line": 10,
                },
                {
                    "id": "xor-0002",
                    "target": "size",
                    "key_int": 0xBB,
                    "confidence": 0.8,
                    "line": 20,
                },
                {
                    "id": "xor-0003",
                    "target": "unknown",
                    "key_int": 0xCC,
                    "confidence": 0.5,
                    "line": 30,
                },
                {
                    "id": "xor-0004",
                    "target": "key",
                    "key_int": 0xDD,
                },
            ],
        }

    def test_from_mapping_and_candidates(self) -> None:
        rules = RuleSet.from_mapping(self._report())
        self.assertTrue(rules.has_rules)
        self.assertEqual(len(rules.rules), 3)
        self.assertEqual([rule.key for rule in rules.candidate_keys("offset")], [0xAA, 0xCC])
        self.assertEqual([rule.key for rule in rules.candidate_keys("size")], [0xBB, 0xCC])
        pairs = rules.candidate_pairs()
        self.assertIn((0xAA, 0xBB), [(a.key, b.key) for a, b in pairs])
        self.assertIn((0xAA, 0xCC), [(a.key, b.key) for a, b in pairs])
        self.assertIn((0xCC, 0xCC), [(a.key, b.key) for a, b in pairs])

    def test_deduplicates_same_key_and_target(self) -> None:
        payload = {
            "rules": [
                {"target": "offset", "key_int": 5, "confidence": 0.4},
                {"target": "offset", "key_int": 5, "confidence": 0.9},
            ]
        }
        rules = RuleSet.from_mapping(payload)
        self.assertEqual(len(rules.rules), 1)
        self.assertEqual(rules.candidate_keys("offset")[0].confidence, 0.9)

    def test_accepts_bare_list_and_hex_keys(self) -> None:
        rules = RuleSet.from_mapping([{"target": "offset", "key_hex": "0x2A"}])
        self.assertEqual(rules.candidate_keys("offset")[0].key, 0x2A)

    def test_invalid_payloads_raise(self) -> None:
        for payload in ("bad", 42, {"rules": "nope"}):
            with self.subTest(payload=payload):
                with self.assertRaises(RuleError):
                    RuleSet.from_mapping(payload)

    def test_empty_ruleset(self) -> None:
        rules = RuleSet.empty()
        self.assertFalse(rules.has_rules)
        self.assertEqual(rules.candidate_pairs(), [])
        self.assertEqual(rules.all_keys(), [])

    def test_load_from_file_and_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text(json.dumps(self._report()), encoding="utf-8")
            rules = load_rules(path)
            self.assertTrue(rules.has_rules)
            self.assertEqual(rules.source, str(path))

            missing = Path(tmp) / "missing.json"
            with self.assertRaises(RuleError):
                load_rules(missing)

    def test_invalid_json_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(RuleError):
                load_rules(path)

    def test_json_serialisation(self) -> None:
        rules = RuleSet.from_mapping(self._report())
        json.dumps(rules.to_dict())


if __name__ == "__main__":
    unittest.main()
