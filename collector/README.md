# collector/

The 24/7 fingerprint collection pipeline. This directory started as scaffolding
from the bootstrap issue
([#1](https://github.com/dolpheen/rettulf-pubdb/issues/1)).

Implemented:

- `pubdev_client.py` — pub.dev archive fetcher with a local cache.

Planned contents (tracked separately):

- `#3` — API-surface collector (produces the `api_surface` field)
- `#4` — source-derived structural fingerprint collector (`source_fingerprint`)
- `#5` — collector daemon: queue + scheduler + atomic commit (`daemon.py`)
- `#6` — obfuscated-build fingerprint variant (`obfuscated_fingerprint`)
- `#7` — per-Flutter-version variant (`flutter_variants`)

Every entry the collector writes to `db/<package>/<version>.json` must validate
against `schema/_schema.v1.json` (`python scripts/validate.py`).
