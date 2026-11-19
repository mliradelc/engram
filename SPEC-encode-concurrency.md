# SPEC: Serialise AV1-hook encodes across jobs (encode concurrency gate)

## Problem (measured, not theoretical)

The post-organize AV1 hook (`backend/app/services/encode_hook.py`) runs one encode per
organized title. Within a single job's organize sweep the call is already serial —
`finalization_coordinator.py` does:

```python
if _av1_pending:
    await asyncio.to_thread(
        encode_hook.after_organize, _av1_pending, context=_av1_context(job, disc_title)
    )
    _av1_pending = None
```

and `transcode_to_av1` blocks on `subprocess.run(...)`.

However, **each job's organize sweep is an independent asyncio task**. When several jobs
are advanced (or several discs complete close together), their sweeps run concurrently,
so multiple `ffmpeg` processes encode at the same time. Observed in production on
2026-11-16 on CT111: **four simultaneous ffmpeg encodes** (2x libsvtav1, 2x libx265)
on a host with an **Intel i3-7100T (2 cores / 4 threads)**. Consequences measured:

- container cgroup hit 4.27 GB of a 4 GB limit with 1.07 GB swapped out
- the FastAPI process went into uninterruptible D-state; the HTTP API stopped answering
  (`pct exec` and every API probe timed out)
- two of the four encodes wrote **0 bytes in four hours** (CPU starvation)
- host load average reached 52

## Goal

Add a **process-wide, codec-agnostic encode concurrency gate** so that at most
`N` (default 1) `ffmpeg` encode processes run at once across the entire Engram
process, regardless of which job, which class, or which codec (AV1 or x265) an
encode uses.

## Required behaviour

1. **New env var `ENGRAM_AV1_MAX_CONCURRENT`**, default `"1"`.
   - `1` = strictly serial (the intended default for this host).
   - `0` or a negative/non-integer value = **unlimited** (current behaviour), for
     operators on bigger hardware. Parse defensively; never raise.
   - `N > 1` = at most N concurrent encodes.

2. **The gate must wrap the encode subprocess**, not the whole `after_organize` call.
   Probing/classification work (`_probe`, `_classify`, idet, grain sampling) should
   NOT hold a permit — it is short and must not serialise unnecessarily. The permit
   should be held for the duration of the `ffmpeg` encode plus the verification probe
   of its output, and released before publishing/renaming.

3. **Codec-agnostic by construction.** The gate sits above the codec classifier, so
   it applies identically to `libsvtav1` and `libx265` (and any future encoder).
   Do not branch on `plan.codec`.

4. **Blocking, not dropping.** A caller that cannot get a permit waits. It must NOT
   skip the encode, and must NOT fail the title. `after_organize` already runs inside
   `asyncio.to_thread`, so a blocking `threading.Semaphore` is correct here — do not
   introduce asyncio primitives into a synchronous function.

5. **Log when queued.** If a permit is not immediately available, log at INFO with the
   source filename and the number of encodes already in flight, e.g.
   `AV1 hook: waiting for encode slot (1/1 busy) for <name>`. This is the operator's
   only signal that the gate is working.

6. **Timeout interaction.** `ENCODE_TIMEOUT_SECONDS` currently bounds the
   `subprocess.run` call. Time spent waiting for a permit must NOT count against that
   timeout (a file that waited 3 hours for a slot should still get its full encode
   budget). Acquire the permit first, then start the subprocess with its own timeout.

7. **No behaviour change when the hook is disabled** (`ENGRAM_AV1_HOOK != "1"`).

## Files allowed to change

- `backend/app/services/encode_hook.py` — the gate, env parsing, logging, docstring
- `backend/tests/unit/` — tests for the gate. Create
  `backend/tests/unit/test_encode_hook_concurrency.py`, following the conventions in the
  existing sibling `backend/tests/unit/test_codec_classifier.py` (same import style,
  same fixtures, same pytest idioms).
- `CODEC-CLASSIFIER.md` — add `ENGRAM_AV1_MAX_CONCURRENT` to the configuration table

**Do not change** `finalization_coordinator.py`, `organizer.py`, or the classifier's
threshold logic. The gate belongs entirely inside the hook.

## Constraints

- Python 3.13, ffmpeg 5.1.9 on Debian 12. No new third-party dependencies —
  `threading` from stdlib only.
- The existing fail-open behaviour must be preserved: any error in the hook still
  leaves the file untouched and the job unaffected.
- Keep the existing `_cfg()` helper convention for reading env vars.
- Match the file's existing style: module-level constants, typed signatures,
  docstrings explaining *why* (this codebase documents rationale heavily).

## Tests required

1. With `ENGRAM_AV1_MAX_CONCURRENT=1`, two concurrent calls to the encode path never
   have overlapping subprocess executions. Prove this with a fake/stubbed
   `subprocess.run` that records enter/exit timestamps and asserts no overlap.
2. With `ENGRAM_AV1_MAX_CONCURRENT=0`, concurrency is unlimited (overlap is observed).
3. An invalid value (`"abc"`, `"-5"`) falls back to unlimited and does not raise.
4. The permit is released when the encode fails (non-zero returncode) and when it
   raises `subprocess.TimeoutExpired` — i.e. no permit leak that would deadlock the
   process.
5. Waiting time does not consume the encode timeout budget.

## Verification before you report done

- `python -m pytest backend/tests/unit/ -k "av1 or codec or concurrency" -q` passes
- `python -c "import ast,sys; ast.parse(open('backend/app/services/encode_hook.py').read())"`
- `git diff --stat` shows only the allowed files
- Report the exact test command and its output; do not summarise as "tests pass"
  without pasting the result line.

## Commit

One commit, message style matching the repo's conventional commits, e.g.:

```
feat(av1-hook): serialise encodes with ENGRAM_AV1_MAX_CONCURRENT gate

Multiple concurrent job sweeps could run several ffmpeg encodes at once,
starving a 2-core host and taking the API down. Add a process-wide
semaphore (default 1) wrapping the encode subprocess, codec-agnostic.
```
