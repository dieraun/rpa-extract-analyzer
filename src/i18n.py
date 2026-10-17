"""Minimal two-language UI localisation.

Supported languages are intentionally limited to Simplified Chinese and
English.  The selected language is persisted in a small local JSON file under
``%APPDATA%`` (or ``~/.config`` on non-Windows systems).
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

SUPPORTED_LANGUAGES: dict[str, str] = {
    "zh_CN": "简体中文",
    "en": "English",
}
DEFAULT_LANGUAGE = "zh_CN"


def normalize_language(value: Any) -> str | None:
    """Return a supported language code, or ``None`` for unsupported input."""

    if not isinstance(value, str):
        return None
    key = value.strip().lower().replace("-", "_")
    if key in {"zh", "zh_cn", "zh_hans", "zh_hans_cn"}:
        return "zh_CN"
    if key in {"en", "en_us", "en_gb"}:
        return "en"
    return None


def default_settings_path() -> Path:
    """Return the per-user settings path without creating it."""

    appdata = os.environ.get("APPDATA")
    if appdata:
        base = Path(appdata)
    else:
        base = Path.home() / ".config"
    return base / "rpa-loader-analyzer" / "settings.json"


def load_language(path: str | Path | None = None) -> str:
    """Load the saved language, falling back to Simplified Chinese."""

    settings_path = Path(path) if path is not None else default_settings_path()
    try:
        payload = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return DEFAULT_LANGUAGE
    if not isinstance(payload, dict):
        return DEFAULT_LANGUAGE
    return normalize_language(payload.get("language")) or DEFAULT_LANGUAGE


def save_language(language: str, path: str | Path | None = None) -> Path:
    """Persist a supported language code atomically."""

    normalized = normalize_language(language)
    if normalized is None:
        raise ValueError(f"不支持的语言: {language!r}")
    settings_path = Path(path) if path is not None else default_settings_path()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = settings_path.with_name(
        f".{settings_path.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps({"language": normalized}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(settings_path)
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
    return settings_path


TRANSLATIONS: dict[str, dict[str, str]] = {
    "zh_CN": {
        "app.subtitle": "Ren'Py loader 静态分析 / RPA 导出 / WebM 转换",
        "app.safety": "只读原件 · 不执行反编译代码 · 不使用 pickle.load · 本地处理",
        "language.label": "语言",
        "work.title": "工作目录（规则和导出结果都写在这里）",
        "common.directory": "目录",
        "common.browse": "浏览…",
        "dialog.workdir": "选择工作目录",
        "dialog.game_dir": "选择游戏根目录",
        "dialog.webm": "选择 WebM",
        "dialog.save_mp4": "保存 MP4",
        "dialog.all_files": "所有文件",
        "work.path_info": "规则: {rules}    导出: {export}",
        "label.rules_file": "规则文件",
        "label.export_dir": "资源导出目录",
        "tabs.scan": "1. 扫描与分析",
        "tabs.export": "2. 导出 RPA",
        "tabs.media": "3. 媒体转换",
        "scan.game_root": "游戏根目录",
        "scan.scan": "扫描游戏",
        "scan.loader": "loader",
        "scan.analyze": "分析 loader 并生成规则",
        "scan.not_scanned": "尚未扫描",
        "scan.col.kind": "类型",
        "scan.col.path": "路径",
        "scan.hint": "分析优先使用 uncompyle6 + AST；失败时自动使用 xdis 反汇编。",
        "export.path_info": "导出位置: {export}    规则文件: {rules}",
        "export.hint": "先扫描游戏；存在 rules.json 时会自动用于还原魔改 offset/size。",
        "export.col.archive": "RPA 归档",
        "export.col.size": "大小",
        "export.overwrite": "允许覆盖已存在的输出文件",
        "export.button": "导出全部 RPA",
        "media.input": "输入 WebM",
        "media.output": "输出 MP4",
        "media.save_as": "另存为…",
        "media.hardware": "硬件编码",
        "media.reencode": "强制重编码 H.264/AAC",
        "media.overwrite": "允许覆盖",
        "media.convert": "开始转换",
        "media.hint": (
            "默认先尝试流复制；目标容器不接受时自动重编码。\n"
            "强制重编码可确保 MP4 使用 H.264/AAC（兼容性更好）。\n"
            "需要本机已安装 ffmpeg/ffprobe。"
        ),
        "log.title": "运行日志",
        "log.clear": "清空日志",
        "status.ready": "就绪",
        "status.busy": "处理中…",
        "status.success": "成功",
        "status.failed": "失败",
        "status.not_scanned": "尚未扫描",
        "log.intro": "提示：仅处理你拥有合法授权的本地文件；原始文件只读，导出写入独立目录。",
        "log.scan_start": "扫描游戏目录…",
        "log.scan_done": "扫描完成：loader={loader}，RPA={count} 个",
        "log.no_loader": "警告：未找到 renpy/loader.pyo",
        "log.scan_warning": "扫描警告: {warning}",
        "log.workdir_inside": "警告：工作目录位于游戏目录内，导出将被拒绝。",
        "log.analysis_start": "分析 loader…",
        "log.analysis_result": (
            "分析方式={analysis} XOR节点={xor_nodes} 规则={rules} -> {path}"
        ),
        "log.rule_line": "  {rule_id} {target} key={key} confidence={confidence}",
        "log.analysis_failed": "分析未成功，报告已写入: {path}",
        "log.analysis_warning": "警告: {warning}",
        "log.export_start": "导出全部 RPA…",
        "log.export_progress": "[{index}/{total}] {name}",
        "log.export_done": (
            "导出完成: 归档={archives} 文件={exported} 跳过={skipped} "
            "失败={failed} -> {output}"
        ),
        "log.export_failure": "导出失败: {failure}",
        "log.media_start": "媒体转换…",
        "log.media_done": "媒体转换完成（{mode}）: {path}",
        "log.media_attempt": "  {attempt}",
        "log.task_done": "{title}完成。",
        "log.task_failed": "{title}失败: {error}",
        "log.result_error": "处理结果失败: {error}",
        "log.language_changed": "界面语言已切换为简体中文。",
        "task.scan": "扫描游戏目录",
        "task.analysis": "分析 loader",
        "task.export": "导出全部 RPA",
        "task.media": "媒体转换",
        "summary.scan": "loader: {loader}    RPA: {count} 个",
        "summary.analysis": "分析: {analysis}  规则: {rules}  {status}",
        "summary.export": "导出: {count} 个文件 -> {output}",
        "summary.media": "媒体转换完成: {path}",
        "mode.copy": "流复制",
        "mode.reencode": "重编码",
        "message.busy.title": "请稍候",
        "message.busy.text": "当前已有任务在执行。",
        "message.missing_dir.title": "缺少目录",
        "message.missing_dir.text": "请先选择游戏根目录。",
        "message.scan.title": "扫描结果",
        "message.scan.empty": "没有找到 loader.pyo 或 RPA 归档。",
        "message.missing_loader.title": "缺少 loader",
        "message.missing_loader.text": "请先扫描或填写 loader.pyo / loader.py 路径。",
        "message.file_missing.title": "文件不存在",
        "message.output_blocked.title": "输出被拒绝",
        "message.output_blocked.text": (
            "{label}不能位于游戏目录内：\n{target}\n\n游戏根目录：{root}"
        ),
        "message.analysis_failed.title": "分析未成功",
        "message.analysis_failed.text": (
            "未能生成可用规则；请查看日志或使用 CLI 的 disassemble 命令人工分析。"
        ),
        "message.no_archive.title": "没有归档",
        "message.no_archive.text": "请先扫描游戏目录。",
        "message.missing_game_root.title": "缺少游戏根目录",
        "message.missing_game_root.text": "请先扫描游戏目录。",
        "message.export_partial.title": "导出完成但有失败",
        "message.export_partial.text": "成功 {exported}，失败 {failed}。详情见日志。",
        "message.export_done.title": "导出完成",
        "message.export_done.text": "已导出 {count} 个文件。",
        "message.missing_paths.title": "缺少路径",
        "message.missing_paths.text": "请选择输入 WebM 和输出 MP4。",
        "message.input_missing.title": "输入不存在",
        "message.media_done.title": "转换完成",
        "message.media_done.text": "已输出：\n{path}",
        "message.language_busy.title": "请稍候",
        "message.language_busy.text": "任务执行中不能切换语言。",
        "message.error.title": "操作失败",
        "message.result_error.title": "处理结果失败",
    },
    "en": {
        "app.subtitle": "Static Ren'Py loader analysis / RPA export / WebM conversion",
        "app.safety": (
            "Read-only originals · No decompiled-code execution · "
            "No pickle.load · Local only"
        ),
        "language.label": "Language",
        "work.title": "Workspace (rules and exports are written here)",
        "common.directory": "Directory",
        "common.browse": "Browse…",
        "dialog.workdir": "Select workspace",
        "dialog.game_dir": "Select game root",
        "dialog.webm": "Select WebM",
        "dialog.save_mp4": "Save MP4",
        "dialog.all_files": "All files",
        "work.path_info": "Rules: {rules}    Export: {export}",
        "label.rules_file": "Rules file",
        "label.export_dir": "Resource export directory",
        "tabs.scan": "1. Scan & Analyze",
        "tabs.export": "2. Export RPA",
        "tabs.media": "3. Media Convert",
        "scan.game_root": "Game root",
        "scan.scan": "Scan Game",
        "scan.loader": "Loader",
        "scan.analyze": "Analyze loader and create rules",
        "scan.not_scanned": "Not scanned yet",
        "scan.col.kind": "Type",
        "scan.col.path": "Path",
        "scan.hint": (
            "Analysis uses uncompyle6 + AST first; falls back to xdis "
            "disassembly automatically."
        ),
        "export.path_info": "Export to: {export}    Rules: {rules}",
        "export.hint": (
            "Scan the game first. If rules.json exists it is used to restore "
            "patched offsets/sizes."
        ),
        "export.col.archive": "RPA archive",
        "export.col.size": "Size",
        "export.overwrite": "Overwrite existing output files",
        "export.button": "Export All RPA",
        "media.input": "Input WebM",
        "media.output": "Output MP4",
        "media.save_as": "Save as…",
        "media.hardware": "Hardware encoder",
        "media.reencode": "Force H.264/AAC re-encode",
        "media.overwrite": "Allow overwrite",
        "media.convert": "Start Conversion",
        "media.hint": (
            "Tries stream copy first; falls back to re-encode if needed.\n"
            "Force re-encode ensures MP4 uses H.264/AAC for better compatibility.\n"
            "Requires local ffmpeg/ffprobe."
        ),
        "log.title": "Log",
        "log.clear": "Clear Log",
        "status.ready": "Ready",
        "status.busy": "Working…",
        "status.success": "Success",
        "status.failed": "Failed",
        "status.not_scanned": "Not scanned yet",
        "log.intro": (
            "Notice: only process local files you are authorized to use; "
            "originals are read-only and exports go to a separate directory."
        ),
        "log.scan_start": "Scanning game directory…",
        "log.scan_done": "Scan complete: loader={loader}, {count} RPA archive(s)",
        "log.no_loader": "Warning: renpy/loader.pyo was not found",
        "log.scan_warning": "Scan warning: {warning}",
        "log.workdir_inside": (
            "Warning: the workspace is inside the game directory; export will "
            "be refused."
        ),
        "log.analysis_start": "Analyzing loader…",
        "log.analysis_result": (
            "Analysis={analysis} XOR nodes={xor_nodes} rules={rules} -> {path}"
        ),
        "log.rule_line": "  {rule_id} {target} key={key} confidence={confidence}",
        "log.analysis_failed": "Analysis did not succeed; report written to: {path}",
        "log.analysis_warning": "Warning: {warning}",
        "log.export_start": "Exporting all RPA archives…",
        "log.export_progress": "[{index}/{total}] {name}",
        "log.export_done": (
            "Export complete: archives={archives} files={exported} "
            "skipped={skipped} failed={failed} -> {output}"
        ),
        "log.export_failure": "Export failure: {failure}",
        "log.media_start": "Converting media…",
        "log.media_done": "Media conversion complete ({mode}): {path}",
        "log.media_attempt": "  {attempt}",
        "log.task_done": "{title} complete.",
        "log.task_failed": "{title} failed: {error}",
        "log.result_error": "Failed to handle result: {error}",
        "log.language_changed": "Interface language switched to English.",
        "task.scan": "Scan game directory",
        "task.analysis": "Analyze loader",
        "task.export": "Export all RPA",
        "task.media": "Media conversion",
        "summary.scan": "Loader: {loader}    RPA: {count}",
        "summary.analysis": "Analysis: {analysis}  Rules: {rules}  {status}",
        "summary.export": "Exported: {count} file(s) -> {output}",
        "summary.media": "Media conversion complete: {path}",
        "mode.copy": "stream copy",
        "mode.reencode": "re-encode",
        "message.busy.title": "Please wait",
        "message.busy.text": "Another task is already running.",
        "message.missing_dir.title": "Missing directory",
        "message.missing_dir.text": "Select a game root first.",
        "message.scan.title": "Scan result",
        "message.scan.empty": "No loader.pyo or RPA archive was found.",
        "message.missing_loader.title": "Missing loader",
        "message.missing_loader.text": (
            "Scan the game or enter a loader.pyo / loader.py path first."
        ),
        "message.file_missing.title": "File not found",
        "message.output_blocked.title": "Output refused",
        "message.output_blocked.text": (
            "{label} cannot be inside the game directory:\n{target}\n\n"
            "Game root: {root}"
        ),
        "message.analysis_failed.title": "Analysis did not succeed",
        "message.analysis_failed.text": (
            "No usable rules were produced. Check the log or use the CLI "
            "disassemble command for manual analysis."
        ),
        "message.no_archive.title": "No archives",
        "message.no_archive.text": "Scan the game directory first.",
        "message.missing_game_root.title": "Missing game root",
        "message.missing_game_root.text": "Scan the game directory first.",
        "message.export_partial.title": "Export completed with failures",
        "message.export_partial.text": (
            "Succeeded: {exported}; failed: {failed}. See the log for details."
        ),
        "message.export_done.title": "Export complete",
        "message.export_done.text": "Exported {count} file(s).",
        "message.missing_paths.title": "Missing paths",
        "message.missing_paths.text": "Select an input WebM and an output MP4.",
        "message.input_missing.title": "Input not found",
        "message.media_done.title": "Conversion complete",
        "message.media_done.text": "Output:\n{path}",
        "message.language_busy.title": "Please wait",
        "message.language_busy.text": "The language cannot be changed while a task is running.",
        "message.error.title": "Operation failed",
        "message.result_error.title": "Failed to handle result",
    },
}


class Translator:
    """Look up templates for one language and apply ``str.format`` values."""

    def __init__(self, language: str) -> None:
        self.language = normalize_language(language) or DEFAULT_LANGUAGE

    def __call__(self, key: str, **values: Any) -> str:
        template = TRANSLATIONS.get(self.language, {}).get(key)
        if template is None:
            template = TRANSLATIONS[DEFAULT_LANGUAGE].get(key, key)
        try:
            return template.format(**values)
        except (KeyError, IndexError, ValueError):
            return template


def language_labels() -> dict[str, str]:
    """Return a copy of the display-label mapping."""

    return dict(SUPPORTED_LANGUAGES)


def label_to_code(label: str) -> str | None:
    for code, display in SUPPORTED_LANGUAGES.items():
        if display == label:
            return code
    return None
