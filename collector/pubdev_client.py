"""Fetch and cache pub.dev package archives for collector workers."""

from __future__ import annotations

import argparse
import email.utils
import fcntl
import hashlib
import json
import os
import shutil
import tarfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote

try:
    import httpx
except ModuleNotFoundError as exc:  # pragma: no cover - surfaced directly
    raise ModuleNotFoundError(
        "httpx is required (pip install -r collector/requirements.txt)"
    ) from exc

CLIENT_VERSION = "0.1.0"
USER_AGENT = (
    f"rettulf-pubdb/{CLIENT_VERSION} "
    "(+https://github.com/dolpheen/rettulf-pubdb)"
)
PUB_DEV_URL = "https://pub.dev"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "rettulf-pubdb" / "archives"
DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 0.2
MAX_BACKOFF_SECONDS = 8.0
MAX_RETRY_AFTER_SECONDS = 120.0
MARKER_FILE = ".rettulf-pubdb.json"

# In-flight reference counts keyed by the resolved absolute archive path, so the
# bounded cache only evicts an entry once every work item using it has finished.
# Collector workers are threads sharing this process, so the counter lives at
# module scope (guarded by ``_REFCOUNT_LOCK``) rather than per ``PubDevClient``.
_REFCOUNTS: dict[str, int] = {}
_REFCOUNT_LOCK = threading.Lock()


class PubDevError(RuntimeError):
    """Base error raised by the pub.dev archive client."""


class PackageVersionNotFound(PubDevError):
    """Raised when pub.dev metadata does not contain the requested version."""


class ChecksumMismatch(PubDevError):
    """Raised when a downloaded archive does not match pub.dev metadata."""


@dataclass(frozen=True)
class ArchiveMetadata:
    archive_url: str
    archive_sha256: str


