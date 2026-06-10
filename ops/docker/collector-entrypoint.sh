#!/usr/bin/env bash
# Translate the ops env-file variables into `collector.daemon` CLI flags.
#
# The daemon itself only reads CLI flags; this shim is the single place that
# maps env -> flags so the Docker/systemd deploy stays declarative. Any extra
# arguments passed to the container are forwarded verbatim (e.g. `--once`).
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/app}"
CACHE_ROOT="${CACHE_ROOT:-/root/.cache/rettulf-pubdb}"

# Identity + HTTPS push auth for the DB git checkout mounted at $REPO_ROOT.
git config --global --add safe.directory "$REPO_ROOT" || true
git config --global user.name "${GIT_AUTHOR_NAME:-rettulf-pubdb collector}"
git config --global user.email "${GIT_AUTHOR_EMAIL:-collector@users.noreply.github.com}"
if [ -n "${GITHUB_TOKEN:-}" ]; then
  git config --global \
    url."https://x-access-token:${GITHUB_TOKEN}@github.com/".insteadOf \
    "https://github.com/"
fi

args=(
  --repo-root "$REPO_ROOT"
  --cache-root "$CACHE_ROOT"
  --metrics-host "${METRICS_HOST:-0.0.0.0}"
  --metrics-port "${COLLECTOR_PORT:-9305}"
)
[ -n "${WORKERS:-}" ] && args+=( --workers "$WORKERS" )
[ -n "${PUBDEV_TIMEOUT:-}" ] && args+=( --pubdev-timeout "$PUBDEV_TIMEOUT" )
[ -n "${BATCH_SIZE:-}" ] && args+=( --batch-size "$BATCH_SIZE" )
[ -n "${PUSH_INTERVAL:-}" ] && args+=( --push-interval "$PUSH_INTERVAL" )
[ -n "${FLUTTER_CACHE_DIR:-}" ] && args+=( --flutter-cache-dir "$FLUTTER_CACHE_DIR" )
# shellcheck disable=SC2086  # PACKAGES is intentionally word-split into a list.
[ -n "${PACKAGES:-}" ] && args+=( --packages ${PACKAGES} )
[ "${NO_PUSH:-0}" = "1" ] && args+=( --no-push )
[ "${DASHBOARD:-1}" = "0" ] && args+=( --no-dashboard )
[ "${BASE_ONLY:-0}" = "1" ] && args+=( --base-only )

exec python3 -m collector.daemon "${args[@]}" "$@"
