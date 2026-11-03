# SPEC-remote-asr — Remote (Mistral Voxtral) ASR backend for episode matching

Status: draft for implementation. Branch: `feat/remote-asr` on `mliradelc/engram`.
Base commit: `f5c33be65aab48318bb6c5c4b7887a288b5764e9` (upstream `main`, 2026-11-03).
License: AGPL-3.0 (inherited). This is a private homelab fork; no network-service obligation today.

## 1. Goal

Let Engram transcribe episode-matching audio through an **external ASR service** instead of
local faster-whisper, so a low-power container (4 vCPU / 4 GB) can run the full pipeline.
Provider for this fork: **Mistral Voxtral** (`voxtral-small-transcribe-3`).
Granularity: **chunk-level** — one remote call per existing 30 s audio chunk. No batching.

Non-goals (explicitly out of scope, do not implement):
- Whole-episode batching or transcript re-slicing.
- Changing matching, scoring, subtitle download, TMDB/TVDB, or the organizer.
- Changing the default provider. Local whisper stays the default; remote is opt-in.
- Any UI redesign beyond the minimum settings fields.

## 2. Files that may change

Only these:

1. `backend/app/matcher/asr_models.py` — add `RemoteASRModel`; add factory branch; extend `model_output_key`.
2. `backend/app/matcher/asr_provider.py` — add `RemoteASRProvider` (thin wrapper over `create_asr_model`).
3. `backend/app/matcher/models.py` — widen `Config.asr_provider`; add remote config fields.
4. `backend/app/models/app_config.py` — add persisted config columns.
5. `backend/app/api/routes.py` — **redaction line only** for `asr_remote_api_key` (§6). No other change.
6. `backend/tests/unit/test_remote_asr.py` — new test module (hermetic).

Anything else needs a spec amendment first. In particular do **not** touch
`episode_identification.py`, `transcript_store.py`, `config_manager.py`, or the frontend.

## 3. Why these files

Both ASR consumers converge on two choke points, which is what makes this patch small:

- `MatchEngineV2.__init__` (`matcher/core/engine.py:149`) calls `get_asr_provider(...)`.
- The real deep-identification path (`episode_identification.py:1852`, `:2171`) calls
  `get_cached_model(self._model_config())` → `create_asr_model(...)`.

`create_asr_model` is therefore the single place a new backend must be registered. `asr_provider.py`
is updated in parallel so `MatchEngineV2` sees the same type.

## 4. Interface contract

`ASRModel` (in `asr_models.py`) is the base class. `RemoteASRModel` must subclass it and implement:

```python
class RemoteASRModel(ASRModel):
    def load(self) -> None: ...                 # no model weights; validate config, mark loaded
    def transcribe(self, audio_path: str | Path) -> dict: ...   # returns {"text": str, ...}
```

- `transcribe` MUST return a dict with a `"text"` key. Callers do
  `model.transcribe(path).get("text")` and treat `None` as an empty transcript, so return
  `{"text": ""}` for a successful call that produced no speech — never `None`.
- `calculate_match_score` is **inherited unchanged** from `ASRModel` (rapidfuzz token_sort 0.7 +
  partial 0.3). Do not override it.
- `is_loaded` / `unload` are inherited; `unload` sets `_model = None`, so set `self._model` to a
  sentinel (e.g. `True`) in `load()` for `is_loaded` to report correctly.
- `device` attribute: set `self.device = "remote"`. Code in `episode_identification._model_key_for`
  reads `getattr(model, "device", None)` and only overrides for `"cpu"`/`"cuda"`, so `"remote"` is
  preserved. Verify this by reading the function, not by assumption.

## 5. Provider call (verified against Mistral docs)

- Endpoint: `POST https://api.mistral.ai/v1/audio/transcriptions`
- Auth: `Authorization: Bearer <MISTRAL_API_KEY>`
- Body: **multipart/form-data** with:
  - `file` — the WAV chunk, `(filename, bytes, "audio/wav")`
  - `model` — e.g. `voxtral-small-transcribe-3`
  - optional `language` — two-letter code; omit to auto-detect
- Response JSON: `{"model": ..., "text": "...", "language": ..., "segments": [], "usage": {...}}`

Use `httpx` — it is the project's HTTP client (`backend/pyproject.toml` line 32,
`httpx>=0.28.1`). **`requests` is NOT a dependency**; do not add it.

Error handling — all of these must raise a clear exception, not return empty text:
- non-2xx: raise with status code and a **redacted** body (never log the Authorization header).
- missing/blank `text` key: return `{"text": ""}`.
- network/timeout: raise, so the caller's existing retry/fallback logic applies.

Timeouts: connect 10 s, read 120 s (a 30 s chunk over a slow link). Make both configurable.

