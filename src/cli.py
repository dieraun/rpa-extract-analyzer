"""Command-line interface for rpa-loader-analyzer.

All commands are local and read-only with respect to game files.  JSON reports
go to an explicit output location; resource exports must use an independent
directory outside the game tree.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Sequence

from src import __version__
from src.ast_analyzer import analyze_disassembly, analyze_file, analyze_source
from src.decompiler import decompile_pyo, disassemble_pyo, save_source
from src.exporter import ExportError, export_archive, write_export_report
from src.media_convert import FfmpegOptions, MediaError, convert_webm
from src.rpa_core import (
    RpaError,
    RuleError,
    RuleSet,
    parse_archive,
)
from src.scanner import GameScanError, scan_game

logger = logging.getLogger("rpa_loader_analyzer")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


class CliError(RuntimeError):
    """Raised for user-facing CLI errors."""


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        return os.path.commonpath((str(root), str(candidate))) == str(root)
    except ValueError:
        return False


def _assert_outside(path: str | Path, roots: Sequence[Path], *, what: str) -> Path:
    target = Path(path).expanduser()
    try:
        resolved = target.resolve(strict=False)
    except OSError as exc:
        raise CliError(f"无法解析{what}路径 {target}: {exc}") from exc
    for root in roots:
        try:
            root_resolved = root.resolve(strict=False)
        except OSError:
            continue
        if _is_within(root_resolved, resolved):
            raise CliError(f"{what}不能写入游戏原始目录: {resolved} (游戏根 {root_resolved})")
    return resolved


def _write_json(path: str | Path, payload: Any) -> Path:
    target = Path(path).expanduser()
    if target.exists() and target.is_dir():
        raise CliError(f"JSON 输出路径是目录: {target}")
    if target.is_symlink():
        raise CliError(f"拒绝写入符号链接: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
    return target


def _loader_roots(loader: Path) -> list[Path]:
    loader = loader.resolve()
    if loader.parent.name.casefold() == "renpy":
        return [loader.parent.parent]
    return [loader.parent]


def _infer_game_root(path: Path) -> Path:
    """Walk upward to the directory containing ``renpy/`` if present."""

    resolved = path.resolve()
    for parent in (resolved.parent, *resolved.parents):
        if (parent / "renpy").is_dir():
            return parent
    return resolved.parent


def _load_rules(path: str | None) -> RuleSet | None:
    if not path:
        return None
    return RuleSet.from_file(path)


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_scan(args: argparse.Namespace) -> int:
    layout = scan_game(args.game_dir, max_depth=args.max_depth, max_entries=args.max_entries)
    payload = layout.to_dict()
    if args.json_out:
        _assert_outside(args.json_out, [layout.root], what="扫描报告")
        target = _write_json(args.json_out, payload)
        print(f"扫描报告: {target}")
    if layout.loader is None:
        print("警告: 未找到 renpy/loader.pyo")
    else:
        print(f"loader: {layout.loader}")
    print(f"RPA 归档: {len(layout.archives)} 个")
    for archive in layout.archives:
        print(f"  {archive}")
    for warning in layout.warnings:
        print(f"警告: {warning}")
    return EXIT_OK


def cmd_decompile(args: argparse.Namespace) -> int:
    source_path = Path(args.input).expanduser()
    if source_path.suffix.casefold() == ".py":
        raise CliError("输入已是 .py 源码；请使用 analyze 命令")
    output_dir = None
    if args.output_dir:
        output_dir = _assert_outside(
            args.output_dir, _loader_roots(source_path), what="反编译输出"
        )
    result = decompile_pyo(
        source_path,
        output_dir=output_dir,
        output_name=args.output_name,
        overwrite=args.overwrite,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    if args.print_source and result.source:
        print("--- source ---")
        print(result.source)
    return EXIT_OK if result.ok else EXIT_ERROR


def cmd_disassemble(args: argparse.Namespace) -> int:
    source_path = Path(args.input).expanduser()
    if source_path.suffix.casefold() == ".py":
        raise CliError("输入已是 .py 源码；disassemble 只接受 .pyo/.pyc")
    output_dir = None
    if args.output_dir:
        output_dir = _assert_outside(
            args.output_dir, _loader_roots(source_path), what="反汇编输出"
        )
    result = disassemble_pyo(
        source_path,
        output_dir=output_dir,
        output_name=args.output_name,
        overwrite=args.overwrite,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    if args.print_text and result.text:
        print("--- disassembly ---")
        print(result.text)
    return EXIT_OK if result.ok else EXIT_ERROR


def cmd_analyze(args: argparse.Namespace) -> int:
    input_path = Path(args.input).expanduser()
    roots = _loader_roots(input_path)
    source_out = None
    rules_out = None
    asm_out = None
    if args.source_out:
        source_out = _assert_outside(args.source_out, roots, what="源码输出")
    if args.rules_out:
        rules_out = _assert_outside(args.rules_out, roots, what="规则输出")
    if args.asm_out:
        asm_out = _assert_outside(args.asm_out, roots, what="反汇编输出")

    decompiler_summary: dict[str, Any] | None = None
    report: dict[str, Any] | None = None
    primary_ok = False
    if input_path.suffix.casefold() in {".pyo", ".pyc"}:
        decompiled = decompile_pyo(input_path)
        decompiler_summary = decompiled.to_dict()
        if decompiled.ok:
            report = analyze_source(decompiled.source or "", str(input_path))
            report["decompiler"] = decompiler_summary
            primary_ok = bool(report.get("parse", {}).get("ok"))
            if source_out is not None:
                try:
                    saved = save_source(
                        decompiled,
                        source_out,
                        filename=args.source_name,
                        overwrite=args.overwrite,
                    )
                    print(f"反编译源码: {saved}")
                except (OSError, ValueError) as exc:
                    print(f"警告: 源码保存失败: {exc}", file=sys.stderr)
        else:
            report = _decompile_failure_report(input_path, decompiler_summary)

        # Disassembly is an explicit output and/or a fallback when source
        # recovery or AST parsing failed.
        if asm_out is not None or not primary_ok:
            disassembled = disassemble_pyo(
                input_path,
                output_dir=asm_out,
                output_name=args.asm_name,
                overwrite=args.overwrite,
            )
            disassembly_summary = disassembled.to_dict()
            report["disassembly"] = disassembly_summary
            if disassembled.text:
                if disassembled.output_path:
                    print(f"反汇编: {disassembled.output_path}")
                if not primary_ok:
                    asm_report = analyze_disassembly(
                        str(input_path), disassembled.text
                    )
                    asm_report["decompiler"] = decompiler_summary
                    asm_report["disassembly"] = disassembly_summary
                    report = asm_report
            else:
                report.setdefault("warnings", []).append(disassembled.message)
            if disassembled.status != "success":
                print(f"警告: {disassembled.message}", file=sys.stderr)
    else:
        report = analyze_file(str(input_path))
        if source_out is not None:
            print("提示: 输入已是源码，无需再次保存", file=sys.stderr)
        if asm_out is not None:
            print("提示: 输入已是源码，无字节码可反汇编", file=sys.stderr)

    if report is None:
        report = _decompile_failure_report(
            input_path,
            decompiler_summary or {"status": "failed", "message": "未知错误"},
        )
    if rules_out is not None:
        target = _write_json(rules_out, report)
        print(f"规则报告: {target}")
    _print_analysis_summary(report)
    return EXIT_OK if report.get("parse", {}).get("ok") else EXIT_ERROR


def _decompile_failure_report(
    input_path: Path, decompiler_summary: dict[str, Any]
) -> dict[str, Any]:
    message = str(decompiler_summary.get("message") or "反编译失败")
    return {
        "schema": "rpa-loader-analyzer/rules-v1",
        "source": str(input_path),
        "parse": {
            "ok": False,
            "mode": "decompile-failed",
            "warnings": [message],
        },
        "decompiler": decompiler_summary,
        "rules": [],
        "findings": [],
        "warnings": [message],
        "stats": {"xor_nodes": 0, "rule_count": 0},
    }


def _print_analysis_summary(report: dict[str, Any]) -> None:
    parse = report.get("parse", {})
    analysis = report.get("analysis", "ast")
    print(f"分析方式: {analysis}")
    print(f"解析: {'成功' if parse.get('ok') else '失败'} ({parse.get('mode')})")
    print(f"XOR 节点: {report.get('stats', {}).get('xor_nodes', 0)}")
    print(f"规则: {report.get('stats', {}).get('rule_count', 0)}")
    for rule in report.get("rules", []):
        target = rule.get("target", "unknown")
        key = rule.get("key_hex") or rule.get("key_bytes_hex") or "?"
        print(
            f"  {rule.get('id')} target={target} key={key} "
            f"confidence={rule.get('confidence')} line={rule.get('line')}"
        )
    for warning in report.get("warnings", []):
        print(f"警告: {warning}")


def cmd_list(args: argparse.Namespace) -> int:
    archive_path = Path(args.archive).expanduser()
    if args.json_out:
        _assert_outside(args.json_out, [_infer_game_root(archive_path)], what="归档报告")
    rules = _load_rules(args.rules)
    archive = parse_archive(archive_path, rules=rules)
    if args.json_out:
        target = _write_json(args.json_out, archive.to_dict())
        print(f"归档报告: {target}")
    print(
        f"{archive.version} entries={len(archive.entries)} "
        f"key={archive.selection} size={archive.file_size}"
    )
    for entry in archive.entries:
        print(f"{entry.length:>12}  {entry.name}")
    for warning in archive.warnings:
        print(f"警告: {warning}")
    return EXIT_OK


def cmd_export(args: argparse.Namespace) -> int:
    archive_path = Path(args.archive).expanduser()
    rules = _load_rules(args.rules)
    game_root = (
        Path(args.game_root).expanduser()
        if args.game_root
        else _infer_game_root(archive_path)
    )
    _assert_outside(args.output, [game_root], what="资源输出")
    report_path = None
    if args.report:
        report_path = _assert_outside(args.report, [game_root], what="导出报告")
    archive = parse_archive(archive_path, rules=rules)
    result = export_archive(
        archive,
        args.output,
        overwrite=args.overwrite,
        continue_on_error=not args.stop_on_error,
        protected_roots=[game_root],
    )
    if report_path is not None:
        write_export_report(result, report_path)
        print(f"导出报告: {report_path}")
    print(
        f"导出: exported={len(result.exported)} skipped={len(result.skipped)} "
        f"failed={len(result.failed)} bytes={result.bytes_written}"
    )
    for failure in result.failed:
        print(f"失败: {failure.name}: {failure.message}", file=sys.stderr)
    return EXIT_OK if result.ok else EXIT_ERROR


def _unique_subdir(base: Path, stem: str, used: set[str]) -> str:
    candidate = stem or "archive"
    counter = 2
    while candidate.casefold() in used:
        candidate = f"{stem}_{counter}"
        counter += 1
    used.add(candidate.casefold())
    return candidate


def cmd_export_all(args: argparse.Namespace) -> int:
    layout = scan_game(args.game_dir)
    if not layout.archives:
        raise CliError("游戏目录中没有找到 .rpa 归档")
    _assert_outside(args.output, [layout.root], what="资源输出")
    if args.report:
        _assert_outside(args.report, [layout.root], what="导出报告")
    rules = _load_rules(args.rules)
    output_root = Path(args.output).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    if args.report:
        report_resolved = Path(args.report).expanduser().resolve(strict=False)
        if _is_within(output_root, report_resolved):
            raise CliError("汇总报告不能写入资源输出目录，避免污染资源树")

    aggregate: dict[str, Any] = {
        "game_root": str(layout.root),
        "output_dir": str(output_root),
        "loader": str(layout.loader) if layout.loader else None,
        "scan_warnings": list(layout.warnings),
        "archives": [],
        "ok": True,
    }
    used: set[str] = set()
    for archive_path in layout.archives:
        item: dict[str, Any] = {"archive": str(archive_path), "status": "failed"}
        try:
            archive = parse_archive(archive_path, rules=rules)
            subdir = _unique_subdir(output_root, archive_path.stem, used)
            result = export_archive(
                archive,
                output_root / subdir,
                overwrite=args.overwrite,
                continue_on_error=not args.stop_on_error,
                protected_roots=[layout.root],
            )
            item.update(
                {
                    "status": "ok" if result.ok else "partial",
                    "version": archive.version,
                    "entry_count": len(archive.entries),
                    "selection": archive.selection,
                    "output_dir": str(result.output_dir),
                    "export": result.to_dict(),
                }
            )
            if not result.ok:
                aggregate["ok"] = False
            print(
                f"{archive_path.name}: exported={len(result.exported)} "
                f"failed={len(result.failed)}"
            )
        except (RpaError, ExportError, RuleError, OSError, ValueError) as exc:
            aggregate["ok"] = False
            item["error"] = f"{type(exc).__name__}: {exc}"
            print(f"{archive_path.name}: 失败: {exc}", file=sys.stderr)
        aggregate["archives"].append(item)

    if args.report:
        _write_json(args.report, aggregate)
        print(f"汇总报告: {args.report}")
    print(
        f"总计: archives={len(aggregate['archives'])} "
        f"ok={sum(1 for item in aggregate['archives'] if item['status'] == 'ok')}"
    )
    return EXIT_OK if aggregate["ok"] else EXIT_ERROR


def cmd_convert(args: argparse.Namespace) -> int:
    options = FfmpegOptions(
        ffmpeg_path=args.ffmpeg,
        ffprobe_path=args.ffprobe,
        hardware=args.hardware,
        hardware_codec=args.codec,
        crf=args.crf,
        preset=args.preset,
        audio_bitrate=args.audio_bitrate,
        timeout=args.timeout,
        overwrite=args.overwrite,
        vaapi_device=args.vaapi_device,
        force_reencode=args.reencode,
    )
    result = convert_webm(args.input, args.output, options=options)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return EXIT_OK if result.success else EXIT_ERROR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rpa-loader-analyzer",
        description=(
            "本地 Ren'Py loader.pyo / RPA 研究工具。只读分析，不执行反编译源码，"
            "不使用 pickle.load，不上传任何文件。"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
        help="日志级别（默认 INFO）",
    )
    parser.add_argument("--debug", action="store_true", help="出错时输出完整堆栈")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="扫描游戏目录中的 loader.pyo 与 RPA")
    scan.add_argument("game_dir")
    scan.add_argument("--json-out")
    scan.add_argument("--max-depth", type=int, default=32)
    scan.add_argument("--max-entries", type=int, default=200_000)
    scan.set_defaults(func=cmd_scan)

    decompile = subparsers.add_parser("decompile", help="用 uncompyle6 反编译 .pyo")
    decompile.add_argument("input")
    decompile.add_argument("--output-dir")
    decompile.add_argument("--output-name", default="loader_decompiled.py")
    decompile.add_argument("--overwrite", action="store_true")
    decompile.add_argument("--print-source", action="store_true")
    decompile.set_defaults(func=cmd_decompile)

    disassemble = subparsers.add_parser(
        "disassemble", help="用 xdis 静态反汇编 .pyo/.pyc（不执行字节码）"
    )
    disassemble.add_argument("input")
    disassemble.add_argument("--output-dir")
    disassemble.add_argument("--output-name", default="loader_disassembly.txt")
    disassemble.add_argument("--overwrite", action="store_true")
    disassemble.add_argument("--print-text", action="store_true")
    disassemble.set_defaults(func=cmd_disassemble)

    analyze = subparsers.add_parser(
        "analyze", help="静态提取 XOR 密钥规则（AST 优先，失败时反汇编回退）"
    )
    analyze.add_argument("input")
    analyze.add_argument("--source-out", help="保存反编译源码的目录")
    analyze.add_argument("--source-name", default="loader_decompiled.py")
    analyze.add_argument("--asm-out", help="保存 xdis 反汇编文本的目录")
    analyze.add_argument("--asm-name", default="loader_disassembly.txt")
    analyze.add_argument("--rules-out", help="规则 JSON 输出路径")
    analyze.add_argument("--overwrite", action="store_true")
    analyze.set_defaults(func=cmd_analyze)

    listing = subparsers.add_parser("list", help="只读列出 RPA 索引")
    listing.add_argument("archive")
    listing.add_argument("--rules")
    listing.add_argument("--json-out")
    listing.set_defaults(func=cmd_list)

    export = subparsers.add_parser("export", help="导出单个 RPA 到独立目录")
    export.add_argument("archive")
    export.add_argument("--output", required=True)
    export.add_argument("--game-root", help="受保护的游戏根目录（默认归档所在目录）")
    export.add_argument("--rules")
    export.add_argument("--report")
    export.add_argument("--overwrite", action="store_true")
    export.add_argument("--stop-on-error", action="store_true")
    export.set_defaults(func=cmd_export)

    export_all = subparsers.add_parser("export-all", help="扫描并导出游戏内全部 RPA")
    export_all.add_argument("game_dir")
    export_all.add_argument("--output", required=True)
    export_all.add_argument("--rules")
    export_all.add_argument("--report")
    export_all.add_argument("--overwrite", action="store_true")
    export_all.add_argument("--stop-on-error", action="store_true")
    export_all.set_defaults(func=cmd_export_all)

    convert = subparsers.add_parser("convert", help="WebM -> MP4（先流复制，失败后重编码）")
    convert.add_argument("input")
    convert.add_argument("output")
    convert.add_argument("--hardware", choices=("none", "nvenc", "qsv", "vaapi"), default="none")
    convert.add_argument("--codec", choices=("h264", "hevc"), default="h264")
    convert.add_argument("--crf", type=int, default=23)
    convert.add_argument("--preset", default="medium")
    convert.add_argument("--audio-bitrate", default="192k")
    convert.add_argument("--timeout", type=float, default=3600.0)
    convert.add_argument("--overwrite", action="store_true")
    convert.add_argument(
        "--reencode",
        action="store_true",
        help="跳过流复制，强制重编码（例如确保 MP4 使用 H.264/AAC）",
    )
    convert.add_argument("--ffmpeg", default="ffmpeg")
    convert.add_argument("--ffprobe", default="ffprobe")
    convert.add_argument("--vaapi-device", default="/dev/dri/renderD128")
    convert.set_defaults(func=cmd_convert)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns a process exit code."""

    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
    try:
        return int(args.func(args))
    except (
        CliError,
        GameScanError,
        RpaError,
        RuleError,
        ExportError,
        MediaError,
        OSError,
        ValueError,
    ) as exc:
        logger.error("%s", exc)
        # Print directly as well: logging may have been configured before a
        # caller redirected stderr (notably in tests and embedding contexts).
        print(f"错误: {exc}", file=sys.stderr)
        if args.debug:
            traceback.print_exc()
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
