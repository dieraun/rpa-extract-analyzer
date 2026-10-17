"""Static ``.pyo``/``.pyc`` decompilation and xdis disassembly.

Security boundary
-----------------
This module only turns bytecode into **text**.  It never imports, compiles
into a running process, ``exec``s or ``eval``s the recovered source or
disassembly.  A caller may hand the text to the AST/disassembly analysers,
which only parse strings.
"""

from __future__ import annotations

import contextlib
import io
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:  # pragma: no cover - import availability is environment dependent
    from xdis.magics import by_magic as _MAGIC_TO_VERSIONS
except Exception as _magic_exc:  # pragma: no cover
    _MAGIC_TO_VERSIONS = {}
    _MAGIC_IMPORT_ERROR = _magic_exc
else:
    _MAGIC_IMPORT_ERROR = None

try:  # pragma: no cover - exercised through the real dependency in integration
    from uncompyle6.main import decompile_file as _decompile_file
except Exception as _uncompyle_exc:  # pragma: no cover
    _decompile_file = None
    _UNCOMPYLE_IMPORT_ERROR: Exception | None = _uncompyle_exc
else:
    _UNCOMPYLE_IMPORT_ERROR = None

try:  # pragma: no cover - import availability is environment dependent
    from xdis.disasm import disco as _xdis_disco
    from xdis.load import load_module as _xdis_load_module
except Exception as _xdis_disasm_exc:  # pragma: no cover
    _xdis_disco = None
    _xdis_load_module = None
    _XDIS_DISASM_ERROR: Exception | None = _xdis_disasm_exc
else:
    _XDIS_DISASM_ERROR = None

MAX_DISASSEMBLY_BYTES = 32 * 1024 * 1024
MAX_BYTECODE_BYTES = 128 * 1024 * 1024


class PyoFormatError(ValueError):
    """Raised when a bytecode file has an unusable header."""


@dataclass(frozen=True, slots=True)
class PyoHeader:
    """Metadata read from the first four bytes of a ``.pyo``/``.pyc`` file."""

    magic: bytes
    magic_hex: str
    python_versions: tuple[str, ...]

    @property
    def is_python2(self) -> bool:
        """Whether the magic belongs to a Python 2 interpreter."""

        return any(version.startswith("2.") for version in self.python_versions)

    @property
    def version_label(self) -> str:
        """Human-readable version label (``unknown`` when unmapped)."""

        if not self.python_versions:
            return "unknown"
        return ", ".join(self.python_versions)


