from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

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


class FakePipeline:
    def __init__(self) -> None:
        self.items: list[daemon.WorkItem] = []

    def collect(self, item: daemon.WorkItem) -> dict[str, object]:
        self.items.append(item)
        return _valid_entry(item.package, item.version)


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

    def test_once_skips_collection_when_commit_key_already_exists(self) -> None:
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
