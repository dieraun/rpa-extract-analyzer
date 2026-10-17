"""Tests for the two-language localisation and settings persistence."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.i18n import (
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    TRANSLATIONS,
    Translator,
    label_to_code,
    load_language,
    normalize_language,
    save_language,
)


class I18nTests(unittest.TestCase):
    def test_only_chinese_and_english_are_supported(self) -> None:
        self.assertEqual(set(SUPPORTED_LANGUAGES), {"zh_CN", "en"})
        self.assertEqual(len(TRANSLATIONS), 2)

    def test_translation_keys_are_complete(self) -> None:
        self.assertEqual(set(TRANSLATIONS["zh_CN"]), set(TRANSLATIONS["en"]))

    def test_translation_switches_language(self) -> None:
        zh = Translator("zh_CN")
        en = Translator("en")
        self.assertEqual(zh("tabs.scan"), "1. 扫描与分析")
        self.assertEqual(en("tabs.scan"), "1. Scan & Analyze")
        self.assertNotEqual(zh("log.intro"), en("log.intro"))

    def test_format_values(self) -> None:
        en = Translator("en")
        self.assertEqual(
            en("summary.export", count=3, output="D:/out"),
            "Exported: 3 file(s) -> D:/out",
        )

    def test_normalize_language(self) -> None:
        self.assertEqual(normalize_language("zh-CN"), "zh_CN")
        self.assertEqual(normalize_language("zh_Hans"), "zh_CN")
        self.assertEqual(normalize_language("en-US"), "en")
        self.assertIsNone(normalize_language("ja"))
        self.assertIsNone(normalize_language(None))

    def test_unsupported_language_falls_back(self) -> None:
        self.assertEqual(Translator("ja").language, DEFAULT_LANGUAGE)
        self.assertEqual(Translator("ja")("status.ready"), "就绪")

    def test_settings_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            self.assertEqual(load_language(path), "zh_CN")
            save_language("en", path)
            self.assertEqual(load_language(path), "en")
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["language"], "en")

    def test_invalid_settings_are_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text("{broken", encoding="utf-8")
            self.assertEqual(load_language(path), "zh_CN")
            path.write_text(json.dumps({"language": "ja"}), encoding="utf-8")
            self.assertEqual(load_language(path), "zh_CN")
            with self.assertRaises(ValueError):
                save_language("ja", path)

    def test_label_mapping(self) -> None:
        self.assertEqual(label_to_code("简体中文"), "zh_CN")
        self.assertEqual(label_to_code("English"), "en")
        self.assertIsNone(label_to_code("日本語"))


if __name__ == "__main__":
    unittest.main()
