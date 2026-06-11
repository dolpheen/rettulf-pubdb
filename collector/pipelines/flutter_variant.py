"""Collect per-Flutter-version fingerprint variants from probe apps."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from collector.pipelines.api_surface import (
    DEFAULT_TIMEOUT_SECONDS as DEFAULT_HELPER_TIMEOUT_SECONDS,
)
from collector.pipelines.api_surface import collect_api_surface
from collector.pipelines.obfuscated_build import (
    DEFAULT_ANDROID_ABI,
    DEFAULT_BUILD_TIMEOUT_SECONDS,
    DEFAULT_COVERAGE_THRESHOLD,
    CommandRunner,
    JsonObject,
    ObfuscatedBuildError,
    ProbeManifest,
    _build_target_snapshot,
    _cached_baseline_snapshot,
    _command_parts,
    _evict_package_dir,
    _iso_now,
    _resolve_package_dir,
    _run_command,
    _validate_package_version,
    _work_root,
    build_obfuscated_fingerprint,
    collect_probe_manifest,
    reachable_surface_metadata,
)
from collector.pipelines.source_fingerprint import collect_source_fingerprint

STRATEGY = "flutter-version-structural-v1"
FLUTTER_VARIANT_PREFIX = "flutter-"
PROBE_SDK_CONSTRAINT = ">=2.12.0 <4.0.0"
FLUTTER_REPOSITORY_URL = "https://github.com/flutter/flutter.git"
DEFAULT_FLUTTER_CACHE_DIR = Path.home() / ".cache" / "rettulf-pubdb" / "flutter"
DEFAULT_INSTALL_TIMEOUT_SECONDS = 30 * 60.0
SKIP_RECORDS_FILE = Path("db") / "_flutter_variant_skips.json"

_STABLE_FLUTTER_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_FLUTTER_VARIANT_RE = re.compile(r"^flutter-(\d+\.\d+\.\d+)$")
_VERSION_PREFIX_RE = re.compile(r"(\d+\.\d+\.\d+)")
_SDK_LINE_RE = re.compile(r"^\s+sdk:\s*(.+?)\s*$")
_COMPARATOR_RE = re.compile(r"^(>=|<=|>|<|==|=)?(.+)$")


class FlutterVariantError(RuntimeError):
    """Raised when a Flutter-version variant cannot be collected."""


@dataclass(frozen=True)
class FlutterSdk:
    version: str
    dart_version: str
    root: Path
    flutter_executable: Path
    dart_executable: Path


@dataclass(frozen=True)
class FlutterVariantSkip:
    package: str
    version: str
    flutter_version: str
    reason: str
    collected_at: str

    def to_json(self) -> JsonObject:
        return {
            "package": self.package,
            "version": self.version,
            "flutter_version": self.flutter_version,
            "reason": self.reason,
            "collected_at": self.collected_at,
        }


class FlutterVariantSkipped(FlutterVariantError):
    """Raised when a variant is intentionally skipped and should be recorded."""

    def __init__(self, skip: FlutterVariantSkip) -> None:
        super().__init__(skip.reason)
        self.skip = skip


@dataclass(frozen=True)
class FlutterVariantResult:
    flutter_version: str
    entry: JsonObject | None = None
    skip: FlutterVariantSkip | None = None


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: str) -> "Version":
        match = _VERSION_PREFIX_RE.match(value.strip())
        if match is None:
            raise ValueError(f"invalid version: {value}")
        major, minor, patch = match.group(1).split(".")
        return cls(int(major), int(minor), int(patch))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


class FlutterVersionManager:
    """Install Flutter git worktrees under ``cache_dir/<version>/``."""

    def __init__(
        self,
        *,
        cache_dir: Path | str | None = None,
        repository_url: str = FLUTTER_REPOSITORY_URL,
        command_runner: CommandRunner | None = None,
    ) -> None:
        self.cache_dir = (
            Path(cache_dir).expanduser() if cache_dir else DEFAULT_FLUTTER_CACHE_DIR
        )
        self.repository_url = repository_url
        self.command_runner = command_runner or _run_command

    def ensure(
        self,
        version: str,
        *,
        timeout: float = DEFAULT_INSTALL_TIMEOUT_SECONDS,
    ) -> FlutterSdk:
        _validate_stable_flutter_version(version)
        root = self.cache_dir / version
        flutter = root / "bin" / "flutter"
        dart = root / "bin" / "dart"
        if not flutter.is_file():
            self._install_worktree(version, root, timeout)
        if not dart.is_file():
            raise FlutterVariantError(f"Flutter {version} did not provide {dart}")
        dart_version = self._dart_version(flutter, root, timeout)
        return FlutterSdk(
            version=version,
            dart_version=dart_version,
            root=root,
            flutter_executable=flutter,
            dart_executable=dart,
        )

    def _install_worktree(self, version: str, root: Path, timeout: float) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        source = self.cache_dir / "_src"
        if root.exists():
            self._remove_incomplete_worktree(source, root, timeout)
        if not source.exists():
            self._checked_run(
                [
                    "git",
                    "clone",
                    "--filter=blob:none",
                    "--no-checkout",
                    self.repository_url,
                    str(source),
                ],
                self.cache_dir,
                timeout,
            )
        else:
            self._checked_run(
                ["git", "-C", str(source), "fetch", "--tags"],
                source,
                timeout,
            )
        self._checked_run(
            [
                "git",
                "-C",
                str(source),
                "worktree",
                "add",
                "--detach",
                str(root),
                version,
            ],
            source,
            timeout,
        )

    def _remove_incomplete_worktree(
        self,
        source: Path,
        root: Path,
        timeout: float,
    ) -> None:
        if source.exists():
            self.command_runner(
                [
                    "git",
                    "-C",
                    str(source),
                    "worktree",
                    "remove",
                    "--force",
                    str(root),
                ],
                source,
                timeout,
            )
        if root.is_dir() and not root.is_symlink():
            shutil.rmtree(root, ignore_errors=True)
        else:
            root.unlink(missing_ok=True)
        if root.exists():
            raise FlutterVariantError(
                f"could not remove incomplete Flutter cache path: {root}"
            )

    def _dart_version(self, flutter: Path, root: Path, timeout: float) -> str:
        completed = self._checked_run(
            [str(flutter), "--version", "--machine"],
            root,
            timeout,
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise FlutterVariantError(
                "flutter --version --machine returned invalid JSON"
            ) from exc
        raw_version = payload.get("dartSdkVersion")
        if not isinstance(raw_version, str):
            raise FlutterVariantError(
                "flutter --version output is missing dartSdkVersion"
            )
        return _normalize_version(raw_version)

    def _checked_run(
        self,
        command: Sequence[str],
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        completed = self.command_runner(command, cwd, timeout)
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            rendered = " ".join(str(part) for part in command)
            raise FlutterVariantError(f"command failed ({rendered}): {detail}")
        return completed


def collect_flutter_variant_entry(
    package: str,
    version: str,
    *,
    flutter_version: str,
    package_dir: Path | str | None = None,
    archive_cache_dir: Path | str | None = None,
    work_dir: Path | str | None = None,
    keep_work_dir: bool = False,
    flutter_manager: FlutterVersionManager | None = None,
    flutter_cache_dir: Path | str | None = None,
    rettulf_command: str | Path | Sequence[str] = "rettulf",
    dart_executable: str | Path | None = None,
    helper_dir: Path | str | None = None,
    timeout: float = DEFAULT_BUILD_TIMEOUT_SECONDS,
    helper_timeout: float = DEFAULT_HELPER_TIMEOUT_SECONDS,
    coverage_threshold: float = DEFAULT_COVERAGE_THRESHOLD,
    android_abi: str = DEFAULT_ANDROID_ABI,
    command_runner: CommandRunner | None = None,
) -> JsonObject:
    """Build one non-obfuscated probe entry for a single Flutter version."""
    _validate_package_version(package, version)
    _validate_stable_flutter_version(flutter_version)
    package_root = _resolve_package_dir(
        package,
        version,
        package_dir=package_dir,
        archive_cache_dir=archive_cache_dir,
    )

    # A self-fetched archive holds one bounded-cache reference; release it once
    # collection finishes (including the early Dart-incompatibility skip).
    try:
        runner = command_runner or _run_command
        manager = flutter_manager or FlutterVersionManager(
            cache_dir=flutter_cache_dir,
            command_runner=runner,
        )
        sdk = manager.ensure(flutter_version, timeout=timeout)

        sdk_constraint = read_package_sdk_constraint(package_root)
        if not is_dart_sdk_compatible(sdk.dart_version, sdk_constraint):
            reason = (
                f"{package} {version} requires Dart SDK {sdk_constraint}, "
                f"but Flutter {flutter_version} pins Dart {sdk.dart_version}"
            )
            raise FlutterVariantSkipped(
                FlutterVariantSkip(
                    package=package,
                    version=version,
                    flutter_version=flutter_version,
                    reason=reason,
                    collected_at=_iso_now(),
                )
            )

        helper_dart = (
            str(dart_executable)
            if dart_executable is not None
            else shutil.which("dart") or str(sdk.dart_executable)
        )
        try:
            api_surface = collect_api_surface(
                package_root,
                package,
                dart_executable=helper_dart,
                helper_dir=helper_dir,
                timeout=helper_timeout,
            )
            source_fingerprint = collect_source_fingerprint(
                package_root,
                package,
                dart_executable=helper_dart,
                helper_dir=helper_dir,
                timeout=helper_timeout,
            )
            manifest = collect_probe_manifest(
                package_root,
                package,
                dart_executable=helper_dart,
                helper_dir=helper_dir,
                timeout=helper_timeout,
            )
        except Exception as exc:
            raise FlutterVariantError(str(exc)) from exc

        rettulf = _command_parts(rettulf_command)
        with _work_root(work_dir, keep_work_dir=keep_work_dir) as root:
            variant_root = root / variant_name(flutter_version)
            variant_root.mkdir(parents=True, exist_ok=True)
            try:
                baseline_snapshot = _cached_baseline_snapshot(
                    variant_root,
                    package=package,
                    version=version,
                    flutter_executable=str(sdk.flutter_executable),
                    rettulf_command=rettulf,
                    timeout=timeout,
                    android_abi=android_abi,
                    command_runner=runner,
                    sdk_constraint=PROBE_SDK_CONSTRAINT,
                    obfuscate=False,
                )
                (
                    target_snapshot,
                    used_manifest,
                    used_probe_fallback,
                ) = _build_target_snapshot(
                    variant_root,
                    package=package,
                    version=version,
                    manifest=manifest,
                    flutter_executable=str(sdk.flutter_executable),
                    rettulf_command=rettulf,
                    timeout=timeout,
                    android_abi=android_abi,
                    command_runner=runner,
                    sdk_constraint=PROBE_SDK_CONSTRAINT,
                    obfuscate=False,
                )
            except ObfuscatedBuildError as exc:
                raise FlutterVariantError(str(exc)) from exc

        reachable_surface = reachable_surface_metadata(
            api_surface,
            used_manifest,
            coverage_threshold=coverage_threshold,
        )
        if used_probe_fallback:
            requested_declarations = {
                reference.declaration for reference in manifest.references
            }
            used_declarations = {
                reference.declaration for reference in used_manifest.references
            }
            reachable_surface["partial"] = True
            reachable_surface["probe_fallback"] = True
            reachable_surface["omitted_declarations"] = sorted(
                requested_declarations - used_declarations
            )

        fingerprint = build_flutter_variant_fingerprint(
            target_snapshot,
            baseline_snapshot,
            reachable_surface=reachable_surface,
        )
        return {
            "pubdb_schema_version": 1,
            "package": package,
            "version": version,
            "collected_at": _iso_now(),
            "api_surface": api_surface,
            "source_fingerprint": source_fingerprint,
            "flutter_variants": [
                {
                    "flutter_version": flutter_version,
                    "dart_version": sdk.dart_version,
                    "fingerprint": fingerprint,
                }
            ],
        }
    finally:
        if package_dir is None:
            _evict_package_dir(package, version, archive_cache_dir)


def collect_flutter_variant_results(
    package: str,
    version: str,
    *,
    flutter_versions: Sequence[str] | None = None,
    config_path: Path | str | None = None,
    **kwargs: Any,
) -> list[FlutterVariantResult]:
    """Collect all configured Flutter variants, returning entries and skips."""
    versions = list(
        load_flutter_versions(config_path)
        if flutter_versions is None
        else flutter_versions
    )
    results: list[FlutterVariantResult] = []
    for flutter_version in versions:
        try:
            entry = collect_flutter_variant_entry(
                package,
                version,
                flutter_version=flutter_version,
                **kwargs,
            )
        except FlutterVariantSkipped as exc:
            results.append(
                FlutterVariantResult(
                    flutter_version=flutter_version,
                    skip=exc.skip,
                )
            )
            continue
        results.append(
            FlutterVariantResult(
                flutter_version=flutter_version,
                entry=entry,
            )
        )
    return results


def build_flutter_variant_fingerprint(
    target_snapshot: JsonObject,
    baseline_snapshot: JsonObject | None = None,
    *,
    reachable_surface: JsonObject | None = None,
) -> JsonObject:
    fingerprint = build_obfuscated_fingerprint(
        target_snapshot,
        baseline_snapshot,
        reachable_surface=reachable_surface,
    )
    fingerprint["strategy"] = STRATEGY
    return fingerprint


def load_flutter_versions(path: Path | str | None = None) -> list[str]:
    config_path = Path(path) if path else default_flutter_versions_path()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as exc:
        raise FlutterVariantError(f"invalid JSON in {config_path}") from exc
    if not isinstance(payload, list):
        raise FlutterVariantError(f"{config_path} must be a JSON array")

    versions: list[str] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, str):
            raise FlutterVariantError(f"{config_path} contains a non-string version")
        _validate_stable_flutter_version(item)
        if item not in seen:
            seen.add(item)
            versions.append(item)
    return versions


def default_flutter_versions_path() -> Path:
    return Path(__file__).resolve().parents[2] / "db" / "_flutter_versions.json"


def variant_name(flutter_version: str) -> str:
    _validate_stable_flutter_version(flutter_version)
    return f"{FLUTTER_VARIANT_PREFIX}{flutter_version}"


def flutter_version_from_variant(variant: str) -> str | None:
    match = _FLUTTER_VARIANT_RE.match(variant)
    return match.group(1) if match else None


def read_package_sdk_constraint(package_dir: Path | str) -> str:
    pubspec = Path(package_dir) / "pubspec.yaml"
    try:
        lines = pubspec.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "any"

    in_environment = False
    environment_indent = 0
    for line in lines:
        stripped = line.split("#", 1)[0].rstrip()
        if not stripped:
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        if re.match(r"^environment:\s*$", stripped):
            in_environment = True
            environment_indent = indent
            continue
        if in_environment and indent <= environment_indent:
            in_environment = False
        if not in_environment:
            continue
        match = _SDK_LINE_RE.match(stripped)
        if match is not None:
            return _strip_quotes(match.group(1).strip())
    return "any"


def is_dart_sdk_compatible(dart_version: str, constraint: str) -> bool:
    dart = Version.parse(dart_version)
    normalized = _strip_quotes((constraint or "any").strip())
    if normalized in {"", "any"}:
        return True
    return any(
        _branch_allows(dart, branch.strip())
        for branch in normalized.split("||")
    )


def upsert_skip_record(path: Path | str, skip: FlutterVariantSkip) -> None:
    target = Path(path)
    payload = _read_skip_payload(target)
    records = [
        item
        for item in payload.get("skips", [])
        if not (
            isinstance(item, dict)
            and item.get("package") == skip.package
            and item.get("version") == skip.version
            and item.get("flutter_version") == skip.flutter_version
        )
    ]
    records.append(skip.to_json())
    payload["generated_at"] = _iso_now()
    payload["skips"] = sorted(
        records,
        key=lambda item: (
            str(item.get("package", "")),
            str(item.get("version", "")),
            str(item.get("flutter_version", "")),
        ),
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def has_skip_record(
    path: Path | str,
    package: str,
    version: str,
    flutter_version: str,
) -> bool:
    for item in _read_skip_payload(Path(path)).get("skips", []):
        if not isinstance(item, dict):
            continue
        if (
            item.get("package") == package
            and item.get("version") == version
            and item.get("flutter_version") == flutter_version
        ):
            return True
    return False


def skip_records_path(repo_root: Path | str) -> Path:
    return Path(repo_root) / SKIP_RECORDS_FILE


def _read_skip_payload(path: Path) -> JsonObject:
    if not path.exists():
        return {"generated_at": None, "skips": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"generated_at": None, "skips": []}
    if not isinstance(payload, dict):
        return {"generated_at": None, "skips": []}
    skips = payload.get("skips")
    if not isinstance(skips, list):
        payload["skips"] = []
    return payload


def _branch_allows(dart: Version, branch: str) -> bool:
    if not branch:
        return True
    expanded = _expand_caret(branch)
    tokens = expanded.replace(",", " ").split()
    if not tokens:
        return True
    comparators: list[tuple[str, Version]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {">", ">=", "<", "<=", "=", "=="} and index + 1 < len(tokens):
            comparators.append((token, Version.parse(tokens[index + 1])))
            index += 2
            continue
        match = _COMPARATOR_RE.match(token)
        if match is None:
            # Unknown pub constraint syntax may still be buildable; let
            # Flutter's pub resolver decide instead of recording a permanent skip.
            return True
        operator = match.group(1) or "=="
        try:
            comparators.append((operator, Version.parse(match.group(2))))
        except ValueError:
            # Same fallback as above: an unparsed constraint should attempt the
            # build and fail loudly rather than become a permanent skip verdict.
            return True
        index += 1
    return all(
        _compare_version(dart, operator, expected)
        for operator, expected in comparators
    )


def _expand_caret(branch: str) -> str:
    branch = branch.strip()
    if not branch.startswith("^"):
        return branch
    base = Version.parse(branch[1:].strip())
    upper = _next_breaking_version(base)
    return f">={base} <{upper}"


def _next_breaking_version(version: Version) -> Version:
    if version.major > 0:
        return Version(version.major + 1, 0, 0)
    if version.minor > 0:
        return Version(0, version.minor + 1, 0)
    return Version(0, 0, version.patch + 1)


def _compare_version(actual: Version, operator: str, expected: Version) -> bool:
    if operator == ">":
        return actual > expected
    if operator == ">=":
        return actual >= expected
    if operator == "<":
        return actual < expected
    if operator == "<=":
        return actual <= expected
    return actual == expected


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _validate_stable_flutter_version(version: str) -> None:
    if _STABLE_FLUTTER_VERSION_RE.match(version) is None:
        raise FlutterVariantError(
            f"Flutter version must be a stable x.y.z release: {version}"
        )


def _normalize_version(value: str) -> str:
    match = _VERSION_PREFIX_RE.search(value)
    if match is None:
        raise FlutterVariantError(f"could not parse version: {value}")
    return match.group(1)


def _default_output_paths(
    package: str,
    version: str,
    results: list[FlutterVariantResult],
) -> list[Path]:
    paths = []
    for result in results:
        if result.entry is not None:
            paths.append(
                Path("db")
                / package
                / f"{version}.{variant_name(result.flutter_version)}.json"
            )
    if any(result.skip is not None for result in results):
        paths.append(SKIP_RECORDS_FILE)
    return paths


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package")
    parser.add_argument("version")
    parser.add_argument("--package-dir", type=Path)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--keep-work-dir", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--flutter-cache-dir", type=Path)
    parser.add_argument("--rettulf", dest="rettulf_command", default="rettulf")
    parser.add_argument("--dart", dest="dart_executable")
    parser.add_argument("--timeout", type=float, default=DEFAULT_BUILD_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)

    try:
        results = collect_flutter_variant_results(
            args.package,
            args.version,
            config_path=args.config,
            package_dir=args.package_dir,
            work_dir=args.work_dir,
            keep_work_dir=args.keep_work_dir,
            flutter_cache_dir=args.flutter_cache_dir,
            rettulf_command=args.rettulf_command,
            dart_executable=args.dart_executable,
            timeout=args.timeout,
        )
    except FlutterVariantError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    payload = {
        "entries": [result.entry for result in results if result.entry is not None],
        "skips": [
            result.skip.to_json()
            for result in results
            if result.skip is not None
        ],
        "default_paths": [
            path.as_posix()
            for path in _default_output_paths(args.package, args.version, results)
        ],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
