"""Atomic text/JSON writes and path-containment checks.

Writers create the temporary file in the destination directory and replace the
target only after the complete payload has been flushed, so a failed write does
not leave a truncated report or settings file.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any


def is_within(root: str | Path, candidate: str | Path) -> bool:
    """Return whether *candidate* resolves to *root* or one of its children."""

    try:
        root_path = Path(root).resolve(strict=False)
        candidate_path = Path(candidate).resolve(strict=False)
        return candidate_path == root_path or candidate_path.is_relative_to(root_path)
    except (OSError, ValueError):
        return False


def atomic_write_text(
    path: str | Path,
    text: str,
    *,
    encoding: str = "utf-8",
    newline: str | None = None,
    overwrite: bool = True,
) -> Path:
    """Write *text* atomically and return the final path.

    With ``overwrite=False``, both the initial and final existence checks raise
    :class:`FileExistsError` rather than replacing an existing target.
    """

    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not overwrite and target.exists():
        raise FileExistsError(f"输出文件已存在（如需覆盖请显式允许）: {target}")
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding=encoding, newline=newline) as handle:
            handle.write(text)
        if not overwrite and target.exists():
            raise FileExistsError(f"输出文件已存在: {target}")
        os.replace(temporary, target)
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
    return target


def atomic_write_json(
    path: str | Path,
    payload: Any,
    *,
    encoding: str = "utf-8",
    indent: int = 2,
    trailing_newline: bool = True,
) -> Path:
    """Serialise *payload* as UTF-8 JSON and write it atomically."""

    text = json.dumps(payload, ensure_ascii=False, indent=indent)
    if trailing_newline:
        text += "\n"
    return atomic_write_text(path, text, encoding=encoding)
