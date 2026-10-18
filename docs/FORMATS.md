# RPA 与 XOR 规则格式说明

本文档描述 `rpa-loader-analyzer` 当前实现的格式假设，便于审计和扩展。格式来自 Ren'Py
官方 RPA-2/3 布局及常见兼容变体；具体游戏可能魔改，工具不会盲目猜测。

## 1. 头部

文件第一行是 ASCII 文本，以换行结束。

### RPA-2.0

```text
RPA-2.0 <index_offset>
```

示例：

```text
RPA-2.0 0000000000000025
```

- `index_offset`：索引在文件中的绝对偏移，十六进制。
- 索引字段不做 XOR。

### RPA-3.0 / 3.2 / 4.0

```text
RPA-3.0 <index_offset> <key>
```

示例：

```text
RPA-3.0 0000000000000022 DEADBEEF
```

- `index_offset`：zlib 压缩 pickle 的绝对偏移，十六进制。
- `key`：32/64 位整数，用于对每个索引分段的 `offset` 和 `length` 做 XOR。
- 工具也接受 `RPA-3.2`、`RPA-4.0` 头部（按相同布局处理），并在报告中保留版本号。

## 2. 索引

`index_offset` 开始是 zlib 压缩的 pickle。解压后的顶层通常是：

```python
{
    "images/bg.png": [(offset, length, prefix)],
    "audio/theme.ogg": [(offset2, length2, prefix2)],
}
```

说明：

- 值可以是单个 `(offset, length[, prefix])` 元组，也可以是分段列表
  （`[(...), (...)]`）；多段按顺序拼接。
- `prefix` 可为 bytes、str 或省略。
- `length` **包含** `prefix` 的长度。磁盘上的实际数据长度为
  `length - len(prefix)`；导出内容为 `prefix + 归档数据`。
- 官方 RPA-3.0 中 `offset`/`length` 与头部 `key` 异或；RPA-2.0 不异或。

本工具的安全 pickle 读取器只接受基础容器/标量，因此索引中的自定义对象会导致
`RpaSecurityError`。这是有意为之。

## 3. offset/size 还原

对每个分段：

```text
real_offset = raw_offset ^ offset_key
real_length = raw_length ^ size_key
```

通常 `offset_key == size_key == header_key`。工具也支持：

- 头部 key 为诱饵，使用 AST 规则中的 offset/size key；
- offset 与 size 使用不同 key；
- 压缩索引本身被 XOR（显式 `index` 规则或 header key 回退）。

候选 key 的评分要求**全部**分段满足：

```text
0 <= real_offset
0 <= real_length
0 <= real_length - len(prefix)
real_offset + (real_length - len(prefix)) <= file_size
数据段不与索引区域重叠
```

只有全部合法才接受，否则输出友好错误。这样可以避免错误 key 导出随机数据。

## 4. 规则 JSON

AST 分析器输出：

```json
{
  "schema": "rpa-loader-analyzer/rules-v1",
  "source": "loader.pyo",
  "parse": {"ok": true, "mode": "python2-normalized"},
  "stats": {"xor_nodes": 4, "rule_count": 2},
  "rules": [
    {
      "id": "xor-0001",
      "target": "offset",
      "key_int": 3735928559,
      "key_hex": "0xDEADBEEF",
      "key_bytes_hex": null,
      "confidence": 0.85,
      "line": 42,
      "expression": "raw_offset ^ KEY",
      "occurrence_count": 1,
      "occurrences": [
        {
          "line": 42,
          "expression": "raw_offset ^ KEY",
          "assigned_to": ["offset"],
          "unknown_operands": ["raw_offset"],
          "key_sources": ["KEY", "literal"],
          "scope": "load_archive",
          "ambiguous_assignment": false
        }
      ]
    }
  ],
  "findings": [],
  "warnings": []
}
```

字段：

| 字段 | 含义 |
| --- | --- |
| `target` | `offset` / `size` / `data` / `unknown`；`key` 目标不生成规则 |
| `key_int` | 整数密钥（0..2^64-1） |
| `key_hex` | 整数密钥的十六进制展示 |
| `key_bytes_hex` | 字节密钥的十六进制（如 `1234`） |
| `confidence` | 静态置信度，仅用于候选排序，不是正确性保证 |
| `occurrences` | 相同 key+target 的源码证据集合 |
| `findings` | 所有 XOR 节点，包括未生成规则的节点，用于人工审计 |
| `warnings` | 解析、歧义赋值、未解析操作数等提示 |

### 规则应用语义

- 整数 key：`value ^ key`。
- 字节 key 用于整数 offset/size 时按大端解释为整数。
- 字节 key 用于字节数据时循环重复。
- 目标不匹配时 `XorRule.apply_*` 默认拒绝；RPA 候选选择器会在受控范围内绕过目标
  限制，以便尝试统一 key。

## 5. 反汇编回退

当 uncompyle6 无法恢复源码，或恢复的源码无法解析 AST 时，工具使用 xdis：

```bash
python -m src disassemble loader.pyo --output-dir output --print-text
python -m src analyze loader.pyo --asm-out output --rules-out output/rules.json
```

反汇编分析只识别以下模式：

- Python 2 / 早期 Python 3 的 `BINARY_XOR`、`INPLACE_XOR`
- Python 3.11+ 的 `BINARY_OP ... (^)`
- XOR 前 12 个字节范围内最近的 `LOAD_CONST` 整数/bytes
- 通过 `LOAD_CONST` + `STORE_*` 建立的简单变量名到常量映射
- 后续 `STORE_FAST/NAME/GLOBAL` 或最近 `LOAD_*` 名称推断 `offset`/`size`/`data`

输出报告的 `analysis` 为 `bytecode-disassembly`，规则 ID 形如 `asm-xor-0001`，
置信度通常低于 AST 规则。反汇编不能可靠恢复分支、循环和跨函数数据流，因此这些规则
必须经过人工核对。

## 6. 媒体转换

WebM -> MP4 转换器基于 `mp4_merger.py v4.5` 的策略重构：

- 仅输出 `.mp4`。
- 默认 H.264/AAC：CRF 16、AAC 320k、48 kHz 立体声、`+faststart`。
- 自动检测 NVENC / QSV / AMF，并用 1 秒测试源实际试编码验证。
- 显式指定的硬件编码器验证失败时回退 CPU；运行时失败再次回退 CPU。
- 源为 10-bit 时保留色深；否则统一 `yuv420p` 并修正奇数尺寸。
- `--copy` 可先尝试流复制；失败后仍会重编码。
- 其他选项：`--hardware auto|cpu|nvidia|intel|amd|vaapi`、`--crf`、
  `--audio-bitrate`、`--fast`、`--overwrite`、`--vaapi-device`。
- 输入和输出不能是同一路径；默认不覆盖输出。

## 7. 限制

- 无头部的 RPA-1.0 `.rpi` 不是当前 CLI 的目标格式。
- 索引中的自定义对象、集合、循环引用、极深嵌套会被安全读取器拒绝。
- 超大索引、超长字符串、zip/pickle 炸弹会被资源上限拒绝。
- 头部/索引结构未知时直接报错，不生成“可能正确”的导出。
