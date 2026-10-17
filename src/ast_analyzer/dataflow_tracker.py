"""Simple constant/data-flow tracking that turns XOR nodes into RPA rules.

The tracker is intentionally not a full decompiler.  It resolves literal
assignments, aliases and a small set of pure builtin calls (``int``, ``ord``,
``chr``, ``bytes``) from an AST.  It never evaluates source with ``eval`` or
``exec`` and it never imports recovered modules.
"""

from __future__ import annotations

import ast
import operator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from .node_finder import (
    SourceAnalysisError,
    XorFinding,
    _safe_unparse,
    find_xor_nodes,
    infer_target,
    parse_source,
)

MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_RESOLVE_DEPTH = 30
MAX_INT_BITS = 4096
MAX_SHIFT = 256

_ALLOWED_BINOPS: dict[type[ast.operator], Any] = {
    ast.BitXor: operator.xor,
    ast.BitAnd: operator.and_,
    ast.BitOr: operator.or_,
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}


@dataclass(frozen=True, slots=True)
class Resolution:
    """A constant value plus the variable chain that produced it."""

    value: int | bytes | str | float | tuple[Any, ...]
    sources: tuple[str, ...] = ()
    ambiguous: bool = False


@dataclass(frozen=True, slots=True)
class Assignment:
    """One statically recorded assignment inside a lexical scope."""

    name: str
    value: ast.AST | None
    line: int
    col: int
    augmented: bool = False


@dataclass(slots=True)
class ScopeInfo:
    """Lexical scope and its assignments, in source order."""

    key: tuple[tuple[str, int], ...]
    name: str
    line: int
    parent: "ScopeInfo | None" = None
    assignments: dict[str, list[Assignment]] = field(default_factory=dict)


def _module_scope() -> ScopeInfo:
    return ScopeInfo(key=(), name="<module>", line=0)


