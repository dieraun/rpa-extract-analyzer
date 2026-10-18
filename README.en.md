# RPA Loader Analyzer 1.0.0

**English** | [简体中文](README.zh-CN.md) | [Project Home](README.md)

A local, read-only research toolkit for Ren'Py `loader.pyo` and RPA archives, with a
bilingual GUI, automatic update checking, and a Windows single-file executable.

> **Usage restriction**: intended only for technical study, compatibility research, and
> data recovery on files you are **authorized** to process. Do not use it to bypass access
> controls, crack commercial content, evade anti-cheat systems, or redistribute game assets.
> You are responsible for complying with local law, the game EULA, and the scope of your
> authorization. See [docs/COMPLIANCE.md](docs/COMPLIANCE.md).

## v1.0.0 Highlights

- **Help → About** shows developer `dieraun`, build version `1.0.0 (2026-10-17)`,
  MIT License, repository URL, and runtime versions.
- **Help → Check for Updates** compares the local version with GitHub Releases and tags.
  A check also runs at startup; network failures are logged without blocking the app.
- **WebM → MP4 conversion rebuilt from `mp4_merger.py v4.5`**: real GPU encode
  verification, NVENC/QSV/AMF/VAAPI support, automatic CPU fallback, CRF 16 / AAC 320k.
- **Single-file Windows EXE** and a `v1.0.0` GitHub tag are included.

## What It Does

`rpa-loader-analyzer` is a local GUI/CLI research tool that solves three problems:

1. Statically decompile `renpy/loader.pyo` to text with uncompyle6.
2. Extract XOR decryption constants with Python AST analysis and emit normalized JSON rules.
3. Parse RPA indexes **without `pickle.load`**, apply XOR rules to restore offset/size,
   and export resources to an independent directory.

It never imports, executes, or `eval`s recovered source, and it never uploads files.

## Modules

| Module | Responsibility |
| --- | --- |
| `src/scanner` | Recursively locate `renpy/loader.pyo`, collect all `*.rpa`, reject symlink escapes |
| `src/decompiler` | Read `.pyo` magic, run uncompyle6, fall back to static xdis disassembly |
| `src/ast_analyzer` | Walk `BitXor` nodes, track constants/aliases, analyze disassembly fallback |
| `src/rpa_core` | RPA headers, zlib index, safe pickle reader, XOR rule application |
| `src/exporter` | Rebuild directory trees, atomic writes, empty-file filtering, traversal protection |
| `src/media_convert` | mp4_merger v4.5 strategy: GPU verification, CPU fallback, H.264/AAC |

## Security Design

- **Read-only originals**: `.pyo`/`.rpa` files are opened only with `rb`; export targets
  are checked to be outside the game root.
- **No recovered-code execution**: no `exec`, `eval`, or importlib loading. Only text,
  `ast.parse`, and static xdis disassembly.
- **Static disassembly**: xdis reads bytecode/code objects and emits instruction text; it
  does not import or execute them.
- **No `pickle.load`**: the RPA index is interpreted from `pickletools.genops` with a
  narrow whitelist. `GLOBAL`/`REDUCE`/`BUILD`/`NEWOBJ` and other object-construction
  paths are rejected. Only two pure bytes construction patterns are recognized for
  Python 3 pickles: `_codecs.encode(str, encoding)` and `__builtin__ bytes`.
- **Resource ceilings**: input size, decompressed size, string length, container count,
  nesting depth, and node count are all bounded to resist zlib/pickle bombs.
- **Path traversal protection**: absolute paths, `..`, drive letters, Windows ADS colons,
  reserved device names, and linked directories are rejected.
- **Atomic export**: write a same-directory temporary file, then replace; existing files
  are not overwritten by default.
- **Local processing**: no upload path. FFmpeg is restricted to the `file` protocol.
- **Minimal external processes**: FFmpeg/ffprobe are launched only for an explicit
  `convert` action or GUI media button, using argument arrays and `shell=False`.
