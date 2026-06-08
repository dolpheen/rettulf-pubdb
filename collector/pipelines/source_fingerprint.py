"""Collect a Dart package's source-derived structural fingerprint."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

from collector.pipelines.api_surface import (
    ApiSurfaceError,
    DEFAULT_TIMEOUT_SECONDS,
    _default_dart_executable,
    _default_helper_dir,
    _ensure_helper_executable,
    _helper_env,
)

JsonObject = dict[str, Any]

STRATEGY = "source-structural-v1"


class SourceFingerprintError(RuntimeError):
    """Raised when source-fingerprint collection fails."""


def collect_source_fingerprint(
    package_dir: Path | str,
    package: str,
    *,
    dart_executable: str | Path | None = None,
    helper_dir: Path | str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> JsonObject:
    """Return schema-v1-compatible ``source_fingerprint`` JSON."""
    root = Path(package_dir).resolve()
    if not root.is_dir():
        raise SourceFingerprintError(f"package directory does not exist: {root}")
    if not (root / "lib").is_dir():
        return _normalize_fingerprint({"_hierarchy": []})

    helper_root = Path(helper_dir).resolve() if helper_dir else _default_helper_dir()
    try:
        dart = str(dart_executable or _default_dart_executable())
        helper_executable = _ensure_helper_executable(
            helper_root,
            dart,
            timeout,
            "source_fingerprint",
        )
    except ApiSurfaceError as exc:
        raise SourceFingerprintError(str(exc)) from exc

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
        raise SourceFingerprintError(f"source_fingerprint helper failed: {detail}")

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SourceFingerprintError(
            "source_fingerprint helper returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise SourceFingerprintError("source_fingerprint helper returned non-object JSON")
    return _normalize_fingerprint(payload)


def _normalize_fingerprint(payload: JsonObject) -> JsonObject:
    hierarchy = _normalize_hierarchy(payload.get("_hierarchy"))
    body: JsonObject = {
        "hierarchy_hash": _sha256_json(hierarchy),
        "string_literals": _sorted_strings(payload.get("string_literals")),
        "method_channels": _sorted_strings(payload.get("method_channels")),
        "event_channels": _sorted_strings(payload.get("event_channels")),
        "basic_message_channels": _sorted_strings(
            payload.get("basic_message_channels")
        ),
        "ffi_symbols": _sorted_strings(payload.get("ffi_symbols")),
        "const_classes": _sorted_strings(payload.get("const_classes")),
    }
    return {
        "strategy": STRATEGY,
        "digest": _sha256_json(body),
        **body,
    }


def _normalize_hierarchy(value: object) -> list[JsonObject]:
    if not isinstance(value, list):
        return []

    entries: list[JsonObject] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        class_name = item.get("class")
        super_name = item.get("super")
        fields_count = item.get("fields_count")
        methods_count = item.get("methods_count")
        if (
            not isinstance(class_name, str)
            or not isinstance(super_name, str)
            or not isinstance(fields_count, int)
            or not isinstance(methods_count, int)
        ):
            continue
        entries.append(
            {
                "class": class_name,
                "super": super_name,
                "fields_count": fields_count,
                "methods_count": methods_count,
            }
        )
    return sorted(
        entries,
        key=lambda entry: (
            entry["class"],
            entry["super"],
            entry["fields_count"],
            entry["methods_count"],
        ),
    )


def _sorted_strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted(
        {
            unicodedata.normalize("NFC", item)
            for item in value
            if isinstance(item, str)
        }
    )


def _sha256_json(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package_dir", type=Path)
    parser.add_argument("--package", required=True)
    parser.add_argument("--dart", dest="dart_executable")
    args = parser.parse_args(argv)

    try:
        fingerprint = collect_source_fingerprint(
            args.package_dir,
            args.package,
            dart_executable=args.dart_executable,
        )
    except SourceFingerprintError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(fingerprint, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
