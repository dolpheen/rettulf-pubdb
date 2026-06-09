# collector/

The 24/7 fingerprint collection pipeline. This directory started as scaffolding
from the bootstrap issue
([#1](https://github.com/dolpheen/rettulf-pubdb/issues/1)).

Implemented:

- `pubdev_client.py` — pub.dev archive fetcher with a local cache.
- `daemon.py` — SQLite-backed queue/scheduler and atomic git publisher.
- `pipelines/api_surface.py` — analyzer-backed Dart API-surface collector.
- `pipelines/source_fingerprint.py` — analyzer-backed source structural
  fingerprint collector.
- `pipelines/obfuscated_build.py` — Flutter probe-app builder for
  `db/<package>/<version>.obf.json` obfuscated-build variants.

Planned contents (tracked separately):

- `#7` — per-Flutter-version variant (`flutter_variants`)

Every entry the collector writes under `db/<package>/` must validate against
`schema/_schema.v1.json` (`python scripts/validate.py`).
