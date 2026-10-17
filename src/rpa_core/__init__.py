"""Safe RPA parsing, XOR rule handling and defensive pickle decoding."""

from __future__ import annotations

from .rpa_parser import (
    RpaArchive,
    RpaEntry,
    RpaError,
    RpaFormatError,
    RpaLimits,
    RpaPart,
    RpaReadError,
    RpaSecurityError,
    normalize_archive_name,
    parse_archive,
)
from .rule_applier import RuleError, RuleSet, XorRule, load_rules
from .safe_pickle_reader import (
    PickleFormatError,
    PickleLimitError,
    PickleLimits,
    SafePickleError,
    UnsafePickleError,
    safe_pickle_loads,
)

__all__ = [
    "PickleFormatError",
    "PickleLimitError",
    "PickleLimits",
    "RpaArchive",
    "RpaEntry",
    "RpaError",
    "RpaFormatError",
    "RpaLimits",
    "RpaPart",
    "RpaReadError",
    "RpaSecurityError",
    "RuleError",
    "RuleSet",
    "SafePickleError",
    "UnsafePickleError",
    "XorRule",
    "load_rules",
    "normalize_archive_name",
    "parse_archive",
    "safe_pickle_loads",
]
