# SPEC: rename the output directory av1-out -> encode-out, and migrate existing output

Repo `/home/hermes/.hermes/engram-work`, branch `feat/remote-asr`, HEAD `994e49d`.
Backends: **5499 passed / 10 skipped / 1 xfailed**, frontend **842 passed**.

The principal: *"av1-out is a bad name, should it be encode-out"* — yes. The hook writes x265 for
grainy content, so "av1" misnames the directory exactly as it misnamed the module.

## Current state (verified on CT111, do not re-derive)

```
/staging/av1-out                       831 MB, files dated 2026-11-13/14
                                        (S10E01.av1.mkv, 2 Extras, av1-measurement.json)
/opt/engram/.env             written   2026-11-16 13:58  → ENGRAM_AV1_STAGING=/media/_staging/engram_av1
/media/_staging/engram_av1  (mounted)  empty, 0 bytes
```

The on-disk files **predate** the NAS reconfiguration, so they are stale leftovers from before the
repoint — the setting is honoured. Nothing is actively writing to `/staging/av1-out`.

## Part A — code

1. **Default path:** change the `ENGRAM_ENCODE_STAGING` default from `/staging/av1-out` to
   `/staging/encode-out`. This is the fallback when the variable is unset; the live container sets
   it explicitly, so this changes no live behaviour — but it must not point at the old name.
2. **Remove the "historical" comment** that says the directory name is stale; it will no longer be.
3. Sweep the repo for the remaining `av1-out` occurrences — docstrings, `CODEC-CLASSIFIER.md`, any
   test fixtures and the compose comment — and update them. Keep the `.engram-av1.json` **marker
   suffix** unchanged: renaming it would make every already-encoded file look unconverted and
   trigger a re-encode of the whole library. Add a comment saying why it stays.
4. Config/env variable names stay `ENGRAM_ENCODE_*` — no change there.

## Part B — migration runbook (document, do not execute)

Add a short section to `CODEC-CLASSIFIER.md` (or a `docs/` note) describing how to retire
`/staging/av1-out`:

- The directory is **inside the container's 24 GB rootfs**, not on the NAS mount.
- Two encoders' worth of output is stale there; the NAS path is the live target.
- Migration is a **manual, human-run** step with the hook idle: verify
  `GET /api/encoding` shows `active: []`, stop the container, move any file worth keeping with
  `docker exec engram mv`, then `rmdir`/`rm` the old directory, and start again.
- **Do not automate this into the app.** A startup routine that deletes a directory because its name
  changed is a data-loss bug waiting to happen.
- State the expected reclaim (~831 MB) so the operator can confirm it.

## Tests

- The default staging path is `/staging/encode-out`, and no code path emits `av1-out`.
- A test asserting the marker suffix is still `.engram-av1.json` (guard against a well-meaning
  future rename).
- `grep -rn "av1-out" backend/ frontend/ *.md` returns only the migration note explaining the old
  name historically, if you choose to mention it there.

## Verify

```
cd backend && uv run pytest tests/unit/ -q      # >= 5499 passed
cd frontend && npm run test:unit && npm run lint && npm run build
```

One commit. Specific files, never `git add -A`. Do not push, tag, touch CT111, or delete anything on
CT111 — the migration runbook is documentation the parent agent executes, not you.

## Report

Files changed, test counts, the final grep output for `av1-out`, and anything in this spec you found
wrong.
