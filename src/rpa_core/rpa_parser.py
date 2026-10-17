"""Read-only RPA archive parser for Ren'Py and XOR-modified variants.

Layout supported
----------------
* ``RPA-2.0 <index-offset>``: zlib-compressed pickle at the offset, no XOR.
* ``RPA-3.0 <index-offset> <key>`` and compatible ``3.2``/``4.0`` headers:
  the same index, with 32/64-bit offset and length fields XORed by ``key``.
* A static rule set may supply an alternative offset/size key when the header
  key is a decoy or was changed by a mod.

The index pickle is parsed by :mod:`safe_pickle_reader`, never by
``pickle.load``.  Archive files are opened read-only; extraction is done by
the exporter.
"""

from __future__ import annotations

import logging
import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from .rule_applier import RuleError, RuleSet, XorRule
from .safe_pickle_reader import (
    PickleLimitError,
    PickleLimits,
    SafePickleError,
    UnsafePickleError,
    safe_pickle_loads,
)

logger = logging.getLogger(__name__)

MAX_KEY_INT = (1 << 64) - 1
DEFAULT_CHUNK_SIZE = 1024 * 1024


class RpaError(RuntimeError):
    """Base class for RPA parser failures."""


class RpaFormatError(RpaError):
    """Raised for malformed headers, indices or offsets."""


class RpaSecurityError(RpaError):
    """Raised when a defensive limit or pickle rule rejects the archive."""


class RpaReadError(RpaError):
    """Raised when archive data cannot be read safely."""


@dataclass(frozen=True, slots=True)
class RpaLimits:
    """Resource ceilings for attacker-controlled archive metadata."""

    max_header_bytes: int = 4096
    max_compressed_index_bytes: int = 64 * 1024 * 1024
    max_index_bytes: int = 256 * 1024 * 1024
    max_entries: int = 200_000
    max_parts_per_entry: int = 64
    max_prefix_bytes: int = 4096
    max_name_length: int = 1024


_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def normalize_archive_name(value: str | bytes) -> str:
    """Validate an archive member name as a safe relative POSIX path.

    Absolute paths, drive letters, ``..`` traversal, NULs, control characters,
    Windows alternate-data-stream colons and Windows device names are rejected.
    Backslashes are normalised to ``/`` so a Windows archive cannot smuggle a
    traversal sequence past the check.
    """

    if isinstance(value, bytes):
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError:
            text = value.decode("latin-1")
    elif isinstance(value, str):
        text = value
    else:
        raise RpaFormatError(f"资源名必须是 str 或 bytes，收到 {type(value).__name__}")

    if not text or len(text) > 1024:
        raise RpaFormatError("资源名为空或超过 1024 字符")
    if "\x00" in text or any(ord(char) < 32 for char in text):
        raise RpaFormatError(f"资源名包含 NUL 或控制字符: {text!r}")
    text = text.replace("\\", "/")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise RpaFormatError(f"拒绝绝对资源路径: {text!r}")
    if text.endswith("/"):
        raise RpaFormatError(f"资源名不能以斜杠结尾: {text!r}")

    parts = text.split("/")
    for part in parts:
        if part in {"", ".", ".."}:
            raise RpaFormatError(f"拒绝路径穿越或空路径段: {text!r}")
        if ":" in part:
            raise RpaFormatError(f"拒绝含冒号的 Windows ADS 路径: {text!r}")
        if part.endswith((" ", ".")):
            raise RpaFormatError(f"拒绝 Windows 不安全的结尾字符: {text!r}")
        stem = part.split(".", 1)[0].upper()
        if stem in _WINDOWS_RESERVED:
            raise RpaFormatError(f"拒绝 Windows 保留设备名: {text!r}")
    normalised = PurePosixPath(*parts).as_posix()
    if len(normalised) > 1024:
        raise RpaFormatError("规范化资源名超过 1024 字符")
    return normalised


def _normalise_prefix(value: Any, limits: RpaLimits) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        prefix = value
    elif isinstance(value, bytearray):
        prefix = bytes(value)
    elif isinstance(value, str):
        try:
            prefix = value.encode("latin-1")
        except UnicodeEncodeError:
            prefix = value.encode("utf-8")
    elif isinstance(value, int) and not isinstance(value, bool) and value == 0:
        prefix = b""
    else:
        raise RpaFormatError(f"不支持的索引 prefix 类型: {type(value).__name__}")
    if len(prefix) > limits.max_prefix_bytes:
        raise RpaSecurityError(
            f"prefix 长度 {len(prefix)} 超过 {limits.max_prefix_bytes} 上限"
        )
    return prefix


