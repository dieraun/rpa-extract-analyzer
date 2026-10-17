"""Find XOR operations in an AST and describe their surrounding context.

Only :mod:`ast` nodes are inspected.  Recovered source text is never compiled
into a running module, imported, executed or passed to ``eval``.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any, Iterable

from src.decompiler.pyo_decompiler import normalize_python2_source


class SourceAnalysisError(ValueError):
    """Raised when source text cannot be parsed even after Py2 normalisation."""


@dataclass(frozen=True, slots=True)
class XorFinding:
    """One ``BitXor`` node and the best static context we can attach to it."""

    line: int
    col: int
    expression: str
    left_expression: str
    right_expression: str
    left_constant: int | bytes | None
    right_constant: int | bytes | None
    target_hint: str
    assignment_targets: tuple[str, ...]
    enclosing_call: str | None
    scope_key: tuple[tuple[str, int], ...]
    scope_name: str
    nested_in_xor: bool
    node: ast.AST = field(repr=False, compare=False)

    @property
    def best_literal(self) -> int | bytes | None:
        """Return the only literal operand, if exactly one is constant."""

        if self.left_constant is not None and self.right_constant is None:
            return self.left_constant
        if self.right_constant is not None and self.left_constant is None:
            return self.right_constant
        return None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""

        return {
            "line": self.line,
            "column": self.col,
            "expression": self.expression,
            "left": self.left_expression,
            "right": self.right_expression,
            "left_constant": _constant_to_json(self.left_constant),
            "right_constant": _constant_to_json(self.right_constant),
            "target_hint": self.target_hint,
            "assignment_targets": list(self.assignment_targets),
            "enclosing_call": self.enclosing_call,
            "scope": self.scope_name,
            "nested_in_xor": self.nested_in_xor,
        }


def _constant_to_json(value: int | bytes | None) -> int | str | None:
    """Represent numeric and byte constants without binary JSON values."""

    if value is None:
        return None
    if isinstance(value, bytes):
        return "0x" + value.hex()
    return value


def _safe_unparse(node: ast.AST) -> str:
    """Unparse an AST node without ever evaluating it."""

    try:
        return ast.unparse(node)
    except Exception:
        return f"<{type(node).__name__}>"


def _literal_value(node: ast.AST) -> int | bytes | None:
    """Extract an integer or bytes literal, including a unary minus int."""

    if isinstance(node, ast.Constant) and isinstance(node.value, (int, bytes)):
        if isinstance(node.value, bool):
            return int(node.value)
        return node.value
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, int)
        and not isinstance(node.operand.value, bool)
    ):
        return -node.operand.value
    return None


def _target_names(target: ast.AST) -> tuple[str, ...]:
    """Collect simple assignment target names from an ``Assign`` target."""

    names: list[str] = []
    for node in ast.walk(target):
        if isinstance(node, ast.Name):
            names.append(node.id)
    return tuple(names)


def _call_name(node: ast.AST) -> str | None:
    """Return a readable function name for a call node."""

    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        base = _safe_unparse(func.value)
        return f"{base}.{func.attr}"
    return _safe_unparse(func)


_TARGET_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("offset", ("offset", "off", "start", "position", "pos", "addr")),
    ("size", ("size", "length", "dlen", "clen", "count")),
    ("data", ("data", "payload", "content", "body")),
    ("key", ("key", "mask", "xor")),
)


def infer_target(names: Iterable[str]) -> str:
    """Infer whether a value is an offset, size, data or key.

    Matching is token-based so ``raw_offset`` and ``offset_xor_key`` both
    suggest an offset without treating ``keys`` inside an unrelated name as a
    match.
    """

    for name in names:
        tokens = [
            token
            for token in _split_identifier(name)
            if token not in {"raw", "real", "encoded", "encrypted", "value"}
        ]
        for target, patterns in _TARGET_PATTERNS:
            if any(token in patterns for token in tokens):
                return target
    return "unknown"


def _split_identifier(value: str) -> list[str]:
    """Split an expression/identifier into lowercase word-ish tokens."""

    result: list[str] = []
    current: list[str] = []
    for char in value:
        if char.isalnum():
            if char.isupper() and current:
                result.append("".join(current).lower())
                current = [char]
            else:
                current.append(char)
        else:
            if current:
                result.append("".join(current).lower())
                current = []
    if current:
        result.append("".join(current).lower())
    return result


def parse_source(source: str) -> tuple[ast.Module, str, tuple[str, ...]]:
    """Parse *source* as Python 3, falling back to conservative Py2 fixes.

    Returns:
        ``(tree, mode, warnings)`` where mode is ``python3`` or
        ``python2-normalized``.

    Raises:
        SourceAnalysisError: If both parse attempts fail.
    """

    try:
        return ast.parse(source), "python3", ()
    except SyntaxError as first_error:
        normalized, warnings = normalize_python2_source(source)
        try:
            tree = ast.parse(normalized)
        except SyntaxError as second_error:
            raise SourceAnalysisError(
                "无法解析反编译源码；"
                f"Python3 错误: {first_error.msg} (第 {first_error.lineno} 行); "
                f"Python2 规范化后错误: {second_error.msg} (第 {second_error.lineno} 行)"
            ) from second_error
        return tree, "python2-normalized", warnings


class XorNodeFinder(ast.NodeVisitor):
    """Depth-first visitor that records every ``BitXor`` with context."""

    def __init__(self) -> None:
        self.findings: list[XorFinding] = []
        self._stack: list[ast.AST] = []
        self._scope_stack: list[tuple[str, int]] = []

    @property
    def _scope_key(self) -> tuple[tuple[str, int], ...]:
        return tuple(self._scope_stack)

    @property
    def _scope_name(self) -> str:
        if not self._scope_stack:
            return "<module>"
        return ".".join(name for name, _line in self._scope_stack)

    def generic_visit(self, node: ast.AST) -> None:
        self._stack.append(node)
        try:
            super().generic_visit(node)
        finally:
            self._stack.pop()

    def _visit_scope_body(
        self, name: str, line: int, body: list[ast.stmt], extra: Iterable[ast.AST] = ()
    ) -> None:
        for item in extra:
            self.visit(item)
        self._scope_stack.append((name, line))
        try:
            for statement in body:
                self.visit(statement)
        finally:
            self._scope_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._stack.append(node)
        try:
            for decorator in node.decorator_list:
                self.visit(decorator)
            for default in list(node.args.defaults) + [
                item for item in node.args.kw_defaults if item is not None
            ]:
                self.visit(default)
            self._visit_scope_body(node.name, node.lineno, node.body)
        finally:
            self._stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)  # type: ignore[arg-type]

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._stack.append(node)
        try:
            self._visit_scope_body("<lambda>", node.lineno, [ast.Expr(node.body)])
        finally:
            self._stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._stack.append(node)
        try:
            for decorator in node.decorator_list:
                self.visit(decorator)
            for base in node.bases:
                self.visit(base)
            self._visit_scope_body(node.name, node.lineno, node.body)
        finally:
            self._stack.pop()

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.BitXor):
            parent = self._stack[-1] if self._stack else None
            assignment_targets: tuple[str, ...] = ()
            enclosing_call: str | None = None
            for ancestor in reversed(self._stack):
                if isinstance(ancestor, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                    if isinstance(ancestor, ast.Assign):
                        names: list[str] = []
                        for item in ancestor.targets:
                            names.extend(_target_names(item))
                        assignment_targets = tuple(names)
                    elif isinstance(ancestor, ast.AnnAssign):
                        assignment_targets = _target_names(ancestor.target)
                    elif isinstance(ancestor, ast.NamedExpr):
                        assignment_targets = _target_names(ancestor.target)
                    break
                if isinstance(ancestor, ast.Call) and enclosing_call is None:
                    enclosing_call = _call_name(ancestor)
            names = list(assignment_targets) + [
                _safe_unparse(node.left),
                _safe_unparse(node.right),
            ]
            self.findings.append(
                XorFinding(
                    line=getattr(node, "lineno", 0),
                    col=getattr(node, "col_offset", 0),
                    expression=_safe_unparse(node),
                    left_expression=_safe_unparse(node.left),
                    right_expression=_safe_unparse(node.right),
                    left_constant=_literal_value(node.left),
                    right_constant=_literal_value(node.right),
                    target_hint=infer_target(names),
                    assignment_targets=assignment_targets,
                    enclosing_call=enclosing_call,
                    scope_key=self._scope_key,
                    scope_name=self._scope_name,
                    nested_in_xor=(
                        isinstance(parent, ast.BinOp)
                        and isinstance(parent.op, ast.BitXor)
                    ),
                    node=node,
                )
            )
        self.generic_visit(node)


def find_xor_nodes(tree: ast.AST) -> list[XorFinding]:
    """Return all XOR findings in source order."""

    visitor = XorNodeFinder()
    visitor.visit(tree)
    visitor.findings.sort(key=lambda item: (item.line, item.col))
    return visitor.findings
