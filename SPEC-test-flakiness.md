# SPEC: diagnose and fix non-deterministic test failures in the unit suite

**Repo:** `/home/hermes/.hermes/engram-work` (durable clone, branch `feat/remote-asr`).
Do NOT work under `~/.hermes/cache/scratch` — it is pruned.

## The problem

The unit suite does not produce a stable result. The same tree, run twice back to back, gave
**different failures**:

```
run 1: 3 failed, 189 passed  (test_episode_identification.py + test_finalization_coordinator.py)
run 2: 5 failed, 187 passed  (same command, same tree, no edits in between)
```

Earlier a full-suite run reported `4041 passed, 1255 errors`, and the very next full run
reported `2 failed, 5294 passed`. The 1255-error state never reproduced.

The failing tests cluster in two files:

- `tests/unit/test_episode_identification.py::TestPlannedAudioStream`
  (`test_extract_audio_chunk_maps_planned_stream`, `test_extract_audio_chunk_no_map_when_unprobed`)
  — failing with `IndexError: list index out of range` on `calls[0]`, i.e. the mocked
  subprocess was never invoked at all.
- `tests/unit/test_finalization_coordinator.py::TestFinalizeDiscJob`
  (`test_extra_title_routes_to_extras_folder`, `test_multiple_extras_organize_without_false_conflict`,
  `test_extra_organize_failure_keeps_is_extra`).

Each of these passes when its file is run alone. They fail only in combination, and not every time.

## Your task

**Find the cause of the non-determinism and fix it.** Do not paper over it by adding retries,
`@pytest.mark.flaky`, sleeps, or by deleting/skipping tests. A test that passes 60 % of the time
is a broken test, and this suite is the gate for a media pipeline that moves and deletes files.

Work in this order:

1. **Reproduce reliably.** Find a command that fails consistently — try running the two files
   together repeatedly, then widen to the whole `tests/unit/` directory. Record the exact
   command and the failure rate you observe over ~10 runs. If you cannot reproduce it at all,
   say so plainly and stop; do not guess a fix.
2. **Diagnose.** Prime suspects, in order:
   - **Module-level mutable state mutated by one test and read by another.** `encode_hook` has
     `_ENCODING`, `_FAILED`, `_RETRY_PENDING`, and monkeypatched `_DOMAIN_ROOTS`;
     `codec_classifier` caches (`lru_cache`) encoder acceptance; `finalization_coordinator` and
     `episode_identification` may hold caches or module globals. A monkeypatch that is not
     undone, or a `sys.modules` / `lru_cache` entry that survives, is the classic cause.
   - **Threads.** `retry_encode` starts a daemon thread; `_consume_progress` runs a reader
     thread. A thread from an earlier test still running (or still holding `_encoding_lock`,
     or still calling the mocked `subprocess.run`) would corrupt a later test's mock call list —
     which is exactly the `calls[0]` IndexError.
   - **Shared temp paths.** Tests that write to a fixed path (not `tmp_path`) will collide.
   - **Test ordering.** pytest-randomly is not installed (check), so ordering is file/definition
     order; the interaction may be "test A leaves state, test B in another file reads it".
3. **Fix the cause**, then re-run the original reproduction command ~10 times to show a stable
   result.

## Constraints

- Prefer fixing the *test isolation* over changing production behaviour, UNLESS the diagnosis
  shows a genuine production bug (e.g. a thread that is never joined, or a lock not released on
  an error path). If it is a production bug, fix it and say so explicitly in your report.
- Do not modify `encode_hook.py`'s domain gate (`_DOMAIN_ROOTS`, `domain_roots`, `in_domain`) —
  that policy is deliberate and newly committed in `6b278c9`.
- `backend/app/core/organizer.py` has an UNCOMMITTED change in the working tree: a
  `conflict_resolution` parameter for `organize_tv_extras`. It is not yours and it is not
  committed. Leave it as you found it — do not commit it, do not revert it, do not build on it.
- Two earlier commits (`af64311` tests, `6b278c9` domain gate) are already in place; build on them.

## Deliverables

1. A short written **diagnosis**: what the shared state or race is, with the evidence that
   proves it (log lines, an instrumented run, the specific test that leaks state).
2. The fix, as a commit. Message form:
   ```
   test(isolation): <what leaked> no longer crosses test boundaries

   <one paragraph: the mechanism, the evidence, and why the fix addresses the cause.>
   ```
3. **Proof of stability**: the same command run 10 times, all green, with the command and the
   pass counts in your report.
4. If the flakiness is a genuine production bug rather than test isolation, a separate commit
   or a clearly marked section of the same one — and say so loudly in the report.

Do not push. Report the diagnosis before the fix summary — the diagnosis is the valuable part.
