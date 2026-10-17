"""Load and apply normalised XOR rules produced by the AST analyser.

Rules are plain JSON so a researcher can inspect or hand-edit them.  Applying
a rule is pure arithmetic; no game code or pickle data is executed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

MAX_RULE_FILE_BYTES = 4 * 1024 * 1024
MAX_KEY_BYTES = 8
MAX_KEY_INT = (1 << 64) - 1
MAX_CANDIDATE_PAIRS = 64

_TARGET_ALIASES = {
    "offset": "offset",
    "off": "offset",
    "start": "offset",
    "position": "offset",
    "pos": "offset",
    "size": "size",
    "length": "size",
    "len": "size",
    "dlen": "size",
    "data": "data",
    "payload": "data",
    "index": "index",
    "auto": "unknown",
    "unknown": "unknown",
    "key": "key",
}


class RuleError(ValueError):
    """Raised for malformed or unusable XOR rules."""


def _normalise_target(value: Any) -> str:
    target = str(value or "unknown").strip().casefold()
    return _TARGET_ALIASES.get(target, target if target in _TARGET_ALIASES.values() else "unknown")


def _parse_key(mapping: dict[str, Any]) -> int | bytes:
    bytes_hex = mapping.get("key_bytes_hex")
    if bytes_hex not in (None, ""):
        if not isinstance(bytes_hex, str):
            raise RuleError("key_bytes_hex 必须是十六进制字符串")
        try:
            key_bytes = bytes.fromhex(bytes_hex)
        except ValueError as exc:
            raise RuleError(f"key_bytes_hex 不是合法十六进制: {bytes_hex}") from exc
        if not 1 <= len(key_bytes) <= MAX_KEY_BYTES:
            raise RuleError(f"字节密钥长度必须在 1..{MAX_KEY_BYTES} 之间")
        return key_bytes

    raw_key = mapping.get("key_int")
    if raw_key is None:
        raw_key = mapping.get("xor_key")
    if raw_key is None:
        raw_key = mapping.get("key_hex")
    if raw_key is None:
        raise RuleError("规则缺少 XOR 密钥（key_int/xor_key/key_hex/key_bytes_hex）")

    if isinstance(raw_key, bool):
        raise RuleError("XOR 密钥不能是布尔值")
    if isinstance(raw_key, int):
        key = raw_key
    elif isinstance(raw_key, str):
        text = raw_key.strip()
        try:
            key = int(text, 0) if text.lower().startswith(("0x", "0o", "0b")) else int(text, 10)
        except ValueError as exc:
            raise RuleError(f"无法解析 XOR 密钥: {raw_key!r}") from exc
    else:
        raise RuleError(f"不支持的 XOR 密钥类型: {type(raw_key).__name__}")

    if key < 0 or key > MAX_KEY_INT:
        raise RuleError(f"XOR 密钥超出 0..2^64-1 范围: {key}")
    return key


@dataclass(frozen=True, slots=True)
class XorRule:
    """One XOR key and the field it applies to."""

    key: int | bytes
    target: str = "unknown"
    confidence: float = 1.0
    rule_id: str = "manual"
    line: int = 0
    expression: str = ""
    evidence: dict[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        if isinstance(self.key, bytes):
            if not 1 <= len(self.key) <= MAX_KEY_BYTES:
                raise RuleError("字节密钥长度非法")
        elif isinstance(self.key, bool) or not isinstance(self.key, int):
            raise RuleError("XOR 密钥必须是 int 或 bytes")
        elif self.key < 0 or self.key > MAX_KEY_INT:
            raise RuleError("整数密钥超出范围")

    @property
    def identity(self) -> tuple[str, Any, str]:
        """Hashable identity used for de-duplication."""

        if isinstance(self.key, bytes):
            return ("bytes", self.key, self.target)
        return ("int", self.key, self.target)

    @property
    def key_hex(self) -> str:
        """Human-readable key representation."""

        if isinstance(self.key, bytes):
            return "0x" + self.key.hex()
        return f"0x{self.key:X}"

    def apply_int(self, value: int, *, target: str | None = None) -> int:
        """XOR an integer offset/size with this rule's key.

        Byte keys are interpreted as a big-endian integer (at most eight
        bytes), matching the 32-bit fields used by RPA headers.
        """

        if not isinstance(value, int) or isinstance(value, bool):
            raise RuleError("只能对整数应用 XOR 规则")
        self._check_target(target)
        if isinstance(self.key, bytes):
            return value ^ int.from_bytes(self.key, "big")
        return value ^ self.key

    def apply_bytes(self, data: bytes, *, target: str | None = None) -> bytes:
        """XOR a byte string, repeating the key as needed."""

        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise RuleError("只能对 bytes-like 数据应用 XOR 规则")
        self._check_target(target)
        payload = bytes(data)
        if isinstance(self.key, bytes):
            key_bytes = self.key
        else:
            length = max(1, (self.key.bit_length() + 7) // 8)
            key_bytes = self.key.to_bytes(length, "big")
        if not key_bytes:
            raise RuleError("XOR 密钥不能为空")
        return bytes(byte ^ key_bytes[index % len(key_bytes)] for index, byte in enumerate(payload))

    def _check_target(self, requested: str | None) -> None:
        if requested is None:
            return
        requested_target = _normalise_target(requested)
        if self.target not in {requested_target, "unknown"}:
            raise RuleError(
                f"规则 {self.rule_id} 的目标是 {self.target}，不能应用到 {requested_target}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dictionary."""

        if isinstance(self.key, bytes):
            key_int = None
            key_bytes_hex = self.key.hex()
        else:
            key_int = self.key
            key_bytes_hex = None
        return {
            "id": self.rule_id,
            "target": self.target,
            "confidence": self.confidence,
            "line": self.line,
            "expression": self.expression,
            "key_int": key_int,
            "key_hex": self.key_hex,
            "key_bytes_hex": key_bytes_hex,
            "evidence": self.evidence,
        }

    @classmethod
    def from_mapping(cls, mapping: dict[str, Any]) -> "XorRule":
        """Build a rule from analyser JSON or a hand-written mapping."""

        if not isinstance(mapping, dict):
            raise RuleError("规则条目必须是 JSON 对象")
        key = _parse_key(mapping)
        target = _normalise_target(mapping.get("target", "unknown"))
        confidence_raw = mapping.get("confidence", 1.0)
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError) as exc:
            raise RuleError("confidence 必须是数字") from exc
        confidence = min(1.0, max(0.0, confidence))
        line_raw = mapping.get("line", 0)
        try:
            line = int(line_raw or 0)
        except (TypeError, ValueError):
            line = 0
        evidence = mapping.get("evidence") or {}
        if not isinstance(evidence, dict):
            evidence = {"raw": evidence}
        return cls(
            key=key,
            target=target,
            confidence=confidence,
            rule_id=str(mapping.get("id") or "manual"),
            line=max(0, line),
            expression=str(mapping.get("expression") or ""),
            evidence=evidence,
        )


