"""Collect obfuscated-build fingerprint variants from Flutter probe apps."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from collector.pipelines.api_surface import (
    ApiSurfaceError,
    DEFAULT_TIMEOUT_SECONDS as DEFAULT_HELPER_TIMEOUT_SECONDS,
    _default_dart_executable,
    _default_helper_dir,
    _ensure_helper_executable,
    _helper_env,
)
from collector.pipelines.api_surface import collect_api_surface
from collector.pipelines.source_fingerprint import collect_source_fingerprint

JsonObject = dict[str, Any]
CommandRunner = Callable[
    [Sequence[str], Path, float],
    subprocess.CompletedProcess[str],
]

STRATEGY = "obfuscated-build-structural-v1"
OBFUSCATED_VARIANT = "obf"
DEFAULT_BUILD_TIMEOUT_SECONDS = 15 * 60.0
DEFAULT_COVERAGE_THRESHOLD = 0.65
DEFAULT_ANDROID_ABI = "arm64-v8a"

_PACKAGE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_VERSION_RE = re.compile(
    r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
_CHANNEL_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_CHANNEL_SUFFIX_RE = re.compile(r"^[A-Za-z0-9_./-]+$")


class ObfuscatedBuildError(RuntimeError):
    """Raised when an obfuscated-build probe cannot produce a fingerprint."""


@dataclass(frozen=True)
class ProbeReference:
    library: str
    expression: str
    declaration: str
    kind: str


@dataclass(frozen=True)
class ProbeManifest:
    libraries: tuple[str, ...]
    references: tuple[ProbeReference, ...]


def collect_obfuscated_build_entries(
    package: str,
    version: str,
    *,
    package_dir: Path | str | None = None,
    archive_cache_dir: Path | str | None = None,
    work_dir: Path | str | None = None,
    keep_work_dir: bool = False,
    flutter_executable: str | Path = "flutter",
    rettulf_command: str | Path | Sequence[str] = "rettulf",
    dart_executable: str | Path | None = None,
    helper_dir: Path | str | None = None,
    timeout: float = DEFAULT_BUILD_TIMEOUT_SECONDS,
    helper_timeout: float = DEFAULT_HELPER_TIMEOUT_SECONDS,
    coverage_threshold: float = DEFAULT_COVERAGE_THRESHOLD,
    android_abi: str = DEFAULT_ANDROID_ABI,
    command_runner: CommandRunner | None = None,
) -> list[JsonObject]:
    """Build a target/baseline obfuscated probe and return schema-v1 entries."""
    _validate_package_version(package, version)
    package_root = _resolve_package_dir(
        package,
        version,
        package_dir=package_dir,
        archive_cache_dir=archive_cache_dir,
    )

    api_surface = collect_api_surface(
        package_root,
        package,
        dart_executable=dart_executable,
        helper_dir=helper_dir,
        timeout=helper_timeout,
    )
    source_fingerprint = collect_source_fingerprint(
        package_root,
        package,
        dart_executable=dart_executable,
        helper_dir=helper_dir,
        timeout=helper_timeout,
    )
    requested_manifest = collect_probe_manifest(
        package_root,
        package,
        dart_executable=dart_executable,
        helper_dir=helper_dir,
        timeout=helper_timeout,
    )

    runner = command_runner or _run_command
    rettulf = _command_parts(rettulf_command)
    flutter = str(flutter_executable)

    with _work_root(work_dir, keep_work_dir=keep_work_dir) as root:
        baseline_snapshot = _cached_baseline_snapshot(
            root,
            package=package,
            version=version,
            flutter_executable=flutter,
            rettulf_command=rettulf,
            timeout=timeout,
            android_abi=android_abi,
            command_runner=runner,
        )
        target_snapshot, manifest, used_probe_fallback = _build_target_snapshot(
            root,
            package=package,
            version=version,
            manifest=requested_manifest,
            flutter_executable=flutter,
            rettulf_command=rettulf,
            timeout=timeout,
            android_abi=android_abi,
            command_runner=runner,
        )

    reachable_surface = reachable_surface_metadata(
        api_surface,
        manifest,
        coverage_threshold=coverage_threshold,
    )
    if used_probe_fallback:
        requested_declarations = {
            reference.declaration for reference in requested_manifest.references
        }
        used_declarations = {reference.declaration for reference in manifest.references}
        reachable_surface["partial"] = True
        reachable_surface["probe_fallback"] = True
        reachable_surface["omitted_declarations"] = sorted(
            requested_declarations - used_declarations
        )

    obfuscated_fingerprint = build_obfuscated_fingerprint(
        target_snapshot,
        baseline_snapshot,
        reachable_surface=reachable_surface,
    )
    return [
        {
            "pubdb_schema_version": 1,
            "package": package,
            "version": version,
            "collected_at": _iso_now(),
            "api_surface": api_surface,
            "source_fingerprint": source_fingerprint,
            "obfuscated_fingerprint": obfuscated_fingerprint,
        }
    ]


def _cached_baseline_snapshot(
    root: Path,
    *,
    package: str,
    version: str,
    flutter_executable: str,
    rettulf_command: Sequence[str],
    timeout: float,
    android_abi: str,
    command_runner: CommandRunner,
    sdk_constraint: str = "^3.12.0",
    obfuscate: bool = True,
) -> JsonObject:
    snapshot_path = root / "baseline.snapshot.json"
    if snapshot_path.is_file():
        return _read_snapshot_json(snapshot_path)
    return _build_probe_snapshot(
        root,
        "baseline",
        package=package,
        version=version,
        manifest=ProbeManifest((), ()),
        include_package=False,
        flutter_executable=flutter_executable,
        rettulf_command=rettulf_command,
        timeout=timeout,
        android_abi=android_abi,
        command_runner=command_runner,
        sdk_constraint=sdk_constraint,
        obfuscate=obfuscate,
    )


def _build_target_snapshot(
    root: Path,
    *,
    package: str,
    version: str,
    manifest: ProbeManifest,
    flutter_executable: str,
    rettulf_command: Sequence[str],
    timeout: float,
    android_abi: str,
    command_runner: CommandRunner,
    sdk_constraint: str = "^3.12.0",
    obfuscate: bool = True,
) -> tuple[JsonObject, ProbeManifest, bool]:
    try:
        snapshot = _build_probe_snapshot(
            root,
            "target",
            package=package,
            version=version,
            manifest=manifest,
            include_package=True,
            flutter_executable=flutter_executable,
            rettulf_command=rettulf_command,
            timeout=timeout,
            android_abi=android_abi,
            command_runner=command_runner,
            sdk_constraint=sdk_constraint,
            obfuscate=obfuscate,
        )
        return snapshot, manifest, False
    except ObfuscatedBuildError as exc:
        original_error = exc

    fallback = _build_compilable_subset(
        root,
        package=package,
        version=version,
        references=manifest.references,
        flutter_executable=flutter_executable,
        rettulf_command=rettulf_command,
        timeout=timeout,
        android_abi=android_abi,
        command_runner=command_runner,
        attempt=[0],
        try_current=False,
        sdk_constraint=sdk_constraint,
        obfuscate=obfuscate,
    )
    if fallback is None:
        raise original_error
    snapshot, fallback_manifest = fallback
    return snapshot, fallback_manifest, True


def _build_compilable_subset(
    root: Path,
    *,
    package: str,
    version: str,
    references: tuple[ProbeReference, ...],
    flutter_executable: str,
    rettulf_command: Sequence[str],
    timeout: float,
    android_abi: str,
    command_runner: CommandRunner,
    attempt: list[int],
    try_current: bool = True,
    sdk_constraint: str = "^3.12.0",
    obfuscate: bool = True,
) -> tuple[JsonObject, ProbeManifest] | None:
    if not references:
        return None

    manifest = _manifest_for_references(references)
    if try_current:
        candidate = _try_build_manifest(
            root,
            package=package,
            version=version,
            manifest=manifest,
            flutter_executable=flutter_executable,
            rettulf_command=rettulf_command,
            timeout=timeout,
            android_abi=android_abi,
            command_runner=command_runner,
            attempt=attempt,
            sdk_constraint=sdk_constraint,
            obfuscate=obfuscate,
        )
        if candidate is not None:
            return candidate, manifest

    if len(references) == 1:
        return None

    midpoint = len(references) // 2
    left = _build_compilable_subset(
        root,
        package=package,
        version=version,
        references=references[:midpoint],
        flutter_executable=flutter_executable,
        rettulf_command=rettulf_command,
        timeout=timeout,
        android_abi=android_abi,
        command_runner=command_runner,
        attempt=attempt,
        sdk_constraint=sdk_constraint,
        obfuscate=obfuscate,
    )
    right = _build_compilable_subset(
        root,
        package=package,
        version=version,
        references=references[midpoint:],
        flutter_executable=flutter_executable,
        rettulf_command=rettulf_command,
        timeout=timeout,
        android_abi=android_abi,
        command_runner=command_runner,
        attempt=attempt,
        sdk_constraint=sdk_constraint,
        obfuscate=obfuscate,
    )

    if left is None:
        return right
    if right is None:
        return left

    combined_manifest = _manifest_for_references(
        left[1].references + right[1].references
    )
    combined_snapshot = _try_build_manifest(
        root,
        package=package,
        version=version,
        manifest=combined_manifest,
        flutter_executable=flutter_executable,
        rettulf_command=rettulf_command,
        timeout=timeout,
        android_abi=android_abi,
        command_runner=command_runner,
        attempt=attempt,
        sdk_constraint=sdk_constraint,
        obfuscate=obfuscate,
    )
    if combined_snapshot is not None:
        return combined_snapshot, combined_manifest
    return left if len(left[1].references) >= len(right[1].references) else right


def _try_build_manifest(
    root: Path,
    *,
    package: str,
    version: str,
    manifest: ProbeManifest,
    flutter_executable: str,
    rettulf_command: Sequence[str],
    timeout: float,
    android_abi: str,
    command_runner: CommandRunner,
    attempt: list[int],
    sdk_constraint: str = "^3.12.0",
    obfuscate: bool = True,
) -> JsonObject | None:
    attempt[0] += 1
    try:
        return _build_probe_snapshot(
            root,
            f"target-fallback-{attempt[0]}",
            package=package,
            version=version,
            manifest=manifest,
            include_package=True,
            flutter_executable=flutter_executable,
            rettulf_command=rettulf_command,
            timeout=timeout,
            android_abi=android_abi,
            command_runner=command_runner,
            sdk_constraint=sdk_constraint,
            obfuscate=obfuscate,
        )
    except ObfuscatedBuildError:
        return None


def _manifest_for_references(
    references: tuple[ProbeReference, ...],
) -> ProbeManifest:
    return ProbeManifest(
        libraries=tuple(sorted({reference.library for reference in references})),
        references=tuple(
            sorted(references, key=lambda ref: (ref.library, ref.expression, ref.kind))
        ),
    )


def _read_snapshot_json(path: Path) -> JsonObject:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ObfuscatedBuildError(f"invalid rettulf snapshot JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ObfuscatedBuildError("rettulf snapshot JSON must be an object")
    return payload


def collect_probe_manifest(
    package_dir: Path | str,
    package: str,
    *,
    dart_executable: str | Path | None = None,
    helper_dir: Path | str | None = None,
    timeout: float = DEFAULT_HELPER_TIMEOUT_SECONDS,
) -> ProbeManifest:
    """Return generated-probe imports and statically invokable references."""
    root = Path(package_dir).resolve()
    if not root.is_dir():
        raise ObfuscatedBuildError(f"package directory does not exist: {root}")
    if not (root / "lib").is_dir():
        return ProbeManifest((), ())

    helper_root = Path(helper_dir).resolve() if helper_dir else _default_helper_dir()
    try:
        dart = str(dart_executable or _default_dart_executable())
        helper_executable = _ensure_helper_executable(
            helper_root,
            dart,
            timeout,
            "probe_manifest",
        )
    except ApiSurfaceError as exc:
        raise ObfuscatedBuildError(str(exc)) from exc

    completed = subprocess.run(
        [
            str(helper_executable),
            "--package-dir",
            str(root),
            "--package",
            package,
        ],
        cwd=helper_root,
        env=_helper_env(),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ObfuscatedBuildError(f"probe_manifest helper failed: {detail}")

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ObfuscatedBuildError("probe_manifest helper returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ObfuscatedBuildError("probe_manifest helper returned non-object JSON")
    return _normalize_probe_manifest(payload)


def reachable_surface_metadata(
    api_surface: JsonObject,
    manifest: ProbeManifest,
    *,
    coverage_threshold: float = DEFAULT_COVERAGE_THRESHOLD,
) -> JsonObject:
    """Return coverage metadata for the generated probe surface."""
    public_declarations = _surface_public_declarations(api_surface)
    manifest_declarations = {
        reference.declaration for reference in manifest.references
    }
    reachable = sorted(public_declarations & manifest_declarations)
    total_count = len(public_declarations)
    reachable_count = len(reachable)
    coverage = 1.0 if total_count == 0 else reachable_count / total_count
    return {
        "reachable_public_declarations": reachable_count,
        "total_public_declarations": total_count,
        "coverage": round(coverage, 4),
        "partial": coverage < coverage_threshold,
        "coverage_threshold": coverage_threshold,
        "declarations": reachable,
    }


def build_obfuscated_fingerprint(
    target_snapshot: JsonObject,
    baseline_snapshot: JsonObject | None = None,
    *,
    reachable_surface: JsonObject | None = None,
) -> JsonObject:
    """Return schema-compatible ``obfuscated_fingerprint`` JSON."""
    hierarchy = _isolated_hierarchy(target_snapshot, baseline_snapshot)
    hierarchy_shapes = _portable_hierarchy_shapes(hierarchy)
    signals: JsonObject = {
        "hierarchy_hash": _sha256_json(hierarchy_shapes),
        "class_shape_count": len(hierarchy),
        "string_literals": _subtract_sorted(
            _snapshot_strings(target_snapshot),
            _snapshot_strings(baseline_snapshot),
        ),
        "method_channels": _subtract_sorted(
            _snapshot_method_channels(target_snapshot),
            _snapshot_method_channels(baseline_snapshot),
        ),
        "ffi_symbols": _subtract_sorted(
            _snapshot_ffi_symbols(target_snapshot),
            _snapshot_ffi_symbols(baseline_snapshot),
        ),
        "const_canonicalization": _subtract_counter_sorted(
            _snapshot_const_canonicalization(target_snapshot),
            _snapshot_const_canonicalization(baseline_snapshot),
        ),
    }
    result: JsonObject = {
        "strategy": STRATEGY,
        "digest": _sha256_json(signals),
        **signals,
        "baseline_subtracted": baseline_snapshot is not None,
    }
    if reachable_surface is not None:
        result["reachable_surface"] = reachable_surface
    return result


def _resolve_package_dir(
    package: str,
    version: str,
    *,
    package_dir: Path | str | None,
    archive_cache_dir: Path | str | None,
) -> Path:
    if package_dir is not None:
        root = Path(package_dir).resolve()
        if not root.is_dir():
            raise ObfuscatedBuildError(f"package directory does not exist: {root}")
        return root

    try:
        from collector.pubdev_client import PubDevClient, PubDevError
    except ModuleNotFoundError as exc:  # pragma: no cover - surfaced directly
        raise ObfuscatedBuildError(
            "httpx is required for pub.dev archive fetches "
            "(pip install -r collector/requirements.txt)"
        ) from exc

    try:
        with PubDevClient(cache_dir=archive_cache_dir) as client:
            lib_dir = client.fetch(package, version)
    except PubDevError as exc:
        raise ObfuscatedBuildError(str(exc)) from exc
    return lib_dir.parent


@contextlib.contextmanager
def _work_root(
    work_dir: Path | str | None,
    *,
    keep_work_dir: bool,
) -> Iterable[Path]:
    if work_dir is not None:
        root = Path(work_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        yield root
        return

    if keep_work_dir:
        root = Path(tempfile.mkdtemp(prefix="rettulf-pubdb-obf-")).resolve()
        print(f"obfuscated probe work dir: {root}", file=sys.stderr)
        yield root
        return

    with tempfile.TemporaryDirectory(prefix="rettulf-pubdb-obf-") as tmp:
        yield Path(tmp).resolve()


def _build_probe_snapshot(
    root: Path,
    name: str,
    *,
    package: str,
    version: str,
    manifest: ProbeManifest,
    include_package: bool,
    flutter_executable: str,
    rettulf_command: Sequence[str],
    timeout: float,
    android_abi: str,
    command_runner: CommandRunner,
    sdk_constraint: str = "^3.12.0",
    obfuscate: bool = True,
) -> JsonObject:
    probe_dir = root / name
    if probe_dir.exists():
        shutil.rmtree(probe_dir)
    probe_dir.mkdir(parents=True)

    _checked_run(
        [
            flutter_executable,
            "create",
            "--project-name",
            "rettulf_pubdb_probe",
            "--platforms=android",
            ".",
        ],
        cwd=probe_dir,
        timeout=timeout,
        command_runner=command_runner,
    )
    _write_text(
        probe_dir / "pubspec.yaml",
        _render_pubspec(package, version, include_package, sdk_constraint),
    )
    _write_text(probe_dir / "lib" / "main.dart", _render_probe_main(manifest))

    split_debug_info = root / f"{name}_symbols"
    _checked_run(
        [flutter_executable, "pub", "get"],
        cwd=probe_dir,
        timeout=timeout,
        command_runner=command_runner,
    )
    build_command = [
        flutter_executable,
        "build",
        "apk",
        "--release",
    ]
    if obfuscate:
        build_command.extend(
            [
                "--obfuscate",
                f"--split-debug-info={split_debug_info}",
            ]
        )
    _checked_run(
        build_command,
        cwd=probe_dir,
        timeout=timeout,
        command_runner=command_runner,
    )

    apk_path = (
        probe_dir / "build" / "app" / "outputs" / "flutter-apk" / "app-release.apk"
    )
    if not apk_path.is_file():
        raise ObfuscatedBuildError(f"Flutter build did not produce {apk_path}")

    snapshot_path = root / f"{name}.snapshot.json"
    _checked_run(
        [
            *rettulf_command,
            "dump",
            str(apk_path),
            "--abi",
            android_abi,
            "-o",
            str(snapshot_path),
        ],
        cwd=probe_dir,
        timeout=timeout,
        command_runner=command_runner,
    )
    return _read_snapshot_json(snapshot_path)


def _render_pubspec(
    package: str,
    version: str,
    include_package: bool,
    sdk_constraint: str = "^3.12.0",
) -> str:
    dependencies = "  flutter:\n    sdk: flutter\n"
    if include_package:
        dependencies += f"  {package}: {version}\n"
    return (
        "name: rettulf_pubdb_probe\n"
        "description: Generated rettulf-pubdb obfuscated fingerprint probe.\n"
        "publish_to: none\n"
        "version: 1.0.0+1\n\n"
        "environment:\n"
        f"  sdk: {sdk_constraint}\n\n"
        "dependencies:\n"
        f"{dependencies}\n"
        "dev_dependencies:\n"
        "  flutter_test:\n"
        "    sdk: flutter\n\n"
        "flutter:\n"
        "  uses-material-design: false\n"
    )


def _render_probe_main(manifest: ProbeManifest) -> str:
    prefixes = {
        library: f"p{index}" for index, library in enumerate(manifest.libraries)
    }
    lines = ["import 'package:flutter/widgets.dart';"]
    for library, prefix in sorted(prefixes.items()):
        lines.append(f"import '{library}' as {prefix};")

    references = []
    for reference in manifest.references:
        prefix = prefixes.get(reference.library)
        if prefix is None:
            continue
        references.append(f"  {prefix}.{reference.expression},")

    if not references:
        references.append("  'rettulf-pubdb-baseline',")

    lines.extend(
        [
            "",
            "final List<Object?> _probeReferences = <Object?>[",
            *references,
            "];",
            "",
            "void main() {",
            "  WidgetsFlutterBinding.ensureInitialized();",
            "  runApp(_ProbeApp(_probeReferences.length));",
            "}",
            "",
            "class _ProbeApp extends StatelessWidget {",
            "  const _ProbeApp(this.count, {super.key});",
            "",
            "  final int count;",
            "",
            "  @override",
            "  Widget build(BuildContext context) {",
            "    return Directionality(",
            "      textDirection: TextDirection.ltr,",
            "      child: Text(count.toString()),",
            "    );",
            "  }",
            "}",
            "",
        ]
    )
    return "\n".join(lines)


def _checked_run(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
    command_runner: CommandRunner,
) -> subprocess.CompletedProcess[str]:
    completed = command_runner(command, cwd, timeout)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        rendered = " ".join(command)
        raise ObfuscatedBuildError(f"command failed ({rendered}): {detail}")
    return completed


def _run_command(
    command: Sequence[str],
    cwd: Path,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        env=_helper_env(),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _normalize_probe_manifest(payload: JsonObject) -> ProbeManifest:
    raw_libraries = payload.get("libraries")
    libraries = tuple(
        sorted(
            {
                item
                for item in raw_libraries
                if isinstance(item, str) and item
            }
        )
        if isinstance(raw_libraries, list)
        else ()
    )
    library_set = set(libraries)
    raw_references = payload.get("references")
    references: list[ProbeReference] = []
    if isinstance(raw_references, list):
        seen: set[tuple[str, str, str]] = set()
        for item in raw_references:
            if not isinstance(item, dict):
                continue
            library = item.get("library")
            expression = item.get("expression")
            declaration = item.get("declaration")
            kind = item.get("kind")
            if (
                not isinstance(library, str)
                or library not in library_set
                or not isinstance(expression, str)
                or not expression
                or not isinstance(declaration, str)
                or not declaration
                or not isinstance(kind, str)
                or not kind
            ):
                continue
            key = (library, expression, declaration)
            if key in seen:
                continue
            seen.add(key)
            references.append(
                ProbeReference(
                    library=library,
                    expression=expression,
                    declaration=declaration,
                    kind=kind,
                )
            )
    return ProbeManifest(
        libraries=libraries,
        references=tuple(
            sorted(
                references,
                key=lambda ref: (ref.library, ref.expression, ref.kind),
            )
        ),
    )


def _surface_public_declarations(api_surface: JsonObject) -> set[str]:
    classes = api_surface.get("classes")
    if not isinstance(classes, dict):
        return set()
    declarations: set[str] = set()
    for class_name, raw_surface in classes.items():
        if not isinstance(class_name, str) or not isinstance(raw_surface, dict):
            continue
        declarations.add(_class_declaration(class_name))
        for bucket in ("methods", "fields"):
            values = raw_surface.get(bucket)
            if not isinstance(values, list):
                continue
            singular = bucket[:-1]
            for value in values:
                if isinstance(value, str) and value:
                    declarations.add(_member_declaration(class_name, singular, value))
    return declarations


def _isolated_hierarchy(
    target_snapshot: JsonObject,
    baseline_snapshot: JsonObject | None,
) -> list[JsonObject]:
    target = _snapshot_hierarchy(target_snapshot)
    baseline = Counter(
        _class_shape_key(entry) for entry in _snapshot_hierarchy(baseline_snapshot)
    )
    isolated: list[JsonObject] = []
    for entry in target:
        key = _class_shape_key(entry)
        if baseline[key] > 0:
            baseline[key] -= 1
            continue
        isolated.append(entry)
    return sorted(
        isolated,
        key=lambda entry: (
            entry["class"],
            entry["super"],
            entry["fields_count"],
            entry["methods_count"],
            entry["interfaces_count"],
        ),
    )


def _portable_hierarchy_shapes(hierarchy: list[JsonObject]) -> list[JsonObject]:
    shapes = [
        {
            "fields_count": int(entry.get("fields_count", 0)),
            "methods_count": int(entry.get("methods_count", 0)),
            "interfaces_count": int(entry.get("interfaces_count", 0)),
            "has_super": bool(entry.get("super")),
        }
        for entry in hierarchy
    ]
    return sorted(
        shapes,
        key=lambda entry: (
            entry["fields_count"],
            entry["methods_count"],
            entry["interfaces_count"],
            entry["has_super"],
        ),
    )


def _snapshot_hierarchy(snapshot: JsonObject | None) -> list[JsonObject]:
    if not isinstance(snapshot, dict):
        return []
    objects = snapshot.get("objects")
    if isinstance(objects, list):
        raw_objects = [item for item in objects if isinstance(item, dict)]
        objects_by_id = {
            item["id"]: item for item in raw_objects if isinstance(item.get("id"), int)
        }
        functions_by_owner: Counter[int] = Counter()
        for item in raw_objects:
            if item.get("type") != "Function":
                continue
            owner = item.get("owner_class")
            if isinstance(owner, int):
                functions_by_owner[owner] += 1

        result = []
        for item in raw_objects:
            if item.get("type") != "Class":
                continue
            name = _object_name(item)
            if name is None:
                continue
            class_id = item.get("id")
            fields_count = _list_count(item.get("fields"))
            direct_methods_count = _list_count(item.get("functions"))
            owner_methods_count = (
                functions_by_owner[class_id] if isinstance(class_id, int) else 0
            )
            super_name = _object_name(objects_by_id.get(item.get("super_class"))) or ""
            result.append(
                {
                    "class": name,
                    "super": super_name,
                    "fields_count": fields_count,
                    "methods_count": max(direct_methods_count, owner_methods_count),
                    "interfaces_count": _list_count(item.get("interfaces")),
                }
            )
        return result

    classes = snapshot.get("classes")
    if not isinstance(classes, list):
        return []
    result = []
    for item in classes:
        if not isinstance(item, dict):
            continue
        name = _clean_string(item.get("name"))
        if name is None:
            continue
        result.append(
            {
                "class": name,
                "super": _clean_string(item.get("super_class")) or "",
                "fields_count": _list_count(item.get("fields")),
                "methods_count": _list_count(item.get("functions")),
                "interfaces_count": _list_count(item.get("interfaces")),
            }
        )
    return result


def _class_shape_key(entry: JsonObject) -> tuple[int, int, int, bool]:
    return (
        int(entry.get("fields_count", 0)),
        int(entry.get("methods_count", 0)),
        int(entry.get("interfaces_count", 0)),
        bool(entry.get("super")),
    )


def _snapshot_strings(snapshot: JsonObject | None) -> set[str]:
    if not isinstance(snapshot, dict):
        return set()
    direct = snapshot.get("strings")
    if isinstance(direct, list):
        return _string_set(direct)

    strings: set[str] = set()
    objects = snapshot.get("objects")
    if isinstance(objects, list):
        for item in objects:
            if not isinstance(item, dict):
                continue
            value = item.get("value")
            if isinstance(value, str):
                strings.add(unicodedata.normalize("NFC", value))
    return strings


def _snapshot_method_channels(snapshot: JsonObject | None) -> set[str]:
    if not isinstance(snapshot, dict):
        return set()
    direct = snapshot.get("method_channels")
    if isinstance(direct, list):
        return _string_set(direct)
    return {
        value
        for value in _snapshot_strings(snapshot)
        if _looks_like_method_channel(value)
    }


def _snapshot_ffi_symbols(snapshot: JsonObject | None) -> set[str]:
    if not isinstance(snapshot, dict):
        return set()
    direct = snapshot.get("ffi_symbols")
    if isinstance(direct, list):
        return _string_set(direct)

    symbols: set[str] = set()
    trampolines = snapshot.get("ffi_trampolines")
    if isinstance(trampolines, list):
        for item in trampolines:
            if not isinstance(item, dict):
                continue
            symbol = item.get("native_symbol")
            if isinstance(symbol, str) and symbol:
                symbols.add(unicodedata.normalize("NFC", symbol))
    return symbols


def _snapshot_const_canonicalization(snapshot: JsonObject | None) -> Counter[str]:
    if not isinstance(snapshot, dict):
        return Counter()
    direct = snapshot.get("const_canonicalization")
    if isinstance(direct, list):
        return Counter(_string_set(direct))

    values: Counter[str] = Counter()
    closures = snapshot.get("closures")
    if isinstance(closures, list):
        for item in closures:
            if not isinstance(item, dict) or item.get("is_canonical") is not True:
                continue
            function_id = item.get("function_id")
            if isinstance(function_id, int):
                values["canonical_closure"] += 1

    objects = snapshot.get("objects")
    if isinstance(objects, list):
        for item in objects:
            if not isinstance(item, dict) or item.get("is_canonical") is not True:
                continue
            obj_type = _clean_string(item.get("type")) or "Object"
            values[f"canonical_object:{obj_type}"] += 1
    return values


def _object_name(obj: JsonObject | None) -> str | None:
    if not isinstance(obj, dict):
        return None
    return _clean_string(obj.get("name")) or _clean_string(obj.get("value"))


def _clean_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = unicodedata.normalize("NFC", value.strip())
    return value or None


def _string_set(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {
        unicodedata.normalize("NFC", item)
        for item in value
        if isinstance(item, str) and item
    }


def _subtract_sorted(values: set[str], baseline: set[str]) -> list[str]:
    return sorted(values - baseline)


def _subtract_counter_sorted(values: Counter[str], baseline: Counter[str]) -> list[str]:
    delta = values.copy()
    delta.subtract(baseline)
    return sorted(f"{key}:{count}" for key, count in delta.items() if count > 0)


def _list_count(value: object) -> int:
    return len(value) if isinstance(value, list) else 0


def _looks_like_method_channel(value: str) -> bool:
    if (
        not value
        or " " in value
        or "\\" in value
        or ":" in value
        or value.startswith("package:")
        or value.startswith("dev.flutter/")
    ):
        return False
    if value.startswith(("plugins.flutter.io/", "dev.flutter.pigeon.")):
        return True
    if "/" not in value:
        return False

    prefix, suffix = value.split("/", 1)
    return (
        "." in prefix
        and bool(suffix)
        and _CHANNEL_COMPONENT_RE.fullmatch(prefix) is not None
        and _CHANNEL_SUFFIX_RE.fullmatch(suffix) is not None
    )


def _command_parts(command: str | Path | Sequence[str]) -> list[str]:
    if isinstance(command, Path):
        return [str(command)]
    if isinstance(command, str):
        return shlex.split(command)
    return [str(part) for part in command]


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _sha256_json(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _class_declaration(name: str) -> str:
    return f"class:{name}"


def _member_declaration(owner: str, bucket: str, name: str) -> str:
    return f"member:{owner}:{bucket}:{name}"


def _validate_package_version(package: str, version: str) -> None:
    if not _PACKAGE_RE.match(package):
        raise ObfuscatedBuildError(f"invalid package name: {package}")
    if not _VERSION_RE.match(version):
        raise ObfuscatedBuildError(f"invalid package version: {version}")


def _iso_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package")
    parser.add_argument("version")
    parser.add_argument("--package-dir", type=Path)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--keep-work-dir", action="store_true")
    parser.add_argument("--flutter", dest="flutter_executable", default="flutter")
    parser.add_argument("--rettulf", dest="rettulf_command", default="rettulf")
    parser.add_argument("--dart", dest="dart_executable")
    parser.add_argument("--timeout", type=float, default=DEFAULT_BUILD_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)

    try:
        entries = collect_obfuscated_build_entries(
            args.package,
            args.version,
            package_dir=args.package_dir,
            work_dir=args.work_dir,
            keep_work_dir=args.keep_work_dir,
            flutter_executable=args.flutter_executable,
            rettulf_command=args.rettulf_command,
            dart_executable=args.dart_executable,
            timeout=args.timeout,
        )
    except ObfuscatedBuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(entries, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
