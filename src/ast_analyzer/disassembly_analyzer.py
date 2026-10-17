"""Heuristic XOR-key extraction from xdis disassembly text.

This module is a **fallback** for loaders that uncompyle6 cannot turn back
into source.  Bytecode disassembly is not as precise as AST data flow: it
reports nearby constants and variable names, then assigns a deliberately low
confidence.  The text is parsed only; no opcode is executed.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from .node_finder import infer_target

MAX_DISASSEMBLY_ANALYSIS_BYTES = 32 * 1024 * 1024
MAX_FINDINGS = 5000
MAX_RULES = 64

_CODE_NAME_RE = re.compile(
    r"^#\s*(?:Method Name|Code Name|Code object|Method name)\s*:\s*(?P<name>.+?)\s*$"
)
_INSTR_RE = re.compile(
    r"^\s*(?:(?P<line>\d+):\s+)?(?P<offset>\d+)\s+"
    r"(?P<op>[A-Z][A-Z0-9_]+)(?:\s+(?P<arg>.*?))?\s*$"
)
_PAREN_RE = re.compile(r"\((?P<value>[^()]*)\)\s*$")
_INT_RE = re.compile(r"^[+-]?(?:0[xX][0-9a-fA-F]+|\d+)[lL]?$")


@dataclass(frozen=True, slots=True)
class _Instruction:
    code_name: str
    line: int
    offset: int
    op: str
    arg: str


@dataclass(frozen=True, slots=True)
class DisassemblyFinding:
    code_name: str
    line: int
    offset: int
    opcode: str
    target_hint: str
    candidate_keys: tuple[int | bytes, ...]
    context: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code_name,
            "line": self.line,
            "offset": self.offset,
            "opcode": self.opcode,
            "target_hint": self.target_hint,
            "candidate_keys": [_key_json(key) for key in self.candidate_keys],
            "context": list(self.context),
        }


def _key_json(key: int | bytes) -> int | str:
    if isinstance(key, bytes):
        return "0x" + key.hex()
    return key


def _key_fields(key: int | bytes) -> dict[str, Any]:
    if isinstance(key, bytes):
        return {
            "key_int": None,
            "key_hex": "0x" + key.hex(),
            "key_bytes_hex": key.hex(),
            "xor_key": "0x" + key.hex(),
        }
    return {
        "key_int": key,
        "key_hex": f"0x{key:X}",
        "key_bytes_hex": None,
        "xor_key": key,
    }


def _key_identity(key: int | bytes) -> tuple[str, Any]:
    if isinstance(key, bytes):
        return ("bytes", key)
    return ("int", key)


def _parse_instruction(line: str, code_name: str) -> _Instruction | None:
    match = _INSTR_RE.match(line)
    if not match:
        return None
    return _Instruction(
        code_name=code_name,
        line=int(match.group("line") or 0),
        offset=int(match.group("offset")),
        op=match.group("op"),
        arg=(match.group("arg") or "").strip(),
    )


def _paren_value(argument: str) -> str | None:
    match = _PAREN_RE.search(argument)
    if not match:
        return None
    return match.group("value").strip()


def _parse_constant(argument: str) -> int | bytes | None:
    value = _paren_value(argument)
    if value is None:
        return None
    if _INT_RE.fullmatch(value):
        text = value.rstrip("lL")
        try:
            parsed = int(text, 0)
        except ValueError:
            return None
        if parsed < 0 or parsed > (1 << 64) - 1:
            return None
        return parsed
    if value.startswith(("b'", 'b"')):
        try:
            parsed = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return None
        if isinstance(parsed, bytes) and 1 <= len(parsed) <= 8:
            return parsed
    return None


def _parse_name(argument: str) -> str | None:
    value = _paren_value(argument)
    candidate = value if value is not None else argument.strip()
    if re.fullmatch(r"[A-Za-z_]\w*", candidate):
        return candidate
    return None


def _is_xor(instruction: _Instruction) -> bool:
    if instruction.op in {"BINARY_XOR", "INPLACE_XOR"}:
        return True
    return instruction.op == "BINARY_OP" and "^" in instruction.arg


def _looks_like_pickle_key(key: int | bytes) -> bool:
    if isinstance(key, bytes):
        return True
    return 0 < key <= (1 << 64) - 1


def _merge_rules(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, tuple[str, Any], str], dict[str, Any]] = {}
    order: list[tuple[str, tuple[str, Any], str]] = []
    for candidate in candidates:
        key = (
            candidate["key_int"]
            if candidate["key_int"] is not None
            else bytes.fromhex(candidate["key_bytes_hex"])
        )
        identity = (candidate["target"], _key_identity(key), candidate["code"])
        if identity not in merged:
            merged[identity] = dict(candidate)
            order.append(identity)
        else:
            existing = merged[identity]
            existing["occurrences"].extend(candidate["occurrences"])
            existing["confidence"] = max(existing["confidence"], candidate["confidence"])
    rules: list[dict[str, Any]] = []
    for index, identity in enumerate(order[:MAX_RULES], start=1):
        rule = merged[identity]
        rule["id"] = f"asm-xor-{index:04d}"
        rule["occurrence_count"] = len(rule["occurrences"])
        rules.append(rule)
    return rules


def analyze_disassembly(source_name: str, text: str) -> dict[str, Any]:
    """Extract XOR evidence from xdis classic/xasm-style text.

    Returns the same top-level schema as the AST analyser but marks
    ``analysis`` as ``bytecode-disassembly``.  The output is intentionally
    conservative: a nearby constant is a *candidate*, not a proof.
    """

    report: dict[str, Any] = {
        "schema": "rpa-loader-analyzer/rules-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source_name,
        "analysis": "bytecode-disassembly",
        "parse": {"ok": False, "mode": "disassembly", "warnings": []},
        "rules": [],
        "findings": [],
        "warnings": [],
        "stats": {"xor_nodes": 0, "rule_count": 0, "instructions": 0, "code_objects": 0},
    }
    if not isinstance(text, str) or not text.strip():
        report["warnings"].append("反汇编文本为空")
        return report
    if len(text.encode("utf-8", "replace")) > MAX_DISASSEMBLY_ANALYSIS_BYTES:
        report["warnings"].append("反汇编文本超过分析上限")
        return report

    code_name = "<module>"
    code_names: set[str] = set()
    instructions: list[_Instruction] = []
    for line in text.splitlines():
        header = _CODE_NAME_RE.match(line.strip())
        if header:
            code_name = header.group("name")
            code_names.add(code_name)
            continue
        parsed = _parse_instruction(line, code_name)
        if parsed is not None:
            instructions.append(parsed)
    if not instructions:
        report["warnings"].append("未从反汇编文本中识别到指令")
        return report

    findings: list[DisassemblyFinding] = []
    rule_candidates: list[dict[str, Any]] = []
    warnings: list[str] = []
    recent_constants: list[tuple[int | bytes, str, int]] = []
    name_constants: dict[str, int | bytes] = {}
    recent_loads: list[str] = []
    last_const: int | bytes | None = None
    last_const_index = -1
    current_code = "<module>"
    code_names.add(current_code)

    for index, instruction in enumerate(instructions):
        if instruction.code_name != current_code:
            current_code = instruction.code_name
            recent_constants.clear()
            name_constants.clear()
            recent_loads.clear()
            last_const = None
            last_const_index = -1
        if instruction.op.startswith("LOAD_") and instruction.op == "LOAD_CONST":
            value = _parse_constant(instruction.arg)
            if value is not None:
                recent_constants.append((value, current_code, instruction.offset))
                recent_constants = recent_constants[-8:]
                last_const = value
                last_const_index = index
        elif instruction.op.startswith("STORE_"):
            name = _parse_name(instruction.arg)
            if name is not None and last_const is not None and index - last_const_index <= 2:
                name_constants[name] = last_const
            last_const = None
            last_const_index = -1
        elif instruction.op.startswith("LOAD_"):
            name = _parse_name(instruction.arg)
            if name is not None:
                recent_loads.append(name)
                recent_loads = recent_loads[-6:]
        elif _is_xor(instruction):
            nearby_constants: list[int | bytes] = []
            # Constants immediately around the XOR are the best evidence;
            # broad scanning of every constant would produce too much noise.
            for value, _code, offset in recent_constants[-4:]:
                if instruction.offset - offset <= 12 and _looks_like_pickle_key(value):
                    nearby_constants.append(value)
            for name in recent_loads[-3:]:
                value = name_constants.get(name)
                if value is not None and _looks_like_pickle_key(value):
                    nearby_constants.append(value)

            lookahead_names: list[str] = []
            for future in instructions[index + 1 : index + 4]:
                if future.op.startswith("STORE_"):
                    name = _parse_name(future.arg)
                    if name is not None:
                        lookahead_names.append(name)
                elif future.op in {"RETURN_VALUE", "JUMP_FORWARD", "POP_TOP"}:
                    break
            context_lines = tuple(
                f"{item.offset:>5} {item.op} {item.arg}".strip()
                for item in instructions[max(0, index - 3) : index + 3]
            )
            target = infer_target(lookahead_names + recent_loads[-3:])
            if target == "key":
                continue
            unique_keys: list[int | bytes] = []
            for key in nearby_constants:
                if key not in unique_keys:
                    unique_keys.append(key)
            unique_keys = unique_keys[:3]
            finding = DisassemblyFinding(
                code_name=current_code,
                line=instruction.line,
                offset=instruction.offset,
                opcode=instruction.op,
                target_hint=target,
                candidate_keys=tuple(unique_keys),
                context=context_lines,
            )
            if len(findings) < MAX_FINDINGS:
                findings.append(finding)
            for key in unique_keys:
                confidence = 0.35
                if target != "unknown":
                    confidence += 0.1
                if recent_constants and key == recent_constants[-1][0]:
                    confidence += 0.05
                rule_candidates.append(
                    {
                        "target": target,
                        "confidence": round(min(0.55, confidence), 2),
                        "line": instruction.line,
                        "expression": f"{instruction.op} @ {instruction.offset}",
                        "scope": current_code,
                        "code": current_code,
                        **_key_fields(key),
                        "occurrences": [
                            {
                                "line": instruction.line,
                                "offset": instruction.offset,
                                "opcode": instruction.op,
                                "candidates": [_key_json(item) for item in unique_keys],
                                "target_hint": target,
                                "context": list(context_lines),
                            }
                        ],
                    }
                )
        if instruction.op in {"RETURN_VALUE", "YIELD_VALUE"}:
            recent_loads.clear()
            last_const = None

    rules = _merge_rules(rule_candidates)
    if not findings:
        warnings.append("反汇编中未找到 XOR 常量候选；可能需要人工分析")
    if len(rule_candidates) > MAX_RULES:
        warnings.append(f"规则候选超过 {MAX_RULES} 条，已截断")

    report.update(
        {
            "rules": rules,
            "findings": [finding.to_dict() for finding in findings],
            "warnings": warnings,
            "stats": {
                "xor_nodes": len(findings),
                "rule_count": len(rules),
                "instructions": len(instructions),
                "code_objects": len(code_names),
            },
        }
    )
    report["parse"]["ok"] = True
    return report


def disassembly_rule_count(report: dict[str, Any]) -> int:
    """Small helper for CLI summaries and tests."""

    return len(report.get("rules", ()))
