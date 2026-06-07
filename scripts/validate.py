#!/usr/bin/env python3
"""Validate rettulf-pubdb entries against schema/_schema.v1.json.

Usage:
    python scripts/validate.py                 # validate all entries + entry examples
    python scripts/validate.py db/dio/5.4.0.json ...   # validate specific files

Entry files are db/<package>/<version>.json plus schema/examples/*.json entry
examples. Raw API-surface fixtures named *.api.json are skipped by default.
Meta files under db/ whose name starts with "_" (e.g. db/_index.json,
db/_top1000.json) are not entries and are skipped.

Exit code is non-zero if any file fails validation; each violation is printed
as "<file>: <json-path>: <message>" so the CI log names the exact problem.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
except ModuleNotFoundError:  # pragma: no cover - surfaced to the user directly
    sys.exit("error: jsonschema is not installed (pip install -r scripts/requirements.txt)")

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "schema" / "_schema.v1.json"


def entry_files() -> list[Path]:
    """Return all entry files: db/<pkg>/<version>.json (not db/_*.json) + examples."""
    db_entries = [
        path
        for path in sorted((REPO_ROOT / "db").rglob("*.json"))
        if not path.name.startswith("_")
    ]
    examples = [
        path
        for path in sorted((REPO_ROOT / "schema" / "examples").glob("*.json"))
        if not path.name.endswith(".api.json")
    ]
    return db_entries + examples


def validate_file(validator: Draft202012Validator, path: Path) -> list[str]:
    """Return a list of human-readable violation strings for one file."""
    rel = path.relative_to(REPO_ROOT)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"{rel}: <file>: invalid JSON: {exc}"]

    violations = []
    for error in sorted(validator.iter_errors(document), key=lambda e: list(e.path)):
        location = "/".join(str(part) for part in error.path) or "<root>"
        violations.append(f"{rel}: {location}: {error.message}")
    return violations


def main(argv: list[str]) -> int:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    if argv:
        targets = [Path(arg).resolve() for arg in argv]
    else:
        targets = entry_files()

    if not targets:
        print("No entry files found to validate.")
        return 0

    failures = 0
    for path in targets:
        violations = validate_file(validator, path)
        if violations:
            failures += 1
            for line in violations:
                print(line)
        else:
            print(f"{path.relative_to(REPO_ROOT)}: OK")

    total = len(targets)
    print(f"\nValidated {total} file(s): {total - failures} passed, {failures} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