- **Version check**: the only outbound request is a fixed GitHub API HTTPS GET. It does
  not include local paths, game data, keys, or user identity.

See [docs/SECURITY.md](docs/SECURITY.md) for the threat model.

## Requirements

- Python 3.10+
- Python packages: `uncompyle6`, `xdis`
- Optional: local `ffmpeg` and `ffprobe` for media conversion only

> The bytecode versions supported by uncompyle6 are determined by `uncompyle6`/`xdis`.
> Ren'Py 7's Python 2.7 `loader.pyo` is the primary target. If source recovery fails,
> the tool disassembles with xdis and tries low-confidence XOR extraction. Unsupported
> versions return a structured error.

## Installation

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
python -m pip install -r requirements.txt
```

## Windows GUI / EXE

A single-file GUI is included:

```text
dist\RpaLoaderAnalyzer.exe
```

![RPA Loader Analyzer GUI](docs/gui.png)

Double-click to run. The GUI has three tabs:

1. **Scan & Analyze**: choose a game root, scan `renpy/loader.pyo` and RPA archives,
   generate `workspace\rules.json`.
2. **Export RPA**: use `rules.json` automatically when present, export all archives to
   `workspace\export\<archive-name>\`.
3. **Media Convert**: choose a WebM and output MP4; the mp4_merger v4.5 pipeline verifies
   NVIDIA / Intel / AMD encoders, falls back to CPU, and defaults to H.264/AAC
   (CRF 16 / 320k).

Use the language selector in the top-right to switch between **Simplified Chinese** and
**English**. The choice is saved to:

```text
%APPDATA%\rpa-loader-analyzer\settings.json
```

English interface:

![RPA Loader Analyzer GUI English](docs/gui-en.png)

### About and Update Check

**Help → About** (or `F1`) shows:

- Developer: `dieraun`
- Build: `1.0.0 (2026-10-17)`
- MIT License
- Repository URL
- Python / Tk runtime versions

**Help → Check for Updates** requests:

```text
https://api.github.com/repos/dieraun/rpa-extract-analyzer/releases/latest
```

It falls back to repository tags when no GitHub Release exists. The check runs once at
startup and only performs a GET. Offline/rate-limit/certificate failures are logged and
do not block startup. A newer version prompts to open the download page.

Rebuild the EXE:

```bash
python -m pip install -r requirements-build.txt
python -m PyInstaller --noconfirm --clean --onefile --windowed \
  --name RpaLoaderAnalyzer --paths . \
  --collect-all uncompyle6 --collect-all xdis \
  --hidden-import urllib.request --hidden-import urllib.error \
  --distpath dist --workpath build --specpath build gui_entry.py
```

Artifacts:

- `dist\RpaLoaderAnalyzer.exe`: windowed single-file application
- `build\`: PyInstaller intermediate files and warning reports

> One-file EXEs unpack runtime dependencies on first launch and may take a few seconds.
> Some antivirus products flag PyInstaller one-file builds; if needed, run from source in a
> trusted directory or build with `--onedir`.

## Git Repository

The project uses a `main` branch Git repository containing source, tests, docs,
screenshots, and the final EXE:

```bash
git status
git log --oneline
```

[.gitignore](.gitignore) excludes Python caches, virtual environments, `build/`, runtime
output, and temporary files. The final EXE is intentionally retained through an exception:

```text
dist/RpaLoaderAnalyzer.exe
```

## Quick Start

### 1. Scan a Game Directory

```bash
python -m src scan "D:\Games\MyGame"
```

Prints the loader path, all RPA archives, and warnings. Add `--json-out scan.json` for JSON.

### 2. Analyze the Loader

```bash
# Decompile .pyo and extract rules
python -m src analyze "D:\Games\MyGame\renpy\loader.pyo" \
  --source-out .\output \
  --asm-out .\output \
  --rules-out .\output\rules.json

# Analyze an existing loader.py directly
python -m src analyze "D:\Games\MyGame\renpy\loader.py" \
  --rules-out .\output\rules.json

