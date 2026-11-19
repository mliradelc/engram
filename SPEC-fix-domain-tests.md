# SPEC: fix the seven tests broken by the Engram domain gate

**Repo:** `/home/hermes/.hermes/engram-work` (durable clone, branch `feat/remote-asr`).
Do NOT work under `~/.hermes/cache/scratch` — it is pruned and will delete your work.

## Context

`backend/app/services/encode_hook.py` in the working tree carries an uncommitted change that
restricts the encode hook to **Engram's own domain**: `/media/_staging` plus
`ENGRAM_AV1_STAGING` (`_DOMAIN_ROOTS`, `domain_roots()`, `in_domain()`). `/media/tv` and
`/media/movies` are deliberately excluded — the principal's rule is that Engram may only
touch the rips it produces and the encodes of those rips, never the library.

That change removed `_MEDIA_ROOTS` and made `after_organize()` and `retry_encode()` refuse
any path outside the domain. Seven existing tests still assert the OLD policy and now fail:

```
FAILED tests/unit/test_encode_hook_progress.py::test_after_organize_forwards_loop
FAILED tests/unit/test_encode_hook_progress.py::test_retry_deletes_marker_and_starts_an_encode
FAILED tests/unit/test_encode_hook_progress.py::test_retry_goes_through_the_shared_gate_and_registry
FAILED tests/unit/test_encode_hook_progress.py::test_retry_moves_the_row_out_of_failed
FAILED tests/unit/test_encode_hook_progress.py::test_second_retry_refused_while_the_first_is_queued
FAILED tests/unit/test_encode_hook_progress.py::test_default_retry_roots_skip_empty_and_add_staging_parent
FAILED tests/unit/test_encode_hook_progress.py::test_default_retry_roots_include_real_media_mounts
```

Run them yourself to see the current failures before changing anything:
`cd backend && uv run pytest tests/unit/test_encode_hook_progress.py -q`

## The task

Rewrite those seven tests so they assert the NEW domain policy. **Do not weaken the
production code to make tests pass.** The domain gate is deliberate and stays.

Guidance per failure class:

1. **`test_default_retry_roots_include_real_media_mounts`** — reads `encode_hook._MEDIA_ROOTS`,
   which no longer exists. Delete it or replace it with a test of `domain_roots()`: the roots
   are `/media/_staging` and `ENGRAM_AV1_STAGING`, and they must NOT include `/media/tv` or
   `/media/movies`.
2. **`test_default_retry_roots_skip_empty_and_add_staging_parent`** — asserts
   `Path("/media/tv") in roots`. Rewrite: empty/None library paths are skipped, a library
   path inside `_DOMAIN_ROOTS` is honoured, and one outside it is ignored.
3. **The five `after_organize` / `retry` tests** — they build files under `tmp_path` and
   expect the hook to encode them. They now fail because `tmp_path` is outside the domain.
   Fix by making the domain root point at the test fixture: monkeypatch
   `encode_hook._DOMAIN_ROOTS` to `(str(tmp_path),)` (and/or `ENGRAM_AV1_STAGING`), so the
   fixture files are legitimately inside Engram's domain. Keep each test's original
   assertion — the behaviour under test (loop forwarding, marker deletion, shared gate and
   registry, failure-row removal, double-retry refusal) is still correct and must still be
   proven.

Also check the other fixtures in the same file (`hook_env`, `retry_env` or whatever they are
called) and any other test file that constructs paths the hook now refuses —
`tests/unit/test_encode_progress_api.py` has a `retry_env` fixture using `tmp/tv` that will
now 404. Fix those the same way.

## Add one new test

`test_after_organize_refuses_a_library_path` — a file under `/media/tv` (create the path
under `tmp_path` and monkeypatch `_DOMAIN_ROOTS` so it does NOT include it, or simply point
the domain elsewhere) must: never call `transcode_to_av1`, never write a marker, and log a
refusal. This is the regression test for the bug that motivated the change.

## Constraints

- Only touch test files. Do not modify `encode_hook.py`, `routes.py` or any production code.
- Keep the existing test style in the file (pytest, monkeypatch, the file's own helpers).
- Do not delete a test just because it is inconvenient; replace it with one that asserts the
  new contract, unless the behaviour it covered no longer exists (then say so in your summary).
- Run `cd backend && uv run pytest tests/unit/test_encode_hook_progress.py tests/unit/test_encode_progress_api.py -q`
  and then the full `uv run pytest tests/unit/ -q` (baseline ~5260 passed, 10 skipped,
  1 xfailed, ~6 min). Everything must pass before you commit.

## Commit

One commit, message:

```
test(av1-hook): assert the staging-only domain in the retry and after_organize tests

The hook now refuses any path outside /media/_staging and ENGRAM_AV1_STAGING, so the
tests that built fixtures under tmp_path and expected an encode no longer described
the contract. Point the domain root at the fixture instead of widening the gate, and
add a regression test that a library path is refused outright.
```

Check `git config user.email` in the repo first — it must be
`8040628+mliradelc@users.noreply.github.com` (a repo-local override to a bare
`mliradel@users.noreply.github.com` may be present; the `+id` form is the safe one).
Do not push.

## Report

In your final answer: which tests you rewrote and how, which you replaced and why, the new
test, and the final pass/fail counts from both the targeted and the full run.
