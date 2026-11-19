# SPEC — finish the upstream sync of the Engram fork (v0.45.0 → v0.45.2)

Repo: `/home/hermes/.hermes/engram-work` (branch `sync/upstream-0.45.2`).
This is Max's fork of `Jsakkos/engram`. **Do not re-clone and do not reset.**

## Context you need

- `origin` is a local recovery bundle (`~/.hermes/engram-recover.bundle`) — ignore it.
- `github` = `https://github.com/mliradelc/engram.git` (the fork; its `main` is upstream-tracking).
- `upstream` = `https://github.com/Jsakkos/engram.git` (added by the orchestrator).
- Merge base was `7aa1090`. Upstream `main` = `3c034d8` (release v0.45.2).
  The fork had **37** commits upstream lacks; upstream has **16** the fork lacks.
  The fork's 37 are all *fork-only feature work* (remote ASR, custom AI provider,
  AV1/encode hook + codec classifier, encode panel) — **none of them are upstream**, so
  none may be dropped.
- A merge is **already in progress** (`MERGE_HEAD` exists, `--no-ff --no-commit`), staged,
  with exactly one conflict: `backend/app/matcher/llm_episode_matcher.py`. The orchestrator
  already resolved both conflict hunks by hand, keeping BOTH sides:
  - import block now includes `LOCAL_PROVIDERS` (upstream) **and** `resolve_ai_base_url` (fork)
  - the `complete_json` call uses upstream's token budget
    `max_tokens=4096 if ai_provider in LOCAL_PROVIDERS else 512`
    **and** the fork's `base_url=resolve_ai_base_url(ai_provider, ai_local_base_url, ai_custom_base_url)`.
  Verify this resolution is right, but do not blindly rewrite it.

## What the fork must keep working (regression guards)

Any resolution must preserve all of these. Grep and confirm each after merging:

1. `resolve_ai_base_url(provider, local, custom)` in `backend/app/core/ai_client.py`, used at
   **every** AI call site (there are several: matcher, validation, identification coordinator).
   Upstream reintroduced bare `ai_local_base_url` in places; that would silently break the
   `custom` provider (a regression this fork already fixed once).
2. `LOCAL_PROVIDERS` / the 4096-token budget for local reasoning models.
3. `backend/app/services/encode_hook.py` (renamed from `encode_hook`), `codec_classifier.py`,
   `ENGRAM_AV1_MAX_CONCURRENT` gate, `ENGRAM_AV1_*` stale-env startup guard.
4. `custom` provider in the AI provider slug list and its settings UI.

## Your task

1. **Confirm the conflict resolution** in `llm_episode_matcher.py` is correct (both intents kept).
   Check every other auto-merged file for a *semantic* conflict the 3-way merge papered over —
   especially `backend/app/api/routes.py`, `backend/app/api/validation.py`,
   `backend/app/core/ai_client.py`, `backend/app/matcher/episode_identification.py`,
   `backend/app/services/identification_coordinator.py`. Upstream changed matching/discdb/
   extractor logic; confirm the fork's custom-provider wiring survived inside the changed functions.
2. **Complete the merge** with a proper message:
   `git commit` using the prepared `MERGE_MSG` plus a short paragraph naming the conflict and
   how it was resolved. Keep the existing git identity (it is already the correct noreply one).
3. **Run the tests.** `cd backend && uv sync --frozen && uv run pytest tests/unit/ -q`.
   Use `-p no:cacheprovider`. The suite is large and slow — run it once in the background and read
   the summary; do not re-run in a loop.
   - For every failure, decide: **caused by this merge** or **pre-existing**?
     Prove it with a stash A/B only if the suite is deterministic; state which you did.
   - Fix failures caused by the merge. Do **not** edit tests merely to make them pass unless the
     test encodes a contract the merge legitimately changed — and say so if you do.
4. **Run the linters** the repo's CI runs: `ruff check .` and `ruff format --check .`
   (format and lint are separate gates). Fix only what this merge broke.
5. **Report.** Finish with:
   - files changed by resolution, and why
   - the exact test summary line you got (passed/failed/skipped), from your own run
   - any failure you judged pre-existing, with the evidence
   - anything in this spec you found to be **wrong about the code** — this is expected; say so plainly.

## Hard constraints

- **Never `git reset --hard`, never force-push, never delete the tag `backup-pre-sync-20261118`
  or branch `sync/base-20261118`.** They are the recovery points.
- **Do not push to GitHub.** The orchestrator decides on publishing after review.
- Do not touch `/opt/engram/src` on CT111 or any container.
- Do not alter `backend/pyproject.toml`'s version; leave release/changelog decisions to the principal.
- Leave the untracked `SPEC-*.md` files in the repo root alone.
