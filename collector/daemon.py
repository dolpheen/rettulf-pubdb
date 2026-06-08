"""24/7 collector queue, scheduler, and atomic git publisher.

The real archive/API/source collectors are still split across other issues.
This daemon wires the durable queue, checkout serialization, atomic writes,
schema validation, and publish loop around injectable pipeline/discovery seams.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol
from urllib.parse import quote

try:
    import httpx
except ModuleNotFoundError as exc:  # pragma: no cover - surfaced directly
    raise ModuleNotFoundError(
        "httpx is required (pip install -r collector/requirements.txt)"
    ) from exc

try:
    from jsonschema import Draft202012Validator
except ModuleNotFoundError as exc:  # pragma: no cover - surfaced directly
    raise ModuleNotFoundError(
        "jsonschema is required (pip install -r collector/requirements.txt)"
    ) from exc

from collector.pipelines.api_surface import collect_api_surface
from collector.pipelines.source_fingerprint import collect_source_fingerprint
from collector.pubdev_client import PUB_DEV_URL, USER_AGENT, PubDevClient, PubDevError

JsonObject = dict[str, Any]

SCHEMA_VERSION = 1
BASE_VARIANT = "base"
DEFAULT_CACHE_ROOT = Path.home() / ".cache" / "rettulf-pubdb"
DEFAULT_QUEUE_DB = DEFAULT_CACHE_ROOT / "queue.db"
DEFAULT_METRICS_PORT = 9305
DEFAULT_BATCH_SIZE = 10
DEFAULT_PUSH_INTERVAL_SECONDS = 300.0
DEFAULT_TICK_INTERVAL_SECONDS = 60.0
DEFAULT_DISCOVER_INTERVAL_SECONDS = 3600.0
STALE_AFTER_DAYS = 30
DEFAULT_MAX_ATTEMPTS = 5
INITIAL_FAILURE_BACKOFF_SECONDS = 60.0
MAX_FAILURE_BACKOFF_SECONDS = 3600.0
MAX_DISCOVERY_RETRIES = 3
INITIAL_DISCOVERY_BACKOFF_SECONDS = 0.5
MAX_DISCOVERY_BACKOFF_SECONDS = 8.0

PRIORITY_MISSING_BASE = 10
PRIORITY_STALE_BASE = 20
PRIORITY_MISSING_OBF = 30
PRIORITY_MISSING_FLUTTER_VARIANT = 40

_PACKAGE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
_VARIANT_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


class PipelineError(RuntimeError):
    """Raised when the injected collector pipeline cannot produce an entry."""


@dataclass(frozen=True)
class WorkItem:
    package: str
    version: str
    variant: str = BASE_VARIANT
    priority: int = PRIORITY_MISSING_BASE
    schema_version: int = SCHEMA_VERSION
    attempts: int = 0

    @property
    def commit_key(self) -> str:
        return (
            f"{self.package}:{self.version}:{self.variant}:"
            f"schema-v{self.schema_version}"
        )


class Pipeline(Protocol):
    def collect(self, item: WorkItem) -> JsonObject:
        """Return a schema-v1 entry for ``item``."""


class Discovery(Protocol):
    def versions(self, package: str) -> list[str]:
        """Return pub.dev versions for ``package``."""


class GitBackend(Protocol):
    def add(self, paths: Iterable[Path]) -> None:
        """Stage paths relative to the repository root."""

    def commit(self, message: str) -> bool:
        """Create a commit if staged changes exist. Return whether one was made."""

    def push_with_rebase_retry(self, revalidate: Callable[[], None]) -> None:
        """Push the current branch, rebasing and revalidating on conflicts."""


class Metrics:
    def __init__(self) -> None:
        self.entries_collected_total = 0
        self.pubdev_429_total = 0
        self.publish_conflict_total = 0
        self.last_commit_at: datetime | None = None
        self._lock = threading.Lock()

    def inc_entries(self, count: int = 1) -> None:
        with self._lock:
            self.entries_collected_total += count

    def inc_pubdev_429(self) -> None:
        with self._lock:
            self.pubdev_429_total += 1

    def inc_publish_conflict(self) -> None:
        with self._lock:
            self.publish_conflict_total += 1

    def mark_commit(self, now: datetime | None = None) -> None:
        with self._lock:
            self.last_commit_at = now or _utcnow()

    def render(self, *, queue_size: int, now: datetime | None = None) -> str:
        now = now or _utcnow()
        with self._lock:
            entries = self.entries_collected_total
            pubdev_429 = self.pubdev_429_total
            conflicts = self.publish_conflict_total
            last_commit_at = self.last_commit_at

        age = -1.0
        if last_commit_at is not None:
            age = max(0.0, (now - last_commit_at).total_seconds())

        lines = [
            "# HELP pubdb_queue_size Number of queued collector work items.",
            "# TYPE pubdb_queue_size gauge",
            f"pubdb_queue_size {queue_size}",
            "# HELP pubdb_entries_collected_total Number of entries collected and published.",
            "# TYPE pubdb_entries_collected_total counter",
            f"pubdb_entries_collected_total {entries}",
            "# HELP pubdb_pubdev_429_total Number of pub.dev HTTP 429 responses observed.",
            "# TYPE pubdb_pubdev_429_total counter",
            f"pubdb_pubdev_429_total {pubdev_429}",
            "# HELP pubdb_publish_conflict_total Number of git push conflicts handled.",
            "# TYPE pubdb_publish_conflict_total counter",
            f"pubdb_publish_conflict_total {conflicts}",
            "# HELP pubdb_last_commit_age_seconds Seconds since the last successful collector commit.",
            "# TYPE pubdb_last_commit_age_seconds gauge",
            f"pubdb_last_commit_age_seconds {age:.3f}",
            "",
        ]
        return "\n".join(lines)


class MetricsServer:
    def __init__(
        self,
        host: str,
        port: int,
        render: Callable[[], str],
    ) -> None:
        self._render = render

        class Handler(BaseHTTPRequestHandler):
            def do_GET(handler_self) -> None:  # noqa: N802 - http.server API
                if handler_self.path != "/metrics":
                    handler_self.send_response(404)
                    handler_self.end_headers()
                    return
                payload = render().encode("utf-8")
                handler_self.send_response(200)
                handler_self.send_header("Content-Type", "text/plain; version=0.0.4")
                handler_self.send_header("Content-Length", str(len(payload)))
                handler_self.end_headers()
                handler_self.wfile.write(payload)

            def log_message(
                handler_self, format: str, *args: object
            ) -> None:  # pragma: no cover - avoids noisy daemon logs
                return

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(
            target=self.httpd.serve_forever,
            name="pubdb-metrics",
            daemon=True,
        )

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)


class WorkQueue:
    def __init__(
        self,
        path: Path | str = DEFAULT_QUEUE_DB,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self.path = Path(path).expanduser()
        self.max_attempts = max_attempts
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._init_schema()
        self.reset_in_progress()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _init_schema(self) -> None:
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS work_items (
                  package TEXT NOT NULL,
                  version TEXT NOT NULL,
                  variant TEXT NOT NULL,
                  schema_version INTEGER NOT NULL,
                  priority INTEGER NOT NULL,
                  state TEXT NOT NULL,
                  attempts INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  next_attempt_at TEXT NOT NULL,
                  last_error TEXT,
                  PRIMARY KEY (package, version, variant, schema_version)
                );
                """
            )
            columns = {
                str(row["name"])
                for row in self._db.execute("PRAGMA table_info(work_items)")
            }
            if "next_attempt_at" not in columns:
                now = _iso_now()
                self._db.execute(
                    "ALTER TABLE work_items ADD COLUMN next_attempt_at TEXT"
                )
                self._db.execute(
                    """
                    UPDATE work_items
                    SET next_attempt_at = COALESCE(next_attempt_at, updated_at, ?)
                    """,
                    (now,),
                )
            self._db.executescript(
                """
                DROP INDEX IF EXISTS work_items_ready_idx;
                CREATE INDEX work_items_ready_idx
                ON work_items (
                  state, next_attempt_at, priority, updated_at,
                  package, version, variant
                );
                """
            )
            self._db.commit()

    def reset_in_progress(self) -> None:
        now = _iso_now()
        with self._lock:
            self._db.execute(
                """
                UPDATE work_items
                SET state = 'queued', updated_at = ?, next_attempt_at = ?
                WHERE state = 'in_progress'
                """,
                (now, now),
            )
            self._db.commit()

    def enqueue(self, item: WorkItem) -> None:
        _validate_work_item(item)
        now = _iso_now()
        with self._lock, self._db:
            self._db.execute(
                """
                INSERT INTO work_items (
                  package, version, variant, schema_version, priority, state,
                  attempts, created_at, updated_at, next_attempt_at
                )
                VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?)
                ON CONFLICT(package, version, variant, schema_version) DO UPDATE SET
                  priority = CASE
                    WHEN work_items.state = 'done'
                      AND excluded.priority = ? THEN excluded.priority
                    WHEN work_items.state = 'failed' THEN work_items.priority
                    ELSE min(work_items.priority, excluded.priority)
                  END,
                  state = CASE
                    WHEN work_items.state = 'done'
                      AND excluded.priority = ? THEN 'queued'
                    WHEN work_items.state = 'failed' THEN work_items.state
                    WHEN work_items.state IN ('done', 'in_progress') THEN work_items.state
                    ELSE 'queued'
                  END,
                  attempts = CASE
                    WHEN work_items.state = 'done'
                      AND excluded.priority = ? THEN 0
                    ELSE work_items.attempts
                  END,
                  updated_at = excluded.updated_at,
                  next_attempt_at = CASE
                    WHEN work_items.state IN ('failed', 'in_progress') THEN work_items.next_attempt_at
                    ELSE excluded.next_attempt_at
                  END,
                  last_error = CASE
                    WHEN work_items.state = 'failed' THEN work_items.last_error
                    ELSE NULL
                  END
                """,
                (
                    item.package,
                    item.version,
                    item.variant,
                    item.schema_version,
                    item.priority,
                    now,
                    now,
                    now,
                    PRIORITY_STALE_BASE,
                    PRIORITY_STALE_BASE,
                    PRIORITY_STALE_BASE,
                ),
            )

    def enqueue_many(self, items: Iterable[WorkItem]) -> int:
        count = 0
        for item in items:
            self.enqueue(item)
            count += 1
        return count

    def dequeue(self) -> WorkItem | None:
        now = _iso_now()
        with self._transaction_immediate():
            row = self._db.execute(
                """
                SELECT package, version, variant, schema_version, priority, attempts
                FROM work_items
                WHERE state = 'queued'
                  AND next_attempt_at <= ?
                ORDER BY priority ASC, updated_at ASC, package ASC, version ASC, variant ASC
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            attempts = int(row["attempts"]) + 1
            self._db.execute(
                """
                UPDATE work_items
                SET state = 'in_progress',
                    attempts = ?,
                    updated_at = ?,
                    next_attempt_at = ?
                WHERE package = ? AND version = ? AND variant = ? AND schema_version = ?
                """,
                (
                    attempts,
                    now,
                    now,
                    row["package"],
                    row["version"],
                    row["variant"],
                    row["schema_version"],
                ),
            )
            return WorkItem(
                package=str(row["package"]),
                version=str(row["version"]),
                variant=str(row["variant"]),
                schema_version=int(row["schema_version"]),
                priority=int(row["priority"]),
                attempts=attempts,
            )

    def complete(self, item: WorkItem) -> None:
        now = _iso_now()
        with self._lock, self._db:
            self._db.execute(
                """
                UPDATE work_items
                SET state = 'done',
                    updated_at = ?,
                    next_attempt_at = ?,
                    last_error = NULL
                WHERE package = ? AND version = ? AND variant = ? AND schema_version = ?
                """,
                (
                    now,
                    now,
                    item.package,
                    item.version,
                    item.variant,
                    item.schema_version,
                ),
            )

    def fail(self, item: WorkItem, error: Exception | str) -> None:
        now = _iso_now()
        state = "failed" if item.attempts >= self.max_attempts else "queued"
        next_attempt_at = (
            now
            if state == "failed"
            else _iso_after_seconds(_failure_backoff_seconds(item.attempts))
        )
        with self._lock, self._db:
            self._db.execute(
                """
                UPDATE work_items
                SET state = ?, updated_at = ?, next_attempt_at = ?, last_error = ?
                WHERE package = ? AND version = ? AND variant = ? AND schema_version = ?
                """,
                (
                    state,
                    now,
                    next_attempt_at,
                    str(error)[:1000],
                    item.package,
                    item.version,
                    item.variant,
                    item.schema_version,
                ),
            )

    def queued_count(self) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS count FROM work_items WHERE state = 'queued'"
            ).fetchone()
            return int(row["count"])

    @contextlib.contextmanager
    def _transaction_immediate(self) -> Iterable[None]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self._db.rollback()
                raise
            else:
                self._db.commit()


class PubDevDiscovery:
    def __init__(
        self,
        *,
        base_url: str = PUB_DEV_URL,
        client: httpx.Client | None = None,
        metrics: Metrics | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = MAX_DISCOVERY_RETRIES,
        backoff_initial: float = INITIAL_DISCOVERY_BACKOFF_SECONDS,
        backoff_max: float = MAX_DISCOVERY_BACKOFF_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.metrics = metrics
        self._sleep = sleep
        self.max_retries = max_retries
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self._client = client or httpx.Client(timeout=30.0, follow_redirects=True)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def versions(self, package: str) -> list[str]:
        response = self._request(package)
        try:
            payload = response.json()
        finally:
            response.close()

        versions = payload.get("versions")
        if not isinstance(versions, list):
            return []
        result = []
        for candidate in versions:
            if not isinstance(candidate, dict):
                continue
            version = candidate.get("version")
            if isinstance(version, str):
                result.append(version)
        return result

    def _request(self, package: str) -> httpx.Response:
        url = f"{self.base_url}/api/packages/{quote(package, safe='')}"
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.get(
                    url,
                    headers={
                        "Accept": "application/vnd.pub.v2+json",
                        "User-Agent": USER_AGENT,
                    },
                )
            except httpx.RequestError as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                self._sleep(self._backoff_delay(attempt))
                continue

            if response.status_code == 429:
                if self.metrics is not None:
                    self.metrics.inc_pubdev_429()
                if attempt >= self.max_retries:
                    try:
                        response.raise_for_status()
                    finally:
                        response.close()
                delay = _retry_after_delay(response.headers.get("Retry-After"))
                if delay is None:
                    delay = self._backoff_delay(attempt)
                response.close()
                self._sleep(min(delay, MAX_DISCOVERY_BACKOFF_SECONDS))
                continue

            if 500 <= response.status_code <= 599 and attempt < self.max_retries:
                response.close()
                self._sleep(self._backoff_delay(attempt))
                continue

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError:
                response.close()
                raise
            return response

        raise RuntimeError(f"pub.dev discovery request failed for {package}: {last_error}") from last_error

    def _backoff_delay(self, attempt: int) -> float:
        return min(self.backoff_initial * (2**attempt), self.backoff_max)


class DefaultPipeline:
    def __init__(self, *, archive_cache_dir: Path | str | None = None) -> None:
        self.archive_cache_dir = archive_cache_dir

    def collect(self, item: WorkItem) -> JsonObject:
        if item.variant != BASE_VARIANT:
            raise PipelineError(f"variant pipeline is not implemented: {item.variant}")
        try:
            with PubDevClient(cache_dir=self.archive_cache_dir) as client:
                lib_dir = client.fetch(item.package, item.version)
        except PubDevError as exc:
            raise PipelineError(str(exc)) from exc

        package_dir = lib_dir.parent
        return {
            "pubdb_schema_version": item.schema_version,
            "package": item.package,
            "version": item.version,
            "collected_at": _iso_now(),
            "api_surface": collect_api_surface(package_dir, item.package),
            "source_fingerprint": collect_source_fingerprint(
                package_dir,
                item.package,
            ),
        }


class EntryValidator:
    def __init__(self, schema_path: Path | str) -> None:
        self.schema_path = Path(schema_path)
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        self._validator = Draft202012Validator(schema)

    def validate_document(self, document: JsonObject) -> None:
        self._validator.validate(document)

    def validate_file(self, path: Path) -> None:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError(f"entry must be a JSON object: {path}")
        self.validate_document(document)


class AtomicEntryWriter:
    def __init__(self, repo_root: Path | str, validator: EntryValidator) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.validator = validator

    def write_entry(self, relative_path: Path, entry: JsonObject) -> Path:
        self.validator.validate_document(entry)
        target = _safe_repo_path(self.repo_root, relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(target, entry)
        self.validator.validate_file(target)
        return target

    def write_index_entry(self, entry: JsonObject) -> Path:
        index_path = self.repo_root / "db" / "_index.json"
        index = _read_index(index_path)
        packages = index.setdefault("packages", {})
        if not isinstance(packages, dict):
            packages = {}
            index["packages"] = packages

        package = str(entry["package"])
        version = str(entry["version"])
        raw_versions = packages.get(package)
        if isinstance(raw_versions, list):
            versions = [item for item in raw_versions if isinstance(item, str)]
        else:
            versions = []
        if version not in versions:
            versions.append(version)
        packages[package] = _sort_versions(versions)
        index["pubdb_schema_version"] = SCHEMA_VERSION
        index["generated_at"] = _iso_now()
        _atomic_write_json(index_path, index)
        return index_path


class CheckoutLock:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self._handle: Any | None = None

    def __enter__(self) -> "CheckoutLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()

    def acquire(self, timeout: float | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+")
        start = time.monotonic()
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._handle = handle
                return
            except BlockingIOError:
                if timeout is not None and time.monotonic() - start >= timeout:
                    handle.close()
                    raise TimeoutError(f"timed out waiting for checkout lock {self.path}")
                time.sleep(0.05)

    def release(self) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


class GitRepository:
    def __init__(
        self,
        repo_root: Path | str,
        *,
        metrics: Metrics | None = None,
        remote: str = "origin",
        retries: int = 2,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.metrics = metrics
        self.remote = remote
        self.retries = retries

    def add(self, paths: Iterable[Path]) -> None:
        args = ["add", "--", *(str(path) for path in paths)]
        self._git(args)

    def commit(self, message: str) -> bool:
        status = subprocess.run(
            ["git", "-C", str(self.repo_root), "diff", "--cached", "--quiet"],
            check=False,
        ).returncode
        if status == 0:
            return False
        if status != 1:
            raise RuntimeError("git diff --cached --quiet failed")
        self._git(["commit", "-m", message])
        return True

    def push_with_rebase_retry(self, revalidate: Callable[[], None]) -> None:
        branch = self._current_branch()
        for attempt in range(self.retries + 1):
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.repo_root),
                    "push",
                    self.remote,
                    f"HEAD:{branch}",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return
            if attempt >= self.retries:
                detail = result.stderr.strip() or result.stdout.strip()
                raise RuntimeError(f"git push failed after retries: {detail}")
            if self.metrics is not None:
                self.metrics.inc_publish_conflict()
            self._git(["fetch", self.remote, branch])
            try:
                self._git(["rebase", f"{self.remote}/{branch}"])
            except subprocess.CalledProcessError as exc:
                subprocess.run(
                    ["git", "-C", str(self.repo_root), "rebase", "--abort"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                raise RuntimeError(
                    "git rebase failed during publish retry; aborted rebase"
                ) from exc
            revalidate()

    def has_commit_key(self, key: str) -> bool:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(self.repo_root),
                "log",
                "--extended-regexp",
                f"--grep=^pubdb-commit-key: {re.escape(key)}$",
                "-n",
                "1",
                "--format=%H",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        return bool(result.stdout.strip())

    def _current_branch(self) -> str:
        result = self._git(["branch", "--show-current"], capture=True)
        branch = result.stdout.strip()
        if not branch:
            raise RuntimeError("cannot push from detached HEAD")
        return branch

    def _git(
        self,
        args: list[str],
        *,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.repo_root), *args],
            check=True,
            capture_output=capture,
            text=True,
        )


@dataclass
class _PendingEntry:
    item: WorkItem
    entry_path: Path
    index_path: Path


class GitPublisher:
    def __init__(
        self,
        *,
        repo_root: Path | str,
        writer: AtomicEntryWriter,
        checkout_lock: CheckoutLock,
        git: GitBackend,
        metrics: Metrics,
        batch_size: int = DEFAULT_BATCH_SIZE,
        push_interval: float = DEFAULT_PUSH_INTERVAL_SECONDS,
        push: bool = True,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.writer = writer
        self.checkout_lock = checkout_lock
        self.git = git
        self.metrics = metrics
        self.batch_size = batch_size
        self.push_interval = push_interval
        self.push = push
        self._pending: list[_PendingEntry] = []
        self._last_flush = time.monotonic()

    @property
    def pending_items(self) -> list[WorkItem]:
        return [pending.item for pending in self._pending]

    def stage_entry(self, item: WorkItem, entry: JsonObject) -> None:
        relative_path = entry_relative_path(item)
        with self.checkout_lock:
            entry_path = self.writer.write_entry(relative_path, entry)
            index_path = self.writer.write_index_entry(entry)
            self._pending.append(
                _PendingEntry(
                    item=item,
                    entry_path=entry_path.relative_to(self.repo_root),
                    index_path=index_path.relative_to(self.repo_root),
                )
            )

    def should_flush(self, *, force: bool = False) -> bool:
        if not self._pending:
            return False
        if force:
            return True
        if len(self._pending) >= self.batch_size:
            return True
        return time.monotonic() - self._last_flush >= self.push_interval

    def flush(self, *, force: bool = False) -> list[WorkItem]:
        if not self.should_flush(force=force):
            return []
        with self.checkout_lock:
            pending = list(self._pending)
            paths = _unique_paths(
                path
                for item in pending
                for path in (item.entry_path, item.index_path)
            )
            self._revalidate(pending)
            if self.push and self._all_commit_keys_exist(pending):
                self.git.push_with_rebase_retry(lambda: self._revalidate(pending))
                self._pending.clear()
                self._last_flush = time.monotonic()
                return [item.item for item in pending]
            self.git.add(paths)
            message = _commit_message([item.item for item in pending])
            committed = self.git.commit(message)
            if committed and self.push:
                self.git.push_with_rebase_retry(lambda: self._revalidate(pending))
            if committed:
                self.metrics.mark_commit()
            self._pending.clear()
            self._last_flush = time.monotonic()
            return [item.item for item in pending]

    def _revalidate(self, pending: list[_PendingEntry]) -> None:
        for item in pending:
            self.writer.validator.validate_file(self.repo_root / item.entry_path)

    def _all_commit_keys_exist(self, pending: list[_PendingEntry]) -> bool:
        has_commit_key = getattr(self.git, "has_commit_key", None)
        if not callable(has_commit_key):
            return False
        return all(has_commit_key(item.item.commit_key) for item in pending)

    def complete_if_committed(self, item: WorkItem) -> bool:
        has_commit_key = getattr(self.git, "has_commit_key", None)
        if not callable(has_commit_key) or not has_commit_key(item.commit_key):
            return False

        relative_path = entry_relative_path(item)
        with self.checkout_lock:
            self.writer.validator.validate_file(self.repo_root / relative_path)
            if self.push:
                self.git.push_with_rebase_retry(
                    lambda: self.writer.validator.validate_file(
                        self.repo_root / relative_path
                    )
                )
        return True


class CollectorDaemon:
    def __init__(
        self,
        *,
        repo_root: Path | str,
        queue: WorkQueue,
        pipeline: Pipeline,
        publisher: GitPublisher,
        metrics: Metrics,
        discovery: Discovery | None = None,
        packages: list[str] | None = None,
        tick_interval: float = DEFAULT_TICK_INTERVAL_SECONDS,
        discover_interval: float = DEFAULT_DISCOVER_INTERVAL_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.queue = queue
        self.pipeline = pipeline
        self.publisher = publisher
        self.metrics = metrics
        self.discovery = discovery
        self.packages = packages
        self.tick_interval = tick_interval
        self.discover_interval = discover_interval
        self.sleep = sleep
        self.shutdown = threading.Event()
        self._last_discovery = 0.0

    def request_shutdown(self, signum: int | None = None, frame: object | None = None) -> None:
        self.shutdown.set()

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self.request_shutdown)
        signal.signal(signal.SIGINT, self.request_shutdown)

    def run(self, *, once: bool = False) -> int:
        try:
            if once:
                self.discover(force=True)
                processed = self.process_one(force_flush=True, raise_errors=True)
                if not processed:
                    self.publisher.flush(force=True)
                    return 0
                return 0

            while not self.shutdown.is_set():
                self.discover()
                processed = self.process_one()
                self._flush_completed()
                if not processed:
                    self.sleep(self.tick_interval)
            self._flush_completed(force=True)
            return 0
        finally:
            close = getattr(self.discovery, "close", None)
            if callable(close):
                close()

    def process_one(
        self,
        *,
        force_flush: bool = False,
        raise_errors: bool = False,
    ) -> bool:
        item = self.queue.dequeue()
        if item is None:
            return False
        try:
            if item.attempts > 1 and self.publisher.complete_if_committed(item):
                self.queue.complete(item)
                return True
            entry = self.pipeline.collect(item)
            self.publisher.stage_entry(item, entry)
            completed = self.publisher.flush(force=force_flush)
        except Exception as exc:
            if item in self.publisher.pending_items:
                if raise_errors:
                    raise
                print(f"collector publish error for {item.commit_key}: {exc}", file=sys.stderr)
                return True
            self.queue.fail(item, exc)
            if raise_errors:
                raise
            print(f"collector error for {item.commit_key}: {exc}", file=sys.stderr)
            return True

        for completed_item in completed:
            self.queue.complete(completed_item)
        self.metrics.inc_entries(len(completed))
        return True

    def _flush_completed(
        self,
        *,
        force: bool = False,
        raise_errors: bool = False,
    ) -> list[WorkItem]:
        try:
            completed = self.publisher.flush(force=force)
        except Exception as exc:
            if raise_errors:
                raise
            print(f"collector publish error: {exc}", file=sys.stderr)
            return []
        for item in completed:
            self.queue.complete(item)
        self.metrics.inc_entries(len(completed))
        return completed

    def discover(self, *, force: bool = False) -> int:
        if self.discovery is None:
            return 0
        now = time.monotonic()
        if not force and now - self._last_discovery < self.discover_interval:
            return 0
        self._last_discovery = now
        items = discover_work(self.repo_root, self.discovery, self.packages)
        return self.queue.enqueue_many(items)


def discover_work(
    repo_root: Path | str,
    discovery: Discovery,
    packages: list[str] | None = None,
) -> list[WorkItem]:
    root = Path(repo_root)
    index_versions = _index_versions(root / "db" / "_index.json")
    tracked = packages or sorted(index_versions) or _top1000_packages(root / "db" / "_top1000.json")
    items: list[WorkItem] = []
    for package in tracked:
        if not _PACKAGE_RE.match(package):
            continue
        existing_versions = index_versions.get(package, set())
        try:
            versions = discovery.versions(package)
        except Exception as exc:
            print(f"collector discovery error for {package}: {exc}", file=sys.stderr)
            continue
        for version in versions:
            if not _VERSION_RE.match(version):
                continue
            item = _work_for_version(root, package, version, existing_versions)
            if item is not None:
                items.append(item)
    return items


def entry_relative_path(item: WorkItem) -> Path:
    _validate_work_item(item)
    if item.variant == BASE_VARIANT:
        filename = f"{item.version}.json"
    else:
        filename = f"{item.version}.{item.variant}.json"
    return Path("db") / item.package / filename


def _work_for_version(
    repo_root: Path,
    package: str,
    version: str,
    existing_versions: set[str],
) -> WorkItem | None:
    path = repo_root / "db" / package / f"{version}.json"
    if version not in existing_versions or not path.is_file():
        return WorkItem(package, version, BASE_VARIANT, PRIORITY_MISSING_BASE)
    if _entry_is_stale(path):
        return WorkItem(package, version, BASE_VARIANT, PRIORITY_STALE_BASE)
    return None


def _entry_is_stale(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    collected_at = payload.get("collected_at")
    if not isinstance(collected_at, str):
        return True
    try:
        timestamp = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    return _utcnow() - timestamp.astimezone(timezone.utc) > timedelta(days=STALE_AFTER_DAYS)


def _index_versions(path: Path) -> dict[str, set[str]]:
    payload = _read_index(path)
    packages = payload.get("packages")
    result: dict[str, set[str]] = {}
    if not isinstance(packages, dict):
        return result
    for package, raw_versions in packages.items():
        if not isinstance(package, str):
            continue
        versions: set[str] = set()
        if isinstance(raw_versions, list):
            versions.update(item for item in raw_versions if isinstance(item, str))
        elif isinstance(raw_versions, dict):
            nested = raw_versions.get("versions")
            if isinstance(nested, list):
                for item in nested:
                    if isinstance(item, str):
                        versions.add(item)
                    elif isinstance(item, dict) and isinstance(item.get("version"), str):
                        versions.add(str(item["version"]))
        result[package] = versions
    return result


def _top1000_packages(path: Path) -> list[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    packages = payload.get("packages")
    if not isinstance(packages, list):
        return []
    return [item for item in packages if isinstance(item, str)]


def _read_index(path: Path) -> JsonObject:
    if not path.exists():
        return {
            "pubdb_schema_version": SCHEMA_VERSION,
            "generated_at": None,
            "packages": {},
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "pubdb_schema_version": SCHEMA_VERSION,
            "generated_at": None,
            "packages": {},
        }
    return payload if isinstance(payload, dict) else {}


def _commit_message(items: list[WorkItem]) -> str:
    title = f"collector: publish {len(items)} pub.dev entr"
    title += "y" if len(items) == 1 else "ies"
    keys = "\n".join(f"pubdb-commit-key: {item.commit_key}" for item in items)
    return f"{title}\n\n{keys}"


def _atomic_write_json(path: Path, document: JsonObject) -> None:
    payload = (
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    tmp_path = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}"
    )
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        _fsync_dir(path.parent)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _fsync_dir(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _safe_repo_path(repo_root: Path, relative_path: Path) -> Path:
    if relative_path.is_absolute():
        raise ValueError(f"entry path must be relative: {relative_path}")
    target = (repo_root / relative_path).resolve()
    try:
        target.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError(f"entry path escapes repository: {relative_path}") from exc
    return target


def _validate_work_item(item: WorkItem) -> None:
    if not _PACKAGE_RE.match(item.package):
        raise ValueError(f"invalid package name: {item.package}")
    if not _VERSION_RE.match(item.version):
        raise ValueError(f"invalid version: {item.version}")
    if not _VARIANT_RE.match(item.variant):
        raise ValueError(f"invalid variant: {item.variant}")
    if item.schema_version != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema version: {item.schema_version}")


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = path.as_posix()
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _sort_versions(versions: list[str]) -> list[str]:
    return sorted(set(versions), key=_version_sort_key)


def _version_sort_key(version: str) -> tuple[Any, ...]:
    main = version.split("-", 1)[0].split("+", 1)[0]
    parts: list[Any] = []
    for part in main.split("."):
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(part)
    parts.append(version)
    return tuple(parts)


def _default_schema_path(repo_root: Path) -> Path:
    return repo_root / "schema" / "_schema.v1.json"


def _default_lock_path(repo_root: Path, cache_root: Path) -> Path:
    digest = hashlib.sha256(str(repo_root.resolve()).encode("utf-8")).hexdigest()[:16]
    return cache_root / f"checkout-{digest}.lock"


def _iso_now() -> str:
    return _utcnow().isoformat(timespec="seconds").replace("+00:00", "Z")


def _iso_after_seconds(seconds: float) -> str:
    return (
        (_utcnow() + timedelta(seconds=seconds))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _failure_backoff_seconds(attempts: int) -> float:
    exponent = max(0, attempts - 1)
    return min(
        INITIAL_FAILURE_BACKOFF_SECONDS * (2**exponent),
        MAX_FAILURE_BACKOFF_SECONDS,
    )


def _retry_after_delay(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def build_daemon(args: argparse.Namespace) -> CollectorDaemon:
    repo_root = Path(args.repo_root).resolve()
    cache_root = Path(args.cache_root).expanduser()
    metrics = Metrics()
    queue = WorkQueue(Path(args.queue_db).expanduser())
    discovery: Discovery | None = PubDevDiscovery(metrics=metrics)
    pipeline = DefaultPipeline(archive_cache_dir=args.archive_cache_dir)
    validator = EntryValidator(_default_schema_path(repo_root))
    writer = AtomicEntryWriter(repo_root, validator)
    lock_path = Path(args.lock_path) if args.lock_path else _default_lock_path(repo_root, cache_root)
    checkout_lock = CheckoutLock(lock_path)
    git = GitRepository(repo_root, metrics=metrics)
    publisher = GitPublisher(
        repo_root=repo_root,
        writer=writer,
        checkout_lock=checkout_lock,
        git=git,
        metrics=metrics,
        batch_size=args.batch_size,
        push_interval=args.push_interval,
        push=not args.no_push,
    )
    return CollectorDaemon(
        repo_root=repo_root,
        queue=queue,
        pipeline=pipeline,
        publisher=publisher,
        metrics=metrics,
        discovery=discovery,
        packages=args.packages,
        tick_interval=args.tick_interval,
        discover_interval=args.discover_interval,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m collector.daemon")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--queue-db", type=Path, default=DEFAULT_QUEUE_DB)
    parser.add_argument("--archive-cache-dir", type=Path)
    parser.add_argument("--lock-path", type=Path)
    parser.add_argument("--packages", nargs="*")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--push-interval", type=float, default=DEFAULT_PUSH_INTERVAL_SECONDS)
    parser.add_argument("--tick-interval", type=float, default=DEFAULT_TICK_INTERVAL_SECONDS)
    parser.add_argument("--discover-interval", type=float, default=DEFAULT_DISCOVER_INTERVAL_SECONDS)
    parser.add_argument("--metrics-host", default="127.0.0.1")
    parser.add_argument("--metrics-port", type=int, default=DEFAULT_METRICS_PORT)
    parser.add_argument("--no-metrics", action="store_true")
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="commit without pushing; intended for local smoke tests",
    )
    args = parser.parse_args(argv)

    daemon = build_daemon(args)
    daemon.install_signal_handlers()
    metrics_server: MetricsServer | None = None
    if not args.no_metrics:
        metrics_server = MetricsServer(
            args.metrics_host,
            args.metrics_port,
            lambda: daemon.metrics.render(queue_size=daemon.queue.queued_count()),
        )
        metrics_server.start()
    try:
        return daemon.run(once=args.once)
    finally:
        if metrics_server is not None:
            metrics_server.stop()
        daemon.queue.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
