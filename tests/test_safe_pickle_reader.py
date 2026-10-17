"""Tests for the defensive, opcode-whitelisted pickle reader."""

from __future__ import annotations

import pickle
import unittest

from src.rpa_core.safe_pickle_reader import (
    PickleFormatError,
    PickleLimitError,
    PickleLimits,
    SafePickleError,
    UnsafePickleError,
    safe_pickle_loads,
)


class SafePickleReaderTests(unittest.TestCase):
    def test_roundtrips_basic_structures_across_protocols(self) -> None:
        payload = {
            "index": {
                "images/bg.png": (1024, 2048, "prefix"),
                "audio/theme.ogg": [1, 2, 3],
            },
            "count": 2,
            "active": True,
            "nothing": None,
        }
        for protocol in range(3):
            with self.subTest(protocol=protocol):
                parsed = safe_pickle_loads(pickle.dumps(payload, protocol=protocol))
                self.assertEqual(parsed, payload)

    def test_roundtrips_bytes_on_modern_protocols(self) -> None:
        payload = {"name": "test", "prefix": b"\x00\x01\xff"}
        for protocol in range(6):
            with self.subTest(protocol=protocol):
                parsed = safe_pickle_loads(pickle.dumps(payload, protocol=protocol))
                self.assertEqual(parsed, payload)

    def test_global_opcode_is_rejected(self) -> None:
        malicious = b"cbuiltins\neval\n(S'1+1'\ntR."
        with self.assertRaises(UnsafePickleError):
            safe_pickle_loads(malicious)

    def test_stack_global_is_rejected(self) -> None:
        # Protocol 4 STACK_GLOBAL with two memoized strings.
        malicious = b"\x80\x04\x8c\x08builtins\x94\x8c\x04eval\x94\x93."
        with self.assertRaises(UnsafePickleError):
            safe_pickle_loads(malicious)

    def test_reduce_from_custom_object_is_rejected(self) -> None:
        class Exploit:
            def __reduce__(self):
                return (eval, ("1+1",))

        with self.assertRaises(UnsafePickleError):
            safe_pickle_loads(pickle.dumps(Exploit(), protocol=4))

    def test_build_and_newobj_are_rejected(self) -> None:
        for payload in (
            b"}\x94b.",  # EMPTY_DICT MEMOIZE BUILD
            b"\x80\x04cbuiltins\nobject\n)\x81.",  # GLOBAL EMPTY_TUPLE NEWOBJ
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(UnsafePickleError):
                    safe_pickle_loads(payload)

    def test_malformed_input_raises_format_error(self) -> None:
        for payload in (b"", b"not a pickle", b"}\x94"):
            with self.subTest(payload=payload):
                with self.assertRaises(PickleFormatError):
                    safe_pickle_loads(payload)

    def test_non_bytes_input_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            safe_pickle_loads("not bytes")  # type: ignore[arg-type]

    def test_input_size_limit(self) -> None:
        limits = PickleLimits(max_input_bytes=8)
        with self.assertRaises(PickleLimitError):
            safe_pickle_loads(pickle.dumps({"key": "value"}), limits=limits)

    def test_string_size_limit(self) -> None:
        limits = PickleLimits(max_string_bytes=4)
        with self.assertRaises(PickleLimitError):
            safe_pickle_loads(pickle.dumps("abcdef", protocol=4), limits=limits)

    def test_recursive_structure_is_rejected(self) -> None:
        recursive: list[object] = []
        recursive.append(recursive)
        data = pickle.dumps(recursive, protocol=4)
        with self.assertRaises((UnsafePickleError, PickleLimitError)):
            safe_pickle_loads(data)

    def test_too_deep_structure_is_rejected(self) -> None:
        nested: object = []
        current = nested
        for _ in range(30):
            child: list[object] = []
            current.append(child)  # type: ignore[attr-defined]
            current = child
        limits = PickleLimits(max_structure_depth=10)
        with self.assertRaises(PickleLimitError):
            safe_pickle_loads(pickle.dumps(nested, protocol=4), limits=limits)

    def test_mark_based_tuple_order_is_preserved(self) -> None:
        payload = ("a", "b", "c")
        parsed = safe_pickle_loads(pickle.dumps(payload, protocol=2))
        self.assertEqual(parsed, payload)

    def test_memo_aliases_are_supported(self) -> None:
        shared = {"x": 1}
        payload = [shared, shared]
        parsed = safe_pickle_loads(pickle.dumps(payload, protocol=4))
        self.assertEqual(parsed, payload)
        self.assertIs(parsed[0], parsed[1])

    def test_safe_error_base_class(self) -> None:
        self.assertTrue(issubclass(UnsafePickleError, SafePickleError))
        self.assertTrue(issubclass(PickleLimitError, SafePickleError))
        self.assertTrue(issubclass(PickleFormatError, SafePickleError))


if __name__ == "__main__":
    unittest.main()
