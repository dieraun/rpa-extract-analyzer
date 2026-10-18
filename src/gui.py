"""Simple Tkinter GUI for rpa-loader-analyzer.

The GUI is a thin local front-end over the same read-only modules as the CLI.
It never executes recovered source, never uploads files, and only invokes
FFmpeg when the user presses the media conversion button.
"""

from __future__ import annotations

import argparse
import json
import logging
import queue
import sys
import threading
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from src import __version__
from src.ast_analyzer import analyze_loader, analyze_source
from src.build_info import (
    APP_NAME,
    BUILD_DATE,
    BUILD_VERSION,
    DEVELOPER,
    GITHUB_URL,
    LICENSE_NAME,
)
from src.exporter import export_archive
from src.i18n import (
    SUPPORTED_LANGUAGES,
    Translator,
    label_to_code,
    load_language,
    save_language,
)
from src.media_convert import FfmpegOptions, convert_webm
from src.rpa_core import RuleSet, parse_archive
from src.scanner import scan_game
from src.update_checker import UpdateInfo, check_latest_version

APP_TITLE = f"{APP_NAME} {__version__}"
DEFAULT_WORK_DIR = Path.home() / "rpa-loader-analyzer-output"
LOG_FORMAT = "%(levelname)s %(message)s"


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        return candidate.resolve(strict=False).is_relative_to(root.resolve(strict=False))
    except (OSError, ValueError):
        return False


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def _unique_subdir(stem: str, used: set[str]) -> str:
    candidate = stem or "archive"
    counter = 2
    while candidate.casefold() in used:
        candidate = f"{stem}_{counter}"
        counter += 1
    used.add(candidate.casefold())
    return candidate


class _QueueLogHandler(logging.Handler):
    """Forward Python logging records to the Tk event queue."""

    def __init__(self, event_queue: queue.Queue) -> None:
        super().__init__()
        self.event_queue = event_queue
        self.setFormatter(logging.Formatter(LOG_FORMAT))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.event_queue.put(("log", self.format(record)))
        except Exception:  # pragma: no cover - logging must never crash the app
            pass