class _ScopeCollector(ast.NodeVisitor):
    """Collect assignments per module/function/class/lambda scope."""

    def __init__(self) -> None:
        self.module = _module_scope()
        self.scopes: dict[tuple[tuple[str, int], ...], ScopeInfo] = {
            self.module.key: self.module
        }
        self.current = self.module
        self._stack: list[ast.AST] = []
        self.warnings: list[str] = []

    def generic_visit(self, node: ast.AST) -> None:
        self._stack.append(node)
        try:
            super().generic_visit(node)
        finally:
            self._stack.pop()

    def _push_scope(self, name: str, line: int) -> ScopeInfo:
        key = tuple(
            list(self.current.key) + [(name, line)]
        )
        scope = ScopeInfo(key=key, name=name, line=line, parent=self.current)
        self.scopes[key] = scope
        self.current = scope
        return scope

    def _pop_scope(self, previous: ScopeInfo) -> None:
        self.current = previous

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._stack.append(node)
        try:
            for decorator in node.decorator_list:
                self.visit(decorator)
            for default in list(node.args.defaults) + [
                item for item in node.args.kw_defaults if item is not None
            ]:
                self.visit(default)
            previous = self.current
            self._push_scope(node.name, node.lineno)
            try:
                for statement in node.body:
                    self.visit(statement)
            finally:
                self._pop_scope(previous)
        finally:
            self._stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)  # type: ignore[arg-type]

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._stack.append(node)
        try:
            previous = self.current
            self._push_scope("<lambda>", node.lineno)
            try:
                self.visit(node.body)
            finally:
                self._pop_scope(previous)
        finally:
            self._stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._stack.append(node)
        try:
            for decorator in node.decorator_list:
                self.visit(decorator)
            for base in node.bases:
                self.visit(base)
            previous = self.current
            self._push_scope(node.name, node.lineno)
            try:
                for statement in node.body:
                    self.visit(statement)
            finally:
                self._pop_scope(previous)
        finally:
            self._stack.pop()

    def _record(self, name: str, value: ast.AST | None, node: ast.AST, *, augmented: bool = False) -> None:
        if not name:
            return
        assignment = Assignment(
            name=name,
            value=value,
            line=getattr(node, "lineno", 0),
            col=getattr(node, "col_offset", 0),
            augmented=augmented,
        )
        self.current.assignments.setdefault(name, []).append(assignment)

    def _record_target(self, target: ast.AST, value: ast.AST | None, node: ast.AST) -> None:
        for name, bound_value in _iter_target_bindings(target, value):
            self._record(name, bound_value, node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._record_target(target, node.value, node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._record_target(node.target, node.value, node)
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._record_target(node.target, node.value, node)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if isinstance(node.target, ast.Name):
            self._record(node.target.id, None, node, augmented=True)
            self.warnings.append(
                f"第 {node.lineno} 行: 增量赋值 {node.target.id} 未参与常量折叠"
            )
        self.generic_visit(node)


def _iter_target_bindings(
    target: ast.AST, value: ast.AST | None
) -> Iterable[tuple[str, ast.AST | None]]:
    """Yield ``(name, bound_value)`` for simple and tuple assignments."""

    if isinstance(target, ast.Name):
        yield target.id, value
        return
    if isinstance(target, (ast.Tuple, ast.List)):
        value_items: list[ast.AST] | None = None
        if isinstance(value, (ast.Tuple, ast.List)) and len(value.elts) == len(target.elts):
            value_items = list(value.elts)
        for index, element in enumerate(target.elts):
            bound = value_items[index] if value_items is not None else None
            if isinstance(element, ast.Starred):
                continue
            yield from _iter_target_bindings(element, bound)
        return
    # Attribute/subscript targets are deliberately ignored: they are not
    # reliable static key sources and are often mutations of game state.


class ConstantResolver:
    """Resolve constants from an AST without executing it."""

    def __init__(self, scopes: dict[tuple[tuple[str, int], ...], ScopeInfo]) -> None:
        self.scopes = scopes
        self.warnings: list[str] = []

    def resolve_expr(
        self,
        node: ast.AST,
        scope_key: tuple[tuple[str, int], ...],
        at_line: int,
        *,
        seen: frozenset[tuple[Any, ...]] = frozenset(),
        depth: int = 0,
    ) -> Resolution | None:
        if depth > MAX_RESOLVE_DEPTH:
            return None
        scope = self.scopes.get(scope_key)
        if scope is None:
            return None

        if isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, bool):
                return Resolution(int(value), ("literal:bool",))
            if isinstance(value, (int, bytes, str, float)):
                return Resolution(value, ("literal",))
            return None

        if isinstance(node, ast.Name):
            return self._resolve_name(node.id, scope, at_line, seen=seen, depth=depth + 1)

        if isinstance(node, ast.UnaryOp):
            operand = self.resolve_expr(
                node.operand, scope_key, at_line, seen=seen, depth=depth + 1
            )
            if operand is None or not isinstance(operand.value, int):
                return None
            try:
                if isinstance(node.op, ast.USub):
                    value = -operand.value
                elif isinstance(node.op, ast.UAdd):
                    value = +operand.value
                elif isinstance(node.op, ast.Invert):
                    value = ~operand.value
                else:
                    return None
            except (ArithmeticError, ValueError):
                return None
            if not _small_int(value):
                return None
            return Resolution(value, operand.sources, operand.ambiguous)

        if isinstance(node, ast.BinOp):
            left = self.resolve_expr(
                node.left, scope_key, at_line, seen=seen, depth=depth + 1
            )
            right = self.resolve_expr(
                node.right, scope_key, at_line, seen=seen, depth=depth + 1
            )
            if left is None or right is None:
                return None
            return self._apply_binop(node.op, left, right)

        if isinstance(node, ast.Call):
            return self._resolve_call(node, scope_key, at_line, seen=seen, depth=depth + 1)

        if isinstance(node, ast.Subscript):
            container = self.resolve_expr(
                node.value, scope_key, at_line, seen=seen, depth=depth + 1
            )
            index = self.resolve_expr(
                node.slice, scope_key, at_line, seen=seen, depth=depth + 1
            )
            if (
                container is None
                or index is None
                or not isinstance(index.value, int)
                or not isinstance(container.value, (str, bytes))
            ):
                return None
            try:
                value = container.value[index.value]
            except (IndexError, TypeError):
                return None
            return Resolution(value, container.sources + index.sources, container.ambiguous)

        if isinstance(node, (ast.Tuple, ast.List)):
            values: list[Any] = []
            sources: list[str] = []
            ambiguous = False
            for element in node.elts:
                resolved = self.resolve_expr(
                    element, scope_key, at_line, seen=seen, depth=depth + 1
                )
                if resolved is None:
                    return None
                values.append(resolved.value)
                sources.extend(resolved.sources)
                ambiguous = ambiguous or resolved.ambiguous
            return Resolution(tuple(values), tuple(sources), ambiguous)

        return None

    def _apply_binop(
        self, op: ast.operator, left: Resolution, right: Resolution
    ) -> Resolution | None:
        function = _ALLOWED_BINOPS.get(type(op))
        if function is None:
            return None
        left_value, right_value = left.value, right.value
        if isinstance(left_value, int) and isinstance(right_value, int):
            if isinstance(op, (ast.LShift, ast.RShift)):
                if right_value < 0 or right_value > MAX_SHIFT:
                    return None
            if isinstance(op, ast.Mult) and (
                left_value.bit_length() > MAX_INT_BITS
                or right_value.bit_length() > MAX_INT_BITS
            ):
                return None
            try:
                value = function(left_value, right_value)
            except (ArithmeticError, ValueError):
                return None
            if not _small_int(value):
                return None
            return Resolution(
                value,
                left.sources + right.sources,
                left.ambiguous or right.ambiguous,
            )
        if (
            isinstance(op, ast.BitXor)
            and isinstance(left_value, bytes)
            and isinstance(right_value, bytes)
            and len(left_value) == len(right_value)
        ):
            value = bytes(a ^ b for a, b in zip(left_value, right_value))
            return Resolution(
                value,
                left.sources + right.sources,
                left.ambiguous or right.ambiguous,
            )
        return None

    def _resolve_call(
        self,
        node: ast.Call,
        scope_key: tuple[tuple[str, int], ...],
        at_line: int,
        *,
        seen: frozenset[tuple[Any, ...]],
        depth: int,
    ) -> Resolution | None:
        if node.keywords:
            return None
        function = node.func
        name: str | None = None
        if isinstance(function, ast.Name):
            name = function.id
        elif isinstance(function, ast.Attribute):
            base = function.value
            if isinstance(base, ast.Name) and base.id in {"_codecs", "codecs"}:
                name = f"{base.id}.{function.attr}"
            else:
                name = function.attr
        if name is None:
            return None

        arguments = [
            self.resolve_expr(
                argument, scope_key, at_line, seen=seen, depth=depth + 1
            )
            for argument in node.args
        ]
        if any(argument is None for argument in arguments):
            return None
        values = [argument.value for argument in arguments if argument is not None]  # type: ignore[union-attr]
        sources: list[str] = []
        ambiguous = False
        for argument in arguments:
            if argument is not None:
                sources.extend(argument.sources)
                ambiguous = ambiguous or argument.ambiguous

        if name in {"int", "long"}:
            if not 1 <= len(values) <= 2:
                return None
            value, base = values[0], values[1] if len(values) == 2 else 10
            if not isinstance(base, int) or not 2 <= base <= 36:
                return None
            if isinstance(value, bool):
                value = int(value)
            if isinstance(value, int):
                if not _small_int(value):
                    return None
                return Resolution(value, tuple(sources) + ("call:int",), ambiguous)
            if isinstance(value, str) and len(value) <= 128:
                try:
                    parsed = int(value, base)
                except ValueError:
                    return None
                if not _small_int(parsed):
                    return None
                return Resolution(parsed, tuple(sources) + ("call:int",), ambiguous)
            return None

        if name == "ord":
            if len(values) != 1:
                return None
            value = values[0]
            if isinstance(value, str) and len(value) == 1:
                return Resolution(ord(value), tuple(sources) + ("call:ord",), ambiguous)
            if isinstance(value, bytes) and len(value) == 1:
                return Resolution(value[0], tuple(sources) + ("call:ord",), ambiguous)
            return None

        if name == "chr":
            if len(values) != 1 or not isinstance(values[0], int):
                return None
            try:
                return Resolution(
                    chr(values[0]), tuple(sources) + ("call:chr",), ambiguous
                )
            except ValueError:
                return None

        if name in {"bytes", "bytearray"}:
            if len(values) == 1 and isinstance(values[0], (list, tuple)):
                items = values[0]
                if all(isinstance(item, int) and 0 <= item <= 255 for item in items):
                    return Resolution(
                        bytes(items), tuple(sources) + (f"call:{name}",), ambiguous
                    )
            if (
                len(values) == 2
                and isinstance(values[0], str)
                and isinstance(values[1], str)
                and values[1] in {"utf-8", "latin-1", "latin1"}
            ):
                return Resolution(
                    values[0].encode(values[1]),
                    tuple(sources) + (f"call:{name}",),
                    ambiguous,
                )
            return None

        if name in {"_codecs.encode", "codecs.encode"} and len(values) == 2:
            if (
                isinstance(values[0], str)
                and isinstance(values[1], str)
                and values[1] in {"utf-8", "latin-1", "latin1"}
            ):
                return Resolution(
                    values[0].encode(values[1]),
                    tuple(sources) + ("call:codecs.encode",),
                    ambiguous,
                )
        return None

    def _resolve_name(
        self,
        name: str,
        scope: ScopeInfo,
        at_line: int,
        *,
        seen: frozenset[tuple[Any, ...]],
        depth: int,
    ) -> Resolution | None:
        current: ScopeInfo | None = scope
        while current is not None:
            candidates = current.assignments.get(name, [])
            if candidates:
                before = [
                    assignment
                    for assignment in candidates
                    if (assignment.line, assignment.col) <= (at_line, 1 << 30)
                ]
                chosen = before[-1] if before else candidates[0]
                marker = (current.key, name, chosen.line, chosen.col)
                if marker in seen:
                    return None
                if chosen.augmented or chosen.value is None:
                    return None
                resolved = self.resolve_expr(
                    chosen.value,
                    current.key,
                    chosen.line,
                    seen=seen | {marker},
                    depth=depth + 1,
                )
                if resolved is None:
                    return None
                ambiguous = resolved.ambiguous
                # Two assignments with different constant values mean control
                # flow matters.  Report it instead of silently pretending the
                # nearest textual assignment is always executed.
                if len(before) > 1:
                    previous = before[-2]
                    if previous.value is not None and previous.line != chosen.line:
                        previous_resolved = self.resolve_expr(
                            previous.value,
                            current.key,
                            previous.line,
                            seen=seen | {marker},
                            depth=depth + 1,
                        )
                        if (
                            previous_resolved is not None
                            and previous_resolved.value != resolved.value
                        ):
                            ambiguous = True
                            self.warnings.append(
                                f"变量 {name} 存在多个可能赋值（第 {previous.line} 行与第 {chosen.line} 行）"
                            )
                return Resolution(
                    resolved.value,
                    (name,) + resolved.sources,
                    ambiguous,
                )
            current = current.parent
        return None


def _small_int(value: int) -> bool:
    """Reject enormous folded integers that could exhaust memory."""

    return abs(value).bit_length() <= MAX_INT_BITS


def collect_scopes(tree: ast.AST) -> tuple[ScopeInfo, dict[tuple[tuple[str, int], ...], ScopeInfo], tuple[str, ...]]:
    """Collect lexical scopes and assignments from *tree*."""

    collector = _ScopeCollector()
    collector.visit(tree)
    return collector.module, collector.scopes, tuple(dict.fromkeys(collector.warnings))


@dataclass(slots=True)
class _FlattenedXor:
    constants: list[Resolution] = field(default_factory=list)
    unknown: list[ast.AST] = field(default_factory=list)


def _flatten_xor(
    node: ast.AST,
    resolver: ConstantResolver,
    scope_key: tuple[tuple[str, int], ...],
    at_line: int,
    depth: int = 0,
) -> _FlattenedXor:
    if depth > MAX_RESOLVE_DEPTH:
        return _FlattenedXor(unknown=[node])
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitXor):
        left = _flatten_xor(node.left, resolver, scope_key, at_line, depth + 1)
        right = _flatten_xor(node.right, resolver, scope_key, at_line, depth + 1)
        return _FlattenedXor(
            constants=left.constants + right.constants,
            unknown=left.unknown + right.unknown,
        )
    resolved = resolver.resolve_expr(node, scope_key, at_line)
    if resolved is not None and isinstance(resolved.value, (int, bytes)):
        return _FlattenedXor(constants=[resolved])
    return _FlattenedXor(unknown=[node])


