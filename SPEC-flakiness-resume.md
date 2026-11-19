# SPEC: verify and finish the test-flakiness fix (resume)

**Repo:** `/home/hermes/.hermes/engram-work` (branch `feat/remote-asr`). Do NOT work under
`~/.hermes/cache/scratch` — it is pruned.

## Context: this is a RESUME, not a fresh start

A previous agent already diagnosed this and wrote the fix. Its process was killed mid-run by a
container restart, so **nothing is committed and nothing is verified**. Your job is to check its
work, not to redo the investigation from scratch.

### The diagnosis already on disk

An autouse fixture exists in `backend/tests/conftest.py` (uncommitted — `git diff` it). Its
docstring states the mechanism, which you must verify rather than trust:

> The matchers keep audio chunks in a fixed directory under the system temp dir
> (`EpisodeMatcher.temp_dir` = `<tmp>/whisper_chunks`) and skip ffmpeg when the chunk file
> already exists. Tests that call `extract_audio_chunk` directly write a fake 2 KB wav there,
> and nothing deletes it, so the file outlived the test and the run. The chunk name hashes the
> source path, which sits under `pytest-of-<user>/pytest-<N>/`; once a `pytest-<N>` number came
> round again, the old chunk was found, ffmpeg never ran and the test's `calls[0]` raised
> IndexError.

That is consistent with the observed symptom:

```
run 1: 3 failed, 189 passed
run 2: 5 failed, 187 passed   (identical command, identical tree, no edits)
```
and with the failures being `IndexError: list index out of range` on `calls[0]` in
`tests/unit/test_episode_identification.py::TestPlannedAudioStream`.

## Step 1 — Verify the diagnosis is TRUE

Do not skip this. A plausible docstring is not proof.

1. **Confirm the leak mechanism** by inspecting the code: find where the matcher builds its
   chunk path (look for `whisper_chunks`, `temp_dir`, `tempfile.gettempdir`, and the
   "skip ffmpeg if the chunk exists" branch) and where a test writes a fake wav into it.
   Quote the lines.
2. **Prove the stale file accumulates.** Run the affected test file twice with a *fixed*
   basetemp number (e.g. `--basetemp=/tmp/pt-fixed`) and look for leftover chunk files under
   `tempfile.gettempdir()/whisper_chunks/`. If the mechanism is right, the second run finds a
   stale chunk. State what you observed.
3. **Prove the fixture prevents it.** With the conftest change in place, the same fixed-basetemp
   sequence must leave nothing behind and pass both times.

If the diagnosis does NOT hold up, say so plainly, revert the fixture, and pursue the real
cause. Do not force-fit the existing explanation.

## Step 2 — Assess the fix as written

Read the fixture critically:

- It monkeypatches `tempfile.tempdir` per test via a `tmp_path_factory.mktemp("systemp")`
  sandbox, deliberately a sibling of `tmp_path` because some tests assert on `tmp_path`'s exact
  contents. Is that reasoning correct for this suite?
- Is `tempfile.tempdir` the right knob? Production code reads `gettempdir()` at call time —
  confirm that, because a value captured at import would not be affected.
- Does it interact badly with the other autouse fixtures in `conftest.py`, or with
  `pytest-xdist` if that is ever used?
- Is it too broad — does redirecting the whole system temp dir hide other leaks that should be
  fixed at source instead?

If a narrower or more correct fix exists (e.g. the matcher should clean up its own chunk, or
the test should use a unique source path), prefer it — but only if you can argue it from the
code. Fixing the *producer* of the leak in production code is acceptable and better if the
matcher genuinely should not leave permanent temp files around; say clearly which you chose and
why.

## Step 3 — Prove stability

Run the original reproduction command **ten times** and record every result:

```
cd backend && uv run pytest tests/unit/test_episode_identification.py tests/unit/test_finalization_coordinator.py -q
```

All ten must pass. Then run the full suite once:
`uv run pytest tests/unit/ -q` (baseline ~5296 passed, 10 skipped, 1 xfailed, ~6 min).

## Constraints

- Do **not** touch the domain gate in `encode_hook.py` (`_DOMAIN_ROOTS`, `domain_roots`,
  `in_domain`) — committed deliberately in `6b278c9`.
- `backend/app/core/organizer.py` has an uncommitted change (a `conflict_resolution` parameter
  for `organize_tv_extras`) that is **not yours**: do not commit it, revert it, or build on it.
  Leave it exactly as you found it.
- No `@pytest.mark.flaky`, no retries, no sleeps, no skipping or deleting tests to get green.
- There is now 302 GB of scratch at `/mnt/scratch` if you need room. The root disk has ~3.9 GB
  free — do not fill it.

## Commit

One commit for the fix. Message form:

```
test(isolation): stop a stale whisper chunk leaking between test runs

<the mechanism, the evidence that proves it, and why this fix addresses the cause>
```

Keep `organizer.py` out of the commit (`git add` the specific files, never `git add -A`).
Check `git config user.email` is `8040628+mliradelc@users.noreply.github.com`. Do not push.

## Report

Lead with **whether the existing diagnosis was confirmed, corrected, or refuted** — that is the
most valuable part. Then: what the fix is, the ten-run stability table, the full-suite result,
and anything you found that the previous agent missed.