class _FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = None

    def __enter__(self) -> "_FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+")
        fcntl.flock(self._file.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._file is None:
            return
        fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        self._file.close()
        self._file = None


class PubDevClient:
    """HTTPX-backed pub.dev archive fetcher with an on-disk cache."""

    def __init__(
        self,
        *,
        cache_dir: Path | str | None = None,
        base_url: str = PUB_DEV_URL,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] | None = None,
        max_retries: int = MAX_RETRIES,
        backoff_initial: float = INITIAL_BACKOFF_SECONDS,
        backoff_max: float = MAX_BACKOFF_SECONDS,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.cache_dir = Path(cache_dir).expanduser() if cache_dir else DEFAULT_CACHE_DIR
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self._owns_client = client is None
        self._sleep = sleep or time.sleep
        self.max_retries = max_retries
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max

    def __enter__(self) -> "PubDevClient":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def fetch(self, package: str, version: str) -> Path:
        """Return the cached extracted lib/ directory for a package version.

        Each successful fetch (cache hit or download+extract) takes one
        reference on the cache entry; ``evict`` releases it. The archive's
        files are only removed once the reference count drops back to zero.
        """
        cache_key = _cache_key(package, version)
        archive_path = self.cache_dir / f"{cache_key}.tar.gz"
        extract_dir = self.cache_dir / cache_key
        lib_dir = extract_dir / "lib"
        lock_path = self.cache_dir / ".locks" / f"{cache_key}.lock"

        with _FileLock(lock_path):
            if self._cache_hit(package, version, archive_path, extract_dir):
                _retain(archive_path)
                return lib_dir

            metadata = self._metadata_for(package, version)
            self._ensure_archive(archive_path, extract_dir, metadata)
            self._extract_archive(
                archive_path,
                extract_dir,
                package,
                version,
                metadata.archive_sha256,
            )
            _retain(archive_path)
            return lib_dir

    def evict(self, package: str, version: str) -> None:
        """Release one reference on a cached entry, deleting it at zero.

        Idempotent: removes the ``<key>.tar.gz`` archive, the extracted
        ``<key>/`` directory, and any ``.<key>.*tmp*`` leftovers once no
        in-flight fetch still needs them. Calling it without a matching
        ``fetch`` simply deletes the entry if present.
        """
        cache_key = _cache_key(package, version)
        archive_path = self.cache_dir / f"{cache_key}.tar.gz"
        extract_dir = self.cache_dir / cache_key
        lock_path = self.cache_dir / ".locks" / f"{cache_key}.lock"

        with _FileLock(lock_path):
            if not _release(archive_path):
                return
            archive_path.unlink(missing_ok=True)
            shutil.rmtree(extract_dir, ignore_errors=True)
            for leftover in self.cache_dir.glob(f".{cache_key}.*tmp*"):
                if leftover.is_dir():
                    shutil.rmtree(leftover, ignore_errors=True)
                else:
                    leftover.unlink(missing_ok=True)

    def _cache_hit(
        self, package: str, version: str, archive_path: Path, extract_dir: Path
    ) -> bool:
        marker_path = extract_dir / MARKER_FILE
        lib_dir = extract_dir / "lib"
        if not archive_path.is_file() or not marker_path.is_file() or not lib_dir.is_dir():
            return False

        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False

        archive_sha256 = marker.get("archive_sha256")
        if (
            marker.get("package") != package
            or marker.get("version") != version
            or not isinstance(archive_sha256, str)
        ):
            return False

        return _sha256_file(archive_path) == archive_sha256.lower()

    def _metadata_for(self, package: str, version: str) -> ArchiveMetadata:
        package_url = f"{self.base_url}/api/packages/{quote(package, safe='')}"
        response = self._request(
            "GET",
            package_url,
            headers={"Accept": "application/vnd.pub.v2+json"},
        )
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise PubDevError(f"invalid JSON from {package_url}: {exc}") from exc
        finally:
            response.close()

        candidates = []
        latest = data.get("latest")
        if isinstance(latest, dict):
            candidates.append(latest)
        versions = data.get("versions")
        if isinstance(versions, list):
            candidates.extend(item for item in versions if isinstance(item, dict))

        for candidate in candidates:
            if candidate.get("version") != version:
                continue
            archive_url = candidate.get("archive_url")
            archive_sha256 = candidate.get("archive_sha256")
            if not isinstance(archive_url, str) or not archive_url:
                raise PubDevError(f"{package} {version} metadata is missing archive_url")
            if not isinstance(archive_sha256, str) or not archive_sha256:
                raise PubDevError(
                    f"{package} {version} metadata is missing archive_sha256"
                )
            return ArchiveMetadata(archive_url, archive_sha256.lower())

        raise PackageVersionNotFound(f"{package} {version} was not found on pub.dev")

    def _ensure_archive(
        self, archive_path: Path, extract_dir: Path, metadata: ArchiveMetadata
    ) -> None:
        if archive_path.exists():
            actual = _sha256_file(archive_path)
            if actual == metadata.archive_sha256:
                return
            self._invalidate(archive_path, extract_dir)

        self._download_verified_archive(
            metadata.archive_url,
            archive_path,
            extract_dir,
            metadata.archive_sha256,
        )

    def _download_verified_archive(
        self,
        archive_url: str,
        archive_path: Path,
        extract_dir: Path,
        expected_sha256: str,
    ) -> None:
        last_actual = ""
        for _ in range(2):
            self._download_archive(archive_url, archive_path)
            last_actual = _sha256_file(archive_path)
            if last_actual == expected_sha256:
                return
            self._invalidate(archive_path, extract_dir)

        raise ChecksumMismatch(
            f"{archive_path.name} sha256 mismatch: expected "
            f"{expected_sha256}, got {last_actual}"
        )

    def _download_archive(self, archive_url: str, archive_path: Path) -> None:
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = archive_path.with_name(f".{archive_path.name}.tmp-{os.getpid()}")

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            response = self._request("GET", archive_url, stream=True)
            try:
                try:
                    with tmp_path.open("wb") as handle:
                        for chunk in response.iter_bytes():
                            if chunk:
                                handle.write(chunk)
                except httpx.RequestError as exc:
                    last_error = exc
                    if tmp_path.exists():
                        tmp_path.unlink()
                    if attempt >= self.max_retries:
                        raise PubDevError(
                            f"download failed for {archive_url}: {exc}"
                        ) from exc
                    self._sleep(self._backoff_delay(attempt))
                    continue

                tmp_path.replace(archive_path)
                return
            finally:
                response.close()

        raise PubDevError(f"download failed for {archive_url}: {last_error}") from last_error

    def _extract_archive(
        self,
        archive_path: Path,
        extract_dir: Path,
        package: str,
        version: str,
        archive_sha256: str,
    ) -> None:
        tmp_dir = extract_dir.with_name(f".{extract_dir.name}.tmp-{os.getpid()}")
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True)

        try:
            with tarfile.open(archive_path, mode="r:gz") as archive:
                _safe_extract(archive, tmp_dir)

            lib_dir = tmp_dir / "lib"
            if not lib_dir.is_dir():
                raise PubDevError(f"{archive_path.name} does not contain a lib/ directory")

            _write_marker(tmp_dir, package, version, archive_sha256, archive_path.name)
            if extract_dir.exists():
                shutil.rmtree(extract_dir)
            tmp_dir.replace(extract_dir)
        finally:
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir)

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        stream: bool = False,
    ) -> httpx.Response:
        request_headers = {"User-Agent": USER_AGENT}
        if headers:
            request_headers.update(headers)

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                request = self._client.build_request(
                    method,
                    url,
                    headers=request_headers,
                )
                response = self._client.send(
                    request,
                    follow_redirects=True,
                    stream=stream,
                )
            except httpx.RequestError as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                self._sleep(self._backoff_delay(attempt))
                continue

            if response.status_code == 429:
                if attempt >= self.max_retries:
                    message = _response_message(response)
                    response.close()
                    raise PubDevError(f"HTTP 429 for {url}: {message}")
                delay = _retry_after_delay(response.headers.get("Retry-After"))
                if delay is None:
                    delay = self._backoff_delay(attempt)
                else:
                    delay = min(delay, MAX_RETRY_AFTER_SECONDS)
                response.close()
                self._sleep(delay)
                continue

            if 500 <= response.status_code <= 599:
                if attempt >= self.max_retries:
                    message = _response_message(response)
                    response.close()
                    raise PubDevError(
                        f"HTTP {response.status_code} for {url}: {message}"
                    )
                response.close()
                self._sleep(self._backoff_delay(attempt))
                continue

            if response.status_code >= 400:
                message = _response_message(response)
                response.close()
                raise PubDevError(f"HTTP {response.status_code} for {url}: {message}")

            return response

        raise PubDevError(f"request failed for {url}: {last_error}") from last_error

    def _backoff_delay(self, attempt: int) -> float:
        return min(self.backoff_initial * (2**attempt), self.backoff_max)

    def _invalidate(self, archive_path: Path, extract_dir: Path) -> None:
        if archive_path.exists():
            archive_path.unlink()
        if extract_dir.exists():
            shutil.rmtree(extract_dir)


