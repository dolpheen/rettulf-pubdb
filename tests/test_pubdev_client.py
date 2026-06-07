from __future__ import annotations

import contextlib
import hashlib
import io
import json
import multiprocessing
import os
import tarfile
import tempfile
import threading
import time
import traceback
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import httpx

from collector import pubdev_client


def _archive_bytes(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in files.items():
            payload = content.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.mode = 0o644
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _metadata(
    package: str, version: str, archive_url: str, sha256: str
) -> dict[str, object]:
    version_info = {
        "version": version,
        "archive_url": archive_url,
        "archive_sha256": sha256,
        "pubspec": {"name": package, "version": version},
    }
    return {"name": package, "latest": version_info, "versions": [version_info]}


class _FailingStream(httpx.SyncByteStream):
    def __iter__(self):
        yield b"partial"
        raise httpx.ReadError("connection dropped")


def _process_fetch(
    base_url: str, cache_dir: str, result_queue: multiprocessing.Queue
) -> None:
    try:
        with pubdev_client.PubDevClient(
            base_url=base_url,
            cache_dir=Path(cache_dir),
            backoff_initial=0.001,
        ) as client:
            path = client.fetch("concurrent_pkg", "1.0.0")
        result_queue.put(("ok", str(path), (path / "concurrent_pkg.dart").read_text()))
    except Exception as exc:  # pragma: no cover - only reported on failure
        result_queue.put(("error", repr(exc), traceback.format_exc()))


class PubDevClientTests(unittest.TestCase):
    def test_fetch_downloads_extracts_and_cache_hit_skips_network(self) -> None:
        archive = _archive_bytes({"lib/provider.dart": "library provider;"})
        archive_url = "https://pub.dev/api/archives/provider-6.0.5.tar.gz"
        calls = {"metadata": 0, "archive": 0}
        user_agents: list[str] = []
        accepts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            user_agents.append(request.headers["user-agent"])
            accepts.append(request.headers["accept"])
            if request.url.path == "/api/packages/provider":
                calls["metadata"] += 1
                return httpx.Response(
                    200,
                    json=_metadata("provider", "6.0.5", archive_url, _sha256(archive)),
                )
            if request.url.path == "/api/archives/provider-6.0.5.tar.gz":
                calls["archive"] += 1
                return httpx.Response(200, content=archive)
            return httpx.Response(404)

        with tempfile.TemporaryDirectory() as tmp:
            http_client = httpx.Client(transport=httpx.MockTransport(handler))
            with pubdev_client.PubDevClient(
                cache_dir=Path(tmp),
                client=http_client,
                sleep=lambda _: None,
            ) as client:
                first = client.fetch("provider", "6.0.5")
                second = client.fetch("provider", "6.0.5")

            self.assertEqual(first, second)
            self.assertEqual((first / "provider.dart").read_text(), "library provider;")
            self.assertEqual(calls, {"metadata": 1, "archive": 1})
            self.assertEqual(user_agents, [pubdev_client.USER_AGENT] * 2)
            self.assertEqual(accepts[0], "application/vnd.pub.v2+json")

    def test_429_honors_retry_after(self) -> None:
        archive = _archive_bytes({"lib/pkg.dart": ""})
        archive_url = "https://pub.dev/api/archives/pkg-1.0.0.tar.gz"
        sleeps: list[float] = []
        metadata_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal metadata_calls
            if request.url.path == "/api/packages/pkg":
                metadata_calls += 1
                if metadata_calls == 1:
                    return httpx.Response(429, headers={"Retry-After": "2"})
                return httpx.Response(
                    200,
                    json=_metadata("pkg", "1.0.0", archive_url, _sha256(archive)),
                )
            if request.url.path == "/api/archives/pkg-1.0.0.tar.gz":
                return httpx.Response(200, content=archive)
            return httpx.Response(404)

        with tempfile.TemporaryDirectory() as tmp:
            http_client = httpx.Client(transport=httpx.MockTransport(handler))
            with pubdev_client.PubDevClient(
                cache_dir=Path(tmp),
                client=http_client,
                sleep=sleeps.append,
                backoff_initial=0.01,
            ) as client:
                path = client.fetch("pkg", "1.0.0")
                fetched_file_exists = (path / "pkg.dart").is_file()

        self.assertTrue(fetched_file_exists)
        self.assertEqual(metadata_calls, 2)
        self.assertEqual(sleeps, [2.0])

    def test_429_clamps_large_retry_after(self) -> None:
        archive = _archive_bytes({"lib/pkg.dart": ""})
        archive_url = "https://pub.dev/api/archives/pkg-1.0.0.tar.gz"
        sleeps: list[float] = []
        metadata_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal metadata_calls
            if request.url.path == "/api/packages/pkg":
                metadata_calls += 1
                if metadata_calls == 1:
                    return httpx.Response(429, headers={"Retry-After": "86400"})
                return httpx.Response(
                    200,
                    json=_metadata("pkg", "1.0.0", archive_url, _sha256(archive)),
                )
            if request.url.path == "/api/archives/pkg-1.0.0.tar.gz":
                return httpx.Response(200, content=archive)
            return httpx.Response(404)

        with tempfile.TemporaryDirectory() as tmp:
            http_client = httpx.Client(transport=httpx.MockTransport(handler))
            with pubdev_client.PubDevClient(
                cache_dir=Path(tmp),
                client=http_client,
                sleep=sleeps.append,
                backoff_initial=0.01,
            ) as client:
                path = client.fetch("pkg", "1.0.0")
                fetched_file_exists = (path / "pkg.dart").is_file()

        self.assertTrue(fetched_file_exists)
        self.assertEqual(metadata_calls, 2)
        self.assertEqual(sleeps, [pubdev_client.MAX_RETRY_AFTER_SECONDS])

    def test_5xx_uses_exponential_backoff(self) -> None:
        archive = _archive_bytes({"lib/pkg.dart": ""})
        archive_url = "https://pub.dev/api/archives/pkg-1.0.0.tar.gz"
        sleeps: list[float] = []
        metadata_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal metadata_calls
            if request.url.path == "/api/packages/pkg":
                metadata_calls += 1
                if metadata_calls < 3:
                    return httpx.Response(503)
                return httpx.Response(
                    200,
                    json=_metadata("pkg", "1.0.0", archive_url, _sha256(archive)),
                )
            if request.url.path == "/api/archives/pkg-1.0.0.tar.gz":
                return httpx.Response(200, content=archive)
            return httpx.Response(404)

        with tempfile.TemporaryDirectory() as tmp:
            http_client = httpx.Client(transport=httpx.MockTransport(handler))
            with pubdev_client.PubDevClient(
                cache_dir=Path(tmp),
                client=http_client,
                sleep=sleeps.append,
                backoff_initial=0.01,
            ) as client:
                path = client.fetch("pkg", "1.0.0")
                fetched_file_exists = (path / "pkg.dart").is_file()

        self.assertTrue(fetched_file_exists)
        self.assertEqual(metadata_calls, 3)
        self.assertEqual(sleeps, [0.01, 0.02])

    def test_invalid_metadata_json_raises_pubdev_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/packages/pkg":
                return httpx.Response(200, content=b"{")
            return httpx.Response(404)

        with tempfile.TemporaryDirectory() as tmp:
            http_client = httpx.Client(transport=httpx.MockTransport(handler))
            with pubdev_client.PubDevClient(
                cache_dir=Path(tmp),
                client=http_client,
                sleep=lambda _: None,
            ) as client:
                with self.assertRaisesRegex(pubdev_client.PubDevError, "invalid JSON"):
                    client.fetch("pkg", "1.0.0")

    def test_checksum_mismatch_invalidates_cached_archive_and_refetches(self) -> None:
        good_archive = _archive_bytes({"lib/pkg.dart": "fresh"})
        bad_archive = _archive_bytes({"lib/pkg.dart": "stale"})
        archive_url = "https://pub.dev/api/archives/pkg-1.0.0.tar.gz"
        archive_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal archive_calls
            if request.url.path == "/api/packages/pkg":
                return httpx.Response(
                    200,
                    json=_metadata("pkg", "1.0.0", archive_url, _sha256(good_archive)),
                )
            if request.url.path == "/api/archives/pkg-1.0.0.tar.gz":
                archive_calls += 1
                return httpx.Response(200, content=good_archive)
            return httpx.Response(404)

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            (cache_dir / "pkg-1.0.0.tar.gz").write_bytes(bad_archive)
            stale_lib = cache_dir / "pkg-1.0.0" / "lib"
            stale_lib.mkdir(parents=True)
            (stale_lib / "pkg.dart").write_text("stale")

            http_client = httpx.Client(transport=httpx.MockTransport(handler))
            with pubdev_client.PubDevClient(
                cache_dir=cache_dir,
                client=http_client,
                sleep=lambda _: None,
            ) as client:
                path = client.fetch("pkg", "1.0.0")

            self.assertEqual((path / "pkg.dart").read_text(), "fresh")
            self.assertEqual(
                _sha256((cache_dir / "pkg-1.0.0.tar.gz").read_bytes()),
                _sha256(good_archive),
            )
            self.assertEqual(archive_calls, 1)

    def test_download_checksum_mismatch_retries_once(self) -> None:
        good_archive = _archive_bytes({"lib/pkg.dart": "fresh"})
        bad_archive = _archive_bytes({"lib/pkg.dart": "bad"})
        archive_url = "https://pub.dev/api/archives/pkg-1.0.0.tar.gz"
        archive_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal archive_calls
            if request.url.path == "/api/packages/pkg":
                return httpx.Response(
                    200,
                    json=_metadata("pkg", "1.0.0", archive_url, _sha256(good_archive)),
                )
            if request.url.path == "/api/archives/pkg-1.0.0.tar.gz":
                archive_calls += 1
                return httpx.Response(
                    200,
                    content=bad_archive if archive_calls == 1 else good_archive,
                )
            return httpx.Response(404)

        with tempfile.TemporaryDirectory() as tmp:
            http_client = httpx.Client(transport=httpx.MockTransport(handler))
            with pubdev_client.PubDevClient(
                cache_dir=Path(tmp),
                client=http_client,
                sleep=lambda _: None,
            ) as client:
                path = client.fetch("pkg", "1.0.0")
                fetched_content = (path / "pkg.dart").read_text()

        self.assertEqual(fetched_content, "fresh")
        self.assertEqual(archive_calls, 2)

    def test_download_stream_error_retries(self) -> None:
        archive = _archive_bytes({"lib/pkg.dart": "fresh"})
        archive_url = "https://pub.dev/api/archives/pkg-1.0.0.tar.gz"
        archive_calls = 0
        sleeps: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal archive_calls
            if request.url.path == "/api/packages/pkg":
                return httpx.Response(
                    200,
                    json=_metadata("pkg", "1.0.0", archive_url, _sha256(archive)),
                )
            if request.url.path == "/api/archives/pkg-1.0.0.tar.gz":
                archive_calls += 1
                if archive_calls == 1:
                    return httpx.Response(200, stream=_FailingStream())
                return httpx.Response(200, content=archive)
            return httpx.Response(404)

        with tempfile.TemporaryDirectory() as tmp:
            http_client = httpx.Client(transport=httpx.MockTransport(handler))
            with pubdev_client.PubDevClient(
                cache_dir=Path(tmp),
                client=http_client,
                sleep=sleeps.append,
                backoff_initial=0.01,
                max_retries=1,
            ) as client:
                path = client.fetch("pkg", "1.0.0")
                fetched_content = (path / "pkg.dart").read_text()

        self.assertEqual(fetched_content, "fresh")
        self.assertEqual(archive_calls, 2)
        self.assertEqual(sleeps, [0.01])

    def test_concurrent_process_fetches_do_not_corrupt_cache(self) -> None:
        archive = _archive_bytes({"lib/concurrent_pkg.dart": "library concurrent_pkg;"})
        archive_url = ""
        counts = {"metadata": 0, "archive": 0}
        counts_lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                nonlocal archive_url
                if self.path == "/api/packages/concurrent_pkg":
                    with counts_lock:
                        counts["metadata"] += 1
                    body = json.dumps(
                        _metadata(
                            "concurrent_pkg",
                            "1.0.0",
                            archive_url,
                            _sha256(archive),
                        )
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path == "/api/archives/concurrent_pkg-1.0.0.tar.gz":
                    with counts_lock:
                        counts["archive"] += 1
                    time.sleep(0.2)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(archive)))
                    self.end_headers()
                    self.wfile.write(archive)
                    return
                self.send_response(404)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        base_url = f"http://127.0.0.1:{server.server_port}"
        archive_url = f"{base_url}/api/archives/concurrent_pkg-1.0.0.tar.gz"
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        try:
            with tempfile.TemporaryDirectory() as tmp:
                context = multiprocessing.get_context("spawn")
                result_queue = context.Queue()
                processes = [
                    context.Process(
                        target=_process_fetch, args=(base_url, tmp, result_queue)
                    )
                    for _ in range(2)
                ]
                for process in processes:
                    process.start()
                for process in processes:
                    process.join(timeout=10)
                    if process.is_alive():
                        process.terminate()
                        self.fail("fetch process did not finish")

                results = [result_queue.get(timeout=3) for _ in processes]

            self.assertEqual([result[0] for result in results], ["ok", "ok"])
            self.assertEqual({result[1] for result in results}, {results[0][1]})
            self.assertEqual({result[2] for result in results}, {"library concurrent_pkg;"})
            self.assertEqual(counts, {"metadata": 1, "archive": 1})
        finally:
            server.shutdown()
            server.server_close()

    def test_cli_fetch_prints_extracted_path(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "pkg-1.0.0" / "lib"
            with mock.patch.object(pubdev_client, "fetch", return_value=fake_path):
                with contextlib.redirect_stdout(output):
                    exit_code = pubdev_client.main(["fetch", "pkg", "1.0.0"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(output.getvalue().strip(), str(fake_path))

    @unittest.skipUnless(
        os.environ.get("RUN_NETWORK_TESTS") == "1",
        "set RUN_NETWORK_TESTS=1 to hit pub.dev",
    )
    def test_network_provider_fetch_is_cached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with pubdev_client.PubDevClient(cache_dir=Path(tmp)) as client:
                first = client.fetch("provider", "6.0.5")
            self.assertTrue((first / "provider.dart").is_file())

            def no_network(request: httpx.Request) -> httpx.Response:
                self.fail(f"unexpected network request: {request.url}")

            http_client = httpx.Client(transport=httpx.MockTransport(no_network))
            with pubdev_client.PubDevClient(
                cache_dir=Path(tmp),
                client=http_client,
            ) as client:
                second = client.fetch("provider", "6.0.5")

        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
