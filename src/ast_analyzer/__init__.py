"""Static XOR/decryption-rule extraction from recovered loader source."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .dataflow_tracker import (
    build_rule_report,
    analyze_file,
    analyze_source,
    collect_scopes,
)
from .disassembly_analyzer import (
    analyze_disassembly,
    disassembly_rule_count,
)
from .node_finder import (
    SourceAnalysisError,
    XorFinding,
    find_xor_nodes,
    infer_target,
    parse_source,
)

__all__ = [
    "SourceAnalysisError",
    "XorFinding",
    "analyze_disassembly",
    "analyze_file",
    "analyze_loader",
    "analyze_source",
    "build_rule_report",
    "collect_scopes",
    "disassembly_rule_count",
    "find_xor_nodes",
    "infer_target",
    "parse_source",
]


def analyze_loader(path: str | Path) -> dict[str, Any]:
    """Analyse a loader ``.pyo``/``.pyc`` or an existing ``loader.py``.

    Bytecode is decompiled to text with uncompyle6 first.  If source recovery
    fails or the recovered AST cannot be parsed, xdis disassembly is used as a
    heuristic fallback.  Neither path executes the recovered code.
    """

    loader_path = Path(path)
    if loader_path.suffix.casefold() in {".pyo", ".pyc"}:
        from src.decompiler import decompile_pyo, disassemble_pyo

        decompiled = decompile_pyo(loader_path)
        if decompiled.ok:
            report = analyze_source(decompiled.source or "", str(loader_path))
            report["decompiler"] = decompiled.to_dict()
            if report.get("parse", {}).get("ok"):
                return report
        else:
            report = {
                "schema": "rpa-loader-analyzer/rules-v1",
                "source": str(loader_path),
                "parse": {
                    "ok": False,
                    "mode": "decompile-failed",
                    "warnings": [decompiled.message],
                },
                "decompiler": decompiled.to_dict(),
                "rules": [],
                "findings": [],
                "warnings": [decompiled.message],
                "stats": {"xor_nodes": 0, "rule_count": 0},
            }

        disassembled = disassemble_pyo(loader_path)
        if disassembled.ok:
            asm_report = analyze_disassembly(str(loader_path), disassembled.text or "")
            asm_report["decompiler"] = decompiled.to_dict()
            asm_report["disassembly"] = disassembled.to_dict()
            return asm_report
        report["disassembly"] = disassembled.to_dict()
        report["warnings"] = list(report.get("warnings", [])) + [disassembled.message]
        return report

    return analyze_file(str(loader_path))