def fetch(package: str, version: str) -> Path:
    """Fetch a pub.dev package archive and return the extracted lib/ path."""
    with PubDevClient() as client:
        return client.fetch(package, version)


def evict(package: str, version: str, cache_dir: Path | str | None = None) -> None:
    """Release one reference on a cached archive, deleting it at zero."""
    with PubDevClient(cache_dir=cache_dir) as client:
        client.evict(package, version)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m collector.pubdev_client")
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch_parser = subparsers.add_parser("fetch", help="fetch a pub.dev archive")
    fetch_parser.add_argument("package")
    fetch_parser.add_argument("version")

    args = parser.parse_args(argv)
    if args.command == "fetch":
        print(fetch(args.package, args.version))
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


def _refcount_key(archive_path: Path) -> str:
    return str(archive_path.resolve())


def _retain(archive_path: Path) -> None:
    key = _refcount_key(archive_path)
    with _REFCOUNT_LOCK:
        _REFCOUNTS[key] = _REFCOUNTS.get(key, 0) + 1


def _release(archive_path: Path) -> bool:
    """Drop one reference; return ``True`` when the entry may now be deleted."""
    key = _refcount_key(archive_path)
    with _REFCOUNT_LOCK:
        remaining = _REFCOUNTS.get(key, 0) - 1
        if remaining > 0:
            _REFCOUNTS[key] = remaining
            return False
        _REFCOUNTS.pop(key, None)
        return True


def _cache_key(package: str, version: str) -> str:
    if not package or not version:
        raise ValueError("package and version are required")
    if "/" in package or "/" in version:
        raise ValueError("package and version must not contain path separators")
    return f"{package}-{version}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extract(archive: tarfile.TarFile, destination: Path) -> None:
    destination_root = destination.resolve()
    members = archive.getmembers()
    for member in members:
        target = (destination / member.name).resolve()
        if not _is_relative_to(target, destination_root):
            raise PubDevError(f"archive member escapes cache directory: {member.name}")
        if member.issym() or member.islnk():
            link_name = Path(member.linkname)
            link_target = (
                link_name.resolve()
                if link_name.is_absolute()
                else (target.parent / link_name).resolve()
            )
            if not _is_relative_to(link_target, destination_root):
                raise PubDevError(
                    f"archive link escapes cache directory: {member.name}"
                )
    archive.extractall(destination, members=members)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _write_marker(
    extract_dir: Path,
    package: str,
    version: str,
    archive_sha256: str,
    archive_name: str,
) -> None:
    marker_path = extract_dir / MARKER_FILE
    marker = {
        "package": package,
        "version": version,
        "archive": archive_name,
        "archive_sha256": archive_sha256,
    }
    tmp_path = marker_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(marker_path)


def _retry_after_delay(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass

    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())


def _response_message(response: httpx.Response) -> str:
    try:
        return response.text[:200]
    except (httpx.HTTPError, RuntimeError):
        return response.reason_phrase


if __name__ == "__main__":
    raise SystemExit(main())
