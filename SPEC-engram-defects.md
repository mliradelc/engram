# SPEC: clear the remaining Engram defects

Everything below was measured on `feat/remote-asr` at `6a4da55`. Nothing here is
speculative — each item has a verified symptom from an actual command run.

## Current verified state

```
backend  5516 passed, 10 skipped, 1 xfailed   (clean run, EXIT=0)
frontend 842 passed (80 files)
ruff check          All checks passed
ruff format --check 10 files would be reformatted   ← defect 1
```

## Defect 1 — ten files fail `ruff format --check`

`ruff format --check .` exits 1. Files and first offending line:

```
app/api/validation.py:898
app/core/ai_client.py:91
app/services/codec_classifier.py:280
app/services/encode_hook.py:355
app/services/finalization_coordinator.py:63
tests/unit/test_ai_custom_provider.py:106
tests/unit/test_codec_classifier.py:47
tests/unit/test_encode_hook_progress.py:141
tests/unit/test_encode_override.py:55
tests/unit/test_encode_progress_api.py:120
```

CI's format gate therefore fails on a clean checkout. Run `ruff format` on these
ten files only (not the whole tree — do not reformat the 523 already-clean ones),
then confirm `ruff format --check .` exits 0 and the suite still passes. Commit
formatting separately from any logic change so the diff is reviewable.

## Defect 2 — `docker-compose.yml` in the repo carries the old env names

The deployment on CT111 uses `/opt/engram/docker-compose.yml`, which I have
already fixed by hand to `ENGRAM_ENCODE_*`. But check whether the repo ships its
own compose file (root `docker-compose.yml`, `deploy/`, or `docker/`). If it does
and it still maps `ENGRAM_AV1_*`, that is a second copy which will reintroduce
the exact silent-disable bug on the next fresh deployment. Rename its keys and
its default staging path to match the deployment
(`ENGRAM_ENCODE_*`, default `/media/_staging/engram_av1` where the NAS path is
intended, otherwise leave the code default alone). If no such file exists in the
repo, report that plainly and change nothing.

## Defect 3 — comments still describe the pre-rename world

`/opt/engram/docker-compose.yml` on CT111 still says
`# Post-organize AV1 hook (backend/app/services/encode_hook.py)` — a file that no
longer exists. Check the repo's own compose/docs for equivalent stale
references (`encode_hook.py`, "AV1 hook" where the hook is meant, rather than AV1 as
a codec). Fix comment/docstring references only. **Do not touch**
`MARKER_SUFFIX = ".engram-av1.json"`, its `"av1"` JSON key, `libsvtav1`,
`svtav1-params`, or AV1 as a codec name — `test_marker_suffix_is_frozen` guards
the marker and must keep passing.

## Defect 4 — verify the startup guard cannot brick a fresh install

`main.py` now calls `check_stale_env()` before the database is set up. Confirm by
test that a **clean** environment (no `ENGRAM_AV1_*` and no `ENGRAM_ENCODE_*`
either) starts normally — i.e. the guard fires only on genuine stale keys and
never on absence. This matters because the guard deliberately aborts startup, and
a false positive would crash-loop a working deployment. If a test for this is
missing, add one.

## Defect 5 — `test_media_probes_classify_real_synthetic_files` is load-flaky

Under a full-suite run this failed once with
`subprocess.CalledProcessError: ffmpeg ... returned non-zero exit status 255`
while generating a synthetic clip; in isolation the same file passes (279 passed)
and a full re-run passed 5516. The same ffmpeg command succeeds by hand.

Do not paper over it with retries or skips. Find the cause. Likely candidates,
check them in this order: a shared scratch-directory collision between parallel
synthetic-media tests (`label='^-^'` / `gradients` fixtures writing to the same
path), an ffmpeg invocation racing another test's temp cleanup, or resource
exhaustion when many encodes run concurrently. Make the fixture path unique per
test (e.g. `tmp_path`) and/or serialise, and add a comment explaining the fix.
The goal is a deterministic suite: it must pass repeatedly, not usually.

## Constraints

- Specific files only. **Never `git add -A`.**
- Do not commit any `SPEC-*.md` file.
- Do not push, do not tag, do not touch CT111, do not run docker.
- Do not rename `MARKER_SUFFIX` or its `"av1"` key.
- Do not regress: backend must stay at or above **5516 passed, 10 skipped,
  1 xfailed**; frontend **842 passed**; `ruff check` clean.
- `ruff format --check .` must exit **0** when you are done.

## Commits

Four commits, each self-contained:
1. `style: apply ruff format to the ten unformatted files`
2. `fix(compose): rename ENGRAM_AV1_* to ENGRAM_ENCODE_*` (only if the repo ships one)
3. `docs: correct stale encode_hook references in comments`
4. `test(encode-hook): guard a clean environment and de-flake the synthetic media probe`

## Report

Files changed per commit, whether a repo-level compose file existed and what you
changed in it, the root cause of the flaky probe, final counts for
`ruff format --check`, `ruff check`, backend suite and frontend suite, and
anything in this spec you found wrong.
