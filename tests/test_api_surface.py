from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from collector.pubdev_client import PubDevClient
from collector.pipelines.api_surface import collect_api_surface

REPO_ROOT = Path(__file__).resolve().parent.parent
PROVIDER_API_FIXTURE = REPO_ROOT / "schema" / "examples" / "provider-6.0.5.api.json"


@unittest.skipIf(shutil.which("dart") is None, "Dart SDK is required")
class ApiSurfaceTests(unittest.TestCase):
    def test_collects_public_api_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package_dir = Path(tmp)
            _write(
                package_dir / "lib" / "sample.dart",
                """
                library sample;

                export 'src/internal.dart' show PublicApi, VisibleEnum, publicTop, PublicAlias;
                part 'src/parts.dart';

                class Root<T> extends Base with Shared implements Contract {
                  final String field;
                  final List<String> values;
                  Root(this.field, this.values);
                  factory Root.named(String value, {required int count, bool flag = false}) => Root(value);
                  String method(int id, {String label = 'x'}) {
                    _PrivateImpl local = _PrivateImpl();
                    return field;
                  }
                  String get title => field;
                  set title(String value) {}
                }

                class _PrivateImpl {}

                mixin Shared on Base {
                  void mixinMethod() {}
                }

                class Base {}
                class Contract {}
                """,
            )
            _write(
                package_dir / "lib" / "src" / "parts.dart",
                """
                part of sample;

                extension RootX on Root {
                  int extensionMethod({required String name}) => name.length;
                }
                """,
            )
            _write(
                package_dir / "lib" / "src" / "internal.dart",
                """
                enum VisibleEnum { one, two }
                class PublicApi {
                  PublicApi();
                  static PublicApi make() => PublicApi();
                }
                class HiddenApi {}
                typedef PublicAlias = PublicApi Function(String value);
                int publicTop({int value = 1}) => value;
                String get publicName => 'sample';
                set publicName(String value) {}
                """,
            )

            surface = collect_api_surface(package_dir, "sample")

        classes = surface["classes"]
        self.assertNotIn("_PrivateImpl", classes)
        self.assertIn("Root", classes)
        self.assertIn("Shared", classes)
        self.assertIn("VisibleEnum", classes)
        self.assertIn("RootX", classes)
        self.assertIn("::", classes)

        root = classes["Root"]
        self.assertEqual(
            root["libraries"],
            ["package:sample/sample.dart"],
        )
        self.assertIn("Root", root["methods"])
        self.assertIn("named", root["methods"])
        self.assertIn("method", root["methods"])
        self.assertIn("field", root["fields"])
        self.assertIn("values", root["fields"])
        self.assertIn("get:title", root["fields"])
        self.assertIn("set:title", root["fields"])
        self.assertIn("Base", root["types"])
        self.assertIn("Contract", root["types"])
        self.assertIn("Shared", root["types"])
        self.assertNotIn("_PrivateImpl", root["types"])
        self.assertNotIn("List", root["types"])
        self.assertNotIn("List<String>", root["types"])

        self.assertIn("Base", classes["Shared"]["types"])
        self.assertEqual(classes["VisibleEnum"]["fields"], ["one", "two"])
        self.assertEqual(classes["VisibleEnum"]["types"], [])
        self.assertIn("extensionMethod", classes["RootX"]["methods"])
        self.assertIn("Root", classes["RootX"]["types"])

        top_level = classes["::"]
        self.assertIn("publicTop", top_level["methods"])
        self.assertIn("get:publicName", top_level["fields"])
        self.assertIn("set:publicName", top_level["fields"])
        self.assertNotIn("typedef:PublicAlias", top_level["fields"])
        self.assertIn("package:sample/sample.dart", classes["PublicApi"]["libraries"])
        self.assertIn("package:sample/src/internal.dart", classes["PublicApi"]["libraries"])
        _assert_no_decorated_tokens(self, surface)

    def test_export_hide_and_deterministic_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package_dir = Path(tmp)
            _write(
                package_dir / "lib" / "pkg.dart",
                """
                export 'src/api.dart' hide HiddenApi;
                """,
            )
            _write(
                package_dir / "lib" / "src" / "api.dart",
                """
                class VisibleApi {}
                class HiddenApi {}
                """,
            )

            first = collect_api_surface(package_dir, "pkg")
            second = collect_api_surface(package_dir, "pkg")

        self.assertEqual(
            json.dumps(first, sort_keys=True),
            json.dumps(second, sort_keys=True),
        )
        self.assertIn("package:pkg/pkg.dart", first["classes"]["VisibleApi"]["libraries"])
        self.assertNotIn(
            "package:pkg/pkg.dart",
            first["classes"]["HiddenApi"]["libraries"],
        )

    def test_dangling_self_package_export_does_not_abort_collection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package_dir = Path(tmp)
            _write(
                package_dir / "lib" / "pkg.dart",
                """
                export 'package:pkg/src/missing.dart';

                class Survives {}
                """,
            )

            surface = collect_api_surface(package_dir, "pkg")

        self.assertIn("Survives", surface["classes"])

    def test_provider_6_0_5_matches_committed_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with PubDevClient(cache_dir=Path(tmp)) as client:
                lib_dir = client.fetch("provider", "6.0.5")
            surface = collect_api_surface(lib_dir.parent, "provider")

        expected = json.loads(PROVIDER_API_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(surface, expected)
        classes = surface["classes"]
        for name in ("ChangeNotifierProvider", "Provider", "MultiProvider"):
            with self.subTest(name=name):
                self.assertIn(name, classes)
                self.assertTrue(classes[name]["methods"])
        _assert_no_decorated_tokens(self, surface)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _assert_no_decorated_tokens(
    test_case: unittest.TestCase,
    surface: dict[str, object],
) -> None:
    prefixes = (
        "sig:",
        "kind:",
        "arity:",
        "extends:",
        "implements:",
        "mixes:",
        "on:",
        "typedef:",
        "annotation:",
        "export:",
    )
    classes = surface["classes"]
    assert isinstance(classes, dict)
    for class_surface in classes.values():
        assert isinstance(class_surface, dict)
        for bucket in ("methods", "fields", "types"):
            values = class_surface[bucket]
            assert isinstance(values, list)
            for value in values:
                assert isinstance(value, str)
                test_case.assertFalse(
                    value.startswith(prefixes),
                    f"unexpected decorated token in {bucket}: {value}",
                )
