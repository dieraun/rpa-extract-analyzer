"""Headless tests for GUI helpers and the packaged-exe smoke hook."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from src.gui import AnalyzerApp, _atomic_json, _is_within, _unique_subdir, main
from src.update_checker import UpdateInfo
from tests.test_rpa_parser import build_rpa


def _pump(root, app, timeout: float = 20.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        root.update()
        if not app.busy:
            return True
        time.sleep(0.02)
    return False


class GuiHelperTests(unittest.TestCase):
    def test_smoke_test_exit_code(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["--smoke-test"])
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["rules"], 1)

    def test_atomic_json_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "rules.json"
            _atomic_json(target, {"ok": True, "规则": "测试"})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["ok"], True)

    def test_is_within(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertTrue(_is_within(root, root / "sub" / "file"))
            self.assertFalse(_is_within(root, root.parent / "outside"))

    def test_unique_subdir(self) -> None:
        used: set[str] = set()
        self.assertEqual(_unique_subdir("game", used), "game")
        self.assertEqual(_unique_subdir("game", used), "game_2")
        self.assertEqual(_unique_subdir("game", used), "game_3")

    def test_gui_scan_analyze_export_workflow(self) -> None:
        try:
            import tkinter as tk
        except ImportError as exc:
            self.skipTest(f"tkinter 不可用: {exc}")
        with tempfile.TemporaryDirectory() as tmp:
            root_dir = Path(tmp)
            game = root_dir / "game"
            (game / "renpy").mkdir(parents=True)
            (game / "renpy" / "loader.pyo").write_bytes(
                b"\x03\xf3\r\n\x00\x00\x00\x00bad"
            )
            loader_source = game / "renpy" / "loader.py"
            loader_source.write_text(
                "KEY = 0xDEADBEEF\noffset = raw ^ KEY\nsize = raw2 ^ KEY\n",
                encoding="utf-8",
            )
            build_rpa(game / "game.rpa")
            work = root_dir / "work"

            try:
                root = tk.Tk()
            except tk.TclError as exc:
                self.skipTest(f"无可用图形环境: {exc}")
            root.withdraw()
            app = AnalyzerApp(
                root,
                settings_path=root_dir / "settings.json",
                startup_update_check=False,
            )
            app.work_dir.set(str(work))
            app.game_dir.set(str(game))
            try:
                with mock.patch("src.gui.messagebox.showinfo"), mock.patch(
                    "src.gui.messagebox.showwarning"
                ), mock.patch("src.gui.messagebox.showerror"):
                    app._start_scan()
                    self.assertTrue(_pump(root, app))
                    self.assertEqual(len(app.archives), 1)
                    app.loader_path.set(str(loader_source))
                    app._start_analysis()
                    self.assertTrue(_pump(root, app))
                    self.assertTrue((work / "rules.json").is_file())
                    app._start_export()
                    self.assertTrue(_pump(root, app))
                    self.assertTrue(
                        (
                            work
                            / "export"
                            / "game"
                            / "images"
                            / "background.png"
                        ).is_file()
                    )
            finally:
                app._on_close()

    def test_gui_language_switch_rebuilds_ui_and_persists(self) -> None:
        try:
            import tkinter as tk
        except ImportError as exc:
            self.skipTest(f"tkinter 不可用: {exc}")
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            try:
                root = tk.Tk()
            except tk.TclError as exc:
                self.skipTest(f"无可用图形环境: {exc}")
            root.withdraw()
            app = AnalyzerApp(
                root,
                settings_path=settings,
                startup_update_check=False,
            )
            try:
                self.assertEqual(app.language, "zh_CN")
                self.assertEqual(app._t("tabs.scan"), "1. 扫描与分析")
                app.language_label_var.set("English")
                app._on_language_selected()
                root.update()
                self.assertEqual(app.language, "en")
                self.assertEqual(app._t("tabs.scan"), "1. Scan & Analyze")
                self.assertEqual(json.loads(settings.read_text(encoding="utf-8"))["language"], "en")
                app.language_label_var.set("简体中文")
                app._on_language_selected()
                root.update()
                self.assertEqual(app.language, "zh_CN")
            finally:
                app._on_close()

    def test_about_dialog_shows_developer_and_build(self) -> None:
        try:
            import tkinter as tk
        except ImportError as exc:
            self.skipTest(f"tkinter 不可用: {exc}")
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"无可用图形环境: {exc}")
        root.withdraw()
        app = AnalyzerApp(
            root,
            settings_path=Path(tempfile.gettempdir()) / "rpa-gui-test-settings.json",
            startup_update_check=False,
        )

        def _texts(widget) -> list[str]:
            values: list[str] = []
            for child in widget.winfo_children():
                try:
                    values.append(str(child.cget("text")))
                except Exception:
                    pass
                values.extend(_texts(child))
            return values

        try:
            app._show_about()
            root.update()
            dialogs = [
                child
                for child in root.winfo_children()
                if isinstance(child, tk.Toplevel)
            ]
            self.assertEqual(len(dialogs), 1)
            texts = " ".join(_texts(dialogs[0]))
            self.assertIn("dieraun", texts)
            self.assertIn("1.0.0", texts)
            self.assertIn("MIT", texts)
            dialogs[0].destroy()
        finally:
            app._on_close()

    def test_update_result_handling(self) -> None:
        try:
            import tkinter as tk
        except ImportError as exc:
            self.skipTest(f"tkinter 不可用: {exc}")
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"无可用图形环境: {exc}")
        root.withdraw()
        app = AnalyzerApp(
            root,
            settings_path=Path(tempfile.gettempdir()) / "rpa-gui-test-settings-2.json",
            startup_update_check=False,
        )
        try:
            with mock.patch("src.gui.messagebox.showinfo") as info_box, mock.patch(
                "src.gui.messagebox.askyesno", return_value=False
            ), mock.patch("src.gui.messagebox.showwarning") as warn_box:
                app._handle_update_info(
                    UpdateInfo(
                        current_version="1.0.0",
                        latest_version="1.0.0",
                        update_available=False,
                    ),
                    manual=True,
                )
                info_box.assert_called_once()
                app._handle_update_info(
                    UpdateInfo(
                        current_version="1.0.0",
                        latest_version="1.1.0",
                        update_available=True,
                        release_url="https://example.invalid/release",
                    ),
                    manual=False,
                )
                app._handle_update_info(
                    UpdateInfo(current_version="1.0.0", error="offline"),
                    manual=True,
                )
                warn_box.assert_called_once()
        finally:
            app._on_close()


if __name__ == "__main__":
    unittest.main()
