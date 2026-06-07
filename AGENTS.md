# rettulf-pubdb — agent guide

Pub.dev **fingerprint database** for the Flutter AOT decompiler
[`dolpheen/rettulf`](https://github.com/dolpheen/rettulf). rettulf matches a
snapshot's API surface / structure against the fingerprints stored here to
identify which pub.dev packages are baked into a `libapp.so`.

This is a **peer repo** of rettulf (separate release cadence; a 24/7 collector
runs on a dedicated server) — **not** a submodule. The consumer client lives in
rettulf ([`rettulf#21`](https://github.com/dolpheen/rettulf/issues/21)); the
**schema is the contract** between the two repos.

See `README.md` for the human-facing overview and the full entry format.

## The one invariant

Every entry `db/<package>/<version>.json` **must validate** against
`schema/_schema.v1.json`. Run before every commit:

```sh
pip install -r scripts/requirements.txt   # first time
python scripts/validate.py                 # all entries + examples
python scripts/validate.py db/dio/5.4.0.json   # or specific files
```

CI (`.github/workflows/validate.yml`) runs the same check on every PR/push and
fails on any violation, naming the file + JSON path. Meta files `db/_*.json`
(`_index.json`, `_top1000.json`) are **not** entries; the default/CI run skips
them (passing one to `validate.py` explicitly will still check it against the
entry schema and fail).

## Schema rules (don't break the consumer)

- `api_surface` is `{"classes": {ClassName: {libraries, methods, fields, types}}}`
  — the shape rettulf's `api_surface.package_api_surfaces` produces and its
  `normalize_surface` consumes (see `README.md`). **Do not change this shape**;
  the consumer reads it directly with no translation.
- `pubdb_schema_version` is the integer **major** version. Additive,
  backward-compatible changes (new optional fields) stay on major `1`. A
  breaking change ships `schema/_schema.v2.json`, bumps the integer, and must be
  coordinated with the rettulf consumer — it **rejects major-version skew**.
- `*_fingerprint` sub-objects only require `strategy` + a 64-hex `digest`; the
  rest is intentionally open until the collectors (#3/#4/#6) finalize them.

## Layout

- `schema/` — JSON Schema (`_schema.v1.json`) + `examples/`.
- `db/` — `<package>/<version>.json` entries; `_index.json` (consumer fetches
  first), `_top1000.json` (collection worklist).
- `collector/` — 24/7 collection pipeline (not implemented yet; issues #2–#7).
- `scripts/` — `validate.py` (entry validator) + `requirements.txt`.
- `ops/` — collector deployment (issue #8).

## Conventions

- Entries are **machine-generated** by the collector; hand-edit only to fix a
  schema problem. Real fingerprint data comes from the pipeline, not by hand.
- **Branch + PR** for changes so CI validates them; don't push entry changes
  straight to `main` unvalidated.
- Use `git -C <dir> ...` instead of `cd <dir> && git ...` for any non-cwd repo.
- Never commit collector working data / archives (gitignored).
