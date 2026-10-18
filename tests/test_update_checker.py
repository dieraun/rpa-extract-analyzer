"""Tests for the GitHub release update checker, with no real network calls."""

from __future__ import annotations

import io
import json
import unittest
import urllib.error

from src.update_checker import (
    LATEST_RELEASE_URL,
    TAGS_URL,
    check_latest_version,
    is_newer,
    parse_version,
)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


def _opener(payload, *, status: int = 200):
    def _open(_request, timeout=None):
        del timeout
        return _Response(json.dumps(payload).encode("utf-8"))

    return _open


class UpdateCheckerTests(unittest.TestCase):
    def test_parse_version(self) -> None:
        self.assertEqual(parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(parse_version("1.0"), (1, 0))
        self.assertEqual(parse_version("release-1.0.0"), ())
        self.assertEqual(parse_version(None), ())

    def test_is_newer(self) -> None:
        self.assertTrue(is_newer("v1.0.1", "1.0.0"))
        self.assertTrue(is_newer("1.1", "1.0.9"))
        self.assertFalse(is_newer("v1.0.0", "1.0.0"))
        self.assertFalse(is_newer("0.9.9", "1.0.0"))
        self.assertFalse(is_newer("bad", "1.0.0"))

    def test_latest_release_newer(self) -> None:
        payload = {
            "tag_name": "v1.0.1",
            "html_url": "https://github.com/dieraun/rpa-extract-analyzer/releases/tag/v1.0.1",
            "published_at": "2026-10-18T00:00:00Z",
        }
        info = check_latest_version("1.0.0", opener=_opener(payload))
        self.assertTrue(info.ok)
        self.assertTrue(info.update_available)
        self.assertEqual(info.latest_version, "v1.0.1")
        self.assertEqual(info.source, "latest-release")

    def test_current_version_is_latest(self) -> None:
        info = check_latest_version(
            "1.0.0",
            opener=_opener({"tag_name": "v1.0.0", "html_url": "x"}),
        )
        self.assertTrue(info.ok)
        self.assertFalse(info.update_available)

    def test_missing_release_falls_back_to_tags(self) -> None:
        def _open(request, timeout=None):
            del timeout
            if request.full_url == LATEST_RELEASE_URL:
                raise urllib.error.HTTPError(
                    request.full_url, 404, "Not Found", {}, io.BytesIO()
                )
            self.assertEqual(request.full_url, TAGS_URL)
            return _Response(
                json.dumps(
                    [{"name": "v1.2.0"}, {"name": "v1.0.0"}]
                ).encode("utf-8")
            )

        info = check_latest_version("1.0.0", opener=_open)
        self.assertTrue(info.ok, info.error)
        self.assertEqual(info.source, "latest-tag")
        self.assertEqual(info.latest_version, "v1.2.0")
        self.assertTrue(info.update_available)

    def test_tags_can_be_newer_than_latest_release(self) -> None:
        def _open(request, timeout=None):
            del timeout
            if request.full_url == LATEST_RELEASE_URL:
                return _Response(
                    json.dumps({"tag_name": "v0.3.0", "html_url": "old"}).encode()
                )
            return _Response(json.dumps([{"name": "v1.0.0"}]).encode())

        info = check_latest_version("1.0.0", opener=_open)
        self.assertTrue(info.ok, info.error)
        self.assertEqual(info.latest_version, "v1.0.0")
        self.assertFalse(info.update_available)
        self.assertEqual(info.source, "latest-tag")

    def test_http_error_is_returned_not_raised(self) -> None:
        def _open(request, timeout=None):
            del timeout
            raise urllib.error.HTTPError(
                request.full_url, 403, "rate limited", {}, io.BytesIO()
            )

        info = check_latest_version("1.0.0", opener=_open)
        self.assertFalse(info.ok)
        self.assertIn("403", info.error or "")

    def test_invalid_json_is_returned(self) -> None:
        def _open(_request, timeout=None):
            del timeout
            return _Response(b"{broken")

        info = check_latest_version("1.0.0", opener=_open)
        self.assertFalse(info.ok)
        self.assertIn("JSON", info.error or "")

    def test_network_exception_is_returned(self) -> None:
        def _open(_request, timeout=None):
            del timeout
            raise OSError("network down")

        info = check_latest_version("1.0.0", opener=_open)
        self.assertFalse(info.ok)
        self.assertIn("network down", info.error or "")

    def test_result_is_json_ready(self) -> None:
        info = check_latest_version(
            "1.0.0", opener=_opener({"tag_name": "1.0.0"})
        )
        json.dumps(info.to_dict())


if __name__ == "__main__":
    unittest.main()