def _compose_key(constants: list[Resolution]) -> Resolution | None:
    if not constants:
        return None
    values = [item.value for item in constants]
    if all(isinstance(value, int) for value in values):
        result = 0
        for value in values:
            result ^= int(value)
        if not _small_int(result):
            return None
    elif all(isinstance(value, bytes) for value in values):
        lengths = {len(value) for value in values if isinstance(value, bytes)}
        if len(lengths) != 1:
            return None
        result_bytes = bytearray(next(iter(lengths)))
        for value in values:
            assert isinstance(value, bytes)
            for index, byte in enumerate(value):
                result_bytes[index] ^= byte
        result = bytes(result_bytes)
    else:
        return None
    sources: list[str] = []
    ambiguous = False
    for item in constants:
        sources.extend(item.sources)
        ambiguous = ambiguous or item.ambiguous
    return Resolution(result, tuple(dict.fromkeys(sources)), ambiguous)


def _key_identity(value: int | bytes) -> tuple[str, Any]:
    if isinstance(value, bytes):
        return "bytes", value
    return "int", value


def _key_fields(value: int | bytes) -> dict[str, Any]:
    if isinstance(value, bytes):
        return {
            "key_int": None,
            "key_hex": None,
            "key_bytes_hex": value.hex(),
            "xor_key": "0x" + value.hex(),
        }
    return {
        "key_int": value,
        "key_hex": f"0x{value:X}",
        "key_bytes_hex": None,
        "xor_key": value,
    }