@dataclass(frozen=True, slots=True)
class RpaPart:
    """One contiguous part of an archive member.

    ``length`` includes the synthetic ``prefix`` bytes: the bytes actually
    stored at ``offset`` are ``length - len(prefix)`` long.
    """

    offset: int
    length: int
    prefix: bytes = b""
    raw_offset: int = 0
    raw_length: int = 0

    @property
    def disk_length(self) -> int:
        """Number of bytes stored in the archive for this part."""

        return self.length - len(self.prefix)

    def to_dict(self) -> dict[str, Any]:
        return {
            "offset": self.offset,
            "length": self.length,
            "disk_length": self.disk_length,
            "prefix_hex": self.prefix.hex(),
            "raw_offset": self.raw_offset,
            "raw_length": self.raw_length,
        }


@dataclass(frozen=True, slots=True)
class RpaEntry:
    """An archive member, possibly made of several index parts."""

    name: str
    parts: tuple[RpaPart, ...]

    @property
    def length(self) -> int:
        return sum(part.length for part in self.parts)

    @property
    def offset(self) -> int:
        return self.parts[0].offset if self.parts else 0

    @property
    def prefix(self) -> bytes:
        return self.parts[0].prefix if self.parts else b""

    @property
    def is_empty(self) -> bool:
        return self.length == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "offset": self.offset,
            "length": self.length,
            "parts": [part.to_dict() for part in self.parts],
        }


@dataclass(slots=True)
class RpaArchive:
    """Parsed archive metadata; no source file is modified."""

    path: Path
    version: str
    file_size: int
    index_offset: int
    index_end: int
    header_key: int | None
    applied_offset_key: int | bytes | None
    applied_size_key: int | bytes | None
    selection: str
    entries: tuple[RpaEntry, ...]
    warnings: tuple[str, ...] = ()
    header_line: str = ""
    _by_name: dict[str, RpaEntry] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._by_name = {entry.name: entry for entry in self.entries}

    def get_entry(self, name: str) -> RpaEntry:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise RpaError(f"归档中不存在资源: {name}") from exc

    def read_entry(self, name: str | RpaEntry, *, max_bytes: int = 1024**3) -> bytes:
        """Read one entry fully, bounded by *max_bytes*."""

        entry = self.get_entry(name) if isinstance(name, str) else name
        if entry.length > max_bytes:
            raise RpaReadError(
                f"资源 {entry.name} 大小 {entry.length} 超过单次读取上限 {max_bytes}"
            )
        chunks = self.iter_entry_data(entry)
        return b"".join(chunks)

    def iter_entry_data(
        self, entry: RpaEntry, *, chunk_size: int = DEFAULT_CHUNK_SIZE
    ) -> Iterator[bytes]:
        """Yield an entry's bytes without loading the whole file into memory."""

        if chunk_size < 4096:
            raise ValueError("chunk_size 不能小于 4096")
        try:
            with self.path.open("rb") as handle:
                for part in entry.parts:
                    if part.disk_length < 0:
                        raise RpaReadError(f"资源 {entry.name} 的 prefix 长度非法")
                    if part.prefix:
                        yield part.prefix
                    remaining = part.disk_length
                    handle.seek(part.offset)
                    while remaining > 0:
                        chunk = handle.read(min(chunk_size, remaining))
                        if not chunk:
                            raise RpaReadError(
                                f"读取资源 {entry.name} 时提前到达文件末尾"
                            )
                        remaining -= len(chunk)
                        yield chunk
        except OSError as exc:
            raise RpaReadError(f"读取归档 {self.path} 失败: {exc}") from exc

    def list_names(self) -> list[str]:
        return [entry.name for entry in self.entries]

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "version": self.version,
            "file_size": self.file_size,
            "index_offset": self.index_offset,
            "index_end": self.index_end,
            "header_key": _key_to_json(self.header_key),
            "applied_offset_key": _key_to_json(self.applied_offset_key),
            "applied_size_key": _key_to_json(self.applied_size_key),
            "selection": self.selection,
            "entry_count": len(self.entries),
            "warnings": list(self.warnings),
            "entries": [entry.to_dict() for entry in self.entries],
        }


