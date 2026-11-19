# SPEC: complete the ENGRAM_AV1_* → ENGRAM_ENCODE_* rename, and clarify the release workflows

## Background

Commit `9cd51e3` renamed `av1_hook.py` → `encode_hook.py` and replaced every
`ENGRAM_AV1_*` env var with `ENGRAM_ENCODE_*` **in the code**. The code side is
verified correct: in the deployed `0.45.0-fork.5` container the module reads
`ENGRAM_ENCODE_HOOK`, `ENGRAM_ENCODE_STAGING`, `ENGRAM_ENCODE_CRF`,
`ENGRAM_ENCODE_PRESET`, `ENGRAM_ENCODE_THREADS`, `ENGRAM_ENCODE_MAX_CONCURRENT`.

**But CT111's `/opt/engram/.env` still defines the six OLD names.** Because the
hook defaults to OFF (`_cfg("ENGRAM_ENCODE_HOOK", "0") == "1"`), the deployed
container is silently running with **encoding disabled**. That is a live fault,
not a theoretical one:

```
$ docker exec engram env | grep ENGRAM
ENGRAM_AV1_HOOK=1
ENGRAM_AV1_STAGING=/media/_staging/engram_av1
ENGRAM_AV1_CRF=22
ENGRAM_AV1_PRESET=6
ENGRAM_AV1_THREADS=4
ENGRAM_AV1_MAX_CONCURRENT=1
```

There is no compose `environment:` block for these — `/opt/engram/.env` is the
single source of truth. So the fix is a rename in that one file, and a guard so
this class of drift is caught in future.

The reason `9cd51e3` missed it: the deployed `.env` lives on CT111, outside the
repo, and the commit's exhaustive grep only covered tracked files. The code sweep
was right; the *deployment* surface was never in scope.

## Part A — make the rename complete and self-defending (repo change)

1. **Fail loudly on the old names.** In `backend/app/services/encode_hook.py`,
   add a startup check (called where the hook initialises, not at import time so
   tests stay fast) that scans `os.environ` for any key matching `ENGRAM_AV1_`.
   If any is found: log an ERROR naming each stale key, and raise a clear
   exception rather than silently running disabled. Wording should say the
   variable was renamed and name the replacement (`ENGRAM_AV1_HOOK` →
   `ENGRAM_ENCODE_HOOK`, etc.), so a human reading the log knows exactly what to
   edit. A stale `ENGRAM_AV1_HOOK=1` next to a missing `ENGRAM_ENCODE_HOOK` is
   precisely the silent-disable case; refuse to start rather than lie about it.

2. **Ship a migration note + example.** Add the six renamed variables to
   `.env.example` (or the equivalent template the repo uses) under their NEW
   names, and add a short section to `CODEC-CLASSIFIER.md` titled
   "Renaming ENGRAM_AV1_* to ENGRAM_ENCODE_*" giving the one-line `sed` a
   deployer runs against their `.env`. It must also state that skipping it
   disables encoding.

3. **Tests.** In `backend/tests/unit/test_encode_hook_*.py`:
   - a test that the guard raises when a stale `ENGRAM_AV1_*` key is present,
     and that the error text names the replacement;
   - a test that no stale `ENGRAM_AV1_*` key means no error;
   - a test asserting `_cfg` reads `ENGRAM_ENCODE_HOOK` (not the old name) and
     that "1" enables while unset/"0"/anything else disables.

## Part B — clarify the two release workflows (repo change)

Fault: `release.yml` and `docker.yml` both trigger on `push: tags ["v*"]` and on
`workflow_dispatch`, they have **no `concurrency` block**, and their names
(`Release`, `Docker`) do not say what each produces. On tag `v0.45.0-fork.5`
this produced two visually identical runs, and cancelling the wrong one left the
image unpublished while the binaries succeeded. That cost a deploy cycle.

Make each workflow self-describing and self-cancelling:

1. **Rename** so the UI says what the artefact is:
   - `release.yml`: `name: Release Binaries (Windows / macOS / Linux)`
   - `docker.yml`: `name: Release Container Image (GHCR)`
   Do not rename the files themselves — other workflows reference them by
   filename (`tag-release.yml` dispatches `release.yml` and `docker.yml`).

2. **Add a `run-name`** to each so the run title carries the artefact and tag,
   e.g. `Container image ${{ inputs.tag || github.ref_name }} → GHCR` and
   `Binaries ${{ inputs.tag || github.ref_name }}`.

3. **Add `concurrency` to each**, keyed on the workflow name plus the tag, with
   `cancel-in-progress: true`. Critically the groups must be DIFFERENT between
   the two workflows, so cancelling a duplicate container build can never cancel
   a binaries build:
   ```yaml
   concurrency:
     group: docker-image-${{ inputs.tag || github.ref_name }}
     cancel-in-progress: true
   ```
   and for release:
   ```yaml
   concurrency:
     group: release-binaries-${{ inputs.tag || github.ref_name }}
     cancel-in-progress: true
   ```

4. Leave the triggers alone — do not remove `push: tags` or `workflow_dispatch`.

## Constraints

- Specific files only. **Never `git add -A`.** Do not commit any `SPEC-*.md`.
- Do not touch CT111, do not push, do not tag, do not run `docker`.
- Do not rename `MARKER_SUFFIX` (`.engram-av1.json`) or its `"av1"` JSON key —
  `test_marker_suffix_is_frozen` guards it and must keep passing.
- Baseline: backend **5501 passed, 10 skipped, 1 xfailed**; frontend 842 passed;
  ruff clean. Do not regress any of them.
- Verify both YAML files still parse (`python -c "import yaml,sys;
  [yaml.safe_load(open(f)) for f in sys.argv[1:]]" ...`).

## Report

Files changed, test counts (before → after), the exact error message the
`ENGRAM_AV1_*` guard produces, confirmation both YAML files parse, and anything
in this spec you found wrong.
