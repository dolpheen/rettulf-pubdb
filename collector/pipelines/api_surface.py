"""Collect a Dart package's public API surface with a Dart analyzer helper."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]

DEFAULT_TIMEOUT_SECONDS = 120.0

_PUB_GET_DONE: set[Path] = set()
_COMPILED_HELPERS: set[Path] = set()
_SETUP_LOCK = threading.Lock()


class ApiSurfaceError(RuntimeError):
    """Raised when API-surface collection fails."""


def collect_api_surface(
    package_dir: Path | str,
    package: str,
    *,
    dart_executable: str | Path | None = None,
    helper_dir: Path | str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> JsonObject:
    """Return schema-v1-compatible ``api_surface`` JSON for an extracted package."""
    root = Path(package_dir).resolve()
    if not root.is_dir():
        raise ApiSurfaceError(f"package directory does not exist: {root}")

    lib_dir = root / "lib"
    if not lib_dir.is_dir():
        return {"classes": {}}

    helper_root = Path(helper_dir).resolve() if helper_dir else _default_helper_dir()
    dart = str(dart_executable or _default_dart_executable())
    helper_executable = _ensure_helper_executable(helper_root, dart, timeout)

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
        raise ApiSurfaceError(f"api_surface helper failed: {detail}")

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ApiSurfaceError("api_surface helper returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ApiSurfaceError("api_surface helper returned non-object JSON")
    return _normalize_surface(payload)


def _normalize_surface(payload: JsonObject) -> JsonObject:
    classes = payload.get("classes")
    if not isinstance(classes, dict):
        raise ApiSurfaceError("api_surface helper JSON is missing classes object")

    normalized: dict[str, JsonObject] = {}
    for raw_name, raw_surface in classes.items():
        if not isinstance(raw_name, str) or not isinstance(raw_surface, dict):
            continue
        normalized[raw_name] = {
            "libraries": _sorted_strings(raw_surface.get("libraries")),
            "methods": _sorted_strings(raw_surface.get("methods")),
            "fields": _sorted_strings(raw_surface.get("fields")),
            "types": _sorted_strings(raw_surface.get("types")),
        }
    return {"classes": dict(sorted(normalized.items()))}


def _sorted_strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted(item for item in value if isinstance(item, str) and item)


def _ensure_helper_executable(helper_root: Path, dart: str, timeout: float) -> Path:
    executable = helper_root / ".dart_tool" / "rettulf_pubdb" / "api_surface"
    with _SETUP_LOCK:
        if helper_root not in _PUB_GET_DONE:
            _run_setup_command(
                [dart, "pub", "get"],
                helper_root,
                timeout,
                "api_surface helper pub get failed",
            )
            _PUB_GET_DONE.add(helper_root)

        source = helper_root / "bin" / "api_surface.dart"
        if helper_root not in _COMPILED_HELPERS or _is_stale(executable, source):
            executable.parent.mkdir(parents=True, exist_ok=True)
            _run_setup_command(
                [
                    dart,
                    "compile",
                    "exe",
                    "bin/api_surface.dart",
                    "-o",
                    str(executable),
                ],
                helper_root,
                timeout,
                "api_surface helper compile failed",
            )
            _COMPILED_HELPERS.add(helper_root)
    return executable


def _run_setup_command(
    command: list[str],
    helper_root: Path,
    timeout: float,
    error_prefix: str,
) -> None:
    completed = subprocess.run(
        command,
        cwd=helper_root,
        env=_helper_env(),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ApiSurfaceError(f"{error_prefix}: {detail}")


def _is_stale(output: Path, source: Path) -> bool:
    if not output.exists():
        return True
    return output.stat().st_mtime < source.stat().st_mtime


def _helper_env() -> dict[str, str]:
    env = os.environ.copy()
    env["DART_SUPPRESS_ANALYTICS"] = "true"
    env["FLUTTER_SUPPRESS_ANALYTICS"] = "true"
    return env


def _default_helper_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "dart_helper"


def _default_dart_executable() -> str:
    dart = shutil.which("dart")
    if dart is None:
        raise ApiSurfaceError("could not find Dart executable on PATH")
    return dart


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package_dir", type=Path)
    parser.add_argument("--package", required=True)
    parser.add_argument("--dart", dest="dart_executable")
    args = parser.parse_args(argv)

    try:
        surface = collect_api_surface(
            args.package_dir,
            args.package,
            dart_executable=args.dart_executable,
        )
    except ApiSurfaceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(surface, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
