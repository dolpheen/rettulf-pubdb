from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from collector.pubdev_client import PubDevClient
from collector.pipelines.source_fingerprint import collect_source_fingerprint


@unittest.skipIf(shutil.which("dart") is None, "Dart SDK is required")
class SourceFingerprintTests(unittest.TestCase):
    def test_collects_source_fingerprint_signals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package_dir = Path(tmp)
            _write(
                package_dir / "lib" / "sample.dart",
                """
                const _methodName = 'const.method.var';

                final method = MethodChannel(_methodName);
                final constMethod = const MethodChannel('const.method.literal');
                final prefixedMethod = svc.MethodChannel('prefixed.method');
                final event = EventChannel('event.channel');
                final prefixedEvent = svc.EventChannel('prefixed.event');
                final basic = BasicMessageChannel<String>('basic.channel', null);
                final prefixedBasic = svc.BasicMessageChannel<String>(
                  'prefixed.basic',
                  null,
                );

                @Native<Int Function()>('native_symbol')
                external int nativeCall();

                void ffi(dynamic library) {
                  library.lookupFunction<Int Function(), int Function()>('lookup_symbol');
                }

                String message() => 'hello fixture';

                class Base {
                  final int value;
                  const Base(this.value);
                  int get doubled => value * 2;
                }

                class Shape extends Base {
                  final String name;
                  final int count;
                  const Shape(this.name, this.count) : super(count);
                  void run() {}
                }

                class NonConst {
                  NonConst();
                }
                """,
            )

            first = collect_source_fingerprint(package_dir, "sample")
            reruns = [
                collect_source_fingerprint(package_dir, "sample")
                for _ in range(2)
            ]

        self.assertEqual(first, reruns[0])
        self.assertEqual(first, reruns[1])
        self.assertEqual(first["strategy"], "source-structural-v1")
        self.assertRegex(first["digest"], r"^[a-f0-9]{64}$")
        self.assertRegex(first["hierarchy_hash"], r"^[a-f0-9]{64}$")
        self.assertEqual(
            first["method_channels"],
            ["const.method.literal", "const.method.var", "prefixed.method"],
        )
        self.assertEqual(
            first["event_channels"],
            ["event.channel", "prefixed.event"],
        )
        self.assertEqual(
            first["basic_message_channels"],
            ["basic.channel", "prefixed.basic"],
        )
        self.assertEqual(first["ffi_symbols"], ["lookup_symbol", "native_symbol"])
        self.assertEqual(first["const_classes"], ["Base", "Shape"])
        self.assertIn("hello fixture", first["string_literals"])

    def test_hierarchy_hash_changes_with_class_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_dir = root / "first"
            second_dir = root / "second"
            _write(
                first_dir / "lib" / "pkg.dart",
                """
                class Shape {
                  final int value;
                  Shape(this.value);
                }
                """,
            )
            _write(
                second_dir / "lib" / "pkg.dart",
                """
                class Shape {
                  final int value;
                  final int extra;
                  Shape(this.value, this.extra);
                }
                """,
            )

            first = collect_source_fingerprint(first_dir, "pkg")
            second = collect_source_fingerprint(second_dir, "pkg")

        self.assertNotEqual(first["hierarchy_hash"], second["hierarchy_hash"])

    def test_primary_constructor_fields_match_explicit_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary_dir = root / "primary"
            classic_dir = root / "classic"
            _write(
                primary_dir / "lib" / "pkg.dart",
                """
                class Shape(final int value, final int extra);
                """,
            )
            _write(
                classic_dir / "lib" / "pkg.dart",
                """
                class Shape {
                  final int value;
                  final int extra;
                  Shape(this.value, this.extra);
                }
                """,
            )

            primary = collect_source_fingerprint(primary_dir, "pkg")
            classic = collect_source_fingerprint(classic_dir, "pkg")

        self.assertEqual(primary["hierarchy_hash"], classic["hierarchy_hash"])

    def test_empty_sections_are_lists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package_dir = Path(tmp)
            _write(
                package_dir / "lib" / "empty.dart",
                """
                class Plain {}
                """,
            )

            fingerprint = collect_source_fingerprint(package_dir, "empty")

        self.assertEqual(fingerprint["string_literals"], [])
        self.assertEqual(fingerprint["method_channels"], [])
        self.assertEqual(fingerprint["event_channels"], [])
        self.assertEqual(fingerprint["basic_message_channels"], [])
        self.assertEqual(fingerprint["ffi_symbols"], [])
        self.assertEqual(fingerprint["const_classes"], [])

    def test_provider_6_0_5_source_fingerprint_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with PubDevClient(cache_dir=Path(tmp)) as client:
                lib_dir = client.fetch("provider", "6.0.5")
            fingerprints = [
                collect_source_fingerprint(lib_dir.parent, "provider")
                for _ in range(3)
            ]

        self.assertEqual(
            {fingerprint["hierarchy_hash"] for fingerprint in fingerprints},
            {fingerprints[0]["hierarchy_hash"]},
        )
        strings = fingerprints[0]["string_literals"]
        self.assertTrue(
            any("Tried to listen to" in value for value in strings),
            strings,
        )
        self.assertEqual(fingerprints[0]["method_channels"], [])
        self.assertEqual(fingerprints[0]["event_channels"], [])
        self.assertEqual(fingerprints[0]["basic_message_channels"], [])
        self.assertEqual(fingerprints[0]["ffi_symbols"], [])


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
