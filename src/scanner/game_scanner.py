"""Recursively locate ``renpy/loader.pyo`` and ``*.rpa`` files.

The scanner is deliberately read-only.  Symbolic links and Windows junctions
are not followed, and every discovered file is checked to make sure its real
path is still inside the requested game root.  This prevents a crafted game
directory from making the exporter read outside the authorised tree.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

DEFAULT_MAX_DEPTH = 32
DEFAULT_MAX_ENTRIES = 200_000


class GameScanError(RuntimeError):
    """Raised when the game root cannot be scanned safely."""


@dataclass(frozen=True, slots=True)
class GameLayout:
    """Result of scanning one game directory.

    Attributes:
        root: Canonical game root.
        loader: Best candidate for ``renpy/loader.pyo`` (``None`` if absent).
        archives: All discovered RPA archives, sorted deterministically.
        warnings: Non-fatal problems encountered while walking the tree.
    """

    root: Path
    loader: Path | None
    archives: tuple[Path, ...]
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable representation (absolute paths)."""

        return {
            "root": str(self.root),
            "loader": str(self.loader) if self.loader else None,
            "archives": [str(path) for path in self.archives],
            "archive_count": len(self.archives),
            "warnings": list(self.warnings),
        }


def _is_within(root: Path, candidate: Path) -> bool:
    """Return ``True`` only when *candidate* is inside *root*.

    Both paths must already be absolute.  ``os.path.commonpath`` is used
    instead of string prefix checks so ``C:\\game2`` is not mistaken for a
    child of ``C:\\game``.
    """

    try:
        return os.path.commonpath((str(root), str(candidate))) == str(root)
    except ValueError:
        # Different drives on Windows.
        return False


def _safe_real_path(path: Path, root: Path, warnings: list[str]) -> Path | None:
    """Resolve *path* and reject symlink escapes from *root*."""

    try:
        resolved = Path(os.path.realpath(path))
    except OSError as exc:
        warnings.append(f"无法解析路径 {path}: {exc}")
        return None
    if not _is_within(root, resolved):
        warnings.append(f"已跳过指向游戏目录之外的链接: {path} -> {resolved}")
        return None
    return resolved


def _dedupe(items: Iterable[str]) -> tuple[str, ...]:
    """Return stable, de-duplicated strings."""

    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result)


def scan_game(
    root: str | os.PathLike[str],
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> GameLayout:
    """Scan a Ren'Py game root without modifying it.

    Args:
        root: Existing game directory.
        max_depth: Directory recursion limit below *root*.
        max_entries: Hard limit on visited filesystem entries.  The scanner
            stops adding results and emits a warning when the limit is hit.

    Returns:
        A :class:`GameLayout` with the loader candidate and all archives.

    Raises:
        GameScanError: If *root* is missing, is not a directory, or cannot be
            resolved.
    """

    if max_depth < 0:
        raise ValueError("max_depth 必须大于等于 0")
    if max_entries <= 0:
        raise ValueError("max_entries 必须大于 0")

    requested = Path(root).expanduser()
    try:
        game_root = requested.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GameScanError(f"无法访问游戏目录 {requested}: {exc}") from exc

    if not game_root.is_dir():
        raise GameScanError(f"游戏路径不是目录: {game_root}")

    loader_candidates: list[tuple[int, str, Path]] = []
    archives: list[Path] = []
    warnings: list[str] = []
    visited_entries = 0
    limit_reached = False

    def _on_walk_error(exc: OSError) -> None:
        warnings.append(f"无法读取目录 {exc.filename or '?'}: {exc.strerror or exc}")

    root_str = str(game_root)
    for dirpath, dirnames, filenames in os.walk(
        game_root, topdown=True, followlinks=False, onerror=_on_walk_error
    ):
        current = Path(dirpath)
        try:
            depth = len(current.relative_to(game_root).parts)
        except ValueError:
            # Should not happen with os.walk, but never escape the root.
            warnings.append(f"已跳过异常路径: {current}")
            dirnames[:] = []
            continue

        if depth >= max_depth:
            if dirnames:
                warnings.append(f"达到目录深度上限 {max_depth}: {current}")
            dirnames[:] = []

        # Prune symlinked/junction directories before os.walk descends into
        # them.  ``followlinks=False`` is a second line of defence.
        kept_dirs: list[str] = []
        for dirname in sorted(dirnames, key=str.casefold):
            child = current / dirname
            try:
                if child.is_symlink() or Path(os.path.realpath(child)).is_symlink():
                    warnings.append(f"已跳过目录链接: {child}")
                    continue
                if not _is_within(game_root, Path(os.path.realpath(child))):
                    warnings.append(f"已跳过指向游戏目录之外的目录: {child}")
                    continue
            except OSError as exc:
                warnings.append(f"无法检查目录 {child}: {exc}")
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs

        for filename in sorted(filenames, key=str.casefold):
            if limit_reached:
                break
            visited_entries += 1
            if visited_entries > max_entries:
                warnings.append(
                    f"已扫描超过 {max_entries} 个文件/目录，后续结果被截断"
                )
                limit_reached = True
                dirnames[:] = []
                break

            candidate = current / filename
            try:
                if candidate.is_symlink():
                    warnings.append(f"已跳过文件链接: {candidate}")
                    continue
                real = _safe_real_path(candidate, game_root, warnings)
                if real is None:
                    continue
                if not real.is_file():
                    continue
            except OSError as exc:
                warnings.append(f"无法检查文件 {candidate}: {exc}")
                continue

            lower_name = filename.casefold()
            if lower_name == "loader.pyo" and current.name.casefold() == "renpy":
                loader_candidates.append((depth, str(real).casefold(), real))
            elif lower_name.endswith(".rpa"):
                archives.append(real)

    # Prefer the shallowest loader, then a deterministic path order.  Multiple
    # candidates are reported as a warning rather than silently ignored.
    loader: Path | None = None
    if loader_candidates:
        loader_candidates.sort(key=lambda item: (item[0], item[1]))
        loader = loader_candidates[0][2]
        if len(loader_candidates) > 1:
            others = ", ".join(str(item[2]) for item in loader_candidates[1:])
            warnings.append(f"发现多个 renpy/loader.pyo，使用 {loader}；忽略: {others}")

    unique_archives = sorted(set(archives), key=lambda path: str(path).casefold())
    layout = GameLayout(
        root=game_root,
        loader=loader,
        archives=tuple(unique_archives),
        warnings=_dedupe(warnings),
    )
    logger.info(
        "扫描完成: root=%s loader=%s archives=%d warnings=%d",
        layout.root,
        layout.loader,
        len(layout.archives),
        len(layout.warnings),
    )
    return layout


def find_loader(root: str | os.PathLike[str]) -> Path | None:
    """Return the best ``renpy/loader.pyo`` candidate under *root*."""

    return scan_game(root).loader


def find_archives(root: str | os.PathLike[str]) -> tuple[Path, ...]:
    """Return every RPA archive under *root*."""

    return scan_game(root).archives