def _build_rule_candidates(
    findings: list[XorFinding], resolver: ConstantResolver
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    candidates: list[dict[str, Any]] = []
    for finding in findings:
        if finding.nested_in_xor:
            # Only the top of an XOR chain is considered; its flattened key
            # already includes all constant operands.
            continue
        flattened = _flatten_xor(finding.node, resolver, finding.scope_key, finding.line)
        key = _compose_key(flattened.constants)
        if key is None:
            continue
        if len(flattened.unknown) != 1:
            if flattened.unknown:
                warnings.append(
                    f"第 {finding.line} 行: XOR 表达式包含 {len(flattened.unknown)} 个未知操作数，未生成可靠规则"
                )
            continue
        unknown_names = [_safe_unparse(item) for item in flattened.unknown]
        names = list(finding.assignment_targets) + unknown_names
        target = infer_target(names)
        if target == "key":
            # This is key derivation, not a decryption rule.
            continue
        confidence = 0.55
        if target != "unknown":
            confidence += 0.2
        if key.sources:
            confidence += 0.1
        if isinstance(key.value, bytes):
            confidence += 0.03
        confidence = min(0.95, confidence)
        evidence = {
            "line": finding.line,
            "expression": finding.expression,
            "assigned_to": list(finding.assignment_targets),
            "unknown_operands": unknown_names,
            "key_sources": list(key.sources),
            "scope": finding.scope_name,
            "enclosing_call": finding.enclosing_call,
            "ambiguous_assignment": key.ambiguous,
        }
        candidate: dict[str, Any] = {
            "target": target,
            "confidence": round(confidence, 2),
            "line": finding.line,
            "expression": finding.expression,
            "scope": finding.scope_name,
            **_key_fields(key.value),
            "occurrences": [evidence],
        }
        if key.ambiguous:
            candidate["confidence"] = round(max(0.3, confidence - 0.2), 2)
        candidates.append(candidate)
    return _merge_rule_candidates(candidates), warnings


def _merge_rule_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge the same key/target seen on multiple source lines."""

    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    order: list[tuple[Any, ...]] = []
    for candidate in candidates:
        identity = (
            candidate["target"],
            _key_identity(candidate["key_int"] if candidate["key_int"] is not None else bytes.fromhex(candidate["key_bytes_hex"])),
        )
        if identity not in merged:
            merged[identity] = dict(candidate)
            order.append(identity)
        else:
            existing = merged[identity]
            existing["occurrences"].extend(candidate["occurrences"])
            existing["confidence"] = max(existing["confidence"], candidate["confidence"])
            if candidate["line"] < existing["line"]:
                existing["line"] = candidate["line"]
                existing["expression"] = candidate["expression"]
    rules: list[dict[str, Any]] = []
    for index, identity in enumerate(order, start=1):
        rule = merged[identity]
        rule["id"] = f"xor-{index:04d}"
        rule["occurrence_count"] = len(rule["occurrences"])
        rules.append(rule)
    return rules


def build_rule_report(
    source: str,
    source_name: str,
    *,
    tree: ast.AST | None = None,
    parse_mode: str = "python3",
    parse_warnings: Iterable[str] = (),
) -> dict[str, Any]:
    """Build a standard, JSON-serialisable XOR rule report.

    The report contains both merged ``rules`` (the machine-readable result)
    and raw ``findings`` for auditability.  An empty rule list is a valid
    research result, not an exception.
    """

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report: dict[str, Any] = {
        "schema": "rpa-loader-analyzer/rules-v1",
        "generated_at": generated_at,
        "source": source_name,
        "parse": {
            "ok": False,
            "mode": parse_mode,
            "warnings": list(parse_warnings),
        },
        "rules": [],
        "findings": [],
        "warnings": [],
        "stats": {"xor_nodes": 0, "rule_count": 0},
    }

    try:
        if len(source.encode("utf-8", "replace")) > MAX_SOURCE_BYTES:
            raise SourceAnalysisError(
                f"源码超过 {MAX_SOURCE_BYTES // (1024 * 1024)} MiB 安全上限"
            )
        if tree is None:
            tree, parse_mode, parse_warnings = parse_source(source)
            report["parse"]["mode"] = parse_mode
            report["parse"]["warnings"] = list(parse_warnings)
        findings = find_xor_nodes(tree)
        _, scopes, scope_warnings = collect_scopes(tree)
        resolver = ConstantResolver(scopes)
        rules, rule_warnings = _build_rule_candidates(findings, resolver)
        warnings = list(dict.fromkeys(
            list(parse_warnings) + list(scope_warnings) + resolver.warnings + rule_warnings
        ))
        report.update(
            {
                "rules": rules,
                "findings": [finding.to_dict() for finding in findings],
                "warnings": warnings,
                "stats": {
                    "xor_nodes": len(findings),
                    "rule_count": len(rules),
                },
            }
        )
        report["parse"]["ok"] = True
    except SourceAnalysisError as exc:
        report["parse"]["ok"] = False
        report["warnings"].append(str(exc))
    except (RecursionError, MemoryError) as exc:
        report["parse"]["ok"] = False
        report["warnings"].append(f"AST 分析触发资源上限: {type(exc).__name__}")
    except Exception as exc:  # noqa: BLE001 - keep CLI alive on malformed code
        report["parse"]["ok"] = False
        report["warnings"].append(f"AST 分析异常: {type(exc).__name__}: {exc}")
    return report


def analyze_source(source: str, source_name: str = "<memory>") -> dict[str, Any]:
    """Analyse source text and return the standard rule report."""

    return build_rule_report(source, source_name)


def analyze_file(path: str, *, encoding: str = "utf-8") -> dict[str, Any]:
    """Read a recovered source file and build a rule report."""

    from pathlib import Path

    source_path = Path(path)
    try:
        source = source_path.read_text(encoding=encoding)
    except OSError as exc:
        return {
            "schema": "rpa-loader-analyzer/rules-v1",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": str(source_path),
            "parse": {"ok": False, "mode": "unknown", "warnings": [str(exc)]},
            "rules": [],
            "findings": [],
            "warnings": [f"无法读取源码: {exc}"],
            "stats": {"xor_nodes": 0, "rule_count": 0},
        }
    return analyze_source(source, str(source_path))
