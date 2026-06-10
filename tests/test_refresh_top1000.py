from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from scripts import refresh_top1000


def _page(packages: list[str], next_url: str | None) -> dict:
    return {"packages": [{"package": name} for name in packages], "next": next_url}


class FetchPopularTests(unittest.TestCase):
    def test_unions_axes_paginates_dedupes_and_validates_names(self) -> None:
        pages = {
            # popularity: 2 pages, with a dupe + an invalid name to filter
            "https://pub.dev/api/search?sort=popularity&page=1": _page(
                ["http", "uuid", "Bad-Name", "uuid"],
                "https://pub.dev/api/search?sort=popularity&page=2",
            ),
            "https://pub.dev/api/search?sort=popularity&page=2": _page(["provider"], None),
            # top axis contributes a new package + a cross-axis dupe (http)
            "https://pub.dev/api/search?sort=top&page=1": _page(["dio", "http"], None),
            "https://pub.dev/api/search?sort=like&page=1": _page([], None),
            "https://pub.dev/api/search?sort=points&page=1": _page([], None),
            "https://pub.dev/api/search?sort=downloads&page=1": _page([], None),
        }

        def fake_get_json(url: str, *, sleep) -> dict:
            return pages[url]

        with mock.patch.object(refresh_top1000, "_get_json", fake_get_json):
            names = refresh_top1000.fetch_popular(count=100, sleep=lambda _s: None)

        self.assertEqual(names, ["http", "uuid", "provider", "dio"])

    def test_stops_at_count(self) -> None:
        def fake_get_json(url: str, *, sleep) -> dict:
            return _page(["a", "b", "c", "d"], "https://pub.dev/next")

        with mock.patch.object(refresh_top1000, "_get_json", fake_get_json):
            names = refresh_top1000.fetch_popular(count=3, sleep=lambda _s: None)

        self.assertEqual(names, ["a", "b", "c"])

    def test_write_worklist_shape_matches_collector_reader(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "_top1000.json"
            refresh_top1000.write_worklist(
                ["http", "provider"],
                out,
                now=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
            )
            payload = json.loads(out.read_text(encoding="utf-8"))

        self.assertEqual(payload["packages"], ["http", "provider"])
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["generated_at"], "2026-01-02T03:04:05Z")


if __name__ == "__main__":
    unittest.main()
