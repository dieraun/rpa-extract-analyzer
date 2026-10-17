"""A deliberately small, defensive pickle reader built on ``pickletools``.

The standard :func:`pickle.loads` can execute arbitrary code through
``GLOBAL``/``REDUCE`` opcodes.  RPA indices only need dictionaries, tuples,
lists and primitive scalars, so this module interprets the opcode stream
itself and rejects every opcode that could construct or call an object.

This is **not** a general-purpose unpickler.  Only the following value types
can ever leave :func:`safe_pickle_loads`: ``None``, ``bool``, ``int``,
``float``, ``str``, ``bytes``, ``tuple``, ``list`` and ``dict``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pickletools

logger = logging.getLogger(__name__)

_MARK = object()


class SafePickleError(ValueError):
    """Base class for safe pickle reader failures."""


class UnsafePickleError(SafePickleError):
    """Raised when a forbidden object-construction opcode is encountered."""


class PickleLimitError(SafePickleError):
    """Raised when a resource limit is exceeded."""


class PickleFormatError(SafePickleError):
    """Raised when the pickle byte stream is malformed."""


@dataclass(frozen=True, slots=True)
class PickleLimits:
    """Resource ceilings for untrusted index payloads."""

    max_input_bytes: int = 64 * 1024 * 1024
    max_string_bytes: int = 16 * 1024 * 1024
    max_total_bytes: int = 128 * 1024 * 1024
    max_container_items: int = 1_000_000
    max_stack_items: int = 200_000
    max_memo_items: int = 200_000
    max_structure_depth: int = 80
    max_nodes: int = 1_000_000


# Opcode names that could import, call or mutate arbitrary objects.  Keeping
# an explicit deny-list gives clearer diagnostics; the dispatcher also rejects
# unknown opcodes by default.
_FORBIDDEN_OPCODES = {
    "STACK_GLOBAL",
    "BUILD",
    "INST",
    "OBJ",
    "NEWOBJ",
    "NEWOBJ_EX",
    "PERSID",
    "BINPERSID",
    "EXT1",
    "EXT2",
    "EXT4",
    "NEXT_BUFFER",
}

# Python 3 pickles encode ``bytes`` with a narrow ``_codecs.encode``
# global+reduce pattern, even at protocol 2.  Modern RPA indices may contain
# byte prefixes, so this one pure builtin is recognised structurally.  It is
# never imported or called through Python: the two arguments are validated and
# the bytes are produced with ``str.encode``.
_SAFE_CODEC_GLOBALS = {
    "_codecs encode",
    "codecs encode",
    "__builtin__ bytes",
    "builtins bytes",
}
_SAFE_ENCODINGS = {"latin1", "latin-1", "utf8", "utf-8", "ascii"}


@dataclass(frozen=True, slots=True)
class _SafeCallable:
    """Marker for the one allowed GLOBAL; not a callable object."""

    name: str


def _check_limit(name: str, value: int, limit: int) -> None:
    if value > limit:
        raise PickleLimitError(f"pickle 超出 {name} 限制: {value} > {limit}")


class _Reader:
    """Single-pass interpreter for the allowed pickle opcodes."""

    def __init__(self, data: bytes, limits: PickleLimits) -> None:
        self.data = data
        self.limits = limits
        self.stack: list[Any] = []
        self.meta: list[Any] = []
        self.memo: dict[int, Any] = {}
        self.total_string_bytes = 0
        self.total_container_items = 0
        self.protocol: int | None = None

    def run(self) -> Any:
        _check_limit("输入字节数", len(self.data), self.limits.max_input_bytes)
        if not self.data:
            raise PickleFormatError("pickle 数据为空")
        try:
            for opcode, argument, position in pickletools.genops(self.data):
                self._execute(opcode.name, argument, position)
                _check_limit("栈元素数", len(self.stack), self.limits.max_stack_items)
                _check_limit("memo 元素数", len(self.memo), self.limits.max_memo_items)
                _check_limit(
                    "容器元素总数",
                    self.total_container_items,
                    self.limits.max_container_items,
                )
        except (UnsafePickleError, PickleLimitError, PickleFormatError):
            raise
        except (ValueError, EOFError, TypeError, OverflowError) as exc:
            raise PickleFormatError(f"pickle 格式错误: {exc}") from exc
        raise PickleFormatError("pickle 没有 STOP 结束标记")

    def _execute(self, name: str, argument: Any, position: int | None) -> None:
        if name in _FORBIDDEN_OPCODES:
            raise UnsafePickleError(
                f"拒绝危险 pickle 操作码 {name}（偏移 {position}）；"
                "RPA 索引不允许构造对象或调用函数"
            )

        if name == "PROTO":
            if not isinstance(argument, int) or not 0 <= argument <= 5:
                raise PickleFormatError(f"不支持的 pickle 协议版本: {argument!r}")
            self.protocol = argument
        elif name == "FRAME":
            if not isinstance(argument, int) or argument < 0:
                raise PickleFormatError("非法 FRAME 长度")
            _check_limit("FRAME 长度", argument, self.limits.max_input_bytes)
        elif name == "STOP":
            if len(self.stack) != 1 or any(item is _MARK for item in self.stack):
                raise PickleFormatError(
                    f"STOP 时栈状态异常（栈大小 {len(self.stack)}）"
                )
            value = self.stack[0]
            self._validate_result(value)
            raise _Stop(value)
        elif name == "MARK":
            self.stack.append(_MARK)
        elif name == "GLOBAL":
            # Reject every import except the pure bytes-codec helper used by
            # Python 3's pickle implementation.
            if not isinstance(argument, str) or argument not in _SAFE_CODEC_GLOBALS:
                raise UnsafePickleError(
                    f"拒绝导入 pickle 全局对象 {argument!r}（偏移 {position}）"
                )
            self.stack.append(_SafeCallable(argument))
        elif name == "REDUCE":
            self._reduce(position)
        elif name == "POP":
            self._pop_value()
        elif name == "POP_MARK":
            self._pop_to_mark()
        elif name == "DUP":
            value = self._top_value()
            self.stack.append(value)
        elif name == "NONE":
            self.stack.append(None)
        elif name == "NEWTRUE":
            self.stack.append(True)
        elif name == "NEWFALSE":
            self.stack.append(False)
        elif name in {"INT", "BININT", "BININT1", "BININT2", "LONG", "LONG1", "LONG4"}:
            if isinstance(argument, bool):
                argument = int(argument)
            if not isinstance(argument, int):
                raise PickleFormatError(f"{name} 参数不是整数")
            self.stack.append(argument)
        elif name in {"FLOAT", "BINFLOAT"}:
            if not isinstance(argument, float):
                try:
                    argument = float(argument)
                except (TypeError, ValueError) as exc:
                    raise PickleFormatError(f"{name} 参数不是浮点数") from exc
            self.stack.append(argument)
        elif name in {
            "STRING",
            "BINSTRING",
            "SHORT_BINSTRING",
            "UNICODE",
            "BINUNICODE",
            "SHORT_BINUNICODE",
            "BINUNICODE8",
        }:
            self._push_string(name, argument)
        elif name in {
            "BINBYTES",
            "SHORT_BINBYTES",
            "BINBYTES8",
            "BYTEARRAY8",
        }:
            if not isinstance(argument, (bytes, bytearray)):
                raise PickleFormatError(f"{name} 参数不是字节串")
            value = bytes(argument)
            self._account_string(len(value))
            self.stack.append(value)
        elif name == "EMPTY_LIST":
            self.stack.append([])
        elif name == "EMPTY_DICT":
            self.stack.append({})
        elif name == "EMPTY_TUPLE":
            self.stack.append(())
        elif name == "EMPTY_SET":
            raise UnsafePickleError("RPA 索引不允许 set，拒绝 EMPTY_SET")
        elif name in {"FROZENSET", "ADDITEMS"}:
            raise UnsafePickleError(f"RPA 索引不允许集合操作，拒绝 {name}")
        elif name == "TUPLE":
            items = self._pop_to_mark()
            self._account_items(len(items))
            self.stack.append(tuple(items))
        elif name in {"TUPLE1", "TUPLE2", "TUPLE3"}:
            count = int(name[-1])
            items = [self._pop_value() for _ in range(count)]
            items.reverse()
            self._account_items(count)
            self.stack.append(tuple(items))
        elif name == "LIST":
            items = self._pop_to_mark()
            self._account_items(len(items))
            self.stack.append(list(items))
        elif name == "DICT":
            items = self._pop_to_mark()
            self._account_items(len(items) // 2)
            if len(items) % 2:
                raise PickleFormatError("DICT 的键值数量为奇数")
            result = self._build_dict(items)
            self.stack.append(result)
        elif name == "SETITEM":
            value = self._pop_value()
            key = self._pop_value()
            target = self._top_value()
            if not isinstance(target, dict):
                raise PickleFormatError("SETITEM 的目标不是 dict")
            self._account_items(1)
            target[self._valid_key(key)] = value
        elif name == "SETITEMS":
            items = self._pop_to_mark()
            target = self._top_value()
            if not isinstance(target, dict):
                raise PickleFormatError("SETITEMS 的目标不是 dict")
            if len(items) % 2:
                raise PickleFormatError("SETITEMS 的键值数量为奇数")
            self._account_items(len(items) // 2)
            for index in range(0, len(items), 2):
                target[self._valid_key(items[index])] = items[index + 1]
        elif name == "APPEND":
            value = self._pop_value()
            target = self._top_value()
            if not isinstance(target, list):
                raise PickleFormatError("APPEND 的目标不是 list")
            self._account_items(1)
            target.append(value)
        elif name == "APPENDS":
            items = self._pop_to_mark()
            target = self._top_value()
            if not isinstance(target, list):
                raise PickleFormatError("APPENDS 的目标不是 list")
            self._account_items(len(items))
            target.extend(items)
        elif name in {"PUT", "BINPUT", "LONG_BINPUT"}:
            if not isinstance(argument, int) or argument < 0:
                raise PickleFormatError(f"{name} 的 memo 索引非法")
            _check_limit("memo 索引", argument, self.limits.max_memo_items)
            self.memo[argument] = self._top_value()
        elif name == "MEMOIZE":
            index = len(self.memo)
            _check_limit("memo 索引", index, self.limits.max_memo_items)
            self.memo[index] = self._top_value()
        elif name in {"GET", "BINGET", "LONG_BINGET"}:
            if not isinstance(argument, int) or argument < 0 or argument not in self.memo:
                raise PickleFormatError(f"{name} 引用了不存在的 memo 索引")
            self.stack.append(self.memo[argument])
        elif name == "READONLY_BUFFER":
            if not isinstance(self._top_value(), bytes):
                raise PickleFormatError("READONLY_BUFFER 的目标不是 bytes")
        else:
            raise UnsafePickleError(
                f"拒绝未列入白名单的 pickle 操作码 {name}（偏移 {position}）"
            )

    def _push_string(self, name: str, argument: Any) -> None:
        if name in {"STRING", "UNICODE", "BINUNICODE", "SHORT_BINUNICODE", "BINUNICODE8"}:
            if isinstance(argument, bytes):
                # pickletools normally decodes STRING/UNICODE already; this
                # branch is for older Python runtimes.
                value: str | bytes = argument.decode("latin-1")
            elif isinstance(argument, str):
                value = argument
            else:
                raise PickleFormatError(f"{name} 参数不是字符串")
        else:  # Python 2 byte strings
            if isinstance(argument, str):
                value = argument.encode("latin-1", "surrogateescape")
            elif isinstance(argument, bytes):
                value = argument
            else:
                raise PickleFormatError(f"{name} 参数不是字节串")
        length = len(value.encode("utf-8", "surrogatepass")) if isinstance(value, str) else len(value)
        self._account_string(length)
        self.stack.append(value)

    def _account_string(self, length: int) -> None:
        _check_limit("单字符串字节数", length, self.limits.max_string_bytes)
        self.total_string_bytes += length
        _check_limit("解码总字节数", self.total_string_bytes, self.limits.max_total_bytes)

    def _reduce(self, position: int | None) -> None:
        """Handle the permitted pure byte-string constructors.

        Python 3 pickles empty ``bytes`` as ``__builtin__ bytes`` and non-empty
        bytes as ``_codecs.encode(str, encoding)``.  Both are reconstructed
        with ordinary string operations; no import or call takes place.
        """

        arguments = self._pop_value()
        function = self._pop_value()
        if not isinstance(function, _SafeCallable):
            raise UnsafePickleError(
                f"拒绝在 pickle REDUCE 中调用非白名单对象（偏移 {position}）"
            )
        if not isinstance(arguments, tuple):
            raise UnsafePickleError("白名单 bytes 构造器的参数必须是元组")

        if function.name.endswith(" bytes"):
            if not arguments:
                value = b""
            elif (
                len(arguments) == 2
                and isinstance(arguments[0], str)
                and isinstance(arguments[1], str)
                and arguments[1].casefold().replace("_", "-") in _SAFE_ENCODINGS
            ):
                try:
                    value = arguments[0].encode(
                        arguments[1].casefold().replace("_", "-")
                    )
                except (UnicodeError, LookupError) as exc:
                    raise PickleFormatError(f"bytes 构造失败: {exc}") from exc
            elif (
                len(arguments) == 1
                and isinstance(arguments[0], (list, tuple))
                and all(
                    isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 255
                    for item in arguments[0]
                )
            ):
                value = bytes(arguments[0])
            else:
                raise UnsafePickleError("bytes 构造器参数不在白名单内")
        else:  # _codecs.encode / codecs.encode
            if len(arguments) != 2:
                raise UnsafePickleError("_codecs.encode 的参数必须是两个值的元组")
            text, encoding = arguments
            if not isinstance(text, str) or not isinstance(encoding, str):
                raise UnsafePickleError("_codecs.encode 只接受 (str, str)")
            normalised = encoding.casefold().replace("_", "-")
            if normalised not in _SAFE_ENCODINGS:
                raise UnsafePickleError(f"拒绝非白名单编码: {encoding!r}")
            try:
                value = text.encode(normalised)
            except (UnicodeError, LookupError) as exc:
                raise PickleFormatError(f"_codecs.encode 解码失败: {exc}") from exc
        self._account_string(len(value))
        self.stack.append(value)

    def _account_items(self, count: int) -> None:
        self.total_container_items += max(0, count)

    def _top_value(self) -> Any:
        if not self.stack or self.stack[-1] is _MARK:
            raise PickleFormatError("pickle 栈为空或栈顶是 MARK")
        return self.stack[-1]

    def _pop_value(self) -> Any:
        value = self._top_value()
        self.stack.pop()
        return value

    def _pop_to_mark(self) -> list[Any]:
        items: list[Any] = []
        while self.stack:
            value = self.stack.pop()
            if value is _MARK:
                items.reverse()
                return items
            items.append(value)
        raise PickleFormatError("pickle 缺少 MARK 标记")

    def _valid_key(self, key: Any) -> Any:
        if not _is_allowed_key(key, depth=0):
            raise UnsafePickleError(f"拒绝非基础类型或不可哈希的 pickle 字典键: {type(key).__name__}")
        try:
            hash(key)
        except TypeError as exc:
            raise UnsafePickleError("pickle 字典键不可哈希") from exc
        return key

    def _build_dict(self, items: list[Any]) -> dict[Any, Any]:
        result: dict[Any, Any] = {}
        for index in range(0, len(items), 2):
            key = self._valid_key(items[index])
            result[key] = items[index + 1]
        return result

    def _validate_result(self, value: Any) -> None:
        """Reject cycles and enforce depth/node limits after construction."""

        visiting: set[int] = set()
        visited: set[int] = set()
        nodes = 0

        def walk(item: Any, depth: int) -> None:
            nonlocal nodes
            nodes += 1
            _check_limit("结构节点数", nodes, self.limits.max_nodes)
            if isinstance(item, (str, bytes, int, float, bool)) or item is None:
                return
            if depth > self.limits.max_structure_depth:
                raise PickleLimitError("pickle 结构嵌套过深")
            if isinstance(item, (list, tuple, dict)):
                identity = id(item)
                if identity in visiting:
                    raise UnsafePickleError("pickle 包含循环引用，已拒绝")
                if identity in visited:
                    return
                visiting.add(identity)
                try:
                    if isinstance(item, dict):
                        for key, child in item.items():
                            walk(key, depth + 1)
                            walk(child, depth + 1)
                    else:
                        for child in item:
                            walk(child, depth + 1)
                finally:
                    visiting.discard(identity)
                visited.add(identity)
                return
            raise UnsafePickleError(
                f"pickle 结果包含不允许的类型: {type(item).__name__}"
            )

        walk(value, 0)


class _Stop(Exception):
    """Internal control flow raised on STOP; never escapes the reader."""

    def __init__(self, value: Any) -> None:
        super().__init__()
        self.value = value


def _is_allowed_key(value: Any, depth: int) -> bool:
    if depth > 20:
        return False
    if value is None or isinstance(value, (str, bytes, int, float, bool)):
        return True
    if isinstance(value, tuple):
        return all(_is_allowed_key(item, depth + 1) for item in value)
    return False


def safe_pickle_loads(data: bytes, *, limits: PickleLimits | None = None) -> Any:
    """Safely parse a restricted pickle payload.

    Args:
        data: Raw pickle bytes (an RPA index after zlib decompression).
        limits: Optional resource ceilings.

    Returns:
        The parsed primitive/container graph.

    Raises:
        UnsafePickleError: A forbidden opcode or value type was found.
        PickleLimitError: A resource ceiling was exceeded.
        PickleFormatError: The stream is malformed or has no ``STOP``.
    """

    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("safe_pickle_loads 只接受 bytes-like 数据")
    effective_limits = limits or PickleLimits()
    reader = _Reader(bytes(data), effective_limits)
    try:
        value = reader.run()
    except _Stop as stop:
        logger.debug(
            "安全 pickle 解析完成: %d 字节, 栈=%d memo=%d",
            len(data),
            len(reader.stack),
            len(reader.memo),
        )
        return stop.value
    # ``run`` always either raises _Stop or raises a SafePickleError.
    raise PickleFormatError("pickle 解析未正常结束")
