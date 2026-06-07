# collector/

The 24/7 fingerprint collection pipeline. **Not implemented yet** — this
directory is scaffolding from the bootstrap issue
([#1](https://github.com/dolpheen/rettulf-pubdb/issues/1)).

Planned contents (tracked separately):

- `#2` — pub.dev archive fetcher + local cache
- `#3` — API-surface collector (produces the `api_surface` field)
- `#4` — source-derived structural fingerprint collector (`source_fingerprint`)
- `#5` — collector daemon: queue + scheduler + atomic commit (`daemon.py`)
- `#6` — obfuscated-build fingerprint variant (`obfuscated_fingerprint`)
- `#7` — per-Flutter-version variant (`flutter_variants`)

Every entry the collector writes to `db/<package>/<version>.json` must validate
against `schema/_schema.v1.json` (`python scripts/validate.py`).
