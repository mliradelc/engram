# SPEC: rename av1_hook -> encode_hook (complete, no legacy aliases)

Repo `/home/hermes/.hermes/engram-work`, branch `feat/remote-asr`, HEAD `625bbbe`.
Backend baseline: **5490 passed, 10 skipped, 1 xfailed**. Must stay at or above this.

The principal's instruction: *"Change name to the av1-hook to encode-hook"* then, when asked
whether to keep the old env var names for safety, *"Rename everything."*

**No backwards-compatible aliases.** The old names are to disappear entirely. This is safe because
the deploy will update CT111's compose file in the same release — do not add fallback reads for
`ENGRAM_AV1_*`.

## The rename map

| Old | New |
|---|---|
| `backend/app/services/av1_hook.py` | `backend/app/services/encode_hook.py` |
| `backend/tests/unit/test_av1_hook_progress.py` | `backend/tests/unit/test_encode_hook_progress.py` |
| `backend/tests/unit/test_av1_hook_concurrency.py` | `backend/tests/unit/test_encode_hook_concurrency.py` |
| `ENGRAM_AV1_HOOK` | `ENGRAM_ENCODE_HOOK` |
| `ENGRAM_AV1_STAGING` | `ENGRAM_ENCODE_STAGING` |
| `ENGRAM_AV1_THREADS` | `ENGRAM_ENCODE_THREADS` |
| `ENGRAM_AV1_CRF` | `ENGRAM_ENCODE_CRF` |
| `ENGRAM_AV1_PRESET` | `ENGRAM_ENCODE_PRESET` |
| `ENGRAM_AV1_MAX_CONCURRENT` | `ENGRAM_ENCODE_MAX_CONCURRENT` |
| `av1_hook` (imports) | `encode_hook` |
| `"AV1 hook: …"` (log strings) | `"Encoding hook: …"` |

Also rename the in-code identifiers that say `av1` but mean the hook: the module docstring, function
names, log messages and comments. Leave genuine AV1 references alone — `libsvtav1`, `svtav1-params`,
`AV1` as a codec name, `is_passthrough`/codec strings, and `ENGRAM_AV1_STAGING`'s *replacement value*
all stay as they are where they refer to the codec rather than to the hook.

**Directory path decision:** the principal did not ask to move files on disk. `ENGRAM_ENCODE_STAGING`
keeps the **same default value** (`/staging/av1-out`) so existing encoded output is not orphaned.
Rename the variable, not the path. Add a short comment saying the directory name is historical.

## Method

Use `git mv` for the three files so history follows them — never delete-and-create.

Then sweep the repository for the old tokens. 416 references exist across `.py`, `.ts`, `.tsx`,
`.md`, `.yml`, `.yaml`. Check `backend/app/api/routes.py`, `finalization_coordinator.py`,
`docker-compose.yml`, `CODEC-CLASSIFIER.md`, `README.md`, `docs/`, and the frontend if it names the
hook anywhere.

Beware these traps:
- `import av1_hook` inside a function, and `from app.services import av1_hook` — both forms.
- `monkeypatch.setattr("app.services.av1_hook.…")` target strings in tests do **not** follow a
  rename automatically; they will fail with `AttributeError` if missed.
- `@patch("...av1_hook.X")` decorator strings.
- Logger names that derive from `__name__` are automatic — do not hardcode a new one.
- Comments and docstrings in `routes.py` that name `ENGRAM_AV1_STAGING`.

After sweeping, run an exhaustive check:
```
grep -rn "av1_hook\|ENGRAM_AV1_" --include=*.py --include=*.ts --include=*.tsx \
  --include=*.md --include=*.yml --include=*.yaml . | grep -v node_modules | grep -v "^./.git"
```
It must return **only** intentional codec references (e.g. `libsvtav1`), and you should be able to
justify each remaining hit in the report. Zero hits containing `av1_hook` or `ENGRAM_AV1_`.

Do not edit `SPEC-*.md` files in the repo root — they are historical planning documents and
untracked.

## Verify

```
cd backend && uv run pytest tests/unit/ -q     # >= 5490 passed, 10 skipped, 1 xfailed
cd backend && uv run ruff check app/ tests/
```

Also confirm the module imports cleanly on its own:
```
cd backend && uv run python -c "from app.services import encode_hook; print(encode_hook.__name__)"
```

## Commit

One commit, message explaining that the hook encodes more than AV1 (the classifier routes grainy
content to x265), so the AV1 name was misleading. `git add -A` is acceptable **only** for the
`git mv` renames plus the token sweep — but check `git status` first and do not sweep unrelated
untracked SPEC files into the commit.

Do not push, tag, or touch CT111. The compose file on CT111 is updated by the parent agent as part
of the release, not by you.

## Report

Files renamed, count of references changed, the final grep output proving no `av1_hook`/
`ENGRAM_AV1_` remains, the test count, and any place where renaming would have been ambiguous or
risky.