class RuleSet:
    """Ordered collection of XOR rules with target-aware selection."""

    def __init__(self, rules: Iterable[XorRule] = (), *, source: str = "<memory>") -> None:
        self.source = source
        self.rules: tuple[XorRule, ...] = self._dedupe(rules)
        self._deduped: tuple[XorRule, ...] = self.rules

    @classmethod
    def empty(cls) -> "RuleSet":
        return cls(())

    @classmethod
    def from_mapping(cls, payload: Any, *, source: str = "<memory>") -> "RuleSet":
        """Load rules from an analyser report or a bare list."""

        if isinstance(payload, list):
            entries = payload
        elif isinstance(payload, dict):
            entries = payload.get("rules", [])
            if entries is None:
                entries = []
        else:
            raise RuleError("规则文件顶层必须是对象或数组")
        if not isinstance(entries, list):
            raise RuleError("rules 字段必须是数组")
        if len(entries) > 10_000:
            raise RuleError("规则数量超过 10000 上限")
        rules: list[XorRule] = []
        for entry in entries:
            try:
                rule = XorRule.from_mapping(entry)
            except RuleError as exc:
                logger.warning("跳过无效 XOR 规则: %s", exc)
                continue
            if rule.target == "key":
                # Key-derivation expressions are not field rules.
                continue
            rules.append(rule)
        return cls(rules, source=source)

    @classmethod
    def from_file(cls, path: str | Path) -> "RuleSet":
        """Read a JSON rule report with a small size ceiling."""

        rule_path = Path(path)
        try:
            size = rule_path.stat().st_size
        except OSError as exc:
            raise RuleError(f"无法读取规则文件 {rule_path}: {exc}") from exc
        if size > MAX_RULE_FILE_BYTES:
            raise RuleError(f"规则文件超过 {MAX_RULE_FILE_BYTES // (1024 * 1024)} MiB 上限")
        try:
            payload = json.loads(rule_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuleError(f"规则文件不是合法 JSON: {exc}") from exc
        return cls.from_mapping(payload, source=str(rule_path))

    @staticmethod
    def _dedupe(rules: Iterable[XorRule]) -> tuple[XorRule, ...]:
        merged: dict[tuple[str, Any, str], XorRule] = {}
        for rule in rules:
            existing = merged.get(rule.identity)
            if existing is None:
                merged[rule.identity] = rule
            elif rule.confidence > existing.confidence:
                merged[rule.identity] = XorRule(
                    key=existing.key,
                    target=existing.target,
                    confidence=rule.confidence,
                    rule_id=existing.rule_id,
                    line=existing.line,
                    expression=existing.expression,
                    evidence=existing.evidence,
                )
        return tuple(merged.values())

    @property
    def has_rules(self) -> bool:
        return bool(self._deduped)

    def _rules_for(self, target: str) -> list[XorRule]:
        requested = _normalise_target(target)
        exact = [rule for rule in self._deduped if rule.target == requested]
        wildcard = [rule for rule in self._deduped if rule.target == "unknown"]
        ordered = exact + wildcard
        ordered.sort(key=lambda rule: (-rule.confidence, rule.rule_id, rule.line))
        return ordered

    def candidate_keys(self, target: str) -> list[XorRule]:
        """Return rules usable for *target*, highest confidence first."""

        return self._rules_for(target)

    def candidate_pairs(self) -> list[tuple[XorRule, XorRule]]:
        """Return ``(offset_rule, size_rule)`` pairs to try against an index.

        Offset and size may use different keys in heavily modified archives.
        Identical keys are paired with themselves first by the confidence sort.
        """

        offset_rules = self._rules_for("offset")
        size_rules = self._rules_for("size")
        if not offset_rules and not size_rules:
            return []
        if not offset_rules:
            offset_rules = list(size_rules)
        if not size_rules:
            size_rules = list(offset_rules)
        pairs: list[tuple[XorRule, XorRule]] = []
        seen: set[tuple[tuple[str, Any, str], tuple[str, Any, str]]] = set()
        for offset_rule in offset_rules:
            for size_rule in size_rules:
                identity = (offset_rule.identity, size_rule.identity)
                if identity in seen:
                    continue
                seen.add(identity)
                pairs.append((offset_rule, size_rule))
        pairs.sort(
            key=lambda pair: (
                -(pair[0].confidence + pair[1].confidence),
                pair[0].rule_id,
                pair[1].rule_id,
            )
        )
        if len(pairs) > MAX_CANDIDATE_PAIRS:
            logger.warning("候选规则组合超过 %d 个，已截断", MAX_CANDIDATE_PAIRS)
            pairs = pairs[:MAX_CANDIDATE_PAIRS]
        return pairs

    def all_keys(self) -> list[XorRule]:
        """Return all distinct rules in confidence order."""

        ordered = list(self._deduped)
        ordered.sort(key=lambda rule: (-rule.confidence, rule.rule_id, rule.line))
        return ordered

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "rpa-loader-analyzer/rules-v1",
            "source": self.source,
            "rules": [rule.to_dict() for rule in self._deduped],
        }


def load_rules(path: str | Path) -> RuleSet:
    """Convenience wrapper around :meth:`RuleSet.from_file`."""

    return RuleSet.from_file(path)