# Disassembly only (when uncompyle6 fails or manual review is needed)
python -m src disassemble "D:\Games\MyGame\renpy\loader.pyo" \
  --output-dir .\output --print-text
```

`analyze` prefers uncompyle6 + AST. If decompilation or AST parsing fails, it automatically
uses xdis and emits `analysis: "bytecode-disassembly"` heuristic rules. Those rules have
lower confidence and must be reviewed before export.

Rule output is written outside the game directory.

### 3. List an RPA Index Without Exporting

```bash
python -m src list "D:\Games\MyGame\game\archive.rpa" \
  --rules .\output\rules.json
```

### 4. Export One RPA

```bash
python -m src export "D:\Games\MyGame\game\archive.rpa" \
  --game-root "D:\Games\MyGame" \
  --output "D:\Research\MyGame-export" \
  --rules .\output\rules.json \
  --report "D:\Research\MyGame-export-report.json"
```

The output directory must be outside the game root. Existing and empty files are skipped
by default; `--overwrite` enables replacement. `--stop-on-error` stops on the first failure.

### 5. Export All RPA Archives

```bash
python -m src export-all "D:\Games\MyGame" \
  --output "D:\Research\MyGame-export" \
  --rules .\output\rules.json \
  --report "D:\Research\MyGame-export-report.json"
```

Each archive is exported to `output/<archive-name>/`.

### 6. WebM to MP4

The converter is rebuilt from `mp4_merger.py v4.5`:

- H.264/AAC by default: CRF 16, AAC 320k, faststart
- Verify each CPU/GPU encoder with a one-second test encode
- NVIDIA NVENC, Intel QSV, AMD AMF, VAAPI, and CPU
- Automatic fallback to CPU on validation or runtime failure
- 8-bit 4:2:0 unless the source is genuinely 10-bit
- Optional stream-copy fast path with automatic transcode fallback

```bash
# Auto-select a hardware encoder, then fall back to CPU if needed
python -m src convert ".\output\video.webm" ".\output\video.mp4"

# Force CPU
python -m src convert ".\output\video.webm" ".\output\video.mp4" --hardware cpu

# NVIDIA / Intel / AMD
python -m src convert ".\output\video.webm" ".\output\video.mp4" --hardware nvidia
python -m src convert ".\output\video.webm" ".\output\video.mp4" --hardware intel
python -m src convert ".\output\video.webm" ".\output\video.mp4" --hardware amd

# Try a fast stream copy first
python -m src convert ".\output\video.webm" ".\output\video.mp4" --copy