def _key_to_json(key: int | bytes | None) -> int | str | None:
    if key is None:
        return None
    if isinstance(key, bytes):
        return "0x" + key.hex()
    return key


@dataclass(frozen=True, slots=True)
class _Header:
    version: str
    index_offset: int
    key: int | None
    line: str
    raw_size: int


def _parse_hex_int(token: str, *, what: str) -> int:
    text = token.strip()
    if text.lower().startswith("0x"):
        text = text[2:]
    if not text or not re.fullmatch(r"[0-9a-fA-F]+", text):
        raise RpaFormatError(f"{what} 不是十六进制整数: {token!r}")
    value = int(text, 16)
    if value > MAX_KEY_INT:
        raise RpaFormatError(f"{what} 超过 64 位上限: {token!r}")
    return value


_HEADER_RE = re.compile(r"^RPA-(\d+)\.(\d+)$", re.IGNORECASE)


def _read_header(handle: Any, limits: RpaLimits) -> _Header:
    raw = handle.readline(limits.max_header_bytes + 1)
    if not raw:
        raise RpaFormatError("RPA 文件为空")
    if len(raw) > limits.max_header_bytes:
        raise RpaSecurityError(f"RPA 头部超过 {limits.max_header_bytes} 字节上限")
    if not raw.endswith(b"\n"):
        raise RpaFormatError("RPA 头部缺少换行符")
    try:
        line = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise RpaFormatError("RPA 头部不是 ASCII") from exc
    tokens = line.split()
    if len(tokens) < 2:
        raise RpaFormatError(f"RPA 头部字段不足: {line!r}")
    match = _HEADER_RE.fullmatch(tokens[0])
    if not match:
        raise RpaFormatError(f"不是可识别的 RPA 头部: {tokens[0]!r}")
    version = f"RPA-{match.group(1)}.{match.group(2)}"
    index_offset = _parse_hex_int(tokens[1], what="索引偏移")
    key: int | None = None
    if len(tokens) >= 3:
        key = _parse_hex_int(tokens[2], what="XOR 密钥")
    major = int(match.group(1))
    if major >= 3 and key is None:
        raise RpaFormatError(f"{version} 头部缺少 XOR 密钥字段")
    return _Header(
        version=version,
        index_offset=index_offset,
        key=key,
        line=line,
        raw_size=len(raw),
    )


def _zlib_decompress_limited(data: bytes, max_output: int, wbits: int) -> tuple[bytes, int]:
    decompressor = zlib.decompressobj(wbits)
    output = bytearray()
    chunk = data
    while True:
        remaining = max_output + 1 - len(output)
        if remaining <= 0:
            raise RpaSecurityError(f"索引解压超过 {max_output} 字节上限")
        try:
            output.extend(decompressor.decompress(chunk, remaining))
        except zlib.error as exc:
            raise RpaFormatError(f"zlib 解压失败: {exc}") from exc
        if len(output) > max_output:
            raise RpaSecurityError(f"索引解压超过 {max_output} 字节上限")
        if decompressor.eof:
            consumed = len(data) - len(decompressor.unused_data)
            return bytes(output), consumed
        chunk = decompressor.unconsumed_tail
        if not chunk:
            raise RpaFormatError("zlib 索引数据不完整")


def _index_xor_candidates(
    rules: RuleSet | None, header_key: int | None
) -> list[tuple[str, int | bytes]]:
    candidates: list[tuple[str, int | bytes]] = []
    if rules is not None:
        for rule in rules.candidate_keys("index"):
            candidates.append((rule.rule_id, rule.key))
    # Some mods XOR the compressed index with the same header key.  This is
    # tried only after the normal zlib path and explicit index rules.
    if header_key is not None:
        candidates.append(("header-key", header_key))
    deduped: list[tuple[str, int | bytes]] = []
    seen: set[tuple[str, Any]] = set()
    for label, key in candidates:
        identity = (type(key).__name__, key)
        if identity not in seen:
            seen.add(identity)
            deduped.append((label, key))
    return deduped[:16]


