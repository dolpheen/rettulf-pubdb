#!/usr/bin/env python3
"""Refresh ``db/_top1000.json`` with the most popular pub.dev packages.

The collector uses ``db/_top1000.json`` as its collection worklist. pub.dev's
search API caps every query at 100 results and exposes no ranked top-1000, so
this script unions the package lists from each popularity/quality ranking axis
(popularity, top, like, points, downloads) and dedupes — yielding the few
hundred most-popular packages the public API allows.

    python scripts/refresh_top1000.py              # -> db/_top1000.json
    python scripts/refresh_top1000.py --count 200  # cap the worklist size

Stdlib only (no extra deps); safe to run from CI or a cron.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

PUB_DEV_URL = "https://pub.dev"
USER_AGENT = "rettulf-pubdb-refresh/1.0 (+https://github.com/dolpheen/rettulf-pubdb)"
DEFAULT_COUNT = 1000
# pub.dev caps search at 100 results/query; union its ranking axes for breadth.
SEARCH_SORTS = ("popularity", "top", "like", "points", "downloads")
# Same constraint the collector enforces (collector/daemon.py:_PACKAGE_RE).
_PACKAGE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_MAX_RETRIES = 5


class RefreshError(RuntimeError):
    """Raised when the pub.dev worklist cannot be refreshed."""


def fetch_popular(
    count: int = DEFAULT_COUNT,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> list[str]:
    """Return up to ``count`` valid package names across the ranking axes."""
    names: list[str] = []
    seen: set[str] = set()
    for sort in SEARCH_SORTS:
        if len(names) >= count:
            break
        url: str | None = f"{PUB_DEV_URL}/api/search?sort={sort}&page=1"
        while url and len(names) < count:
            payload = _get_json(url, sleep=sleep)
            packages = payload.get("packages")
            if not isinstance(packages, list):
                raise RefreshError(
                    f"unexpected pub.dev response for {url}: no 'packages' list"
                )
            for item in packages:
                name = item.get("package") if isinstance(item, dict) else None
                if not isinstance(name, str) or name in seen or not _PACKAGE_RE.match(name):
                    continue
                seen.add(name)
                names.append(name)
                if len(names) >= count:
                    break
            next_url = payload.get("next")
            url = next_url if isinstance(next_url, str) and next_url else None
            if url:
                sleep(0.2)  # be polite to pub.dev between pages
    return names[:count]


def _get_json(url: str, *, sleep: Callable[[float], None]) -> dict:
    last_error: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.load(response)
            if not isinstance(data, dict):
                raise RefreshError(f"pub.dev returned non-object JSON for {url}")
            return data
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 429 and attempt < _MAX_RETRIES:
                retry_after = exc.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 2.0**attempt
                sleep(min(delay, 30.0))
                continue
            raise RefreshError(f"pub.dev search failed ({exc.code}) for {url}") from exc
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < _MAX_RETRIES:
                sleep(2.0**attempt)
                continue
            raise RefreshError(f"pub.dev search failed for {url}: {exc}") from exc
    raise RefreshError(f"pub.dev search failed for {url}: {last_error}")


def _default_output() -> Path:
    return Path(__file__).resolve().parent.parent / "db" / "_top1000.json"


def write_worklist(names: list[str], output: Path, *, now: datetime | None = None) -> None:
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds").replace("+00:00", "Z")
    payload = {
        "generated_at": stamp,
        "source": f"{PUB_DEV_URL}/api/search (union of sorts: {', '.join(SEARCH_SORTS)})",
        "count": len(names),
        "packages": names,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--output", type=Path, default=_default_output())
    args = parser.parse_args(argv)

    if args.count < 1:
        parser.error("--count must be >= 1")

    names = fetch_popular(args.count)
    if not names:
        print("error: pub.dev returned no packages", file=sys.stderr)
        return 1
    write_worklist(names, args.output)
    print(f"wrote {len(names)} packages to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
