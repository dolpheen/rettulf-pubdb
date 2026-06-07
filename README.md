# rettulf-pubdb

Pub.dev **fingerprint database** for [`dolpheen/rettulf`](https://github.com/dolpheen/rettulf),
the Flutter AOT decompiler. rettulf identifies which pub.dev packages are baked
into a `libapp.so` by matching the snapshot's API surface / structure against
the fingerprints collected here.

This repo is split out from rettulf on purpose: the collector runs 24/7 on a
dedicated server (outside rettulf's CI budget) and the DB content has its own
release cadence. It is a **peer project**, not a submodule of rettulf.

Bootstrap issue: [#1 — Repo bootstrap + schema v1 + validation CI](https://github.com/dolpheen/rettulf-pubdb/issues/1).
Consumer counterpart: [`rettulf#21`](https://github.com/dolpheen/rettulf/issues/21).

## Layout

| Path | Purpose |
| --- | --- |
| `schema/_schema.v1.json` | JSON Schema (draft 2020-12) for one `(package, version)` entry. |
| `schema/examples/` | Example entries that must pass validation. |
| `db/<package>/<version>.json` | One fingerprint entry per package version. |
| `db/_index.json` | Index of available `(package, version)` entries (consumer fetches this first). |
| `db/_top1000.json` | Collection worklist (top pub.dev packages); filled by a future `scripts/refresh_top1000.py`. |
| `collector/` | 24/7 collection pipeline (not implemented yet — see `collector/README.md`). |
| `scripts/` | Tooling. `validate.py` validates entries against the schema. |
| `ops/` | Deployment (systemd / Docker) for the collector. |

`db/` files whose name starts with `_` (`_index.json`, `_top1000.json`) are
**meta files**, not entries, and are not validated against the entry schema.

## Entry format (schema v1)

Each `db/<package>/<version>.json` is one object:

```jsonc
{
  "pubdb_schema_version": 1,         // major schema version
  "package": "provider",
  "version": "6.0.5",
  "collected_at": "2026-05-20T09:14:02Z",
  "api_surface": { "classes": { /* class -> {libraries, methods, fields, types} */ } },
  "source_fingerprint": { "strategy": "...", "digest": "<sha256 hex>" },
  "obfuscated_fingerprint": { /* optional */ },
  "flutter_variants": [ /* optional per-Flutter-version fingerprints */ ]
}
```

`api_surface` mirrors rettulf's `api_surface.package_api_surfaces` output for a
single package, so the consumer's `normalize_surface` reads it directly.

### Schema versioning

`pubdb_schema_version` is the **major** version. Additive, backward-compatible
changes (e.g. new optional fields) stay on major `1`. A breaking change ships a
new `schema/_schema.v2.json` and bumps the integer; the rettulf consumer
**rejects entries whose major differs from the one it supports** (major-version
skew). The `db/_index.json` carries the same `pubdb_schema_version` so the
consumer can reject a skewed DB before fetching any entry.

## How it's consumed

rettulf reads this DB over `raw.githubusercontent.com` and caches it under
`~/.cache/rettulf/pubdb/` (24h TTL), falling back to an on-demand pub.dev fetch
when a `(package, version)` is missing. See `rettulf#21`. (Anonymous raw fetch
requires this repo to be **public**.)

## Validation

Every entry must validate against `schema/_schema.v1.json`. CI
(`.github/workflows/validate.yml`) runs on every PR and push and fails on any
schema violation, naming the exact file and JSON path.

Run it locally:

```sh
pip install -r scripts/requirements.txt
python scripts/validate.py                  # all entries + examples
python scripts/validate.py db/dio/5.4.0.json # specific files
```