Retries: **do not** add a retry loop inside the model. Engram already has retry/parallelism at the
job layer; a second retry layer multiplies cost silently. One attempt per chunk.

## 6. Factory and config wiring

In `create_asr_model(model_config)`, add a branch before the `else: raise ValueError`:

```python
elif model_type in ("remote", "mistral", "voxtral"):
    return RemoteASRModel(
        endpoint=model_config.get("endpoint") or DEFAULT_REMOTE_ENDPOINT,
        model_name=model_name or "voxtral-small-transcribe-3",
        api_key=model_config.get("api_key"),
        language=model_config.get("language"),
        timeout_connect=model_config.get("timeout_connect", 10.0),
        timeout_read=model_config.get("timeout_read", 120.0),
    )
```

`Config.asr_provider` in `matcher/models.py` currently is
`Literal["whisper", "parakeet"]`. Widen to `Literal["whisper", "parakeet", "remote"]`.
Leave the parakeet migration validator intact.

New persisted fields in `app_config.py` (all with safe defaults, so an existing DB migrates cleanly):

| Field | Type | Default | Notes |
|---|---|---|---|
| `asr_remote_endpoint` | str | `""` | blank → `DEFAULT_REMOTE_ENDPOINT` |
| `asr_remote_model` | str | `"voxtral-small-transcribe-3"` | |
| `asr_remote_language` | str | `""` | blank → auto-detect |
| `asr_remote_api_key` | str | `""` | **redacted on GET**, same treatment as `tmdb_api_key` |
| `asr_remote_timeout_read` | int | `120` | seconds |

Follow the existing pattern for redaction: in `backend/app/api/routes.py` sensitive fields are
returned as `"***"` when set and `""` when empty (see `makemkv_key`, `tmdb_api_key`,
`opensubtitles_api_key` around lines 1904–1981). **`routes.py` is added to the allowed-file list
for this one purpose only** — add `asr_remote_api_key="***" if config.asr_remote_api_key else ""`
to the same response model and nothing else in that file.

Prefer reading the key from `MISTRAL_API_KEY` in the environment when the DB field is blank, so the
secret need not live in SQLite. Document whichever precedence you implement in the module docstring.

## 7. Cache-key correctness (the trap)

`model_output_key(model_config)` returns `"{type}_{name}_{device}_{compute_type}"`, and that string
is the `model_key` of the **persistent transcript cache**
(`transcript_store.get/put(file_key, start_s, duration_s, model_key)`).

If a remote model produces the same key as a local one, cached local transcripts are served for
remote runs and vice versa — silently wrong, and it looks like a matching bug.

Required: for remote types, the key must include the endpoint host and the remote model name, and
must not claim `int8`/`float16` compute types. Implement as:

```python
if model_type in ("remote", "mistral", "voxtral"):
    host = urlparse(endpoint).netloc or "remote"
    return f"remote_{host}_{model_name}_{language or 'auto'}"
```

Keep the existing branch untouched for whisper/parakeet. Add a unit test asserting that a remote
config and a whisper config never produce the same key, and that two different remote endpoints
differ.

## 8. Tests (hermetic, stdlib unittest)

New file `backend/tests/unit/test_remote_asr.py`. Follow the repo's existing test style; run with
`cd backend && uv run pytest tests/unit/test_remote_asr.py`.

Required cases:
1. `create_asr_model({"type": "remote", ...})` returns a `RemoteASRModel`.
2. `transcribe` with a fake transport returning `{"text": "hello world"}` returns
   `{"text": "hello world"}`.
3. `transcribe` with a fake 401 raises, and the raised message does not contain the API key.
4. `transcribe` with a response lacking `text` returns `{"text": ""}`.
5. `model_output_key` differs between remote and whisper for the same name.
6. `model_output_key` differs between two different remote endpoints.
7. `calculate_match_score` is inherited and returns >0.9 for identical strings (guards against an
   accidental override).

Fake the HTTP layer with `unittest.mock.patch` on the module-level session/function — **no live
network in unit tests**. A separate opt-in live smoke test may be added under `tests/live/` only if
that directory already has a skip-unless-env pattern; otherwise do not add one.

## 9. Acceptance (run by the principal, not by you)

- `uv run pytest tests/unit/test_remote_asr.py` green.
- `uv run pytest` — no new failures elsewhere.
- `uv run ruff check .` clean on changed files.
- One real disc transcribed end-to-end with `asr_provider=remote`, comparing episode IDs against
  the curator's findings for the same disc, and recording: wall time, number of API calls, and
  cost.

## 10. Open questions

None. If any decision is genuinely ambiguous while implementing, stop and report it rather than
choosing silently — list the choice in your summary so it can be reviewed.
