# ops/ — deploying the collector

Deployment for the 24/7 collector daemon (`collector/daemon.py`). The daemon
discovers pub.dev versions, collects the **base** API surface + source
fingerprint for each, and commits/pushes the entries to this repo.

| File | Purpose |
| --- | --- |
| `docker/Dockerfile.collector` | Lean runtime image: Ubuntu 24.04 + Python 3.12 + Dart SDK. |
| `docker/collector-entrypoint.sh` | Maps the env file onto `collector.daemon` CLI flags + git auth. |
| `docker/docker-compose.yml` | Single-service daemon + the three persistent volumes. |
| `systemd/rettulf-pubdb-collector.service` | Runs compose under systemd with `Restart=on-failure`, logs to journald. |
| `env.example` | Configuration template — copy to `docker/.env`. |

## Prerequisites (fresh Ubuntu 24.04 VM)

- Docker Engine + the Compose plugin:
  ```sh
  curl -fsSL https://get.docker.com | sh
  ```
- `git`.
- A GitHub token with `contents:write` on `dolpheen/rettulf-pubdb` (for push).
- Disk: see [Disk budget](#disk-budget). Start with ~20 GB free.

## One-command-ish deploy

```sh
# 1. Clone the DB checkout the collector will commit to (HTTPS, so the token works).
sudo git clone https://github.com/dolpheen/rettulf-pubdb.git /opt/rettulf-pubdb
cd /opt/rettulf-pubdb

# 2. Configure.
cp ops/env.example ops/docker/.env
sudo --edit ops/docker/.env        # set GITHUB_TOKEN (or NO_PUSH=1 to dry-run)

# 3. Smoke-test before enabling the service (builds image, never pushes). Run
#    from ops/docker so Compose loads ops/docker/.env and resolves the relative
#    bind-mount the same way the systemd unit's WorkingDirectory does:
(cd ops/docker && docker compose run --rm --build \
  collector --once --no-metrics --no-push --packages provider)
git -C /opt/rettulf-pubdb log -1 --stat   # a db/provider/<v>.json commit

# 4. Install + start the service.
sudo cp ops/systemd/rettulf-pubdb-collector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rettulf-pubdb-collector
```

> The service's `WorkingDirectory` is `/opt/rettulf-pubdb/ops/docker`. If you
> clone elsewhere, edit that path (and `REPO_CHECKOUT` in `.env`) before installing.

## Verify it's healthy

```sh
systemctl status rettulf-pubdb-collector        # active (running)
journalctl -u rettulf-pubdb-collector -f        # live daemon + container logs
curl -s localhost:9305/metrics                  # Prometheus exposition
```

`/metrics` returns:

| Metric | Meaning |
| --- | --- |
| `pubdb_queue_size` | Queued work items. |
| `pubdb_entries_collected_total` | Entries collected + published. |
| `pubdb_pubdev_429_total` | pub.dev rate-limit responses seen. |
| `pubdb_publish_conflict_total` | git push conflicts auto-rebased. |
| `pubdb_last_commit_age_seconds` | Seconds since the last successful commit (`-1` = none yet). |

The port is published on `127.0.0.1` only. Scrape it from Prometheus over an
SSH tunnel (`ssh -L 9305:localhost:9305 <host>`) or a reverse proxy; change it
with `COLLECTOR_PORT`.

## Configuration

All knobs live in `ops/docker/.env` (template: `ops/env.example`). The
entrypoint maps them to daemon flags:

| Env var | Flag | Default |
| --- | --- | --- |
| `COLLECTOR_PORT` | `--metrics-port` (host bind too) | `9305` |
| `WORKERS` | `--workers` (concurrent collectors) | `4` |
| `PUBDEV_TIMEOUT` | `--pubdev-timeout` (discovery + base fetch) | `60` |
| `FLUTTER_CACHE_DIR` | `--flutter-cache-dir` | `/var/cache/rettulf-pubdb/flutter` |
| `GITHUB_TOKEN` | git HTTPS push credential | — |
| `NO_PUSH=1` | `--no-push` (collect + commit, no push) | off |
| `PACKAGES` | `--packages` (space-separated allowlist) | top1000 worklist |

`WORKERS` parallelises the slow part (pub.dev fetch + Dart analyze); staging,
schema validation, commit, and push stay serialised behind the checkout lock,
so the published history is still linear.

## Disk budget

- **Image:** lean — well under the 3 GB target (no Flutter SDKs baked).
- **`pubdb-cache` volume** (`/root/.cache/rettulf-pubdb`): pub.dev archives +
  the SQLite work queue. Grows with the number of `(package, version)` pairs;
  budget a few GB for the top-1000 worklist.
- **`flutter-cache` volume** (`/var/cache/rettulf-pubdb/flutter`): one full
  Flutter SDK clone (~2 GB) **per** configured Flutter version, downloaded on
  first use. Empty unless you enable the variant pipelines (below). Budget
  ~2.5 GB × number of versions in `db/_flutter_versions.json`.

Inspect usage with `docker system df -v`.

## Backup / restore

The fingerprint **data** is the git repo itself — it's pushed to GitHub, so no
extra backup is needed for entries. Only local operational state lives in
volumes:

- `pubdb-cache` holds `queue.db` (the durable work queue). Losing it just makes
  the next discovery pass re-enqueue outstanding work — safe to discard.
- `flutter-cache` is a pure cache — safe to discard (re-downloaded on demand).

To snapshot/restore the queue anyway:

```sh
docker run --rm -v rettulf-pubdb_pubdb-cache:/data -v "$PWD":/backup ubuntu \
  tar czf /backup/pubdb-cache.tgz -C /data .          # backup
docker run --rm -v rettulf-pubdb_pubdb-cache:/data -v "$PWD":/backup ubuntu \
  tar xzf /backup/pubdb-cache.tgz -C /data            # restore
```

## Flutter-version & obfuscated variants (advanced, opt-in)

The lean image collects the **base** entry and source fingerprint only. The
`.obf.json` (obfuscated-build) and `.flutter-<v>.json` variant pipelines
additionally require a Flutter toolchain (Android SDK/NDK), per-version Flutter
clones, and the **private** `rettulf` CLI — none are baked, to keep the image
small and the SDK caches in volumes.

Until that toolchain is provisioned, discovery will enqueue variant work for
already-collected versions and those items will retry, then dead-letter after
`--max-attempts`. They do **not** block base collection (the queue skips
failed items), but they do show up as failures in the logs. To avoid the noise
on a base-only deployment, leave `db/_flutter_versions.json` empty.

Enabling the variants (Flutter SDK + Android toolchain + a build of `rettulf`
into the image via a build secret) is tracked separately; this deploy targets
base/source collection.

## Bare-Python alternative

To run without Docker (systemd `ExecStart` pointing at a venv), install
`python3.12`, the Dart SDK, and `pip install -r collector/requirements.txt`,
then run `python -m collector.daemon --repo-root <checkout> --metrics-host
0.0.0.0`. The Docker path above is the supported one.