class AnalyzerApp:
    """Main window and background-task coordinator."""

    def __init__(
        self,
        root: tk.Tk,
        *,
        settings_path: Path | None = None,
        startup_update_check: bool = True,
    ) -> None:
        self.root = root
        self.settings_path = settings_path
        self.startup_update_check = startup_update_check
        self.language = load_language(settings_path)
        self.translator = Translator(self.language)
        self.root.title(APP_TITLE)
        self.root.geometry("1040x760")
        self.root.minsize(860, 620)
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.archives: list[Path] = []
        self.game_root: Path | None = None
        self.loader: Path | None = None
        self._action_buttons: list[ttk.Button] = []
        self._log_lines: list[str] = []
        self._workdir_trace: str | None = None
        self._poll_after_id: str | None = None
        self._startup_after_id: str | None = None
        self._update_check_running = False
        self._log_handler = _QueueLogHandler(self.events)
        self.language_label_var = tk.StringVar(
            value=SUPPORTED_LANGUAGES[self.language]
        )

        self.work_dir = tk.StringVar(value=str(DEFAULT_WORK_DIR))
        self.game_dir = tk.StringVar()
        self.loader_path = tk.StringVar()
        self.status = tk.StringVar(value=self._t("status.ready"))
        self.summary = tk.StringVar(value=self._t("status.not_scanned"))
        self.media_input = tk.StringVar()
        self.media_output = tk.StringVar()
        self.media_hardware = tk.StringVar(value="auto")
        self.media_overwrite = tk.BooleanVar(value=False)
        self.media_copy_first = tk.BooleanVar(value=False)
        self.overwrite_export = tk.BooleanVar(value=False)

        self._configure_style()
        self._build_ui()
        self._install_logging()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<F1>", lambda _event: self._show_about())
        self._poll_after_id = self.root.after(100, self._poll_events)
        if startup_update_check:
            self._startup_after_id = self.root.after(
                1500, self._startup_update_check
            )
        self.log(self._t("log.intro"))

    def _t(self, key: str, **values: Any) -> str:
        return self.translator(key, **values)

    # ------------------------------------------------------------------ UI
    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass
        style.configure("TButton", padding=(10, 4))
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 17, "bold"))
        style.configure("Hint.TLabel", foreground="#555555")
        style.configure("Danger.TLabel", foreground="#9b1c1c")

    def _build_menu(self) -> tk.Menu:
        menubar = tk.Menu(self.root)
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(
            label=self._t("menu.check_updates"),
            command=lambda: self._check_for_updates(manual=True),
        )
        help_menu.add_command(
            label=self._t("menu.about"),
            accelerator="F1",
            command=self._show_about,
        )
        menubar.add_cascade(label=self._t("menu.help"), menu=help_menu)
        return menubar

    def _open_url(self, url: str) -> None:
        try:
            webbrowser.open(url, new=2)
        except Exception as exc:  # noqa: BLE001 - browser failure is non-fatal
            self.log(f"{url}: {exc}", level="WARNING")

    def _show_about(self) -> None:
        window = tk.Toplevel(self.root)
        window.title(self._t("about.title"))
        window.transient(self.root)
        window.resizable(False, False)
        frame = ttk.Frame(window, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame, text=APP_NAME, style="Title.TLabel"
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text=self._t("about.description"),
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(4, 12))
        ttk.Label(
            frame, text=self._t("about.developer", developer=DEVELOPER)
        ).pack(anchor="w", pady=2)
        ttk.Label(
            frame,
            text=self._t(
                "about.build", version=BUILD_VERSION, date=BUILD_DATE
            ),
        ).pack(anchor="w", pady=2)
        ttk.Label(
            frame, text=self._t("about.license", license=LICENSE_NAME)
        ).pack(anchor="w", pady=2)
        ttk.Label(
            frame, text=self._t("about.repo")
        ).pack(anchor="w", pady=(10, 0))
        ttk.Label(
            frame, text=GITHUB_URL, foreground="#1d4ed8", cursor="hand2"
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text=self._t(
                "about.runtime",
                python=sys.version.split()[0],
                tk=tk.TkVersion,
            ),
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(8, 2))
        ttk.Label(
            frame, text=self._t("about.safety"), style="Hint.TLabel"
        ).pack(anchor="w", pady=(2, 14))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(
            buttons,
            text=self._t("about.open_repo"),
            command=lambda: self._open_url(GITHUB_URL),
        ).pack(side="left")
        ttk.Button(
            buttons,
            text=self._t("about.check"),
            command=lambda: self._check_for_updates(manual=True),
        ).pack(side="left", padx=6)
        ttk.Button(
            buttons, text=self._t("about.close"), command=window.destroy
        ).pack(side="right")
        window.grab_set()
        window.focus_set()

    def _startup_update_check(self) -> None:
        self._startup_after_id = None
        if self.startup_update_check:
            self._check_for_updates(manual=False)

    def _check_for_updates(self, *, manual: bool) -> None:
        if self._update_check_running:
            if manual:
                messagebox.showinfo(
                    self._t("message.busy.title"), self._t("update.checking")
                )
            return
        self._update_check_running = True
        self.log(self._t("log.update_checking"))

        def worker() -> None:
            info = check_latest_version(__version__)
            self.events.put(("update", info, manual))

        threading.Thread(
            target=worker, daemon=True, name="rpa-update-check"
        ).start()

    def _handle_update_info(self, info: UpdateInfo, manual: bool) -> None:
        self._update_check_running = False
        if info.error:
            self.log(
                self._t("log.update_failed", error=info.error), level="WARNING"
            )
            if manual:
                messagebox.showwarning(
                    self._t("update.failed.title"),
                    self._t("update.failed.text", error=info.error),
                )
            return
        if info.update_available:
            self.log(
                self._t(
                    "log.update_available",
                    latest=info.latest_version,
                    current=info.current_version,
                )
            )
            if messagebox.askyesno(
                self._t("update.available.title"),
                self._t(
                    "update.available.text",
                    current=info.current_version,
                    latest=info.latest_version,
                ),
                parent=self.root,
            ):
                self._open_url(info.release_url)
            return
        self.log(self._t("log.update_latest", current=info.current_version))
        if manual:
            messagebox.showinfo(
                self._t("update.latest.title"),
                self._t("update.latest.text", current=info.current_version),
            )

    def _build_ui(self) -> None:
        self._action_buttons = []
        self.path_info = None
        self.export_path_label = None
        self.root.config(menu=self._build_menu())
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)
        outer.rowconfigure(3, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="RPA Loader Analyzer", style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            header,
            text=self._t("app.subtitle"),
            style="Hint.TLabel",
        ).grid(row=1, column=0, sticky="w")
        ttk.Label(
            header,
            text=self._t("app.safety"),
            style="Hint.TLabel",
        ).grid(row=2, column=0, sticky="w", pady=(4, 0))

        language_frame = ttk.Frame(header)
        language_frame.grid(row=0, column=1, rowspan=2, sticky="ne")
        ttk.Label(language_frame, text=self._t("language.label")).pack(side="left")
        self.language_combo = ttk.Combobox(
            language_frame,
            textvariable=self.language_label_var,
            values=tuple(SUPPORTED_LANGUAGES.values()),
            state="readonly",
            width=12,
        )
        self.language_combo.pack(side="left", padx=(6, 0))
        self.language_combo.bind("<<ComboboxSelected>>", self._on_language_selected)

        common = ttk.LabelFrame(outer, text=self._t("work.title"), padding=8)
        common.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        common.columnconfigure(1, weight=1)
        ttk.Label(common, text=self._t("common.directory")).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Entry(common, textvariable=self.work_dir).grid(
            row=0, column=1, sticky="ew", padx=6
        )
        ttk.Button(common, text=self._t("common.browse"), command=self._browse_work_dir).grid(
            row=0, column=2, padx=(0, 6)
        )
        self.path_info = ttk.Label(common, text="", style="Hint.TLabel")
        self.path_info.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        if self._workdir_trace is not None:
            try:
                self.work_dir.trace_remove("write", self._workdir_trace)
            except tk.TclError:
                pass
        self._workdir_trace = self.work_dir.trace_add(
            "write", lambda *_args: self._refresh_path_info()
        )

        notebook = ttk.Notebook(outer)
        notebook.grid(row=2, column=0, sticky="nsew", pady=(0, 8))
        self._build_scan_tab(notebook)
        self._build_export_tab(notebook)
        self._build_media_tab(notebook)
        self._refresh_path_info()
        self.notebook = notebook

        log_frame = ttk.LabelFrame(outer, text=self._t("log.title"), padding=6)
        log_frame.grid(row=3, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(
            log_frame,
            height=9,
            wrap="word",
            state="disabled",
            background="#111827",
            foreground="#e5e7eb",
            insertbackground="#e5e7eb",
            font=("Consolas", 9),
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)
        ttk.Button(log_frame, text=self._t("log.clear"), command=self._clear_log).grid(
            row=1, column=0, sticky="e", pady=(6, 0)
        )

        status_bar = ttk.Frame(outer)
        status_bar.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        status_bar.columnconfigure(0, weight=1)
        ttk.Label(status_bar, textvariable=self.summary).grid(row=0, column=0, sticky="w")
        ttk.Label(status_bar, textvariable=self.status).grid(row=0, column=1, sticky="e")

    def _build_scan_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=10)
        notebook.add(tab, text=self._t("tabs.scan"))
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(4, weight=1)

        ttk.Label(tab, text=self._t("scan.game_root")).grid(row=0, column=0, sticky="w")
        ttk.Entry(tab, textvariable=self.game_dir).grid(
            row=0, column=1, sticky="ew", padx=6
        )
        ttk.Button(tab, text=self._t("common.browse"), command=self._browse_game_dir).grid(
            row=0, column=2, padx=(0, 6)
        )
        scan_button = ttk.Button(
            tab,
            text=self._t("scan.scan"),
            style="Accent.TButton",
            command=self._start_scan,
        )
        scan_button.grid(row=0, column=3)
        self._action_buttons.append(scan_button)

        ttk.Label(tab, text=self._t("scan.loader")).grid(
            row=1, column=0, sticky="w", pady=(8, 0)
        )
        ttk.Entry(tab, textvariable=self.loader_path).grid(
            row=1, column=1, columnspan=2, sticky="ew", padx=6, pady=(8, 0)
        )
        analyze_button = ttk.Button(
            tab, text=self._t("scan.analyze"), command=self._start_analysis
        )
        analyze_button.grid(row=1, column=3, pady=(8, 0))
        self._action_buttons.append(analyze_button)

        ttk.Label(tab, textvariable=self.summary, style="Hint.TLabel").grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(8, 4)
        )

        columns = ("kind", "path")
        self.scan_tree = ttk.Treeview(tab, columns=columns, show="headings", height=8)
        self.scan_tree.heading("kind", text=self._t("scan.col.kind"))
        self.scan_tree.heading("path", text=self._t("scan.col.path"))
        self.scan_tree.column("kind", width=90, stretch=False)
        self.scan_tree.column("path", width=700, stretch=True)
        self.scan_tree.grid(row=4, column=0, columnspan=4, sticky="nsew")
        tree_scroll = ttk.Scrollbar(tab, orient="vertical", command=self.scan_tree.yview)
        tree_scroll.grid(row=4, column=4, sticky="ns")
        self.scan_tree.configure(yscrollcommand=tree_scroll.set)

        ttk.Label(
            tab,
            text=self._t("scan.hint"),
            style="Hint.TLabel",
        ).grid(row=5, column=0, columnspan=4, sticky="w", pady=(6, 0))

    def _build_export_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=10)
        notebook.add(tab, text=self._t("tabs.export"))
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

        self.export_path_label = ttk.Label(tab, text="", style="Hint.TLabel")
        self.export_path_label.grid(row=0, column=0, sticky="w")
        ttk.Label(
            tab,
            text=self._t("export.hint"),
            style="Hint.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(4, 6))

        columns = ("archive", "size")
        self.archive_tree = ttk.Treeview(tab, columns=columns, show="headings", height=10)
        self.archive_tree.heading("archive", text=self._t("export.col.archive"))
        self.archive_tree.heading("size", text=self._t("export.col.size"))
        self.archive_tree.column("archive", width=760, stretch=True)
        self.archive_tree.column("size", width=120, stretch=False, anchor="e")
        self.archive_tree.grid(row=2, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(tab, orient="vertical", command=self.archive_tree.yview)
        scroll.grid(row=2, column=1, sticky="ns")
        self.archive_tree.configure(yscrollcommand=scroll.set)

        controls = ttk.Frame(tab)
        controls.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        controls.columnconfigure(0, weight=1)
        ttk.Checkbutton(
            controls,
            text=self._t("export.overwrite"),
            variable=self.overwrite_export,
        ).grid(row=0, column=0, sticky="w")
        export_button = ttk.Button(
            controls,
            text=self._t("export.button"),
            style="Accent.TButton",
            command=self._start_export,
        )
        export_button.grid(row=0, column=1)
        self._action_buttons.append(export_button)

    def _build_media_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=10)
        notebook.add(tab, text=self._t("tabs.media"))
        tab.columnconfigure(1, weight=1)

        ttk.Label(tab, text=self._t("media.input")).grid(row=0, column=0, sticky="w")
        ttk.Entry(tab, textvariable=self.media_input).grid(
            row=0, column=1, sticky="ew", padx=6
        )
        ttk.Button(tab, text=self._t("common.browse"), command=self._browse_media_input).grid(
            row=0, column=2
        )

        ttk.Label(tab, text=self._t("media.output")).grid(
            row=1, column=0, sticky="w", pady=(8, 0)
        )
        ttk.Entry(tab, textvariable=self.media_output).grid(
            row=1, column=1, sticky="ew", padx=6, pady=(8, 0)
        )
        ttk.Button(tab, text=self._t("media.save_as"), command=self._browse_media_output).grid(
            row=1, column=2, pady=(8, 0)
        )

        options = ttk.Frame(tab)
        options.grid(row=2, column=0, columnspan=3, sticky="w", pady=(12, 0))
        ttk.Label(options, text=self._t("media.hardware")).pack(side="left")
        ttk.Combobox(
            options,
            textvariable=self.media_hardware,
            values=("auto", "cpu", "nvidia", "intel", "amd", "vaapi"),
            state="readonly",
            width=10,
        ).pack(side="left", padx=(6, 16))
        ttk.Checkbutton(
            options,
            text=self._t("media.copy_first"),
            variable=self.media_copy_first,
        ).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(
            options,
            text=self._t("media.overwrite"),
            variable=self.media_overwrite,
        ).pack(side="left")

        convert_button = ttk.Button(
            tab,
            text=self._t("media.convert"),
            style="Accent.TButton",
            command=self._start_media,
        )
        convert_button.grid(row=3, column=2, sticky="e", pady=(18, 0))
        self._action_buttons.append(convert_button)

        ttk.Label(
            tab,
            text=self._t("media.hint"),
            style="Hint.TLabel",
            justify="left",
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(14, 0))

    # ------------------------------------------------------------- bindings
    def _on_language_selected(self, _event: Any = None) -> None:
        selected = label_to_code(self.language_label_var.get())
        if selected is None or selected == self.language:
            return
        if self.busy:
            messagebox.showinfo(
                self._t("message.language_busy.title"),
                self._t("message.language_busy.text"),
            )
            self.language_label_var.set(SUPPORTED_LANGUAGES[self.language])
            return
        try:
            save_language(selected, self.settings_path)
        except OSError as exc:
            messagebox.showerror(
                self._t("message.error.title"),
                f"{selected}: {exc}",
            )
            self.language_label_var.set(SUPPORTED_LANGUAGES[self.language])
            return
        self.language = selected
        self.translator = Translator(selected)
        self._rebuild_ui()

    def _rebuild_ui(self) -> None:
        existing_log = list(self._log_lines)
        selected_tab = 0
        try:
            selected_tab = self.notebook.index("current")
        except (AttributeError, tk.TclError):
            pass
        for child in self.root.winfo_children():
            child.destroy()
        self._build_ui()
        for line in existing_log:
            self._append_log_text(line)
        try:
            self.notebook.select(selected_tab)
        except (tk.TclError, IndexError):
            pass
        if not self.archives and self.loader is None:
            self.summary.set(self._t("status.not_scanned"))
        self.status.set(self._t("status.ready"))
        self.log(self._t("log.language_changed"))

    def _browse_work_dir(self) -> None:
        selected = filedialog.askdirectory(
            initialdir=self._work_dir(), title=self._t("dialog.workdir")
        )
        if selected:
            self.work_dir.set(selected)

    def _browse_game_dir(self) -> None:
        selected = filedialog.askdirectory(title=self._t("dialog.game_dir"))
        if selected:
            self.game_dir.set(selected)

    def _browse_media_input(self) -> None:
        selected = filedialog.askopenfilename(
            title=self._t("dialog.webm"),
            filetypes=(
                ("WebM", "*.webm"),
                (self._t("dialog.all_files"), "*.*"),
            ),
        )
        if not selected:
            return
        self.media_input.set(selected)
        if not self.media_output.get().strip():
            self.media_output.set(str(Path(selected).with_suffix(".mp4")))

    def _browse_media_output(self) -> None:
        selected = filedialog.asksaveasfilename(
            title=self._t("dialog.save_mp4"),
            defaultextension=".mp4",
            filetypes=(
                ("MP4", "*.mp4"),
                (self._t("dialog.all_files"), "*.*"),
            ),
        )
        if selected:
            self.media_output.set(selected)

    def _work_dir(self) -> Path:
        return Path(self.work_dir.get().strip() or DEFAULT_WORK_DIR).expanduser()

    def _rules_path(self) -> Path:
        return self._work_dir() / "rules.json"

    def _export_dir(self) -> Path:
        return self._work_dir() / "export"

    def _refresh_path_info(self) -> None:
        base = self._work_dir()
        rules = base / "rules.json"
        export = base / "export"
        if getattr(self, "path_info", None) is not None:
            self.path_info.configure(
                text=self._t("work.path_info", rules=rules, export=export)
            )
        if getattr(self, "export_path_label", None) is not None:
            self.export_path_label.configure(
                text=self._t("export.path_info", rules=rules, export=export)
            )

    def _game_root_resolved(self) -> Path | None:
        if self.game_root is not None:
            return self.game_root
        text = self.game_dir.get().strip()
        if not text:
            return None
        try:
            return Path(text).expanduser().resolve(strict=False)
        except OSError:
            return None

    def _ensure_outside_game(self, target: Path, label: str) -> bool:
        root = self._game_root_resolved()
        if root is None:
            return True
        if _is_within(root, target):
            messagebox.showerror(
                self._t("message.output_blocked.title"),
                self._t(
                    "message.output_blocked.text",
                    label=label,
                    target=target,
                    root=root,
                ),
            )
            return False
        return True

    # --------------------------------------------------------------- tasks
    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        for button in self._action_buttons:
            button.configure(state="disabled" if busy else "normal")
        self.status.set(self._t("status.busy") if busy else self._t("status.ready"))

    def _run(
        self,
        title: str,
        function: Callable[[], Any],
        on_success: Callable[[Any], None] | None = None,
    ) -> None:
        if self.busy:
            messagebox.showinfo(
                self._t("message.busy.title"), self._t("message.busy.text")
            )
            return
        self._set_busy(True)
        self.status.set(title)
        self.log(f"{title}…")

        def worker() -> None:
            try:
                result = function()
            except Exception as exc:  # noqa: BLE001 - show a friendly GUI error
                self.events.put(
                    (
                        "error",
                        self._t("log.task_failed", title=title, error=f"{type(exc).__name__}: {exc}"),
                    )
                )
            else:
                self.events.put(("success", title, result, on_success))

        threading.Thread(target=worker, daemon=True, name="rpa-gui-worker").start()

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    self.log(str(event[1]))
                elif kind == "progress":
                    self.log(str(event[1]))
                elif kind == "update":
                    _kind, info, manual = event
                    self._handle_update_info(info, manual)
                elif kind == "error":
                    self.log(str(event[1]), level="ERROR")
                    self._set_busy(False)
                    messagebox.showerror(
                        self._t("message.error.title"), str(event[1])
                    )
                elif kind == "success":
                    _kind, title, result, callback = event
                    self._set_busy(False)
                    self.log(self._t("log.task_done", title=title))
                    if callback is not None:
                        try:
                            callback(result)
                        except Exception as exc:  # noqa: BLE001
                            self.log(
                                self._t("log.result_error", error=exc),
                                level="ERROR",
                            )
                            messagebox.showerror(
                                self._t("message.result_error.title"), str(exc)
                            )
        except queue.Empty:
            pass
        self._poll_after_id = self.root.after(100, self._poll_events)

    # ------------------------------------------------------------- actions
    def _start_scan(self) -> None:
        game_text = self.game_dir.get().strip()
        if not game_text:
            messagebox.showwarning(
                self._t("message.missing_dir.title"),
                self._t("message.missing_dir.text"),
            )
            return
        game_path = Path(game_text).expanduser()
        self._run(
            self._t("task.scan"),
            lambda: scan_game(game_path),
            self._scan_done,
        )

    def _scan_done(self, layout: Any) -> None:
        self.game_root = layout.root
        self.archives = list(layout.archives)
        self.loader = layout.loader
        self.scan_tree.delete(*self.scan_tree.get_children())
        self.archive_tree.delete(*self.archive_tree.get_children())
        if layout.loader is not None:
            self.loader_path.set(str(layout.loader))
            self.scan_tree.insert("", "end", values=("loader.pyo", str(layout.loader)))
        else:
            self.loader_path.set("")
            self.log(self._t("log.no_loader"), level="WARNING")
        for archive in layout.archives:
            self.scan_tree.insert("", "end", values=("RPA", str(archive)))
            try:
                size = f"{archive.stat().st_size:,} B"
            except OSError:
                size = "?"
            self.archive_tree.insert("", "end", values=(str(archive), size))
        self.summary.set(
            self._t(
                "summary.scan",
                loader=layout.loader or self._t("scan.not_scanned"),
                count=len(layout.archives),
            )
        )
        for warning in layout.warnings:
            self.log(
                self._t("log.scan_warning", warning=warning), level="WARNING"
            )
        if layout.loader is None and not layout.archives:
            messagebox.showinfo(
                self._t("message.scan.title"),
                self._t("message.scan.empty"),
            )
        # Keep the workdir outside the game root when possible.
        if _is_within(layout.root, self._work_dir()):
            self.log(self._t("log.workdir_inside"), level="WARNING")

    def _start_analysis(self) -> None:
        loader_text = self.loader_path.get().strip()
        if not loader_text:
            messagebox.showwarning(
                self._t("message.missing_loader.title"),
                self._t("message.missing_loader.text"),
            )
            return
        loader = Path(loader_text).expanduser()
        if not loader.is_file():
            messagebox.showerror(
                self._t("message.file_missing.title"), str(loader)
            )
            return
        if self.game_root is None:
            try:
                resolved_loader = loader.resolve()
            except OSError:
                resolved_loader = loader.absolute()
            self.game_root = (
                resolved_loader.parent.parent
                if resolved_loader.parent.name.casefold() == "renpy"
                else resolved_loader.parent
            )
        rules_path = self._rules_path()
        if not self._ensure_outside_game(
            rules_path, self._t("label.rules_file")
        ):
            return

        def job() -> tuple[dict[str, Any], Path]:
            report = analyze_loader(loader)
            _atomic_json(rules_path, report)
            return report, rules_path

        self._run(self._t("task.analysis"), job, self._analysis_done)

    def _analysis_done(self, result: tuple[dict[str, Any], Path]) -> None:
        report, rules_path = result
        parse = report.get("parse", {})
        stats = report.get("stats", {})
        analysis = report.get("analysis", "ast")
        if parse.get("ok"):
            self.log(
                self._t(
                    "log.analysis_result",
                    analysis=analysis,
                    xor_nodes=stats.get("xor_nodes", 0),
                    rules=stats.get("rule_count", 0),
                    path=rules_path,
                )
            )
            for rule in report.get("rules", [])[:8]:
                key = rule.get("key_hex") or rule.get("key_bytes_hex") or "?"
                self.log(
                    self._t(
                        "log.rule_line",
                        rule_id=rule.get("id"),
                        target=rule.get("target"),
                        key=key,
                        confidence=rule.get("confidence"),
                    )
                )
        else:
            self.log(
                self._t("log.analysis_failed", path=rules_path), level="ERROR"
            )
        for warning in report.get("warnings", [])[:10]:
            self.log(
                self._t("log.analysis_warning", warning=warning), level="WARNING"
            )
        self.summary.set(
            self._t(
                "summary.analysis",
                analysis=analysis,
                rules=stats.get("rule_count", 0),
                status=(
                    self._t("status.success")
                    if parse.get("ok")
                    else self._t("status.failed")
                ),
            )
        )
        if not parse.get("ok"):
            messagebox.showwarning(
                self._t("message.analysis_failed.title"),
                self._t("message.analysis_failed.text"),
            )

    def _start_export(self) -> None:
        if not self.archives:
            messagebox.showwarning(
                self._t("message.no_archive.title"),
                self._t("message.no_archive.text"),
            )
            return
        export_root = self._export_dir()
        if not self._ensure_outside_game(
            export_root, self._t("label.export_dir")
        ):
            return
        game_root = self._game_root_resolved()
        if game_root is None:
            messagebox.showerror(
                self._t("message.missing_game_root.title"),
                self._t("message.missing_game_root.text"),
            )
            return
        archives = list(self.archives)
        rules_path = self._rules_path()
        overwrite = self.overwrite_export.get()

        def job() -> dict[str, Any]:
            rules = RuleSet.from_file(rules_path) if rules_path.is_file() else None
            export_root.mkdir(parents=True, exist_ok=True)
            used: set[str] = set()
            exported = skipped = failed = 0
            failures: list[str] = []
            total = len(archives)
            for index, archive_path in enumerate(archives, start=1):
                self.events.put(
                    (
                        "progress",
                        self._t(
                            "log.export_progress",
                            index=index,
                            total=total,
                            name=archive_path.name,
                        ),
                    )
                )
                try:
                    archive = parse_archive(archive_path, rules=rules)
                    subdir = _unique_subdir(archive_path.stem, used)
                    outcome = export_archive(
                        archive,
                        export_root / subdir,
                        overwrite=overwrite,
                        continue_on_error=True,
                        protected_roots=[game_root],
                    )
                    exported += len(outcome.exported)
                    skipped += len(outcome.skipped)
                    failed += len(outcome.failed)
                    failures.extend(
                        f"{archive_path.name}: {item.name}: {item.message}"
                        for item in outcome.failed
                    )
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    failures.append(
                        f"{archive_path.name}: {type(exc).__name__}: {exc}"
                    )
            return {
                "archives": total,
                "exported": exported,
                "skipped": skipped,
                "failed": failed,
                "failures": failures,
                "output": str(export_root),
            }

        self._run(self._t("task.export"), job, self._export_done)

    def _export_done(self, result: dict[str, Any]) -> None:
        self.log(
            self._t(
                "log.export_done",
                archives=result["archives"],
                exported=result["exported"],
                skipped=result["skipped"],
                failed=result["failed"],
                output=result["output"],
            )
        )
        for failure in result["failures"][:10]:
            self.log(
                self._t("log.export_failure", failure=failure), level="ERROR"
            )
        self.summary.set(
            self._t(
                "summary.export",
                count=result["exported"],
                output=result["output"],
            )
        )
        if result["failed"]:
            messagebox.showwarning(
                self._t("message.export_partial.title"),
                self._t(
                    "message.export_partial.text",
                    exported=result["exported"],
                    failed=result["failed"],
                ),
            )
        else:
            messagebox.showinfo(
                self._t("message.export_done.title"),
                self._t("message.export_done.text", count=result["exported"]),
            )

    def _start_media(self) -> None:
        input_text = self.media_input.get().strip()
        output_text = self.media_output.get().strip()
        if not input_text or not output_text:
            messagebox.showwarning(
                self._t("message.missing_paths.title"),
                self._t("message.missing_paths.text"),
            )
            return
        input_path = Path(input_text).expanduser()
        output_path = Path(output_text).expanduser()
        if not input_path.is_file():
            messagebox.showerror(
                self._t("message.input_missing.title"), str(input_path)
            )
            return
        options = FfmpegOptions(
            hardware=self.media_hardware.get(),
            overwrite=self.media_overwrite.get(),
            stream_copy_first=self.media_copy_first.get(),
        )

        def job() -> Any:
            return convert_webm(input_path, output_path, options=options)

        self._run(self._t("task.media"), job, self._media_done)

    def _media_done(self, result: Any) -> None:
        mode = (
            self._t("mode.copy")
            if result.stream_copy
            else self._t("mode.reencode")
        )
        self.log(
            self._t(
                "log.media_done", mode=mode, path=result.output_path
            )
        )
        if result.attempts:
            for attempt in result.attempts:
                self.log(self._t("log.media_attempt", attempt=attempt))
        messagebox.showinfo(
            self._t("message.media_done.title"),
            self._t("message.media_done.text", path=result.output_path),
        )
        self.summary.set(
            self._t("summary.media", path=result.output_path)
        )

    # -------------------------------------------------------------- logging
    def _install_logging(self) -> None:
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(self._log_handler)

    def _append_log_text(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def log(self, message: str, *, level: str = "INFO") -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {level} {message}"
        self._log_lines.append(line)
        if len(self._log_lines) > 2000:
            del self._log_lines[:500]
        self._append_log_text(line)

    def _clear_log(self) -> None:
        self._log_lines.clear()
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _on_close(self) -> None:
        if self._startup_after_id is not None:
            try:
                self.root.after_cancel(self._startup_after_id)
            except tk.TclError:
                pass
            self._startup_after_id = None
        if self._poll_after_id is not None:
            try:
                self.root.after_cancel(self._poll_after_id)
            except tk.TclError:
                pass
            self._poll_after_id = None
        try:
            logging.getLogger().removeHandler(self._log_handler)
        except Exception:
            pass
        self.root.destroy()


def _smoke_test() -> int:
    """Check imports and core analysis without opening a window."""

    sample = "KEY = 0x1234\noffset = raw_offset ^ KEY\n"
    report = analyze_source(sample)
    if not report.get("rules") or report["rules"][0].get("key_int") != 0x1234:
        print("smoke-test failed: XOR rule extraction", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "version": __version__,
                "rules": report["stats"]["rule_count"],
                "analysis": "ast",
            },
            ensure_ascii=False,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rpa-loader-analyzer-gui")
    parser.add_argument("--version", action="version", version=APP_TITLE)
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.smoke_test:
        return _smoke_test()
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"Failed to create GUI window / 无法创建 GUI 窗口: {exc}", file=sys.stderr)
        return 2
    AnalyzerApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
