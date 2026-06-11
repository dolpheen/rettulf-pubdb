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

## The consumer contract (verified against rettulf `stage_b/packages/`)

The peer repo is at `../rettulf`. The DB is matched in
`src/rettulf/stage_b/packages/`; two functions define what the collector must
emit — read them before changing surface output:

- `api_surface.package_api_surfaces()` — extracts the surface from the **AOT
  snapshot** (the match *target*). Its output is the vocabulary the DB mirrors.
- `api_surface.normalize_surface()` — run on **both** the DB entry and the
  snapshot before comparison; `pubdev_match.match_surface()` then scores.

`normalize_surface` rewrites every surface (DB included): strips `get:`/`set:`
prefixes → bare name; strips `@<n>` suffixes and a leading `new `; drops `#…`,
closures, `<anonymous closure>`; drops the 10 builtin type names (`Object Never
Null bool double dynamic int num String void`); and **drops any `types` entry
that is not the name of another class in the same surface** (sibling-class
filter in `_serializable_surface`).

`match_surface` is a **subset test over the snapshot (target)**: every snapshot
token must be present in the DB candidate (methods matched against candidate
methods+fields; fields/types by set difference). DB **extras never cause a
miss**, but they inflate `extra_token_count`, the tiebreaker
`best_surface_match` uses to choose among subset-matching versions (fewest
extras wins) and a ranking term for low-confidence matches.

**Rule for the collector:** emit only **bare names the snapshot also produces** —
plain method / constructor / top-level-function names, plain field names, enum
values, bare sibling-class names in `types`; use `"::"` for top-level (the
consumer's `SYNTHETIC_CLASS`). Decorated tokens (`sig:…`, `kind:…`, `arity:…`,
`extends:/implements:/mixes:/on:…`, `typedef:…`, `annotation:…`, `export:…`) are
**not** in the snapshot vocabulary: the `types`-bucket ones are silently dropped
by the sibling filter; the `methods`/`fields`-bucket ones (`sig:…`, `typedef:…`)
pass through and skew `extra_token_count` → worse version disambiguation. (This
is the verified basis for the PR #11 review.)

## Layout

- `schema/` — JSON Schema (`_schema.v1.json`) + `examples/`.
- `db/` — `<package>/<version>.json` entries; `_index.json` (consumer fetches
  first), `_top1000.json` (collection worklist).
- `collector/` — the 24/7 collection pipeline, implemented and deployed:
  `daemon.py` (worker pool + SQLite work queue + batched publish),
  `pubdev_client.py` (archive fetch + on-disk cache), `pipelines/`
  (`api_surface` / `obfuscated_build` / `flutter_variant`).
- `scripts/` — `validate.py` (entry validator) + `requirements.txt`.
- `ops/` — collector deployment: `docker/` (`Dockerfile.collector` +
  `docker-compose.yml`), `systemd/` unit, `env.example`. Live in production —
  see **Production collector** below.

## Conventions

- Entries are **machine-generated** by the collector; hand-edit only to fix a
  schema problem. Real fingerprint data comes from the pipeline, not by hand.
- **Branch + PR** for changes so CI validates them; don't push entry changes
  straight to `main` unvalidated.
- Use `git -C <dir> ...` instead of `cd <dir> && git ...` for any non-cwd repo.
- Never commit collector working data / archives (gitignored).

## Production collector (deployment & runtime)

The 24/7 collector runs on a dedicated host: `ssh -p1122
v392persei@rettulf.v392persei.ru`.

- **systemd unit** `rettulf-pubdb-collector`
  (`WorkingDirectory=/opt/rettulf-pubdb/ops/docker`, `ExecStart=docker compose
  up --build`, `COMPOSE_PROFILES=proxy`). Two compose services: `collector`
  (daemon, `/metrics` on `127.0.0.1:9305`) + `proxy` (Caddy TLS/basic-auth
  dashboard, profile-gated). Logs: `journalctl -u rettulf-pubdb-collector` or
  `docker logs rettulf-pubdb-collector-1`.
- **The repo is bind-mounted `/opt/rettulf-pubdb → /app` (rw).** The daemon
  commits and pushes entries to `main` *from that host checkout*; the image is
  only the runtime (Python + Dart). Code / `.gitignore` changes take effect
  through the bind mount, and `Dockerfile.collector` copies only `collector/
  scripts/ schema/ db/` (not `.gitignore`), so the `--build` on restart is a
  near-total cache hit.
- **Redeploy / update** (stop first — otherwise you race the daemon's git ops on
  the live checkout): `sudo systemctl stop rettulf-pubdb-collector` → `sudo git
  -C /opt/rettulf-pubdb fetch origin && sudo git -C /opt/rettulf-pubdb reset
  --hard origin/main` → `sudo systemctl start rettulf-pubdb-collector`.
  Scope/cadence knobs live in `/opt/rettulf-pubdb/ops/docker/.env` (root-only);
  edit then restart.
- **Volumes** (`/var/lib/docker/volumes/`): `pubdb-cache` →
  `/root/.cache/rettulf-pubdb` holds the **durable SQLite work queue `queue.db`**
  (precious, ~5 MB — survives restarts so discovery resumes) **and** `archives/`
  (extracted pub.dev sources). `archives/` is a re-download cache keyed by
  `(package, version)` with **no eviction**, so it grows unbounded (reached
  ~28 GB / 10k entries). Safe to prune for disk: only `queue.db` must persist,
  and discovery skips already-collected packages so pruning their archives
  triggers no re-download. `flutter-cache` is empty in `--base-only` mode. Disk
  is 38 GB — `archives/` dominates; watch for fill.

**Invariant — never let any `db/` path be gitignored.** The daemon publishes via
one batched `git add -- <files>`; a single ignored path makes git exit non-zero
and the *whole* batch fails to commit, wedging collection silently. `.gitignore`
carries `!db/*/` so build-output patterns (`build/`, `dist/`, …) can't swallow a
same-named package (e.g. the `build` package); verify with `git check-ignore
db/<pkg>/x.json` (must print nothing). Incident 2026-06-11: the `build` package
stalled commits ~11 h.

*Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" -> "Write tests for invalid inputs, then make them pass"
- "Fix the bug" -> "Write a test that reproduces it, then make it pass"
- "Refactor X" -> "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] -> verify: [check]
2. [Step] -> verify: [check]
3. [Step] -> verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

## 5. Issue Implementation Workflow

**No coding on main branch. Use git worktree**

**When asked to implement a GitHub issue or feature: always work in an isolated worktree.**

When the user pastes a GitHub issue URL, number, or description and asks to implement it:

1. If given an issue number/URL, fetch it: `gh issue view <number>`
2. Use the `Agent` tool with `isolation: "worktree"` to do ALL implementation work
3. The agent should: read relevant code, implement the change, run tests, then create a PR
4. Include a GitHub closing keyword in the PR body, e.g. `Closes #<number>`, so merging the PR closes the issue automatically
5. If the PR is validated and ready for review, mark it ready for review instead of leaving it as a draft
6. Report the PR URL when done

Never implement features directly on `main`. Always use a worktree agent for isolation.

## 6. Git Across Directories

**Use `git -C <dir>` instead of `cd <dir> && git ...`.**

Chaining `cd` before a git command triggers a permission prompt because hooks in the target repo (pre-commit, post-checkout, etc.) can run with shell permissions. `git -C <dir> <command>` runs git as if from that directory without changing the shell's cwd — same result, no prompt.

Applies to worktrees, submodules, or any non-cwd repo.
