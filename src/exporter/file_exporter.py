"""Safely export parsed RPA members into an independent output tree.

Guarantees:

* archive files are only ever opened read-only;
* output is written atomically through a temporary file in the destination
  directory;
* traversal names are re-validated, even if the exporter is handed a manually
  constructed :class:`RpaEntry`;
* empty members are skipped;
* an output directory inside the game/archive root is refused unless the
  caller explicitly supplies a different protected-root policy.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from src.rpa_core.rpa_parser import (
    RpaArchive,
    RpaEntry,
    RpaError,
    normalize_archive_name,
)

logger = logging.getLogger(__name__)


class ExportError(RuntimeError):
    """Raised when an export cannot be started or completed safely."""


@dataclass(frozen=True, slots=True)
class ExportedFile:
    """Per-entry export outcome."""

    name: str
    status: str
    output_path: Path | None = None
    size: int = 0
    sha256: str | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "output_path": str(self.output_path) if self.output_path else None,
            "size": self.size,
            "sha256": self.sha256,
            "message": self.message,
        }


@dataclass(slots=True)
class ExportResult:
    """Aggregate export result, suitable for a CLI JSON report."""

    archive_path: Path
    output_dir: Path
    exported: list[ExportedFile] = field(default_factory=list)
    skipped: list[ExportedFile] = field(default_factory=list)
    failed: list[ExportedFile] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def bytes_written(self) -> int:
        return sum(item.size for item in self.exported)

    @property
    def ok(self) -> bool:
        return not self.failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "archive": str(self.archive_path),
            "output_dir": str(self.output_dir),
            "exported_count": len(self.exported),
            "skipped_count": len(self.skipped),
            "failed_count": len(self.failed),
            "bytes_written": self.bytes_written,
            "ok": self.ok,
            "exported": [item.to_dict() for item in self.exported],
            "skipped": [item.to_dict() for item in self.skipped],
            "failed": [item.to_dict() for item in self.failed],
            "warnings": list(self.warnings),
        }


ProgressCallback = Callable[[int, int, RpaEntry], None]


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        return os.path.commonpath((str(root), str(candidate))) == str(root)
    except ValueError:
        return False


class FileExporter:
    """Export all members of one :class:`RpaArchive`."""

    def __init__(
        self,
        archive: RpaArchive,
        output_dir: str | Path,
        *,
        overwrite: bool = False,
        continue_on_error: bool = True,
        protected_roots: Iterable[str | Path] | None = None,
        chunk_size: int = 1024 * 1024,
    ) -> None:
        if chunk_size < 4096:
            raise ValueError("chunk_size 不能小于 4096")
        self.archive = archive
        self.overwrite = overwrite
        self.continue_on_error = continue_on_error
        self.chunk_size = chunk_size

        if protected_roots is None:
            protected = [archive.path.parent]
        else:
            protected = [Path(root).expanduser() for root in protected_roots]
        self._protected = []
        for root in protected:
            try:
                resolved = root.resolve(strict=False)
            except OSError as exc:
                raise ExportError(f"无法解析受保护目录 {root}: {exc}") from exc
            self._protected.append(resolved)

        requested = Path(output_dir).expanduser()
        try:
            self._requested_output = requested.absolute()
            if self._requested_output.is_symlink():
                raise ExportError(f"拒绝使用符号链接作为输出目录: {self._requested_output}")
            resolved_output = requested.resolve(strict=False)
        except OSError as exc:
            raise ExportError(f"无法解析输出目录 {requested}: {exc}") from exc
        for root in self._protected:
            if _is_within(root, resolved_output):
                raise ExportError(
                    f"输出目录不能位于游戏/归档目录内: {resolved_output} (受保护根 {root})"
                )
        if resolved_output.exists() and not resolved_output.is_dir():
            raise ExportError(f"输出路径已存在但不是目录: {resolved_output}")
        if resolved_output.is_symlink():
            raise ExportError(f"拒绝使用符号链接作为输出目录: {resolved_output}")
        try:
            resolved_output.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ExportError(f"无法创建输出目录 {resolved_output}: {exc}") from exc
        if resolved_output.is_symlink():
            raise ExportError(f"输出目录是符号链接: {resolved_output}")
        self.output_dir = resolved_output

    def _safe_target(self, name: str) -> Path:
        normalised = normalize_archive_name(name)
        target = self.output_dir.joinpath(*PurePosixPath(normalised).parts)
        # Refuse any symlink component below the output root.  Checking the
        # existing prefix is enough because missing components are created by
        # this process and cannot be pre-planted by the archive.
        current = self.output_dir
        for part in PurePosixPath(normalised).parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise ExportError(f"输出子目录是符号链接，拒绝穿越: {current}")
            if current.exists() and not current.is_dir():
                raise ExportError(f"输出路径组件不是目录: {current}")
        try:
            resolved_target = target.resolve(strict=False)
        except OSError as exc:
            raise ExportError(f"无法解析输出目标 {target}: {exc}") from exc
        if not _is_within(self.output_dir, resolved_target):
            raise ExportError(f"输出目标越出输出目录: {target}")
        return target

    def _export_one(self, entry: RpaEntry) -> ExportedFile:
        target = self._safe_target(entry.name)
        if entry.length == 0:
            return ExportedFile(
                name=entry.name,
                status="skipped-empty",
                message="空文件未导出",
            )
        if target.exists() and not self.overwrite:
            return ExportedFile(
                name=entry.name,
                status="skipped-existing",
                output_path=target,
                message="目标已存在且未启用覆盖",
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        if target.parent.is_symlink():
            raise ExportError(f"输出父目录是符号链接: {target.parent}")
        try:
            resolved_parent = target.parent.resolve(strict=True)
        except OSError as exc:
            raise ExportError(f"无法解析输出父目录 {target.parent}: {exc}") from exc
        if not _is_within(self.output_dir, resolved_parent):
            raise ExportError(f"输出父目录越出输出根: {target.parent}")

        temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.part"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = -1
        digest = hashlib.sha256()
        written = 0
        try:
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                for chunk in self.archive.iter_entry_data(
                    entry, chunk_size=self.chunk_size
                ):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
            if written == 0:
                return ExportedFile(
                    name=entry.name,
                    status="skipped-empty",
                    message="资源读取结果为空，已过滤",
                )
            if target.exists() and not self.overwrite:
                return ExportedFile(
                    name=entry.name,
                    status="skipped-existing",
                    output_path=target,
                    message="目标在导出期间出现，未覆盖",
                )
            os.replace(temporary, target)
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                logger.warning("无法清理临时文件: %s", temporary)

        if written == 0:
            return ExportedFile(
                name=entry.name,
                status="skipped-empty",
                message="空文件未导出",
            )
        return ExportedFile(
            name=entry.name,
            status="exported",
            output_path=target,
            size=written,
            sha256=digest.hexdigest(),
        )

    def export_all(self, progress: ProgressCallback | None = None) -> ExportResult:
        """Export every member, continuing past per-file errors if configured."""

        result = ExportResult(
            archive_path=self.archive.path,
            output_dir=self.output_dir,
            warnings=list(self.archive.warnings),
        )
        total = len(self.archive.entries)
        for index, entry in enumerate(self.archive.entries, start=1):
            if progress is not None:
                progress(index, total, entry)
            try:
                outcome = self._export_one(entry)
            except (ExportError, RpaError, OSError) as exc:
                failure = ExportedFile(
                    name=entry.name,
                    status="failed",
                    message=f"{type(exc).__name__}: {exc}",
                )
                result.failed.append(failure)
                logger.warning("导出失败 %s: %s", entry.name, exc)
                if not self.continue_on_error:
                    break
                continue
            if outcome.status == "exported":
                result.exported.append(outcome)
            else:
                result.skipped.append(outcome)
        logger.info(
            "导出完成: %s -> %s, exported=%d skipped=%d failed=%d",
            self.archive.path,
            self.output_dir,
            len(result.exported),
            len(result.skipped),
            len(result.failed),
        )
        return result


def export_archive(
    archive: RpaArchive,
    output_dir: str | Path,
    *,
    overwrite: bool = False,
    continue_on_error: bool = True,
    protected_roots: Iterable[str | Path] | None = None,
    progress: ProgressCallback | None = None,
) -> ExportResult:
    """Convenience wrapper around :class:`FileExporter`."""

    exporter = FileExporter(
        archive,
        output_dir,
        overwrite=overwrite,
        continue_on_error=continue_on_error,
        protected_roots=protected_roots,
    )
    return exporter.export_all(progress=progress)


def write_export_report(result: ExportResult, path: str | Path) -> Path:
    """Write a JSON export report to *path* (never inside the output tree)."""

    report_path = Path(path).expanduser()
    try:
        resolved = report_path.resolve(strict=False)
    except OSError as exc:
        raise ExportError(f"无法解析报告路径 {report_path}: {exc}") from exc
    if _is_within(result.output_dir, resolved):
        raise ExportError("导出报告不能写入资源输出目录，避免污染资源树")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_name(f".{report_path.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(report_path)
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
    return report_path