def _load_index(
    blob: bytes,
    *,
    header_key: int | None,
    rules: RuleSet | None,
    limits: RpaLimits,
) -> tuple[Any, str, int]:
    attempts: list[str] = []
    pickle_limits = PickleLimits(
        max_input_bytes=limits.max_index_bytes,
        max_total_bytes=limits.max_index_bytes,
    )
    transforms: list[tuple[str, bytes]] = [("plain", blob)]
    for label, key in _index_xor_candidates(rules, header_key):
        try:
            transformed = XorRule(key=key).apply_bytes(blob)
        except RuleError as exc:
            attempts.append(f"{label}: {exc}")
            continue
        transforms.append((f"xor-index:{label}", transformed))

    for label, payload in transforms:
        for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
            try:
                decompressed, consumed = _zlib_decompress_limited(
                    payload, limits.max_index_bytes, wbits
                )
            except RpaSecurityError:
                raise
            except RpaFormatError as exc:
                attempts.append(f"{label}/{wbits}: {exc}")
                continue
            try:
                index = safe_pickle_loads(decompressed, limits=pickle_limits)
            except UnsafePickleError as exc:
                raise RpaSecurityError(f"索引 pickle 被安全策略拒绝: {exc}") from exc
            except PickleLimitError as exc:
                raise RpaSecurityError(f"索引 pickle 超过资源上限: {exc}") from exc
            except SafePickleError as exc:
                attempts.append(f"{label}/{wbits}: pickle: {exc}")
                continue
            return index, label, consumed

    # RPA-1.0-style uncompressed index fallback; only attempt it when the
    # bytes actually look like a pickle, never when they look like zlib (a
    # compressed stream can begin with a byte that happens to be an opcode).
    if blob[:1] in (b"\x80", b"(", b"}", b"]", b"c", b"I", b"N", b"S", b"U", b"V", b"X", b"K", b"J"):
        try:
            index = safe_pickle_loads(blob, limits=pickle_limits)
        except UnsafePickleError as exc:
            raise RpaSecurityError(f"索引 pickle 被安全策略拒绝: {exc}") from exc
        except PickleLimitError as exc:
            raise RpaSecurityError(f"索引 pickle 超过资源上限: {exc}") from exc
        except SafePickleError as exc:
            attempts.append(f"raw-pickle: {exc}")
        else:
            return index, "raw-pickle", len(blob)

    details = "；".join(attempts[-6:]) if attempts else "未知原因"
    raise RpaFormatError(f"无法解压或安全解析 RPA 索引: {details}")


def _extract_index_dict(index: Any) -> dict[Any, Any]:
    if isinstance(index, dict):
        return index
    if isinstance(index, (tuple, list)) and index and isinstance(index[0], dict):
        return index[0]
    raise RpaFormatError(
        f"RPA 索引顶层应为 dict，实际为 {type(index).__name__}"
    )


def _looks_like_part(value: Any) -> bool:
    return (
        isinstance(value, (tuple, list))
        and len(value) >= 2
        and isinstance(value[0], int)
        and not isinstance(value[0], bool)
        and isinstance(value[1], int)
        and not isinstance(value[1], bool)
    )


def _normalise_parts(value: Any, limits: RpaLimits) -> list[tuple[int, int, bytes]]:
    if _looks_like_part(value):
        raw_parts = [value]
    elif isinstance(value, (tuple, list)):
        raw_parts = list(value)
    else:
        raise RpaFormatError(f"索引条目类型非法: {type(value).__name__}")
    if not raw_parts:
        raise RpaFormatError("索引条目为空")
    if len(raw_parts) > limits.max_parts_per_entry:
        raise RpaSecurityError(f"索引条目分段数超过 {limits.max_parts_per_entry}")

    parts: list[tuple[int, int, bytes]] = []
    for raw_part in raw_parts:
        if not isinstance(raw_part, (tuple, list)) or len(raw_part) < 2:
            raise RpaFormatError("索引分段必须是 (offset, length[, prefix])")
        raw_offset, raw_length = raw_part[0], raw_part[1]
        if (
            not isinstance(raw_offset, int)
            or isinstance(raw_offset, bool)
            or not isinstance(raw_length, int)
            or isinstance(raw_length, bool)
        ):
            raise RpaFormatError("offset/length 必须是整数")
        prefix = _normalise_prefix(raw_part[2] if len(raw_part) >= 3 else None, limits)
        parts.append((raw_offset, raw_length, prefix))
    return parts


