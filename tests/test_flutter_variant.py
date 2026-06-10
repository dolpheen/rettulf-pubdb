from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collector.pipelines.flutter_variant import (
    STRATEGY,
    FlutterSdk,
    FlutterVersionManager,
    FlutterVariantSkip,
    FlutterVariantSkipped,
    collect_flutter_variant_entry,
    is_dart_sdk_compatible,
    load_flutter_versions,
    upsert_skip_record,
    variant_name,
)
from collector.pipelines.obfuscated_build import ProbeManifest, ProbeReference

try:
    from jsonschema import Draft202012Validator
except ModuleNotFoundError:  # pragma: no cover - optional local test dependency
    Draft202012Validator = None

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "schema" / "_schema.v1.json"


class FlutterVersionConfigTests(unittest.TestCase):
    def test_loads_stable_versions_and_rejects_prerelease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "_flutter_versions.json"
            path.write_text(
                json.dumps(["3.41.0", "3.44.0", "3.44.0"]),
                encoding="utf-8",
            )

            self.assertEqual(load_flutter_versions(path), ["3.41.0", "3.44.0"])

            path.write_text(json.dumps(["3.45.0-0.1.pre"]), encoding="utf-8")
            with self.assertRaises(Exception):
                load_flutter_versions(path)

    def test_variant_name_uses_full_stable_version(self) -> None:
        self.assertEqual(variant_name("3.44.0"), "flutter-3.44.0")
        with self.assertRaises(Exception):
            variant_name("3.44.0-rc.1")


class DartSdkCompatibilityTests(unittest.TestCase):
    def test_evaluates_common_pub_sdk_constraints(self) -> None:
        self.assertTrue(is_dart_sdk_compatible("3.12.0", ">=2.12.0 <4.0.0"))
        self.assertTrue(is_dart_sdk_compatible("3.12.0", "^3.0.0"))
        self.assertFalse(is_dart_sdk_compatible("3.12.0", ">=2.12.0 <3.0.0"))
        self.assertFalse(is_dart_sdk_compatible("3.2.0", ">=3.3.0 <4.0.0"))


class FlutterVersionManagerTests(unittest.TestCase):
    def test_incomplete_cache_worktree_is_removed_and_recreated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "flutter"
            source = cache_dir / "_src"
            source.mkdir(parents=True)
            root = cache_dir / "3.44.0"
            root.mkdir()
            (root / "poison").write_text("interrupted install", encoding="utf-8")
            runner = _FakeInstallRunner(root)
            manager = FlutterVersionManager(
                cache_dir=cache_dir,
                command_runner=runner,
            )

            sdk = manager.ensure("3.44.0", timeout=1.0)

            self.assertEqual(sdk.dart_version, "3.12.0")
            self.assertFalse((root / "poison").exists())
            self.assertTrue((root / "bin" / "flutter").is_file())
            commands = [" ".join(command) for command, _cwd in runner.commands]
            self.assertIn(
                f"git -C {source} worktree remove --force {root}",
                commands,
            )
            self.assertIn(
                f"git -C {source} worktree add --detach {root} 3.44.0",
                commands,
            )


class FlutterVariantPipelineTests(unittest.TestCase):
    def test_collects_valid_non_obfuscated_variant_entry_with_fake_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package_dir = _package_dir(root, "probe_pkg", ">=2.12.0 <4.0.0")
            runner = _FakeRunner()
            manager = _FakeFlutterManager(root, "3.44.0", "3.12.0")

            with _patched_collectors():
                entry = collect_flutter_variant_entry(
                    "probe_pkg",
                    "1.0.0",
                    flutter_version="3.44.0",
                    package_dir=package_dir,
                    work_dir=root / "work",
                    flutter_manager=manager,
                    command_runner=runner,
                    timeout=1.0,
                )

        _validate_entry(entry)
        variant = entry["flutter_variants"][0]
        self.assertEqual(variant["flutter_version"], "3.44.0")
        self.assertEqual(variant["dart_version"], "3.12.0")
        self.assertEqual(variant["fingerprint"]["strategy"], STRATEGY)
        self.assertEqual(variant["fingerprint"]["string_literals"], ["probe-only"])

        build_commands = [
            command
            for command, _cwd in runner.commands
            if Path(command[0]).name == "flutter" and command[1:3] == ["build", "apk"]
        ]
        self.assertEqual(len(build_commands), 2)
        self.assertTrue(all("--obfuscate" not in command for command in build_commands))
        self.assertTrue(
            all(
                not any(part.startswith("--split-debug-info=") for part in command)
                for command in build_commands
            )
        )

    def test_incompatible_dart_sdk_is_skipped_before_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package_dir = _package_dir(root, "old_pkg", ">=2.12.0 <3.0.0")
            manager = _FakeFlutterManager(root, "3.44.0", "3.12.0")
            runner = _FakeRunner()

            with self.assertRaises(FlutterVariantSkipped) as raised:
                collect_flutter_variant_entry(
                    "old_pkg",
                    "1.0.0",
                    flutter_version="3.44.0",
                    package_dir=package_dir,
                    flutter_manager=manager,
                    command_runner=runner,
                    timeout=1.0,
                )

        skip = raised.exception.skip.to_json()
        self.assertEqual(skip["flutter_version"], "3.44.0")
        self.assertIn("requires Dart SDK", skip["reason"])
        self.assertEqual(runner.commands, [])

    def test_skip_records_are_upserted_by_package_version_and_flutter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "db" / "_flutter_variant_skips.json"
            upsert_skip_record(
                path,
                FlutterVariantSkip(
                    "pkg",
                    "1.0.0",
                    "3.44.0",
                    "old reason",
                    "2026-01-01T00:00:00Z",
                ),
            )
            upsert_skip_record(
                path,
                FlutterVariantSkip(
                    "pkg",
                    "1.0.0",
                    "3.44.0",
                    "new reason",
                    "2026-01-02T00:00:00Z",
                ),
            )

            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(len(payload["skips"]), 1)
        self.assertEqual(payload["skips"][0]["reason"], "new reason")


