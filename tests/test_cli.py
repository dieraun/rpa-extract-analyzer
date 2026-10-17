"""End-to-end CLI tests using local fixtures only."""

from __future__ import annotations

import contextlib
import io
import json
import py_compile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.cli import main
from src.decompiler import DecompileResult, DisassemblyResult
from src.media_convert import MediaError
from tests.test_disassembly import CLASSIC
from tests.test_rpa_parser import STATIC_KEY, build_rpa


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.game = self.root / "game"
        (self.game / "renpy").mkdir(parents=True)
        (self.game / "renpy" / "loader.pyo").write_bytes(b"\x03\xf3\r\n\x00\x00\x00\x00")
        self.archive = build_rpa(self.game / "game.rpa")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_scan_and_json_report(self) -> None:
        report = self.root / "scan.json"
        code, out, _err = self._run(["scan", str(self.game), "--json-out", str(report)])
        self.assertEqual(code, 0)
        self.assertIn("RPA 归档: 1", out)
        payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(payload["archive_count"], 1)
        self.assertTrue(payload["loader"].endswith("loader.pyo"))

    def test_scan_refuses_report_inside_game(self) -> None:
        code, _out, err = self._run(
            ["scan", str(self.game), "--json-out", str(self.game / "scan.json")]
        )
        self.assertEqual(code, 1)
        self.assertIn("游戏原始目录", err)

    def test_analyze_existing_loader_py(self) -> None:
        loader = self.game / "renpy" / "loader.py"
        loader.write_text("KEY = 0xABCD\nsize = raw_size ^ KEY\n", encoding="utf-8")
        rules_path = self.root / "rules.json"
        code, out, _err = self._run(
            ["analyze", str(loader), "--rules-out", str(rules_path)]
        )
        self.assertEqual(code, 0)
        self.assertIn("规则: 1", out)
        payload = json.loads(rules_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["rules"][0]["key_int"], 0xABCD)

    def test_analyze_refuses_rules_inside_game(self) -> None:
        loader = self.game / "renpy" / "loader.py"
        loader.write_text("KEY = 1\nx = y ^ KEY\n", encoding="utf-8")
        code, _out, err = self._run(
            ["analyze", str(loader), "--rules-out", str(self.game / "rules.json")]
        )
        self.assertEqual(code, 1)
        self.assertIn("游戏原始目录", err)

    def test_list_archive(self) -> None:
        code, out, _err = self._run(["list", str(self.archive)])
        self.assertEqual(code, 0)
        self.assertIn("images/background.png", out)
        self.assertIn("RPA-3.0", out)

    def test_export_archive_to_independent_output(self) -> None:
        output = self.root / "out"
        report = self.root / "export.json"
        code, out, _err = self._run(
            [
                "export",
                str(self.archive),
                "--output",
                str(output),
                "--game-root",
                str(self.game),
                "--report",
                str(report),
            ]
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(
            (output / "images" / "background.png").read_bytes(),
            b"PREFIX!!PNGDATA",
        )
        payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["exported_count"], 1)

    def test_export_refuses_output_inside_game(self) -> None:
        code, _out, err = self._run(
            [
                "export",
                str(self.archive),
                "--output",
                str(self.game / "out"),
                "--game-root",
                str(self.game),
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("游戏原始目录", err)
        self.assertFalse((self.game / "out").exists())

    def test_export_all(self) -> None:
        output = self.root / "all-out"
        code, out, _err = self._run(
            ["export-all", str(self.game), "--output", str(output)]
        )
        self.assertEqual(code, 0, out)
        self.assertTrue((output / "game" / "images" / "background.png").is_file())

    def test_export_all_no_archives(self) -> None:
        empty = self.root / "empty-game"
        empty.mkdir()
        code, _out, err = self._run(["export-all", str(empty), "--output", str(self.root / "x")])
        self.assertEqual(code, 1)
        self.assertIn("没有找到", err)

    def test_end_to_end_analyze_then_export_with_static_rules(self) -> None:
        # Header key is a decoy; the real key lives in the loader source.
        archive = build_rpa(
            self.game / "modded.rpa",
            header_key=0x11111111,
            index_key=STATIC_KEY,
            files=[("images/mod.png", b"MODDATA", b"PRE")],
        )
        loader = self.game / "renpy" / "loader.py"
        loader.write_text(
            "KEY = 0xDEADBEEF\n"
            "offset = raw_offset ^ KEY\n"
            "size = raw_size ^ KEY\n",
            encoding="utf-8",
        )
        rules_path = self.root / "rules.json"
        code, _out, _err = self._run(
            ["analyze", str(loader), "--rules-out", str(rules_path)]
        )
        self.assertEqual(code, 0)

        output = self.root / "modded-out"
        code, out, _err = self._run(
            [
                "export",
                str(archive),
                "--output",
                str(output),
                "--game-root",
                str(self.game),
                "--rules",
                str(rules_path),
            ]
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(
            (output / "images" / "mod.png").read_bytes(),
            b"PREMODDATA",
        )

    def test_analyze_falls_back_to_disassembly(self) -> None:
        loader = self.game / "renpy" / "loader.pyo"
        rules_path = self.root / "asm-rules.json"
        asm_out = self.root / "asm"
        failed = DecompileResult(
            path=loader,
            status="failed",
            message="unsupported bytecode",
        )
        disassembled = DisassemblyResult(
            path=loader,
            status="success",
            message="ok",
            text=CLASSIC,
            code_objects=2,
            instruction_count=8,
        )
        with mock.patch("src.cli.decompile_pyo", return_value=failed), mock.patch(
            "src.cli.disassemble_pyo", return_value=disassembled
        ):
            code, out, _err = self._run(
                [
                    "analyze",
                    str(loader),
                    "--asm-out",
                    str(asm_out),
                    "--rules-out",
                    str(rules_path),
                ]
            )
        self.assertEqual(code, 0, out)
        payload = json.loads(rules_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["analysis"], "bytecode-disassembly")
        self.assertGreaterEqual(payload["stats"]["rule_count"], 1)

    def test_disassemble_command_writes_text(self) -> None:
        source = self.game / "renpy" / "loader.py"
        source.write_text("KEY = 0x1234\nvalue = raw ^ KEY\n", encoding="utf-8")
        pyc = self.game / "renpy" / "loader.pyc"
        py_compile.compile(str(source), cfile=str(pyc), doraise=True)
        output = self.root / "disassembly"
        code, out, _err = self._run(
            ["disassemble", str(pyc), "--output-dir", str(output)]
        )
        self.assertEqual(code, 0, out)
        target = output / "loader_disassembly.txt"
        self.assertTrue(target.is_file())
        self.assertIn("LOAD_", target.read_text(encoding="utf-8"))

    def test_disassemble_refuses_output_inside_game(self) -> None:
        source = self.game / "renpy" / "loader.py"
        source.write_text("x = 1\n", encoding="utf-8")
        pyc = self.game / "renpy" / "loader.pyc"
        py_compile.compile(str(source), cfile=str(pyc), doraise=True)
        code, _out, err = self._run(
            ["disassemble", str(pyc), "--output-dir", str(self.game / "asm")]
        )
        self.assertEqual(code, 1)
        self.assertIn("游戏原始目录", err)

    def test_convert_error_is_reported(self) -> None:
        source = self.root / "v.webm"
        source.write_bytes(b"not a real video")
        with mock.patch(
            "src.cli.convert_webm", side_effect=MediaError("synthetic ffmpeg failure")
        ):
            code, _out, err = self._run(
                ["convert", str(source), str(self.root / "v.mp4")]
            )
        self.assertEqual(code, 1)
        self.assertIn("synthetic ffmpeg failure", err)


if __name__ == "__main__":
    unittest.main()
