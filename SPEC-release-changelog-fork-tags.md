# SPEC — fix the failing Release workflow on the fork

## Symptom (measured, not inferred)

On every fork tag push, `.github/workflows/release.yml` fails in the job
**"Create GitHub Release"**, at the step **"Generate release notes from CHANGELOG"**.
All six binary build jobs succeed. `softprops/action-gh-release` never runs.

Failing runs: `v0.45.0-fork.2` (run 43964893081), `v0.45.0-fork.3` (run 43968292961).

Reproduced locally on this working tree:

```
$ python3 backend/scripts/extract_changelog.py --version "0.45.0-fork.3" --changelog CHANGELOG.md
error: no non-empty CHANGELOG section for version '0.45.0-fork.3' in CHANGELOG.md
EXIT=1
```

## Root cause

`release.yml` derives the changelog version from the tag:

```yaml
TAG="${{ inputs.tag || github.ref_name }}"
VERSION="${TAG#v}"
python3 backend/scripts/extract_changelog.py --version "$VERSION" --changelog CHANGELOG.md > release-notes.md
```

The fork tags releases as `v0.45.0-fork.1`, `v0.45.0-fork.2`, `v0.45.0-fork.3`, so
`VERSION` becomes `0.45.0-fork.3`. `CHANGELOG.md` carries upstream's sections only —
`## [0.45.0]`, `## [0.44.1]`, … — and there is no `## [0.45.0-fork.3]` heading, so
`extract_section()` returns `None`, the script exits 1, and `set -e` semantics kill the step.

The CI guard in `.github/workflows/ci.yml` (`CHANGELOG has a section for the pyproject
version`) passes because it checks `backend/pyproject.toml`'s version, which is still
`0.45.0` — it never sees the fork suffix. So the guard cannot catch this class of failure.

## Required behaviour

The release must succeed for **both** tag shapes:

1. Upstream-style tags `v0.45.0` → exact section `## [0.45.0]`.
2. Fork tags `v0.45.0-fork.3` → the fork's own section if one exists, otherwise fall back to
   the base version's section `## [0.45.0]`.

A fork tag must never fail the release merely because no fork-suffixed changelog heading
exists. But a genuinely missing base version must still fail loudly — do not turn the
failure into a silent empty release body.

## Files you may change

- `backend/scripts/extract_changelog.py` — add the fallback.
- `backend/tests/unit/test_extract_changelog.py` — cover the new behaviour.
- `.github/workflows/release.yml` — only if the script-level fix is insufficient; prefer
  fixing the script so the CI guard and the release share one code path.

## Do NOT change

- `backend/app/core/organizer.py` — it carries an **uncommitted** working-tree change that
  must survive untouched. Do not `git stash`, `git checkout`, `git reset`, or `git restore`
  anything outside the files listed above.
- `backend/pyproject.toml` — leave the version at `0.45.0`.
- Any existing `## [x.y.z]` section body in `CHANGELOG.md`. You may **add** a new section
  only if you also explain why; the preferred fix needs no changelog edit.

## Implementation guidance

Add a helper that derives candidate versions from a tag, most specific first, and returns the
first non-empty section:

- `0.45.0-fork.3` → try `0.45.0-fork.3`, then `0.45.0`.
- `0.45.0` → try `0.45.0` only.

Split on the first `-` (PEP 440 local/pre-release separator). Keep `_normalize_version`
behaviour intact (leading `v` tolerated). `--check` mode must report which version matched so
CI output is honest, e.g. `ok: CHANGELOG has a section for 0.45.0 (tag 0.45.0-fork.3)`.

Keep the script pure-stdlib — it runs under plain `python3` in the create-release job.

## Acceptance criteria

1. `python3 backend/scripts/extract_changelog.py --version "0.45.0-fork.3" --changelog CHANGELOG.md`
   exits 0 and prints the `[0.45.0]` body.
2. `python3 backend/scripts/extract_changelog.py --version "v0.45.0" --changelog CHANGELOG.md`
   still exits 0 with the same body (no regression).
3. `python3 backend/scripts/extract_changelog.py --version "9.9.9-fork.1" --changelog CHANGELOG.md`
   exits **1** with a clear error — an unknown base must still fail.
4. `python3 backend/scripts/extract_changelog.py --version "0.45.0" --changelog CHANGELOG.md --check`
   prints an ok line and exits 0.
5. `cd backend && uv run pytest tests/unit/test_extract_changelog.py -q` passes, including
   new tests for the fallback and the unknown-base failure.
6. `git diff --stat` shows changes only in `backend/scripts/extract_changelog.py` and
   `backend/tests/unit/test_extract_changelog.py`. `backend/app/core/organizer.py` must still
   show its original uncommitted diff.

## Commit

Commit the two files with the message:

```
fix(release): fall back to the base version's CHANGELOG section for fork tags
```

Use the existing git identity. Do **not** push, do **not** create or move any tag — the
orchestrator verifies and pushes.