# Faster encoding preset (slight quality/size tradeoff)
python -m src convert ".\output\video.webm" ".\output\video.mp4" --fast
```

If FFmpeg is missing, the command reports a friendly error. Encoder verification,
stream-copy failures, and CPU fallback reasons are recorded in the result's `attempts` field.

## Rule JSON Format

```json
{
  "schema": "rpa-loader-analyzer/rules-v1",
  "source": "loader.pyo",
  "parse": {"ok": true, "mode": "python2-normalized"},
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
      "occurrences": [
        {
          "assigned_to": ["offset"],
          "key_sources": ["KEY", "literal"],
          "scope": "load_archive"
        }
      ]
    }
  ],
  "findings": []
}
```

- `target`: `offset`, `size`, `data`, or `unknown` (the parser tries to match).
- Integer keys use `key_int`/`key_hex`; byte keys use `key_bytes_hex`.
- Rules are merged by key + target while retaining every `occurrences` entry.
- Unresolved keys remain in `findings`; no unreliable `rules` are emitted.

## RPA Format Support

- `RPA-2.0 <index-offset>`: no index XOR.
- `RPA-3.0 <index-offset> <key>`, compatible with `RPA-3.2` and `RPA-4.0`:
  offset/length are XORed with the header key.
- The index is zlib-compressed restricted pickle; values support
  `(offset, length[, prefix])` and multi-part lists.
- `length` includes the synthetic index `prefix`; stored archive length is
  `length - len(prefix)`. Export writes the prefix first, then archive data.
- When the header key is invalid, AST rules are tried as candidates. The parser accepts a
  candidate only when all offset/length values are in range, including variants where the
  compressed index itself is XORed.
- Conflicting header keys and static rules produce a structured error instead of exporting
  suspicious data.

See [docs/FORMATS.md](docs/FORMATS.md) for details.

## Project Layout

```text
rpa-loader-analyzer/
├── src/
│   ├── __main__.py
│   ├── cli.py
│   ├── gui.py
│   ├── i18n.py
│   ├── build_info.py
│   ├── update_checker.py
│   ├── scanner/
│   ├── decompiler/
│   ├── ast_analyzer/
│   ├── rpa_core/
│   ├── exporter/
│   └── media_convert/
├── gui_entry.py
├── tests/
├── docs/
│   ├── SECURITY.md
│   ├── COMPLIANCE.md
│   ├── FORMATS.md
│   ├── gui.png
│   └── gui-en.png
├── dist/
│   └── RpaLoaderAnalyzer.exe
├── README.md
├── README.en.md
├── README.zh-CN.md
├── LICENSE
├── .gitignore
├── .gitattributes
├── requirements.txt
└── requirements-build.txt
```

## Tests

```bash
python -m unittest discover -s tests -v
```

Coverage includes scanning, bytecode headers, uncompyle6 integration wrappers, xdis
disassembly, Python 2 normalization, XOR data flow and disassembly fallback, malicious
pickle rejection, RPA-2/3/3.2/4 parsing, rule handling, path traversal, atomic export,
GUI About/update checks, bilingual switching and settings persistence, GPU verification and
CPU fallback, FFmpeg command construction, and CLI end-to-end flows. FFmpeg unit tests use
mocks; if local `ffmpeg`/`ffprobe` are available, an integration test generates a short
video and performs a real WebM → MP4 conversion (skipped when codecs are missing). Update
checker tests use a fake opener and do not access the real network.

## FAQ

**uncompyle6 reports “Unsupported Python version”**
The bytecode version is outside uncompyle6's range. The tool automatically falls back to
xdis disassembly. You can also switch to a `uncompyle6`/`xdis` version that supports it,
or analyze an existing `loader.py`.

**Decompilation is empty or control flow is corrupted**
The loader may be heavily obfuscated. The tool returns `status=failed` with diagnostics
instead of emitting broken source, then tries disassembly. Use `disassemble` to inspect the
instruction text manually.

**Are disassembly rules reliable?**
Disassembly only sees constants near opcodes and cannot fully recover variable scope, so
confidence is low. Inspect `BINARY_XOR` / `BINARY_OP (^)` context with `disassemble` and
correct the rule JSON manually when needed.

**The parser cannot find valid offset/size values**
Run `analyze` first and pass the result with `--rules`. If parsing still fails, the key
derivation or index layout differs from the supported formats; the tool will not export
guessed data.

**The output directory is rejected**
This is intentional. Resource output must be outside the game root; use `--game-root` to set
the boundary explicitly.

## Compliance and Disclaimer

This project does not provide game content, keys, or cracking services, and does not
guarantee compatibility with any specific game. You must process only authorized files,
follow the game EULA and local law, avoid redistributing extracted assets, and avoid using
the tool to circumvent technical protection measures or infringe copyright. The authors are
not responsible for misuse.

- [Security model](docs/SECURITY.md)
- [Compliance and disclaimer](docs/COMPLIANCE.md)
- [RPA and rule formats](docs/FORMATS.md)
- [MIT License](LICENSE)

## License

The source code is released under the **MIT License**. See [LICENSE](LICENSE).

Copyright (c) 2026 dieraun

> The MIT license covers this repository's source code and documentation only. It does not
> cover game content, game assets, or third-party components (uncompyle6, xdis, FFmpeg,
> PyInstaller, etc.), and does not grant rights to any game content.
