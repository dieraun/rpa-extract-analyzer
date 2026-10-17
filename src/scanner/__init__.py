"""Game directory discovery."""

from __future__ import annotations

from .game_scanner import (
    GameLayout,
    GameScanError,
    find_archives,
    find_loader,
    scan_game,
)

__all__ = [
    "GameLayout",
    "GameScanError",
    "find_archives",
    "find_loader",
    "scan_game",
]
