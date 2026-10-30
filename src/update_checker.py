"""GitHub release version check.

Only an unauthenticated HTTPS GET to the fixed GitHub API endpoints is made.
No local file, game data or user information is uploaded.  The repository is
public; GitHub receives the usual request metadata (IP address, user agent).
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable

from src import __version__
from src.build_info import GITHUB_REPO

LATEST_RELEASE_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
TAGS_URL = f"https://api.github.com/repos/{GITHUB_REPO}/tags?per_page=20"
RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"
MAX_RESPONSE_BYTES = 1024 * 1024
DEFAULT_TIMEOUT = 10.0
CHECK_CACHE_SECONDS = 6 * 60 * 60
_USER_AGENT = f"rpa-loader-analyzer/{__version__}"

_VERSION_RE = re.compile(
    r"^(?P<major>\d+)"
    r"(?:\.(?P<minor>\d+))?"
    r"(?:\.(?P<patch>\d+))?"
    r"(?:[-_.]?(?P<label>[A-Za-z]+)(?:[-_.]?(?P<label_number>\d+))?"
    r"|[-_.](?P<numeric>\d+))?$"
)
_PRERELEASE_RANKS = {
    "alpha": 0,
    "a": 0,
    "pre": 0,
    "preview": 0,
    "dev": 0,
    "beta": 1,
    "b": 1,
    "rc": 2,
    "c": 2,
}
_CACHE: dict[str, tuple[float, "UpdateInfo"]] = {}


class UpdateCheckError(RuntimeError):
    """Raised only by the low-level fetch helper."""


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    """Result of comparing the local build with the latest GitHub version."""

    current_version: str
    latest_version: str | None = None
    update_available: bool = False
    release_url: str = RELEASES_URL
    published_at: str | None = None
    source: str = ""
    error: str | None = None
    checked_at: str = ""
    cached: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and self.latest_version is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_version": self.current_version,
            "latest_version": self.latest_version,
            "update_available": self.update_available,
            "release_url": self.release_url,
            "published_at": self.published_at,
            "source": self.source,
            "error": self.error,
            "checked_at": self.checked_at,
            "cached": self.cached,
        }


def parse_version(value: str | None) -> tuple[int, ...]:
    """Return a comparable key; pre-releases sort below the final release.

    The key is ``(major, minor, patch, stage, rank, number)`` where stage is
    ``1`` for a final release and ``0`` for alpha/beta/rc/numeric pre-releases.
    """

    if not value:
        return ()
    text = str(value).strip()
    if text[:1] in {"v", "V"}:
        text = text[1:]
    text = text.split("+", 1)[0].lower()
    match = _VERSION_RE.match(text)
    if not match:
        return ()
    base = (
        int(match.group("major")),
        int(match.group("minor") or 0),
        int(match.group("patch") or 0),
    )
    label = match.group("label")
    numeric = match.group("numeric")
    if label is None and numeric is None:
        return base + (1, 0, 0)
    if numeric is not None:
        return base + (0, 4, int(numeric))
    rank = _PRERELEASE_RANKS.get(label.lower(), 3)
    return base + (0, rank, int(match.group("label_number") or 0))


def is_newer(latest: str | None, current: str) -> bool:
    """Return true only if *latest* has a strictly greater version key."""

    latest_key = parse_version(latest)
    current_key = parse_version(current)
    return bool(latest_key) and latest_key > current_key


def clear_update_cache() -> None:
    """Clear the in-process check cache (used by tests and manual refreshes)."""

    _CACHE.clear()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fetch_json(
    url: str,
    *,
    opener: Callable[..., Any],
    timeout: float,
) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
        method="GET",
    )
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        try:
            exc.close()
        except Exception:
            pass
        raise UpdateCheckError(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateCheckError(str(exc)) from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise UpdateCheckError("GitHub 响应过大")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise UpdateCheckError(f"GitHub 响应不是合法 JSON: {exc}") from exc


def _from_release(payload: Any, current: str) -> UpdateInfo:
    if not isinstance(payload, dict):
        raise UpdateCheckError("release 响应结构非法")
    tag = payload.get("tag_name") or payload.get("name")
    if not isinstance(tag, str) or not tag.strip():
        raise UpdateCheckError("release 缺少 tag_name")
    url = payload.get("html_url")
    return UpdateInfo(
        current_version=current,
        latest_version=tag.strip(),
        update_available=is_newer(tag, current),
        release_url=url if isinstance(url, str) and url else RELEASES_URL,
        published_at=payload.get("published_at")
        if isinstance(payload.get("published_at"), str)
        else None,
        source="latest-release",
    )


def _from_tags(payload: Any, current: str) -> UpdateInfo:
    if not isinstance(payload, list) or not payload:
        raise UpdateCheckError("仓库没有发布 tag")
    candidates: list[tuple[tuple[int, ...], str]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str) and parse_version(name):
            candidates.append((parse_version(name), name))
    if not candidates:
        raise UpdateCheckError("tag 列表中没有可用版本号")
    candidates.sort(key=lambda item: item[0], reverse=True)
    tag = candidates[0][1]
    return UpdateInfo(
        current_version=current,
        latest_version=tag,
        update_available=is_newer(tag, current),
        release_url=f"https://github.com/{GITHUB_REPO}/releases/tag/{tag}",
        source="latest-tag",
    )


def _query_latest(
    current: str,
    fetch: Callable[..., Any],
    timeout: float,
) -> UpdateInfo:
    release_info: UpdateInfo | None = None
    try:
        release_payload = _fetch_json(
            LATEST_RELEASE_URL, opener=fetch, timeout=timeout
        )
        release_info = _from_release(release_payload, current)
        if release_info.update_available:
            return release_info
    except UpdateCheckError as release_error:
        # A repository without a GitHub Release still has tags.  Only fall
        # back for 404/tag-related failures; surface other errors.
        if "HTTP 404" not in str(release_error):
            raise

    # Releases and tags can diverge (for example a tag is pushed before a
    # Release page is published).  Compare both and use the higher one.
    try:
        tags_payload = _fetch_json(TAGS_URL, opener=fetch, timeout=timeout)
        tag_info = _from_tags(tags_payload, current)
    except UpdateCheckError:
        if release_info is not None:
            return release_info
        raise
    if release_info is None:
        return tag_info
    release_key = parse_version(release_info.latest_version)
    tag_key = parse_version(tag_info.latest_version)
    return tag_info if tag_key > release_key else release_info


def check_latest_version(
    current_version: str | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    opener: Callable[..., Any] | None = None,
    max_age: float = CHECK_CACHE_SECONDS,
    force: bool = False,
) -> UpdateInfo:
    """Query GitHub's latest release, falling back to the newest tag.

    Successful results are cached per current version for *max_age* seconds so
    startup checks do not repeatedly contact GitHub.  Pass ``force=True`` for
    an explicit user-requested refresh.  Network and HTTP failures are returned
    as ``error`` rather than raised, so the GUI can show a non-blocking warning.
    """

    current = current_version or __version__
    if not force and max_age > 0:
        cached = _CACHE.get(current)
        if cached is not None and time.monotonic() - cached[0] < max_age:
            return replace(cached[1], cached=True)

    fetch = opener or urllib.request.urlopen
    try:
        info = _query_latest(current, fetch, timeout)
    except UpdateCheckError as exc:
        info = UpdateInfo(
            current_version=current,
            release_url=RELEASES_URL,
            error=str(exc),
            checked_at=_utc_now(),
        )
    except Exception as exc:  # noqa: BLE001 - GUI should never crash on update check
        info = UpdateInfo(
            current_version=current,
            release_url=RELEASES_URL,
            error=f"{type(exc).__name__}: {exc}",
            checked_at=_utc_now(),
        )
    else:
        info = replace(info, checked_at=_utc_now())

    if info.ok:
        _CACHE[current] = (time.monotonic(), info)
    return info