def _normalise_index(
    index: dict[Any, Any], limits: RpaLimits
) -> tuple[list[tuple[str, list[tuple[int, int, bytes]]]], list[str]]:
    warnings: list[str] = []
    entries: list[tuple[str, list[tuple[int, int, bytes]]]] = []
    seen: set[str] = set()
    seen_casefold: dict[str, str] = {}
    if len(index) > limits.max_entries:
        raise RpaSecurityError(f"索引条目数 {len(index)} 超过 {limits.max_entries}")
    for raw_name, raw_value in index.items():
        name = normalize_archive_name(raw_name)
        if name in seen:
            raise RpaFormatError(f"索引中存在重复资源名: {name}")
        seen.add(name)
        folded = name.casefold()
        if folded in seen_casefold and seen_casefold[folded] != name:
            warnings.append(
                f"资源名仅在大小写上不同，Windows 上可能冲突: "
                f"{seen_casefold[folded]!r} / {name!r}"
            )
        else:
            seen_casefold[folded] = name
        parts = _normalise_parts(raw_value, limits)
        entries.append((name, parts))
    return entries, warnings


@dataclass(frozen=True, slots=True)
class _KeyCandidate:
    label: str
    offset_key: int | bytes | None
    size_key: int | bytes | None
    priority: int


def _build_key_candidates(
    header_key: int | None, rules: RuleSet | None
) -> list[_KeyCandidate]:
    candidates: list[_KeyCandidate] = []
    if header_key is None:
        candidates.append(_KeyCandidate("plain", None, None, 0))
    else:
        candidates.append(_KeyCandidate("header-key", header_key, header_key, 0))

    if rules is not None and rules.has_rules:
        for index, (offset_rule, size_rule) in enumerate(rules.candidate_pairs()):
            candidates.append(
                _KeyCandidate(
                    f"rules:{offset_rule.rule_id}/{size_rule.rule_id}",
                    offset_rule.key,
                    size_rule.key,
                    10 + index,
                )
            )
        # Some analysers only emit a target-less rule.  Try it uniformly as a
        # last resort; target checks are deliberately bypassed by the parser.
        for index, rule in enumerate(rules.all_keys()):
            if rule.target in {"offset", "size", "unknown"}:
                candidates.append(
                    _KeyCandidate(
                        f"rules-uniform:{rule.rule_id}",
                        rule.key,
                        rule.key,
                        1000 + index,
                    )
                )

    deduped: list[_KeyCandidate] = []
    seen: set[tuple[Any, Any]] = set()
    for candidate in candidates:
        identity = (_key_identity(candidate.offset_key), _key_identity(candidate.size_key))
        if identity not in seen:
            seen.add(identity)
            deduped.append(candidate)
    return deduped


def _key_identity(key: int | bytes | None) -> tuple[str, Any]:
    if key is None:
        return ("none", None)
    if isinstance(key, bytes):
        return ("bytes", key)
    return ("int", key)


def _apply_key(raw: int, key: int | bytes | None) -> int:
    if key is None:
        return raw
    if isinstance(key, bytes):
        return raw ^ int.from_bytes(key, "big")
    return raw ^ key


def _score_candidate(
    raw_entries: list[tuple[str, list[tuple[int, int, bytes]]]],
    candidate: _KeyCandidate,
    *,
    file_size: int,
    header_size: int,
    index_offset: int,
    index_end: int,
    header_has_key: bool,
) -> tuple[tuple[int, int, int, int], list[tuple[str, tuple[RpaPart, ...]]]]:
    valid = 0
    after_index = 0
    before_index = 0
    overlap = 0
    total = 0
    decoded_entries: list[tuple[str, tuple[RpaPart, ...]]] = []
    for name, parts in raw_entries:
        decoded_parts: list[RpaPart] = []
        for raw_offset, raw_length, prefix in parts:
            total += 1
            offset = _apply_key(raw_offset, candidate.offset_key)
            length = _apply_key(raw_length, candidate.size_key)
            disk_length = length - len(prefix)
            if (
                offset < header_size
                or length < 0
                or disk_length < 0
                or offset + disk_length > file_size
            ):
                continue
            if disk_length and offset < index_end and offset + disk_length > index_offset:
                overlap += 1
                continue
            valid += 1
            if offset >= index_end:
                after_index += 1
            elif offset + disk_length <= index_offset:
                before_index += 1
            decoded_parts.append(
                RpaPart(
                    offset=offset,
                    length=length,
                    prefix=prefix,
                    raw_offset=raw_offset,
                    raw_length=raw_length,
                )
            )
        if len(decoded_parts) == len(parts):
            decoded_entries.append((name, tuple(decoded_parts)))
    if not header_has_key and candidate.offset_key is None and candidate.size_key is None:
        native_bonus = 1
    else:
        native_bonus = 0
    score = (valid, after_index, -overlap, native_bonus - candidate.priority)
    return score, decoded_entries


