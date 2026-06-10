from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import httpx

from collector import daemon

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "schema" / "_schema.v1.json"


def _valid_entry(package: str = "pkg", version: str = "1.0.0") -> dict[str, object]:
    return {
        "pubdb_schema_version": 1,
        "package": package,
        "version": version,
        "collected_at": "2026-01-01T00:00:00Z",
        "api_surface": {"classes": {}},
        "source_fingerprint": {
            "strategy": "fake-source-v1",
            "digest": "0" * 64,
        },
    }


class WorkQueueTests(unittest.TestCase):
    def test_priority_order_and_resume_in_progress_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue_path = Path(tmp) / "queue.db"
            queue = daemon.WorkQueue(queue_path)
            queue.enqueue(
                daemon.WorkItem(
                    "pkg",
                    "2.0.0",
                    variant="flutter-3.19",
                    priority=daemon.PRIORITY_MISSING_FLUTTER_VARIANT,
                )
            )
            queue.enqueue(
                daemon.WorkItem(
                    "pkg",
                    "1.0.0",
                    priority=daemon.PRIORITY_MISSING_BASE,
                )
            )

            first = queue.dequeue()
            self.assertIsNotNone(first)
            assert first is not None
            self.assertEqual(first.version, "1.0.0")
            queue.complete(first)

            second = queue.dequeue()
            self.assertIsNotNone(second)
            assert second is not None
            self.assertEqual(second.variant, "flutter-3.19")
            self.assertEqual(second.attempts, 1)
            queue.close()

            resumed_queue = daemon.WorkQueue(queue_path)
            resumed = resumed_queue.dequeue()
            self.assertIsNotNone(resumed)
            assert resumed is not None
            self.assertEqual(resumed.variant, "flutter-3.19")
            self.assertEqual(resumed.attempts, 2)
            resumed_queue.close()

    def test_enqueue_does_not_requeue_in_progress_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = daemon.WorkQueue(Path(tmp) / "queue.db")
            item = daemon.WorkItem("pkg", "1.0.0")
            queue.enqueue(item)

            claimed = queue.dequeue()
            self.assertEqual(claimed, daemon.WorkItem("pkg", "1.0.0", attempts=1))

            queue.enqueue(item)
            self.assertIsNone(queue.dequeue())

            assert claimed is not None
            queue.complete(claimed)
            self.assertIsNone(queue.dequeue())
            queue.close()

    def test_stale_done_item_is_requeued(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = daemon.WorkQueue(Path(tmp) / "queue.db")
            item = daemon.WorkItem("pkg", "1.0.0")
            queue.enqueue(item)
            claimed = queue.dequeue()
            assert claimed is not None
            queue.complete(claimed)

            queue.enqueue(item)
            self.assertIsNone(queue.dequeue())

            queue.enqueue(
                daemon.WorkItem(
                    "pkg",
                    "1.0.0",
                    priority=daemon.PRIORITY_STALE_BASE,
                )
            )
            stale = queue.dequeue()
            self.assertIsNotNone(stale)
            assert stale is not None
            self.assertEqual(stale.priority, daemon.PRIORITY_STALE_BASE)
            self.assertEqual(stale.attempts, 1)
            queue.close()

    def test_done_obfuscated_item_can_be_requeued_when_missing_or_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = daemon.WorkQueue(Path(tmp) / "queue.db")
            item = daemon.WorkItem(
                "pkg",
                "1.0.0",
                daemon.OBFUSCATED_VARIANT,
                daemon.PRIORITY_MISSING_OBF,
            )
            queue.enqueue(item)
            claimed = queue.dequeue()
            assert claimed is not None
            queue.complete(claimed)

            queue.enqueue(item)
            requeued = queue.dequeue()

            self.assertIsNotNone(requeued)
            assert requeued is not None
            self.assertEqual(requeued.variant, daemon.OBFUSCATED_VARIANT)
            self.assertEqual(requeued.priority, daemon.PRIORITY_MISSING_OBF)
            self.assertEqual(requeued.attempts, 1)
            queue.close()

    def test_done_flutter_variant_item_can_be_requeued_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = daemon.WorkQueue(Path(tmp) / "queue.db")
            item = daemon.WorkItem(
                "pkg",
                "1.0.0",
                "flutter-3.44.0",
                daemon.PRIORITY_MISSING_FLUTTER_VARIANT,
            )
            queue.enqueue(item)
            claimed = queue.dequeue()
            assert claimed is not None
            queue.complete(claimed)

            queue.enqueue(item)
            requeued = queue.dequeue()

            self.assertIsNotNone(requeued)
            assert requeued is not None
            self.assertEqual(requeued.variant, "flutter-3.44.0")
            self.assertEqual(requeued.priority, daemon.PRIORITY_MISSING_FLUTTER_VARIANT)
            self.assertEqual(requeued.attempts, 1)
            queue.close()

    def test_failure_uses_backoff_then_dead_letters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = daemon.WorkQueue(Path(tmp) / "queue.db", max_attempts=2)
            item = daemon.WorkItem("pkg", "1.0.0")
            queue.enqueue(item)

            first = queue.dequeue()
            assert first is not None
            queue.fail(first, "boom")
            self.assertIsNone(queue.dequeue())

            with queue._lock, queue._db:
                queue._db.execute(
                    """
                    UPDATE work_items
                    SET next_attempt_at = ?
                    WHERE package = ? AND version = ?
                    """,
                    (daemon._iso_now(), item.package, item.version),
                )

            second = queue.dequeue()
            assert second is not None
            self.assertEqual(second.attempts, 2)
            queue.fail(second, "still boom")
            self.assertIsNone(queue.dequeue())
            self.assertEqual(queue.queued_count(), 0)

            queue.enqueue(item)
            self.assertIsNone(queue.dequeue())
            queue.close()


class AtomicWriterTests(unittest.TestCase):
    def test_invalid_entry_is_not_renamed_into_place(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            validator = daemon.EntryValidator(SCHEMA_PATH)
            writer = daemon.AtomicEntryWriter(repo_root, validator)

            written = writer.write_entry(
                Path("db/pkg/1.0.0.json"),
                _valid_entry(),
            )
            self.assertTrue(written.is_file())

            invalid = _valid_entry(version="1.0.1")
            invalid.pop("source_fingerprint")
            invalid_path = repo_root / "db" / "pkg" / "1.0.1.json"
            with self.assertRaises(Exception):
                writer.write_entry(Path("db/pkg/1.0.1.json"), invalid)

            self.assertFalse(invalid_path.exists())
            self.assertEqual(list(invalid_path.parent.glob(".*.tmp-*")), [])


class CheckoutLockTests(unittest.TestCase):
    def test_lock_serializes_concurrent_process_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path = root / "checkout.lock"
            counter_path = root / "counter.txt"
            counter_path.write_text("0", encoding="utf-8")

            script = """
import sys
import time
from pathlib import Path
from collector import daemon

lock = daemon.CheckoutLock(Path(sys.argv[1]))
counter = Path(sys.argv[2])
with lock:
    value = int(counter.read_text(encoding="utf-8"))
    time.sleep(0.05)
    counter.write_text(str(value + 1), encoding="utf-8")
"""
            processes = [
                subprocess.Popen(
                    [sys.executable, "-c", script, str(lock_path), str(counter_path)],
                    cwd=REPO_ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(4)
            ]
            for process in processes:
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stdout + stderr)

            self.assertEqual(counter_path.read_text(encoding="utf-8"), "4")


class MetricsTests(unittest.TestCase):
    def test_metrics_endpoint_returns_prometheus_text(self) -> None:
        metrics = daemon.Metrics()
        metrics.inc_entries(2)
        metrics.inc_pubdev_429()
        metrics.inc_publish_conflict()
        metrics.mark_commit(datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc))

        server = daemon.MetricsServer(
            "127.0.0.1",
            0,
            lambda: metrics.render(
                queue_size=3,
                now=datetime(2026, 1, 1, 0, 0, 5, tzinfo=timezone.utc),
            ),
        )
        server.start()
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.port}/metrics",
                timeout=5,
            ) as response:
                payload = response.read().decode("utf-8")
        finally:
            server.stop()

        self.assertIn("# TYPE pubdb_queue_size gauge", payload)
        self.assertIn("pubdb_queue_size 3", payload)
        self.assertIn("pubdb_entries_collected_total 2", payload)
        self.assertIn("pubdb_pubdev_429_total 1", payload)
        self.assertIn("pubdb_publish_conflict_total 1", payload)
        self.assertIn("pubdb_last_commit_age_seconds 5.000", payload)

    def test_metrics_endpoint_can_read_queue_size_from_server_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = daemon.WorkQueue(Path(tmp) / "queue.db")
            queue.enqueue(daemon.WorkItem("pkg", "1.0.0"))
            metrics = daemon.Metrics()
            server = daemon.MetricsServer(
                "127.0.0.1",
                0,
                lambda: metrics.render(queue_size=queue.queued_count()),
            )
            server.start()
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{server.port}/metrics",
                    timeout=5,
                ) as response:
                    payload = response.read().decode("utf-8")
            finally:
                server.stop()
                queue.close()

        self.assertIn("pubdb_queue_size 1", payload)


