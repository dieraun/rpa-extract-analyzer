"""Mechanical checks that keep the project's hard security promises honest."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def _source_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


class SecurityContractTests(unittest.TestCase):
    def test_all_source_files_parse(self) -> None:
        for path in _source_files():
            with self.subTest(path=path):
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_no_exec_or_eval_calls(self) -> None:
        for path in _source_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(
                        node.func.id,
                        {"exec", "eval", "compile", "__import__"},
                        f"禁止的调用 {node.func.id} in {path}:{node.lineno}",
                    )

    def test_no_pickle_load_or_loads(self) -> None:
        for path in _source_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "pickle"
                    and node.func.attr in {"load", "loads", "Unpickler"}
                ):
                    self.fail(f"禁止的 pickle 调用 in {path}:{node.lineno}")

    def test_no_network_imports_in_source(self) -> None:
        forbidden = {"urllib", "http", "requests", "socket", "ftplib"}
        for path in _source_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn(alias.name.split(".")[0], forbidden)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    self.assertNotIn(node.module.split(".")[0], forbidden)

    def test_subprocess_is_confined_to_media_wrapper(self) -> None:
        for path in _source_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imports_subprocess = any(
                isinstance(node, ast.Import)
                and any(alias.name == "subprocess" for alias in node.names)
                for node in ast.walk(tree)
            )
            if imports_subprocess:
                self.assertEqual(
                    path.name,
                    "ffmpeg_wrapper.py",
                    f"subprocess 只允许出现在媒体封装中: {path}",
                )
                self.assertNotIn(
                    "shell=True",
                    path.read_text(encoding="utf-8"),
                    "禁止通过 shell 执行外部命令",
                )


if __name__ == "__main__":
    unittest.main()
