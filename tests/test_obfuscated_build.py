from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from collector.pipelines.obfuscated_build import (
    STRATEGY,
    build_obfuscated_fingerprint,
    collect_obfuscated_build_entries,
    collect_probe_manifest,
)

try:
    from jsonschema import Draft202012Validator
except ModuleNotFoundError:  # pragma: no cover - optional local test dependency
    Draft202012Validator = None

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "schema" / "_schema.v1.json"


class ObfuscatedFingerprintTests(unittest.TestCase):
    def test_fingerprint_subtracts_baseline_snapshot_signals(self) -> None:
        target = {
            "objects": [
                {
                    "id": 1,
                    "type": "Class",
                    "name": "a",
                    "fields": [10],
                    "functions": [20],
                },
                {
                    "id": 2,
                    "type": "Class",
                    "name": "b",
                    "super_class": 1,
                    "fields": [11, 12],
                    "functions": [21],
                },
                {"id": 3, "type": "String", "value": "pkg literal"},
                {"id": 4, "type": "String", "value": "framework literal"},
            ],
            "strings": [
                "framework literal",
                "pkg literal",
                "com.example/probe",
            ],
            "method_channels": ["framework/channel", "com.example/probe"],
            "ffi_trampolines": [
                {"kind": "call_closure", "native_symbol": "pkg_symbol"}
            ],
            "closures": [
                {"function_id": 42, "is_canonical": True},
                {"function_id": 43, "is_canonical": True},
            ],
        }
        baseline = {
            "objects": [
                {
                    "id": 1,
                    "type": "Class",
                    "name": "z",
                    "fields": [10],
                    "functions": [20],
                }
            ],
            "strings": ["framework literal"],
            "method_channels": ["framework/channel"],
            "ffi_trampolines": [
                {"kind": "call_closure", "native_symbol": "framework_symbol"}
            ],
            "closures": [{"function_id": 7, "is_canonical": True}],
        }

        first = build_obfuscated_fingerprint(target, baseline)
        second = build_obfuscated_fingerprint(target, baseline)

        self.assertEqual(first, second)
        self.assertEqual(first["strategy"], STRATEGY)
        self.assertRegex(first["digest"], r"^[a-f0-9]{64}$")
        self.assertRegex(first["hierarchy_hash"], r"^[a-f0-9]{64}$")
        self.assertEqual(first["class_shape_count"], 1)
        self.assertEqual(
            first["string_literals"],
            ["com.example/probe", "pkg literal"],
        )
        self.assertEqual(first["method_channels"], ["com.example/probe"])
        self.assertEqual(first["ffi_symbols"], ["pkg_symbol"])
        self.assertEqual(first["const_canonicalization"], ["canonical_closure:1"])
        self.assertTrue(first["baseline_subtracted"])

    def test_fingerprint_hash_ignores_obfuscated_names_and_snapshot_ids(self) -> None:
        first = build_obfuscated_fingerprint(
            {
                "objects": [
                    {
                        "id": 1,
                        "type": "Class",
                        "name": "a",
                        "fields": [10],
                        "functions": [20],
                    },
                    {
                        "id": 2,
                        "type": "Class",
                        "name": "b",
                        "super_class": 1,
                        "fields": [11, 12],
                        "functions": [21],
                    },
                ],
                "closures": [{"function_id": 42, "is_canonical": True}],
            },
        )
        second = build_obfuscated_fingerprint(
            {
                "objects": [
                    {
                        "id": 100,
                        "type": "Class",
                        "name": "x",
                        "fields": [1000],
                        "functions": [2000],
                    },
                    {
                        "id": 200,
                        "type": "Class",
                        "name": "y",
                        "super_class": 100,
                        "fields": [1100, 1200],
                        "functions": [2100],
                    },
                ],
                "closures": [{"function_id": 99, "is_canonical": True}],
            },
        )

        self.assertEqual(first["digest"], second["digest"])
        self.assertEqual(first["hierarchy_hash"], second["hierarchy_hash"])
        self.assertEqual(first["const_canonicalization"], second["const_canonicalization"])