class DiscoveryTests(unittest.TestCase):
    def test_pubdev_discovery_retries_429_and_counts_metric(self) -> None:
        calls = 0
        sleeps: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(429, headers={"Retry-After": "2"})
            return httpx.Response(
                200,
                json={
                    "versions": [
                        {"version": "1.0.0"},
                        {"version": "not-semver"},
                    ]
                },
            )

        metrics = daemon.Metrics()
        client = httpx.Client(transport=httpx.MockTransport(handler))
        discovery = daemon.PubDevDiscovery(
            client=client,
            metrics=metrics,
            sleep=sleeps.append,
            max_retries=1,
        )
        try:
            self.assertEqual(discovery.versions("pkg"), ["1.0.0", "not-semver"])
        finally:
            discovery.close()

        self.assertEqual(calls, 2)
        self.assertEqual(metrics.pubdev_429_total, 1)
        self.assertEqual(sleeps, [2.0])

    def test_discover_work_skips_package_errors(self) -> None:
        class Discovery:
            def versions(self, package: str) -> list[str]:
                if package == "bad_pkg":
                    raise RuntimeError("temporary failure")
                return ["1.0.0"]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "db").mkdir()
            (root / "db" / "_top1000.json").write_text(
                json.dumps({"packages": ["bad_pkg", "good_pkg"]}),
                encoding="utf-8",
            )

            items = daemon.discover_work(root, Discovery())

        self.assertEqual(
            items,
            [daemon.WorkItem("good_pkg", "1.0.0", daemon.BASE_VARIANT)],
        )

    def test_discover_work_enqueues_missing_obfuscated_variant_after_base(self) -> None:
        calls: list[str] = []

        class Discovery:
            def versions(self, package: str) -> list[str]:
                calls.append(package)
                return ["1.0.0"]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry_path = root / "db" / "fake_pkg" / "1.0.0.json"
            entry_path.parent.mkdir(parents=True)
            entry = _valid_entry("fake_pkg", "1.0.0")
            entry["collected_at"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
            entry_path.write_text(
                json.dumps(entry),
                encoding="utf-8",
            )
            (root / "db" / "_index.json").write_text(
                json.dumps(
                    {
                        "pubdb_schema_version": 1,
                        "generated_at": None,
                        "packages": {"fake_pkg": ["1.0.0"]},
                    }
                ),
                encoding="utf-8",
            )

            items = daemon.discover_work(root, Discovery())

        self.assertEqual(calls, ["fake_pkg"])
        self.assertEqual(
            items,
            [
                daemon.WorkItem(
                    "fake_pkg",
                    "1.0.0",
                    daemon.OBFUSCATED_VARIANT,
                    daemon.PRIORITY_MISSING_OBF,
                )
            ],
        )

    def test_discover_work_enqueues_configured_flutter_variants_after_base(self) -> None:
        class Discovery:
            def versions(self, package: str) -> list[str]:
                return ["1.0.0"]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry_path = root / "db" / "fake_pkg" / "1.0.0.json"
            entry_path.parent.mkdir(parents=True)
            entry = _valid_entry("fake_pkg", "1.0.0")
            entry["collected_at"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
            entry_path.write_text(
                json.dumps(entry),
                encoding="utf-8",
            )
            (root / "db" / "_index.json").write_text(
                json.dumps(
                    {
                        "pubdb_schema_version": 1,
                        "generated_at": None,
                        "packages": {"fake_pkg": ["1.0.0"]},
                    }
                ),
                encoding="utf-8",
            )
            (root / "db" / "_flutter_versions.json").write_text(
                json.dumps(["3.41.0", "3.44.0"]),
                encoding="utf-8",
            )

            items = daemon.discover_work(root, Discovery())

        self.assertEqual(
            items,
            [
                daemon.WorkItem(
                    "fake_pkg",
                    "1.0.0",
                    daemon.OBFUSCATED_VARIANT,
                    daemon.PRIORITY_MISSING_OBF,
                ),
                daemon.WorkItem(
                    "fake_pkg",
                    "1.0.0",
                    "flutter-3.41.0",
                    daemon.PRIORITY_MISSING_FLUTTER_VARIANT,
                ),
                daemon.WorkItem(
                    "fake_pkg",
                    "1.0.0",
                    "flutter-3.44.0",
                    daemon.PRIORITY_MISSING_FLUTTER_VARIANT,
                ),
            ],
        )

    def test_discover_work_does_not_enqueue_recorded_flutter_skip(self) -> None:
        class Discovery:
            def versions(self, package: str) -> list[str]:
                return ["1.0.0"]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry_path = root / "db" / "fake_pkg" / "1.0.0.json"
            entry_path.parent.mkdir(parents=True)
            entry = _valid_entry("fake_pkg", "1.0.0")
            entry["collected_at"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
            entry_path.write_text(
                json.dumps(entry),
                encoding="utf-8",
            )
            (root / "db" / "_index.json").write_text(
                json.dumps(
                    {
                        "pubdb_schema_version": 1,
                        "generated_at": None,
                        "packages": {"fake_pkg": ["1.0.0"]},
                    }
                ),
                encoding="utf-8",
            )
            (root / "db" / "_flutter_versions.json").write_text(
                json.dumps(["3.41.0", "3.44.0"]),
                encoding="utf-8",
            )
            (root / "db" / "_flutter_variant_skips.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-01-01T00:00:00Z",
                        "skips": [
                            {
                                "package": "fake_pkg",
                                "version": "1.0.0",
                                "flutter_version": "3.41.0",
                                "reason": "unsupported",
                                "collected_at": "2026-01-01T00:00:00Z",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            items = daemon.discover_work(root, Discovery())

        self.assertNotIn(
            daemon.WorkItem(
                "fake_pkg",
                "1.0.0",
                "flutter-3.41.0",
                daemon.PRIORITY_MISSING_FLUTTER_VARIANT,
            ),
            items,
        )
        self.assertIn(
            daemon.WorkItem(
                "fake_pkg",
                "1.0.0",
                "flutter-3.44.0",
                daemon.PRIORITY_MISSING_FLUTTER_VARIANT,
            ),
            items,
        )

    def test_discover_work_unions_worklist_with_nonempty_index(self) -> None:
        class Discovery:
            def versions(self, package: str) -> list[str]:
                return ["1.0.0"]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry_path = root / "db" / "fake_pkg" / "1.0.0.json"
            entry_path.parent.mkdir(parents=True)
            entry = _valid_entry("fake_pkg", "1.0.0")
            entry["collected_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            entry_path.write_text(json.dumps(entry), encoding="utf-8")
            (root / "db" / "_index.json").write_text(
                json.dumps(
                    {"pubdb_schema_version": 1, "generated_at": None, "packages": {"fake_pkg": ["1.0.0"]}}
                ),
                encoding="utf-8",
            )
            # new_pkg is only in the worklist, not the (non-empty) index.
            (root / "db" / "_top1000.json").write_text(
                json.dumps({"packages": ["fake_pkg", "new_pkg"]}), encoding="utf-8"
            )

            items = daemon.discover_work(root, Discovery())

        # The worklist-only package is picked up despite the index being non-empty.
        self.assertIn(
            daemon.WorkItem("new_pkg", "1.0.0", daemon.BASE_VARIANT, daemon.PRIORITY_MISSING_BASE),
            items,
        )

    def test_discover_work_base_only_skips_variants(self) -> None:
        class Discovery:
            def versions(self, package: str) -> list[str]:
                return ["1.0.0"]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry_path = root / "db" / "fake_pkg" / "1.0.0.json"
            entry_path.parent.mkdir(parents=True)
            entry = _valid_entry("fake_pkg", "1.0.0")
            entry["collected_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            entry_path.write_text(json.dumps(entry), encoding="utf-8")
            (root / "db" / "_index.json").write_text(
                json.dumps(
                    {"pubdb_schema_version": 1, "generated_at": None, "packages": {"fake_pkg": ["1.0.0"]}}
                ),
                encoding="utf-8",
            )
            (root / "db" / "_flutter_versions.json").write_text(
                json.dumps(["3.44.0"]), encoding="utf-8"
            )

            normal = daemon.discover_work(root, Discovery())
            base_only = daemon.discover_work(root, Discovery(), base_only=True)

        # Default mode enqueues the obfuscated variant for the fresh base entry.
        self.assertIn(
            daemon.WorkItem("fake_pkg", "1.0.0", daemon.OBFUSCATED_VARIANT, daemon.PRIORITY_MISSING_OBF),
            normal,
        )
        # base-only enqueues nothing for an already-fresh base entry.
        self.assertEqual(base_only, [])

    def test_entry_relative_path_uses_obfuscated_variant_filename(self) -> None:
        path = daemon.entry_relative_path(
            daemon.WorkItem("fake_pkg", "1.0.0", daemon.OBFUSCATED_VARIANT)
        )

        self.assertEqual(path.as_posix(), "db/fake_pkg/1.0.0.obf.json")

    def test_entry_relative_path_uses_flutter_variant_filename(self) -> None:
        path = daemon.entry_relative_path(
            daemon.WorkItem("fake_pkg", "1.0.0", "flutter-3.44.0")
        )

        self.assertEqual(path.as_posix(), "db/fake_pkg/1.0.0.flutter-3.44.0.json")


class FakePipeline:
    def __init__(self) -> None:
        self.items: list[daemon.WorkItem] = []

    def collect(self, item: daemon.WorkItem) -> dict[str, object]:
        self.items.append(item)
        return _valid_entry(item.package, item.version)


class FakeSkipPipeline:
    def collect(self, item: daemon.WorkItem) -> dict[str, object]:
        raise daemon.PipelineSkip(
            {
                "package": item.package,
                "version": item.version,
                "flutter_version": "3.44.0",
                "reason": "Dart SDK incompatible",
                "collected_at": "2026-01-01T00:00:00Z",
            }
        )


class FakeGit:
    def __init__(self) -> None:
        self.adds: list[list[str]] = []
        self.commits: list[str] = []
        self.pushes = 0

    def add(self, paths) -> None:
        self.adds.append([Path(path).as_posix() for path in paths])

    def commit(self, message: str) -> bool:
        self.commits.append(message)
        return True

    def push_with_rebase_retry(self, revalidate) -> None:
        revalidate()
        self.pushes += 1


class FakeCommittedGit(FakeGit):
    def __init__(self, key: str) -> None:
        super().__init__()
        self.key = key

    def has_commit_key(self, key: str) -> bool:
        return key == self.key


class FakeSkipVanishGit(FakeCommittedGit):
    def __init__(self, key: str, skip_path: Path) -> None:
        super().__init__(key)
        self.skip_path = skip_path

    def push_with_rebase_retry(self, revalidate) -> None:
        self.skip_path.unlink()
        revalidate()
        self.pushes += 1


class FakeFlakyPushGit(FakeGit):
    def __init__(self) -> None:
        super().__init__()
        self.commit_keys: set[str] = set()

    def commit(self, message: str) -> bool:
        self.commits.append(message)
        for line in message.splitlines():
            prefix = "pubdb-commit-key: "
            if line.startswith(prefix):
                self.commit_keys.add(line[len(prefix):])
        return True

    def push_with_rebase_retry(self, revalidate) -> None:
        revalidate()
        self.pushes += 1
        if self.pushes == 1:
            raise RuntimeError("simulated push failure")

    def has_commit_key(self, key: str) -> bool:
        return key in self.commit_keys


class OnceFlowTests(unittest.TestCase):
    def test_once_collects_one_item_writes_valid_entry_and_publishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "db").mkdir()
            (repo_root / "db" / "_index.json").write_text(
                json.dumps(
                    {
                        "pubdb_schema_version": 1,
                        "generated_at": None,
                        "packages": {},
                    }
                ),
                encoding="utf-8",
            )

            queue = daemon.WorkQueue(repo_root / "queue.db")
            queue.enqueue(
                daemon.WorkItem(
                    "fake_pkg",
                    "1.0.0",
                    priority=daemon.PRIORITY_MISSING_BASE,
                )
            )
            metrics = daemon.Metrics()
            validator = daemon.EntryValidator(SCHEMA_PATH)
            writer = daemon.AtomicEntryWriter(repo_root, validator)
            git = FakeGit()
            publisher = daemon.GitPublisher(
                repo_root=repo_root,
                writer=writer,
                checkout_lock=daemon.CheckoutLock(repo_root / "checkout.lock"),
                git=git,
                metrics=metrics,
                batch_size=10,
                push_interval=999.0,
            )
            pipeline = FakePipeline()
            collector = daemon.CollectorDaemon(
                repo_root=repo_root,
                queue=queue,
                pipeline=pipeline,
                publisher=publisher,
                metrics=metrics,
            )

            self.assertEqual(collector.run(once=True), 0)

            entry_path = repo_root / "db" / "fake_pkg" / "1.0.0.json"
            self.assertTrue(entry_path.is_file())
            validator.validate_file(entry_path)
            self.assertEqual(queue.dequeue(), None)
            self.assertEqual(len(pipeline.items), 1)
            self.assertEqual(len(git.commits), 1)
            self.assertEqual(git.pushes, 1)
            self.assertIn(
                "pubdb-commit-key: fake_pkg:1.0.0:base:schema-v1",
                git.commits[0],
            )
            self.assertEqual(metrics.entries_collected_total, 1)
            self.assertIn("db/fake_pkg/1.0.0.json", git.adds[0])
            self.assertIn("db/_index.json", git.adds[0])

            index = json.loads((repo_root / "db" / "_index.json").read_text())
            self.assertEqual(index["packages"], {"fake_pkg": ["1.0.0"]})
            queue.close()

    def test_once_records_flutter_variant_skip_and_marks_item_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "db").mkdir()

            queue = daemon.WorkQueue(repo_root / "queue.db")
            queue.enqueue(
                daemon.WorkItem(
                    "fake_pkg",
                    "1.0.0",
                    "flutter-3.44.0",
                    daemon.PRIORITY_MISSING_FLUTTER_VARIANT,
                )
            )
            metrics = daemon.Metrics()
            validator = daemon.EntryValidator(SCHEMA_PATH)
            writer = daemon.AtomicEntryWriter(repo_root, validator)
            git = FakeGit()
            publisher = daemon.GitPublisher(
                repo_root=repo_root,
                writer=writer,
                checkout_lock=daemon.CheckoutLock(repo_root / "checkout.lock"),
                git=git,
                metrics=metrics,
                batch_size=10,
                push_interval=999.0,
            )
            collector = daemon.CollectorDaemon(
                repo_root=repo_root,
                queue=queue,
                pipeline=FakeSkipPipeline(),
                publisher=publisher,
                metrics=metrics,
            )

            self.assertEqual(collector.run(once=True), 0)

            skip_path = repo_root / "db" / "_flutter_variant_skips.json"
            payload = json.loads(skip_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["skips"][0]["flutter_version"], "3.44.0")
            self.assertEqual(queue.dequeue(), None)
            self.assertEqual(len(git.commits), 1)
            self.assertIn("db/_flutter_variant_skips.json", git.adds[0])
            self.assertIn(
                "pubdb-commit-key: fake_pkg:1.0.0:flutter-3.44.0:schema-v1",
                git.commits[0],
            )
            queue.close()

    def test_retry_skips_collection_when_commit_key_already_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            entry_path = repo_root / "db" / "fake_pkg" / "1.0.0.json"
            entry_path.parent.mkdir(parents=True)
            entry_path.write_text(
                json.dumps(_valid_entry("fake_pkg", "1.0.0")),
                encoding="utf-8",
            )

            queue = daemon.WorkQueue(repo_root / "queue.db")
            item = daemon.WorkItem("fake_pkg", "1.0.0")
            queue.enqueue(item)
            self.assertIsNotNone(queue.dequeue())
            queue.close()

            queue = daemon.WorkQueue(repo_root / "queue.db")
            metrics = daemon.Metrics()
            validator = daemon.EntryValidator(SCHEMA_PATH)
            writer = daemon.AtomicEntryWriter(repo_root, validator)
            git = FakeCommittedGit(item.commit_key)
            publisher = daemon.GitPublisher(
                repo_root=repo_root,
                writer=writer,
                checkout_lock=daemon.CheckoutLock(repo_root / "checkout.lock"),
                git=git,
                metrics=metrics,
            )
            pipeline = FakePipeline()
            collector = daemon.CollectorDaemon(
                repo_root=repo_root,
                queue=queue,
                pipeline=pipeline,
                publisher=publisher,
                metrics=metrics,
            )

            self.assertEqual(collector.run(once=True), 0)

            self.assertEqual(pipeline.items, [])
            self.assertEqual(git.commits, [])
            self.assertEqual(git.pushes, 1)
            self.assertEqual(queue.dequeue(), None)
            queue.close()

    def test_committed_flutter_skip_retry_requires_skip_record_after_rebase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            skip_path = repo_root / "db" / "_flutter_variant_skips.json"
            skip_path.parent.mkdir(parents=True)
            skip_path.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-01-01T00:00:00Z",
                        "skips": [
                            {
                                "package": "fake_pkg",
                                "version": "1.0.0",
                                "flutter_version": "3.44.0",
                                "reason": "unsupported",
                                "collected_at": "2026-01-01T00:00:00Z",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            item = daemon.WorkItem(
                "fake_pkg",
                "1.0.0",
                "flutter-3.44.0",
                daemon.PRIORITY_MISSING_FLUTTER_VARIANT,
            )
            validator = daemon.EntryValidator(SCHEMA_PATH)
            writer = daemon.AtomicEntryWriter(repo_root, validator)
            git = FakeSkipVanishGit(item.commit_key, skip_path)
            publisher = daemon.GitPublisher(
                repo_root=repo_root,
                writer=writer,
                checkout_lock=daemon.CheckoutLock(repo_root / "checkout.lock"),
                git=git,
                metrics=daemon.Metrics(),
            )

            with self.assertRaisesRegex(RuntimeError, "skip record vanished"):
                publisher.complete_if_committed(item)

    def test_failed_push_keeps_pending_item_for_retry_without_duplicate_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "db").mkdir()
            (repo_root / "db" / "_index.json").write_text(
                json.dumps(
                    {
                        "pubdb_schema_version": 1,
                        "generated_at": None,
                        "packages": {},
                    }
                ),
                encoding="utf-8",
            )

            queue = daemon.WorkQueue(repo_root / "queue.db")
            item = daemon.WorkItem("fake_pkg", "1.0.0")
            queue.enqueue(item)
            metrics = daemon.Metrics()
            validator = daemon.EntryValidator(SCHEMA_PATH)
            writer = daemon.AtomicEntryWriter(repo_root, validator)
            git = FakeFlakyPushGit()
            publisher = daemon.GitPublisher(
                repo_root=repo_root,
                writer=writer,
                checkout_lock=daemon.CheckoutLock(repo_root / "checkout.lock"),
                git=git,
                metrics=metrics,
            )
            collector = daemon.CollectorDaemon(
                repo_root=repo_root,
                queue=queue,
                pipeline=FakePipeline(),
                publisher=publisher,
                metrics=metrics,
            )

            self.assertTrue(collector.process_one(force_flush=True))
            claimed_item = daemon.WorkItem("fake_pkg", "1.0.0", attempts=1)
            self.assertEqual(len(git.commits), 1)
            self.assertEqual(git.pushes, 1)
            self.assertEqual(publisher.pending_items, [claimed_item])
            self.assertIsNone(queue.dequeue())

            completed = collector._flush_completed(force=True)
            self.assertEqual(completed, [claimed_item])
            self.assertEqual(len(git.commits), 1)
            self.assertEqual(git.pushes, 2)
            self.assertEqual(publisher.pending_items, [])
            self.assertIsNone(queue.dequeue())
            queue.close()


class BarrierPipeline:
    """Collects only when ``parties`` calls run concurrently; proves overlap."""

    def __init__(self, parties: int) -> None:
        self.barrier = threading.Barrier(parties, timeout=5.0)
        self.items: list[daemon.WorkItem] = []
        self._lock = threading.Lock()

    def collect(self, item: daemon.WorkItem) -> dict[str, object]:
        # Raises BrokenBarrierError (-> dead-letter) if fewer than ``parties``
        # collectors run at once, so a serial run would fail the test.
        self.barrier.wait()
        with self._lock:
            self.items.append(item)
        return _valid_entry(item.package, item.version)


class ConcurrentRunTests(unittest.TestCase):
    def _make_collector(
        self,
        repo_root: Path,
        queue: daemon.WorkQueue,
        pipeline: object,
        *,
        workers: int,
    ) -> tuple[daemon.CollectorDaemon, FakeGit, daemon.Metrics]:
        (repo_root / "db").mkdir(exist_ok=True)
        (repo_root / "db" / "_index.json").write_text(
            json.dumps(
                {"pubdb_schema_version": 1, "generated_at": None, "packages": {}}
            ),
            encoding="utf-8",
        )
        metrics = daemon.Metrics()
        validator = daemon.EntryValidator(SCHEMA_PATH)
        writer = daemon.AtomicEntryWriter(repo_root, validator)
        git = FakeGit()
        publisher = daemon.GitPublisher(
            repo_root=repo_root,
            writer=writer,
            checkout_lock=daemon.CheckoutLock(repo_root / "checkout.lock"),
            git=git,
            metrics=metrics,
            batch_size=100,
            push_interval=999.0,
        )
        collector = daemon.CollectorDaemon(
            repo_root=repo_root,
            queue=queue,
            pipeline=pipeline,
            publisher=publisher,
            metrics=metrics,
            workers=workers,
            tick_interval=0.01,
        )
        # Stop the loop the first time it goes idle (queue + inflight drained).
        collector.sleep = lambda _seconds: collector.request_shutdown()
        return collector, git, metrics

    def test_workers_clamped_to_at_least_one(self) -> None:
        collector = daemon.CollectorDaemon(
            repo_root=Path("."),
            queue=mock.Mock(),
            pipeline=mock.Mock(),
            publisher=mock.Mock(),
            metrics=daemon.Metrics(),
            workers=0,
        )
        self.assertEqual(collector.workers, 1)

    def test_concurrent_run_collects_and_publishes_every_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            queue = daemon.WorkQueue(repo_root / "queue.db")
            for index in range(8):
                queue.enqueue(daemon.WorkItem("fake_pkg", f"1.0.{index}"))
            collector, git, metrics = self._make_collector(
                repo_root, queue, FakePipeline(), workers=4
            )

            self.assertEqual(collector.run(), 0)

            self.assertEqual(metrics.entries_collected_total, 8)
            self.assertIsNone(queue.dequeue())
            self.assertEqual(git.pushes, 1)
            published = {
                line.split("pubdb-commit-key: ", 1)[1]
                for commit in git.commits
                for line in commit.splitlines()
                if line.startswith("pubdb-commit-key: ")
            }
            self.assertEqual(
                published,
                {f"fake_pkg:1.0.{index}:base:schema-v1" for index in range(8)},
            )
            for index in range(8):
                entry_path = repo_root / "db" / "fake_pkg" / f"1.0.{index}.json"
                self.assertTrue(entry_path.is_file())
            queue.close()

    def test_collectors_actually_run_in_parallel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            queue = daemon.WorkQueue(repo_root / "queue.db")
            for index in range(3):
                queue.enqueue(daemon.WorkItem("fake_pkg", f"2.0.{index}"))
            pipeline = BarrierPipeline(parties=3)
            collector, _git, metrics = self._make_collector(
                repo_root, queue, pipeline, workers=3
            )

            self.assertEqual(collector.run(), 0)

            # All three only complete if they reached the barrier together.
            self.assertEqual(metrics.entries_collected_total, 3)
            self.assertEqual(len(pipeline.items), 3)
            self.assertIsNone(queue.dequeue())
            queue.close()


class BuildDaemonTests(unittest.TestCase):
    def test_build_daemon_threads_workers_and_pubdev_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                repo_root=REPO_ROOT,
                cache_root=Path(tmp),
                queue_db=Path(tmp) / "queue.db",
                archive_cache_dir=None,
                obfuscated_work_dir=None,
                flutter_variant_work_dir=None,
                flutter_cache_dir=None,
                flutter="flutter",
                rettulf="rettulf",
                obfuscated_timeout=1.0,
                pubdev_timeout=12.5,
                lock_path=None,
                batch_size=10,
                push_interval=300.0,
                tick_interval=60.0,
                discover_interval=3600.0,
                workers=3,
                base_only=False,
                packages=None,
                no_push=True,
            )
            built = daemon.build_daemon(args)
            try:
                self.assertEqual(built.workers, 3)
                self.assertEqual(built.pipeline.pubdev_timeout, 12.5)
            finally:
                built.queue.close()
                close = getattr(built.discovery, "close", None)
                if callable(close):
                    close()


class StatusSnapshotTests(unittest.TestCase):
    def _seed(self, root: Path) -> daemon.WorkQueue:
        (root / "db").mkdir()
        (root / "db" / "_top1000.json").write_text(
            json.dumps({"packages": ["provider", "dio", "http"]}), encoding="utf-8"
        )
        (root / "db" / "_index.json").write_text(
            json.dumps(
                {
                    "pubdb_schema_version": 1,
                    "generated_at": None,
                    # "leftpad" is NOT in the worklist; its versions must not
                    # inflate coverage.versions_collected.
                    "packages": {
                        "provider": ["6.0.5", "6.0.0"],
                        "leftpad": ["1.0.0", "1.0.1", "1.0.2"],
                    },
                }
            ),
            encoding="utf-8",
        )
        queue = daemon.WorkQueue(root / "queue.db")
        queue.enqueue(daemon.WorkItem("provider", "6.0.5"))
        queue.enqueue(
            daemon.WorkItem("dio", "5.4.0", daemon.OBFUSCATED_VARIANT, daemon.PRIORITY_MISSING_OBF)
        )
        queue.enqueue(
            daemon.WorkItem(
                "http", "1.0.0", "flutter-3.44.0", daemon.PRIORITY_MISSING_FLUTTER_VARIANT
            )
        )
        claimed = queue.dequeue()
        assert claimed is not None
        queue.fail(
            daemon.WorkItem(
                claimed.package, claimed.version, claimed.variant, attempts=daemon.DEFAULT_MAX_ATTEMPTS
            ),
            "boom: kaboom",
        )
        return queue

    def test_snapshot_aggregates_queue_metrics_and_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = self._seed(root)
            metrics = daemon.Metrics()
            metrics.inc_entries(7)
            metrics.inc_pubdev_429()
            metrics.mark_commit(datetime(2026, 1, 1, tzinfo=timezone.utc))

            snap = daemon.status_snapshot(
                root,
                queue,
                metrics,
                workers=4,
                push=False,
                now=datetime(2026, 1, 1, 0, 0, 30, tzinfo=timezone.utc),
            )
            queue.close()

        self.assertEqual(snap["queue"]["by_state"], {
            "queued": 2, "in_progress": 0, "done": 0, "failed": 1,
        })
        self.assertEqual(snap["queue"]["by_variant"], {"base": 1, "obf": 1, "flutter": 1})
        self.assertEqual(snap["throughput"]["entries_collected_total"], 7)
        self.assertEqual(snap["throughput"]["pubdev_429_total"], 1)
        self.assertEqual(snap["throughput"]["last_commit_age_seconds"], 30.0)
        self.assertEqual(snap["meta"], {
            "pubdb_schema_version": 1,
            "generated_at": "2026-01-01T00:00:30Z",  # honors the passed `now`
            "workers": 4,
            "push_enabled": False,
        })
        self.assertEqual(snap["coverage"]["worklist_packages"], 3)
        self.assertEqual(snap["coverage"]["packages_with_entries"], 1)
        # provider's 2 versions only; leftpad (off-worklist) is excluded.
        self.assertEqual(snap["coverage"]["versions_collected"], 2)
        self.assertEqual(snap["coverage"]["percent_packages"], 33.3)
        self.assertEqual(len(snap["recent_failures"]), 1)
        self.assertEqual(snap["recent_failures"][0]["package"], "provider")
        self.assertEqual(snap["recent_failures"][0]["last_error"], "boom: kaboom")

    def test_recent_failures_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = daemon.WorkQueue(Path(tmp) / "queue.db")
            for i in range(5):
                queue.enqueue(daemon.WorkItem("pkg", f"1.0.{i}"))
            for _ in range(5):
                item = queue.dequeue()
                assert item is not None
                queue.fail(
                    daemon.WorkItem(item.package, item.version, attempts=daemon.DEFAULT_MAX_ATTEMPTS),
                    "x",
                )
            self.assertEqual(len(queue.recent_failures(limit=2)), 2)
            self.assertEqual(len(queue.recent_failures()), 5)
            queue.close()


class DashboardServerTests(unittest.TestCase):
    def _get(self, port: int, path: str) -> tuple[int, str, str]:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
                return resp.status, resp.headers.get("Content-Type", ""), resp.read().decode()
        except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
            exc.close()
            return exc.code, "", ""

    def test_dashboard_serves_html_json_and_metrics(self) -> None:
        metrics = daemon.Metrics()
        status = lambda: {"meta": {"workers": 1}, "queue": {}, "throughput": {}, "recent_failures": [], "coverage": {}}  # noqa: E731
        server = daemon.MetricsServer(
            "127.0.0.1", 0, lambda: metrics.render(queue_size=0), status=status
        )
        server.start()
        try:
            code, ctype, body = self._get(server.port, "/")
            self.assertEqual(code, 200)
            self.assertIn("text/html", ctype)
            self.assertIn("rettulf-pubdb collector", body)
            self.assertIn("/api/status", body)

            code, ctype, body = self._get(server.port, "/api/status")
            self.assertEqual(code, 200)
            self.assertIn("application/json", ctype)
            self.assertEqual(json.loads(body)["meta"]["workers"], 1)

            code, ctype, body = self._get(server.port, "/metrics")
            self.assertEqual(code, 200)
            self.assertIn("pubdb_queue_size", body)

            self.assertEqual(self._get(server.port, "/nope")[0], 404)
        finally:
            server.stop()

    def test_dashboard_disabled_returns_404_but_metrics_still_served(self) -> None:
        metrics = daemon.Metrics()
        server = daemon.MetricsServer(
            "127.0.0.1", 0, lambda: metrics.render(queue_size=0), status=None
        )
        server.start()
        try:
            self.assertEqual(self._get(server.port, "/")[0], 404)
            self.assertEqual(self._get(server.port, "/api/status")[0], 404)
            self.assertEqual(self._get(server.port, "/metrics")[0], 200)
        finally:
            server.stop()


class GitRepositoryTests(unittest.TestCase):
    def test_has_commit_key_matches_whole_key_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _git(repo_root, "init", "-b", "main")
            _git(repo_root, "config", "user.name", "Test User")
            _git(repo_root, "config", "user.email", "test@example.com")

            (repo_root / "first.txt").write_text("first", encoding="utf-8")
            _git(repo_root, "add", "first.txt")
            _git(
                repo_root,
                "commit",
                "-m",
                "first",
                "-m",
                "pubdb-commit-key: fake_pkg:1.0.0:base:schema-v10",
            )

            repository = daemon.GitRepository(repo_root)
            self.assertFalse(
                repository.has_commit_key("fake_pkg:1.0.0:base:schema-v1")
            )

            (repo_root / "second.txt").write_text("second", encoding="utf-8")
            _git(repo_root, "add", "second.txt")
            _git(
                repo_root,
                "commit",
                "-m",
                "second",
                "-m",
                "pubdb-commit-key: fake_pkg:1.0.0:base:schema-v1",
            )

            self.assertTrue(
                repository.has_commit_key("fake_pkg:1.0.0:base:schema-v1")
            )

    def test_push_retry_aborts_failed_rebase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            repository = daemon.GitRepository(repo_root, retries=1)
            calls: list[list[str]] = []

            def fake_run(
                command,
                *,
                check=False,
                capture_output=False,
                text=False,
            ):
                calls.append(list(command))
                if command[-2:] == ["branch", "--show-current"]:
                    return subprocess.CompletedProcess(command, 0, stdout="main\n")
                if command[-3:] == ["push", "origin", "HEAD:main"]:
                    return subprocess.CompletedProcess(
                        command,
                        1,
                        stdout="",
                        stderr="non-fast-forward",
                    )
                if command[-3:] == ["fetch", "origin", "main"]:
                    return subprocess.CompletedProcess(command, 0)
                if command[-2:] == ["rebase", "origin/main"]:
                    raise subprocess.CalledProcessError(1, command)
                if command[-2:] == ["rebase", "--abort"]:
                    return subprocess.CompletedProcess(command, 0)
                raise AssertionError(f"unexpected command: {command}")

            with mock.patch.object(daemon.subprocess, "run", fake_run):
                with self.assertRaisesRegex(RuntimeError, "aborted rebase"):
                    repository.push_with_rebase_retry(lambda: None)

        self.assertIn(
            ["git", "-C", str(repository.repo_root), "rebase", "--abort"],
            calls,
        )


def _git(repo_root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
