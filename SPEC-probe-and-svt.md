# SPEC: two production defects in the encode pipeline

Both were found by the previous investigation and each was confirmed present in
the code at `4a0d54f`. Neither is cosmetic; both can produce a wrong output file
without any visible error.

## Defect A — a killed or failed probe is treated as "the source says nothing"

`codec_classifier.py:945`:

```python
def _run(cmd, timeout=PROBE_TIMEOUT):
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (subprocess.SubprocessError, OSError):
        return None
```

Every failure mode collapses to `None`: a timeout, an OSError, a missing binary,
and — importantly — a process killed by a signal. Callers then treat `None` as
"probe produced no evidence" and fall through to a default verdict.

Measured consequence: when the `idet` probe is killed, the classifier does not
report a failure. It picks a *different class*. In the observed case a pattern
source came out as `live_action_clean`, which then selects the wrong encode
settings. The file is encoded, the job reports success, and the error is silent.

This also means `subprocess.run` returning a `CompletedProcess` with a non-zero
returncode is handled by callers, but a *signal death* (returncode negative) is
not distinguished from a clean failure.

**Required:**

1. `_run` must distinguish three outcomes: success, a probe that ran and failed
   with a non-zero status, and a probe that could not run or was killed. A
   signal death (`returncode < 0`) is the third kind — it is not evidence about
   the source.
2. Where a probe is *required* to decide correctness (the `idet` interlace /
   telecine verdict is the one that matters — it decides `bwdif` vs
   `fieldmatch` vs nothing), a missing verdict must not silently degrade to a
   default. Either raise so the encode fails visibly, or return an explicit
   "undecided" that the caller handles as a conservative default AND logs at
   WARNING. Choose one, state which, and justify it — do not leave it implicit.
3. Do not change the behaviour when the probe genuinely succeeds and merely
   reports "progressive" or "no repeated fields". Those are real answers.
4. Tests: a probe killed by SIGTERM; a probe that times out; a missing binary.
   For each, assert the classifier does not silently return a confident verdict.
   Use a stub/fake command rather than trying to kill real ffmpeg.

## Defect B — SVT-AV1 silently ignores three settings on this host

`codec_classifier.py:393-399` builds `svt_optional` including:

```
("tf-strength", "1")
("sharpness", "1")
("luminance-qp-bias", "25")
```

On the deployed host, SVT-AV1 rejects these: `Error parsing option
tf-strength/sharpness/luminance-qp-bias`. The encode still succeeds — AV1 output
is produced — but those settings have no effect. That is worse than a failure:
the code believes it applied tuning that never happened.

**Required:**

1. Establish the truth before changing anything: run a tiny encode with
   `-svtav1-params` including each of the three on the host and record whether
   it parses. Report the exact error text. Do not assume the previous finding is
   correct — verify it.
2. If they are genuinely rejected, stop emitting them. Prefer a mechanism that
   cannot rot: probe once for support (or accept a conservative known-good set)
   rather than hard-coding a list that silently does nothing. If a supported
   equivalent exists for the same intent (keyframe blocking, sharpness,
   luminance bias), use it and say what replaced what.
3. If some are supported and others are not, keep the supported ones and drop
   only the rejected ones, with a comment naming the SVT version tested.
4. Tests: assert `video_args()` does not emit any parameter the host rejects,
   and that the emitted `-svtav1-params` string is stable and parseable.

## Constraints

- Specific files only. **Never `git add -A`.** Do not commit `SPEC-*.md`.
- Do not push, do not tag, do not touch CT111, do not `docker`.
- Do not rename `MARKER_SUFFIX` or its `"av1"` key.
- Do not regress: `pytest tests/unit` must stay at or above **5525 passed,
  10 skipped, 1 xfailed**; frontend 842; `ruff check` clean; `ruff format
  --check .` must exit 0.
- Do **not** run the suite with the default temp dir. `TMPDIR` points at
  `~/.hermes/cache/scratch`, where a host-side guard SIGTERMs any ffmpeg when
  free disk is under 3 GiB — it is currently active and will kill test ffmpeg
  processes and produce phantom failures. Run with an explicit basetemp outside
  scratch, e.g. `uv run pytest tests/unit -q --basetemp=/tmp/pytest-engram`.

## Commits

Two commits:
1. `fix(codec-classifier): do not treat a killed probe as evidence`
2. `fix(codec-classifier): stop emitting SVT-AV1 options the host rejects`

## Report

For A: which outcome model you chose and why, and what the classifier now does
when `idet` cannot run.
For B: the measured parse result for each of the three parameters with exact
error text, and what replaced what.
Final counts for backend (with basetemp), ruff check, ruff format check,
frontend. And anything in this spec you found wrong.