@unittest.skipIf(shutil.which("dart") is None, "Dart SDK is required")
class ObfuscatedBuildPipelineTests(unittest.TestCase):
    def test_probe_manifest_uses_public_entrypoints_exports_and_skips_sealed_new(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package_dir = Path(tmp)
            _write(
                package_dir / "lib" / "probe_pkg.dart",
                """
                export 'src/barrel.dart' show PublicThing, Shape, topProbe;
                export 'src/internal.dart' hide HiddenThing;
                """,
            )
            _write(
                package_dir / "lib" / "src" / "barrel.dart",
                """
                export 'api.dart' show PublicThing, Shape, topProbe, LeakedThing;
                """,
            )
            _write(
                package_dir / "lib" / "src" / "api.dart",
                """
                sealed class Shape {
                  const Shape();
                  factory Shape.make() => _ShapeImpl();
                }

                final class _ShapeImpl extends Shape {
                  const _ShapeImpl();
                }

                class PublicThing {
                  PublicThing();
                }

                void topProbe() {}

                class LeakedThing {}
                """,
            )
            _write(
                package_dir / "lib" / "src" / "internal.dart",
                """
                class HiddenThing {}
                class VisibleInternal {}
                """,
            )
            _write(
                package_dir / "lib" / "src" / "standalone.dart",
                """
                class SrcOnly {}
                """,
            )

            manifest = collect_probe_manifest(package_dir, "probe_pkg")

        expressions = {reference.expression for reference in manifest.references}
        self.assertEqual(manifest.libraries, ("package:probe_pkg/probe_pkg.dart",))
        self.assertIn("Shape", expressions)
        self.assertIn("Shape.make", expressions)
        self.assertNotIn("Shape.new", expressions)
        self.assertIn("PublicThing.new", expressions)
        self.assertIn("topProbe", expressions)
        self.assertNotIn("LeakedThing.new", expressions)
        self.assertNotIn("HiddenThing.new", expressions)
        self.assertNotIn("SrcOnly.new", expressions)

    def test_collects_entry_with_generated_probe_and_fake_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package_dir = root / "probe_pkg"
            _write(
                package_dir / "lib" / "probe_pkg.dart",
                """
                const topValue = 'top-value';
                String get topName => 'top-name';
                set topName(String value) {}
                void topProbe() {}

                abstract class AbstractThing {
                  AbstractThing();
                }

                class WidgetFactory {
                  final int instanceField = 0;
                  WidgetFactory();
                  WidgetFactory.named();
                  static const channel = 'com.example/probe';
                  static WidgetFactory make() => WidgetFactory();
                  void instanceMethod() {}
                }

                enum Mode { fast, slow }

                extension ModeX on Mode {
                  static String describe() => 'mode-description';
                  String get label => name;
                }
                """,
            )
            work_dir = root / "work"
            runner = _FakeRunner()

            entries = collect_obfuscated_build_entries(
                "probe_pkg",
                "1.0.0",
                package_dir=package_dir,
                work_dir=work_dir,
                command_runner=runner,
                timeout=1.0,
            )
            rerun_entries = collect_obfuscated_build_entries(
                "probe_pkg",
                "1.0.0",
                package_dir=package_dir,
                work_dir=work_dir,
                command_runner=runner,
                timeout=1.0,
            )

            self.assertEqual(len(entries), 1)
            entry = entries[0]
            _validate_entry(entry)
            fingerprint = entry["obfuscated_fingerprint"]
            self.assertEqual(fingerprint["strategy"], STRATEGY)
            self.assertEqual(fingerprint["string_literals"], ["probe-only"])
            self.assertEqual(fingerprint["method_channels"], ["com.example/probe"])
            self.assertEqual(fingerprint["ffi_symbols"], ["probe_symbol"])

            reachable = fingerprint["reachable_surface"]
            self.assertGreater(reachable["reachable_public_declarations"], 0)
            self.assertGreater(reachable["total_public_declarations"], 0)
            self.assertIn(
                "member:WidgetFactory:method:make",
                reachable["declarations"],
            )
            self.assertIn("member:Mode:field:fast", reachable["declarations"])

            target_main = (work_dir / "target" / "lib" / "main.dart").read_text(
                encoding="utf-8"
            )
            self.assertIn(
                "import 'package:probe_pkg/probe_pkg.dart' as p0;",
                target_main,
            )
            self.assertIn("p0.WidgetFactory.new", target_main)
            self.assertIn("p0.WidgetFactory.named", target_main)
            self.assertIn("p0.WidgetFactory.make", target_main)
            self.assertIn("p0.Mode.fast", target_main)
            self.assertIn("p0.ModeX.describe", target_main)
            self.assertIn("p0.topProbe", target_main)
            self.assertNotIn("p0.AbstractThing.new", target_main)

            build_commands = [
                command
                for command, _cwd in runner.commands
                if command[:3] == ["flutter", "build", "apk"]
            ]
            self.assertEqual(len(build_commands), 3)
            self.assertTrue(
                all("--obfuscate" in command for command in build_commands)
            )
            self.assertTrue(
                all(
                    any(part.startswith("--split-debug-info=") for part in command)
                    for command in build_commands
                )
            )
            self.assertEqual(
                rerun_entries[0]["obfuscated_fingerprint"]["digest"],
                fingerprint["digest"],
            )

    def test_probe_build_falls_back_to_partial_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package_dir = root / "fallback_pkg"
            _write(
                package_dir / "lib" / "fallback_pkg.dart",
                """
                class GoodThing {
                  GoodThing();
                }

                class BadThing {
                  BadThing();
                }
                """,
            )
            runner = _FailingReferenceRunner("BadThing.new")

            entries = collect_obfuscated_build_entries(
                "fallback_pkg",
                "1.0.0",
                package_dir=package_dir,
                work_dir=root / "work",
                command_runner=runner,
                timeout=1.0,
            )

        reachable = entries[0]["obfuscated_fingerprint"]["reachable_surface"]
        self.assertTrue(reachable["partial"])
        self.assertTrue(reachable["probe_fallback"])
        self.assertIn(
            "member:BadThing:method:BadThing",
            reachable["omitted_declarations"],
        )
        self.assertGreater(runner.failed_builds, 0)


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
        command = list(command)
        self.commands.append((command, cwd))
        if command[:2] == ["flutter", "create"]:
            (cwd / "lib").mkdir(parents=True, exist_ok=True)
        elif command[:3] == ["flutter", "build", "apk"]:
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


class _FailingReferenceRunner(_FakeRunner):
    def __init__(self, bad_reference: str) -> None:
        super().__init__()
        self.bad_reference = bad_reference
        self.failed_builds = 0

    def __call__(
        self,
        command,
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["flutter", "build", "apk"]:
            main_path = cwd / "lib" / "main.dart"
            if (
                cwd.name.startswith("target")
                and main_path.is_file()
                and self.bad_reference in main_path.read_text(encoding="utf-8")
            ):
                self.commands.append((list(command), cwd))
                self.failed_builds += 1
                return subprocess.CompletedProcess(
                    command,
                    1,
                    stdout="",
                    stderr=f"bad reference: {self.bad_reference}",
                )
        return super().__call__(command, cwd, timeout)


def _target_snapshot() -> dict[str, object]:
    return {
        "objects": [
            {
                "id": 1,
                "type": "Class",
                "name": "a",
                "fields": [10],
                "functions": [20],
            },
            {
                "id": 2,
                "type": "Class",
                "name": "b",
                "super_class": 1,
                "fields": [11, 12],
                "functions": [21],
            },
        ],
        "strings": ["baseline-only", "probe-only"],
        "method_channels": ["baseline/channel", "com.example/probe"],
        "ffi_trampolines": [
            {"kind": "call_closure", "native_symbol": "probe_symbol"}
        ],
    }


def _baseline_snapshot() -> dict[str, object]:
    return {
        "objects": [
            {
                "id": 1,
                "type": "Class",
                "name": "z",
                "fields": [10],
                "functions": [20],
            }
        ],
        "strings": ["baseline-only"],
        "method_channels": ["baseline/channel"],
        "ffi_trampolines": [],
    }


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _validate_entry(entry: dict[str, object]) -> None:
    if Draft202012Validator is None:
        return
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(entry)