class _FakeFlutterManager:
    def __init__(self, root: Path, flutter_version: str, dart_version: str) -> None:
        self.sdk = FlutterSdk(
            version=flutter_version,
            dart_version=dart_version,
            root=root / "flutter",
            flutter_executable=root / "flutter" / "bin" / "flutter",
            dart_executable=root / "flutter" / "bin" / "dart",
        )

    def ensure(self, version: str, *, timeout: float) -> FlutterSdk:
        del timeout
        self.sdk.flutter_executable.parent.mkdir(parents=True, exist_ok=True)
        self.sdk.flutter_executable.write_text("#!/bin/sh\n", encoding="utf-8")
        self.sdk.dart_executable.write_text("#!/bin/sh\n", encoding="utf-8")
        if version != self.sdk.version:
            raise AssertionError(f"{version!r} != {self.sdk.version!r}")
        return self.sdk


class _FakeRunner:
    def __init__(self) -> None:
        self.commands: list[tuple[list[str], Path]] = []

    def __call__(
        self,
        command,
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        command = [str(part) for part in command]
        self.commands.append((command, cwd))
        executable = Path(command[0]).name
        if executable == "flutter" and command[1:2] == ["create"]:
            (cwd / "lib").mkdir(parents=True, exist_ok=True)
        elif executable == "flutter" and command[1:3] == ["build", "apk"]:
            apk = (
                cwd
                / "build"
                / "app"
                / "outputs"
                / "flutter-apk"
                / "app-release.apk"
            )
            apk.parent.mkdir(parents=True, exist_ok=True)
            apk.write_bytes(b"apk")
        elif command[:2] == ["rettulf", "dump"]:
            out_path = Path(command[command.index("-o") + 1])
            snapshot = (
                _target_snapshot() if cwd.name == "target" else _baseline_snapshot()
            )
            out_path.write_text(json.dumps(snapshot), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


class _FakeInstallRunner:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.commands: list[tuple[list[str], Path]] = []

    def __call__(
        self,
        command,
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        command = [str(part) for part in command]
        self.commands.append((command, cwd))
        if "worktree" in command and "add" in command:
            flutter = self.root / "bin" / "flutter"
            dart = self.root / "bin" / "dart"
            flutter.parent.mkdir(parents=True, exist_ok=True)
            flutter.write_text("#!/bin/sh\n", encoding="utf-8")
            dart.write_text("#!/bin/sh\n", encoding="utf-8")
        if Path(command[0]).name == "flutter" and command[1:] == [
            "--version",
            "--machine",
        ]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps({"dartSdkVersion": "3.12.0"}),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def _patched_collectors():
    manifest = ProbeManifest(
        libraries=("package:probe_pkg/probe_pkg.dart",),
        references=(
            ProbeReference(
                library="package:probe_pkg/probe_pkg.dart",
                expression="WidgetFactory.new",
                declaration="member:WidgetFactory:method:WidgetFactory",
                kind="constructor",
            ),
        ),
    )
    return mock.patch.multiple(
        "collector.pipelines.flutter_variant",
        collect_api_surface=mock.Mock(
            return_value={
                "classes": {
                    "WidgetFactory": {
                        "libraries": ["package:probe_pkg/probe_pkg.dart"],
                        "methods": ["WidgetFactory"],
                        "fields": [],
                        "types": [],
                    }
                }
            }
        ),
        collect_source_fingerprint=mock.Mock(
            return_value={"strategy": "source-structural-v1", "digest": "1" * 64}
        ),
        collect_probe_manifest=mock.Mock(return_value=manifest),
    )


def _package_dir(root: Path, name: str, sdk_constraint: str) -> Path:
    package_dir = root / name
    _write(
        package_dir / "pubspec.yaml",
        f"""
name: {name}
version: 1.0.0
environment:
  sdk: '{sdk_constraint}'
""",
    )
    _write(package_dir / "lib" / f"{name}.dart", "class WidgetFactory {}\n")
    return package_dir


def _target_snapshot() -> dict[str, object]:
    return {
        "objects": [
            {
                "id": 1,
                "type": "Class",
                "name": "WidgetFactory",
                "fields": [10],
                "functions": [20],
            }
        ],
        "strings": ["baseline-only", "probe-only"],
        "method_channels": [],
        "ffi_trampolines": [],
    }


def _baseline_snapshot() -> dict[str, object]:
    return {
        "objects": [],
        "strings": ["baseline-only"],
        "method_channels": [],
        "ffi_trampolines": [],
    }


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")


def _validate_entry(entry: dict[str, object]) -> None:
    if Draft202012Validator is None:
        return
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(entry)
