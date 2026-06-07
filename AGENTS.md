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
4. Report the PR URL when done

Never implement features directly on `main`. Always use a worktree agent for isolation.

## 6. Git Across Directories

**Use `git -C <dir>` instead of `cd <dir> && git ...`.**

Chaining `cd` before a git command triggers a permission prompt because hooks in the target repo (pre-commit, post-checkout, etc.) can run with shell permissions. `git -C <dir> <command>` runs git as if from that directory without changing the shell's cwd — same result, no prompt.

Applies to worktrees, submodules, or any non-cwd repo.