@dataclass(slots=True)
class DecompileResult:
    """Outcome of a decompilation attempt.

    ``source`` is only populated on success and is never executed.  The object
    intentionally exposes a small JSON view so CLI logs cannot dump megabytes
    of recovered source accidentally.
    """

    path: Path
    status: str
    message: str
    source: str | None = None
    header: PyoHeader | None = None
    diagnostics: tuple[str, ...] = ()
    source_path: Path | None = None
    normalization_warnings: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Whether decompilation produced usable source text."""

        return self.status == "success" and bool(self.source)

    @property
    def python_version(self) -> str:
        """Detected Python version label, or ``unknown``."""

        return self.header.version_label if self.header else "unknown"

    def to_dict(self, *, include_source: bool = False) -> dict[str, Any]:
        """Return a JSON-serialisable summary.

        Args:
            include_source: Include the full recovered text.  Disabled by
                default to keep logs and reports small.
        """

        payload: dict[str, Any] = {
            "path": str(self.path),
            "status": self.status,
            "message": self.message,
            "python_version": self.python_version,
            "magic_hex": self.header.magic_hex if self.header else None,
            "source_path": str(self.source_path) if self.source_path else None,
            "source_length": len(self.source) if self.source else 0,
            "diagnostics": list(self.diagnostics),
            "normalization_warnings": list(self.normalization_warnings),
            **self.extra,
        }
        if include_source:
            payload["source"] = self.source
        return payload


def inspect_pyo_header(path: str | Path) -> PyoHeader:
    """Read and classify the four-byte magic of *path*.

    The file is opened read-only.  An unknown magic is reported as an empty
    version tuple rather than guessed, because guessing could produce
    misleading key extraction results.
    """

    pyo_path = Path(path)
    try:
        with pyo_path.open("rb") as handle:
            raw = handle.read(4)
    except OSError as exc:
        raise PyoFormatError(f"无法读取字节码文件 {pyo_path}: {exc}") from exc
    if len(raw) < 4:
        raise PyoFormatError(f"字节码文件头不足 4 字节: {pyo_path}")

    versions = _MAGIC_TO_VERSIONS.get(raw, set())
    sorted_versions = tuple(sorted(versions, key=_version_sort_key))
    header = PyoHeader(
        magic=raw,
        magic_hex=raw.hex(),
        python_versions=sorted_versions,
    )
    logger.debug("字节码头: %s magic=%s versions=%s", pyo_path, header.magic_hex, sorted_versions)
    return header


def _version_sort_key(value: str) -> tuple[int, ...]:
    """Sort version strings such as ``2.7`` and ``3.10`` numerically."""

    parts: list[int] = []
    for part in re.split(r"[.\-+]", value):
        match = re.match(r"\d+", part)
        if match:
            parts.append(int(match.group(0)))
    return tuple(parts)


def _collect_decompile_output(path: Path) -> tuple[str, str]:
    """Call uncompyle6 and capture source plus diagnostics.

    The dependency is imported once at module import time.  ``SystemExit`` is
    caught because uncompyle6's command helpers occasionally terminate on
    malformed bytecode; a research tool should return a structured failure
    instead.
    """

    if _decompile_file is None:
        raise RuntimeError(f"uncompyle6 不可用: {_UNCOMPYLE_IMPORT_ERROR}")

    source_stream = io.StringIO()
    error_stream = io.StringIO()
    with contextlib.redirect_stderr(error_stream):
        try:
            _decompile_file(str(path), outstream=source_stream)
        except SystemExit as exc:  # pragma: no cover - dependency dependent
            raise RuntimeError(f"uncompyle6 提前退出 (code={exc.code})") from exc
    return source_stream.getvalue(), error_stream.getvalue()


def _clean_diagnostics(text: str, *, limit: int = 4000) -> tuple[str, ...]:
    """Turn uncompyle6 stderr into a bounded list of non-empty lines."""

    if not text:
        return ()
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if sum(len(line) for line in lines) > limit:
        clipped: list[str] = []
        used = 0
        for line in lines:
            if used + len(line) > limit:
                clipped.append("... (诊断信息已截断)")
                break
            clipped.append(line)
            used += len(line)
        lines = clipped
    return tuple(lines)


def decompile_pyo(
    path: str | Path,
    *,
    output_dir: str | Path | None = None,
    output_name: str = "loader_decompiled.py",
    overwrite: bool = False,
    normalize_py2: bool = True,
) -> DecompileResult:
    """Decompile a Python 2 ``.pyo`` (or compatible ``.pyc``) to text.

    Args:
        path: Bytecode file to read.
        output_dir: Optional directory for saving the recovered text.  The
            source is always returned in memory as well.
        output_name: File name used when *output_dir* is supplied.
        overwrite: Allow replacing an existing output file.
        normalize_py2: Apply best-effort Python 2 -> Python 3 syntax
            normalisation.  This only rewrites text; it never executes it.

    Returns:
        A structured :class:`DecompileResult`.  Failures are returned rather
        than raised so a batch scan can continue with the next archive.
    """

    pyo_path = Path(path)
    diagnostics: list[str] = []
    try:
        header = inspect_pyo_header(pyo_path)
    except PyoFormatError as exc:
        logger.warning("字节码头无效: %s", exc)
        return DecompileResult(
            path=pyo_path,
            status="failed",
            message=str(exc),
            diagnostics=(str(exc),),
        )

    if _MAGIC_IMPORT_ERROR is not None:
        return DecompileResult(
            path=pyo_path,
            status="failed",
            message=f"xdis 不可用，无法识别字节码版本: {_MAGIC_IMPORT_ERROR}",
            header=header,
        )

    if not header.python_versions:
        message = (
            f"无法识别的字节码 magic 0x{header.magic_hex}；"
            "请确认文件是 Python 2.7 的 renpy/loader.pyo，或安装匹配的 xdis 版本"
        )
        logger.warning(message)
        return DecompileResult(
            path=pyo_path,
            status="unsupported",
            message=message,
            header=header,
            diagnostics=(message,),
        )

    try:
        source, stderr_text = _collect_decompile_output(pyo_path)
        diagnostics.extend(_clean_diagnostics(stderr_text))
    except Exception as exc:  # noqa: BLE001 - return structured failure
        message = f"反编译失败: {type(exc).__name__}: {exc}"
        logger.warning("%s (%s)", message, pyo_path)
        return DecompileResult(
            path=pyo_path,
            status="failed",
            message=message,
            header=header,
            diagnostics=tuple(diagnostics) or (message,),
        )

    if not source.strip():
        message = "uncompyle6 未产生源码；loader 可能被高强度控制流混淆"
        logger.warning("%s (%s)", message, pyo_path)
        return DecompileResult(
            path=pyo_path,
            status="failed",
            message=message,
            header=header,
            diagnostics=tuple(diagnostics) or (message,),
        )

    normalization_warnings: tuple[str, ...] = ()
    if normalize_py2 and header.is_python2:
        source, normalization_warnings = normalize_python2_source(source)
        if normalization_warnings:
            diagnostics.extend(normalization_warnings)

    result = DecompileResult(
        path=pyo_path,
        status="success",
        message=f"反编译成功，Python {header.version_label}",
        source=source,
        header=header,
        diagnostics=tuple(diagnostics),
        normalization_warnings=normalization_warnings,
    )

    if output_dir is not None:
        try:
            result.source_path = save_source(
                result, output_dir, filename=output_name, overwrite=overwrite
            )
            result.message += f"；源码已写入 {result.source_path}"
        except OSError as exc:
            result.status = "failed"
            result.message = f"反编译成功但写入输出失败: {exc}"
            diagnostics.append(str(exc))
            result.diagnostics = tuple(diagnostics)
            logger.warning(result.message)

    return result


def save_source(
    result: DecompileResult,
    output_dir: str | Path,
    *,
    filename: str = "loader_decompiled.py",
    overwrite: bool = False,
) -> Path:
    """Atomically save recovered source under *output_dir*.

    Raises:
        ValueError: If there is no source or the target would overwrite the
            original bytecode file.
        FileExistsError: If the target exists and *overwrite* is false.
        OSError: On filesystem failures.
    """

    if not result.source:
        raise ValueError("没有可保存的反编译源码")
    if not filename or Path(filename).name != filename or filename in {".", ".."}:
        raise ValueError("输出文件名必须是单一文件名，不能包含目录分隔符")
    out_dir = Path(output_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / filename
    try:
        target_resolved = target.resolve()
        source_resolved = result.path.resolve()
    except OSError:
        target_resolved = target.absolute()
        source_resolved = result.path.absolute()
    if target_resolved == source_resolved:
        raise ValueError("输出路径不能覆盖原始 loader.pyo")

    if target.exists() and not overwrite:
        raise FileExistsError(f"输出文件已存在（如需覆盖请显式允许）: {target}")

    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(result.source, encoding="utf-8", newline="\n")
        if target.exists() and not overwrite:
            raise FileExistsError(f"输出文件已存在: {target}")
        temporary.replace(target)
    finally:
        with contextlib.suppress(OSError):
            if temporary.exists():
                temporary.unlink()
    logger.info("反编译源码已保存: %s", target)
    return target


@dataclass(slots=True)
class DisassemblyResult:
    """Outcome of a static xdis bytecode disassembly.

    The text is instruction data only.  Loading a code object with xdis and
    disassembling it never executes the module.
    """

    path: Path
    status: str
    message: str
    text: str | None = None
    header: PyoHeader | None = None
    code_objects: int = 0
    instruction_count: int = 0
    diagnostics: tuple[str, ...] = ()
    output_path: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "success" and bool(self.text)

    @property
    def python_version(self) -> str:
        return self.header.version_label if self.header else "unknown"

    def to_dict(self, *, include_text: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": str(self.path),
            "status": self.status,
            "message": self.message,
            "python_version": self.python_version,
            "magic_hex": self.header.magic_hex if self.header else None,
            "code_objects": self.code_objects,
            "instruction_count": self.instruction_count,
            "text_length": len(self.text) if self.text else 0,
            "output_path": str(self.output_path) if self.output_path else None,
            "diagnostics": list(self.diagnostics),
            **self.extra,
        }
        if include_text:
            payload["text"] = self.text
        return payload


def _count_code_objects(root: Any) -> int:
    """Count code objects nested in ``co_consts`` without executing them."""

    stack = [root]
    seen: set[int] = set()
    count = 0
    while stack:
        current = stack.pop()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        count += 1
        constants = getattr(current, "co_consts", ()) or ()
        for constant in constants:
            if hasattr(constant, "co_code") and hasattr(constant, "co_name"):
                stack.append(constant)
    return count


def _count_instruction_lines(text: str) -> int:
    count = 0
    for line in text.splitlines():
        if re.match(r"^\s*(?:\d+:\s+)?\d+\s+[A-Z][A-Z0-9_]+\b", line):
            count += 1
    return count


def _validate_output_name(filename: str) -> str:
    if not filename or Path(filename).name != filename or filename in {".", ".."}:
        raise ValueError("输出文件名必须是单一文件名，不能包含目录分隔符")
    return filename


def _atomic_write_text(
    text: str,
    output_dir: str | Path,
    filename: str,
    *,
    overwrite: bool,
    protected_path: Path | None = None,
) -> Path:
    filename = _validate_output_name(filename)
    out_dir = Path(output_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / filename
    if protected_path is not None:
        try:
            if target.resolve() == protected_path.resolve():
                raise ValueError("输出路径不能覆盖原始字节码文件")
        except OSError:
            if target.absolute() == protected_path.absolute():
                raise ValueError("输出路径不能覆盖原始字节码文件")
    if target.exists() and not overwrite:
        raise FileExistsError(f"输出文件已存在（如需覆盖请显式允许）: {target}")
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        if target.exists() and not overwrite:
            raise FileExistsError(f"输出文件已存在: {target}")
        temporary.replace(target)
    finally:
        with contextlib.suppress(OSError):
            if temporary.exists():
                temporary.unlink()
    return target


def disassemble_pyo(
    path: str | Path,
    *,
    output_dir: str | Path | None = None,
    output_name: str = "loader_disassembly.txt",
    overwrite: bool = False,
    asm_format: str = "classic",
) -> DisassemblyResult:
    """Disassemble a cross-version ``.pyo``/``.pyc`` with xdis.

    This is the fallback when uncompyle6 cannot recover source.  It only
    produces text; the code object is never imported or executed.
    """

    pyo_path = Path(path)
    diagnostics: list[str] = []
    try:
        header = inspect_pyo_header(pyo_path)
    except PyoFormatError as exc:
        return DisassemblyResult(
            path=pyo_path,
            status="failed",
            message=str(exc),
            diagnostics=(str(exc),),
        )

    if _XDIS_DISASM_ERROR is not None or _xdis_load_module is None or _xdis_disco is None:
        message = f"xdis 反汇编组件不可用: {_XDIS_DISASM_ERROR}"
        return DisassemblyResult(
            path=pyo_path,
            status="failed",
            message=message,
            header=header,
            diagnostics=(message,),
        )
    if not header.python_versions:
        message = f"无法识别的字节码 magic 0x{header.magic_hex}，xdis 可能无法正确反汇编"
        return DisassemblyResult(
            path=pyo_path,
            status="unsupported",
            message=message,
            header=header,
            diagnostics=(message,),
        )
    if asm_format not in {"classic", "xasm"}:
        raise ValueError("asm_format 必须是 classic 或 xasm")
    try:
        bytecode_size = pyo_path.stat().st_size
    except OSError as exc:
        return DisassemblyResult(
            path=pyo_path,
            status="failed",
            message=f"无法读取字节码文件大小: {exc}",
            header=header,
            diagnostics=(str(exc),),
        )
    if bytecode_size > MAX_BYTECODE_BYTES:
        message = f"字节码文件超过 {MAX_BYTECODE_BYTES // (1024 * 1024)} MiB 上限"
        return DisassemblyResult(
            path=pyo_path,
            status="failed",
            message=message,
            header=header,
            diagnostics=(message,),
        )

    try:
        loaded = _xdis_load_module(str(pyo_path))
        if not isinstance(loaded, tuple) or len(loaded) < 7:
            raise RuntimeError(f"xdis.load_module 返回结构异常: {type(loaded).__name__}")
        (
            version_tuple,
            timestamp,
            magic_int,
            code_object,
            is_pypy,
            source_size,
            sip_hash,
        ) = loaded[:7]
        if code_object is None:
            raise RuntimeError("xdis 未返回 code object")
        output = io.StringIO()
        _xdis_disco(
            version_tuple=version_tuple,
            co=code_object,
            timestamp=timestamp,
            out=output,
            is_pypy=is_pypy,
            magic_int=magic_int,
            source_size=source_size,
            sip_hash=sip_hash,
            asm_format=asm_format,
        )
        text = output.getvalue()
    except Exception as exc:  # noqa: BLE001 - return structured failure
        message = f"反汇编失败: {type(exc).__name__}: {exc}"
        logger.warning("%s (%s)", message, pyo_path)
        return DisassemblyResult(
            path=pyo_path,
            status="failed",
            message=message,
            header=header,
            diagnostics=tuple(diagnostics) or (message,),
        )

    if not text.strip():
        message = "xdis 未产生反汇编文本"
        return DisassemblyResult(
            path=pyo_path,
            status="failed",
            message=message,
            header=header,
            diagnostics=(message,),
        )
    if len(text.encode("utf-8", "replace")) > MAX_DISASSEMBLY_BYTES:
        message = f"反汇编文本超过 {MAX_DISASSEMBLY_BYTES // (1024 * 1024)} MiB 上限"
        return DisassemblyResult(
            path=pyo_path,
            status="failed",
            message=message,
            header=header,
            diagnostics=(message,),
        )

    result = DisassemblyResult(
        path=pyo_path,
        status="success",
        message=f"反汇编成功，Python {header.version_label}",
        text=text,
        header=header,
        code_objects=_count_code_objects(code_object),
        instruction_count=_count_instruction_lines(text),
        diagnostics=tuple(diagnostics),
        extra={"asm_format": asm_format},
    )
    if output_dir is not None:
        try:
            result.output_path = _atomic_write_text(
                text,
                output_dir,
                output_name,
                overwrite=overwrite,
                protected_path=pyo_path,
            )
            result.message += f"；反汇编已写入 {result.output_path}"
        except (OSError, ValueError) as exc:
            result.status = "failed"
            result.message = f"反汇编成功但写入输出失败: {exc}"
            diagnostics.append(str(exc))
            result.diagnostics = tuple(diagnostics)
            logger.warning(result.message)
    return result




_EXCEPT_PY2_RE = re.compile(
    r"^(?P<indent>\s*)except\s+(?P<expr>.+?)\s*,\s*(?P<name>[A-Za-z_]\w*)\s*:\s*(?P<tail>#.*)?$"
)
_RAISE_PY2_RE = re.compile(
    r"^(?P<indent>\s*)raise\s+(?P<expr>[^,]+?)\s*,\s*(?P<value>.+?)\s*$"
)
_LONG_SUFFIX_RE = re.compile(r"(?<![\w.])(\d+)[lL]\b")
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_UR_PREFIX_RE = re.compile(r"(?<![A-Za-z0-9_])[uU][rR](?=['\"])")


def normalize_python2_source(source: str) -> tuple[str, tuple[str, ...]]:
    """Best-effort textual Python 2 -> 3 conversion for :mod:`ast`.

    This is intentionally conservative and line-oriented.  It handles the
    constructs commonly emitted by uncompyle6 for Ren'Py loaders (``print``
    statements, ``except E, e``, backticks, long literals and a few renamed
    builtins).  The result is only parsed as an AST; it is never executed.

    Returns:
        ``(normalized_source, warnings)``.  Line count is preserved, which
        keeps line numbers in analyser findings meaningful.
    """

    warnings: list[str] = []
    lines: list[str] = []
    for line_number, original in enumerate(source.splitlines(), start=1):
        line = original
        line, count = _EXCEPT_PY2_RE.subn(
            lambda m: f"{m.group('indent')}except {m.group('expr').strip()} as {m.group('name')}:"
            + (f"  {m.group('tail')}" if m.group("tail") else ""),
            line,
        )
        if count:
            warnings.append(f"第 {line_number} 行: except E, e 已改写为 except E as e")

        line, count = _RAISE_PY2_RE.subn(
            lambda m: f"{m.group('indent')}raise {m.group('expr').strip()}({m.group('value').strip()})",
            line,
        )
        if count:
            warnings.append(f"第 {line_number} 行: raise E, v 已改写为 raise E(v)")

        line = _rewrite_print_statement(line, line_number, warnings)
        line, count = _BACKTICK_RE.subn(r"repr(\1)", line)
        if count:
            warnings.append(f"第 {line_number} 行: 反引号表达式已改写为 repr(...)")
        if "<>" in line:
            line = line.replace("<>", "!=")
            warnings.append(f"第 {line_number} 行: <> 已改写为 !=")

        line = _LONG_SUFFIX_RE.sub(r"\1", line)
        line = _UR_PREFIX_RE.sub("r", line)
        line = re.sub(r"\bxrange\s*\(", "range(", line)
        line = re.sub(r"\bunicode\s*\(", "str(", line)
        line = re.sub(r"\blong\s*\(", "int(", line)
        line = re.sub(r"\braw_input\s*\(", "input(", line)
        line = re.sub(r"\bbasestring\b", "str", line)
        line = re.sub(r"\.iteritems\s*\(", ".items(", line)
        line = re.sub(r"\.itervalues\s*\(", ".values(", line)
        line = re.sub(r"\.iterkeys\s*\(", ".keys(", line)
        lines.append(line)

    # Preserve a trailing newline so the AST line numbers match the text.
    normalized = "\n".join(lines)
    if source.endswith("\n"):
        normalized += "\n"
    return normalized, tuple(dict.fromkeys(warnings))


def _rewrite_print_statement(
    line: str, line_number: int, warnings: list[str]
) -> str:
    """Rewrite one Python 2 ``print`` statement, leaving calls untouched."""

    stripped_line = line.lstrip(" \t")
    indent = line[: len(line) - len(stripped_line)]
    if not stripped_line.startswith("print"):
        return line
    if len(stripped_line) > 5 and stripped_line[5] not in " \t":
        # ``printable``, ``printer`` and similar identifiers.
        return line

    remainder = stripped_line[5:].strip()
    if not remainder:
        return f"{indent}print()"
    if remainder.startswith("("):
        # Already a call such as ``print("x")``.
        return line
    if remainder.startswith("="):
        # ``print = ...`` is a normal assignment.
        return line

    target: str | None = None
    if remainder.startswith(">>"):
        target_text = remainder[2:].lstrip()
        if "," in target_text:
            target, remainder = target_text.split(",", 1)
        else:
            target, remainder = target_text, ""
        target = target.strip()
        remainder = remainder.strip()

    trailing_comma = remainder.endswith(",")
    if trailing_comma:
        remainder = remainder[:-1].rstrip()
    args = remainder

    if target:
        call = f"{indent}print({args}, file={target}"
        call += ", end=' ')" if trailing_comma else ")"
        warnings.append(f"第 {line_number} 行: print >> 已改写为 print(..., file=...)")
        return call

    call = f"{indent}print({args}"
    call += ", end=' ')" if trailing_comma else ")"
    warnings.append(f"第 {line_number} 行: print 语句已改写为 print(...)")
    return call