def parse_archive(
    path: str | Path,
    *,
    rules: RuleSet | None = None,
    limits: RpaLimits | None = None,
) -> RpaArchive:
    """Parse an RPA archive's header and index without modifying it.

    Args:
        path: Archive file to read.
        rules: Optional XOR rules from the AST analyser.
        limits: Resource ceilings for untrusted metadata.

    Raises:
        RpaFormatError: Malformed header/index or unrecoverable offsets.
        RpaSecurityError: A defensive limit or pickle rule rejected the file.
        RpaReadError: The file cannot be read.
    """

    archive_path = Path(path)
    effective_limits = limits or RpaLimits()
    try:
        stat = archive_path.stat()
    except OSError as exc:
        raise RpaReadError(f"无法访问归档 {archive_path}: {exc}") from exc
    if not archive_path.is_file():
        raise RpaReadError(f"归档路径不是文件: {archive_path}")
    file_size = stat.st_size
    if file_size <= 0:
        raise RpaFormatError("RPA 文件为空")

    try:
        with archive_path.open("rb") as handle:
            header = _read_header(handle, effective_limits)
            header_size = header.raw_size
            if header.index_offset < header_size:
                raise RpaFormatError(
                    f"索引偏移 {header.index_offset} 位于头部内（头部 {header_size} 字节）"
                )
            if header.index_offset >= file_size:
                raise RpaFormatError(
                    f"索引偏移 {header.index_offset} 超出文件大小 {file_size}"
                )
            handle.seek(header.index_offset)
            compressed = handle.read(effective_limits.max_compressed_index_bytes + 1)
    except OSError as exc:
        raise RpaReadError(f"读取归档 {archive_path} 失败: {exc}") from exc

    if len(compressed) > effective_limits.max_compressed_index_bytes:
        raise RpaSecurityError(
            f"压缩索引超过 {effective_limits.max_compressed_index_bytes} 字节上限"
        )

    index_object, transform, consumed = _load_index(
        compressed,
        header_key=header.key,
        rules=rules,
        limits=effective_limits,
    )
    index = _extract_index_dict(index_object)
    raw_entries, warnings = _normalise_index(index, effective_limits)
    index_end = min(file_size, header.index_offset + consumed)

    candidates = _build_key_candidates(header.key, rules)
    best: tuple[tuple[int, int, int, int], _KeyCandidate, list[tuple[str, tuple[RpaPart, ...]]]] | None = None
    best_valid = -1
    for candidate in candidates:
        score, decoded_entries = _score_candidate(
            raw_entries,
            candidate,
            file_size=file_size,
            header_size=header.raw_size,
            index_offset=header.index_offset,
            index_end=index_end,
            header_has_key=header.key is not None,
        )
        if best is None or score > best[0]:
            best = (score, candidate, decoded_entries)
        best_valid = max(best_valid, score[0])

    total_parts = sum(len(parts) for _name, parts in raw_entries)
    if best is None or best[0][0] != total_parts:
        sample = ""
        if best is not None:
            sample = f"最佳候选只解码出 {best[0][0]}/{total_parts} 个合法分段"
        raise RpaFormatError(
            "无法用头部密钥或静态 XOR 规则还原有效的 offset/size；"
            f"{sample}。可先用 analyze 提取规则，再通过 --rules 传入。"
        )

    score, selected, decoded_entries = best
    entries = tuple(RpaEntry(name=name, parts=parts) for name, parts in decoded_entries)
    if len(entries) != len(raw_entries):
        raise RpaFormatError("内部错误：部分索引条目未能解码")
    if transform != "plain":
        warnings.append(f"索引变换: {transform}")
    warnings.append(f"选择密钥候选: {selected.label}")
    logger.info(
        "RPA 解析完成: %s %s entries=%d key=%s",
        archive_path,
        header.version,
        len(entries),
        selected.label,
    )
    return RpaArchive(
        path=archive_path,
        version=header.version,
        file_size=file_size,
        index_offset=header.index_offset,
        index_end=index_end,
        header_key=header.key,
        applied_offset_key=selected.offset_key,
        applied_size_key=selected.size_key,
        selection=selected.label,
        entries=entries,
        warnings=tuple(dict.fromkeys(warnings)),
        header_line=header.line,
    )
