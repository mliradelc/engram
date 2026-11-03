# SPEC-remote-asr — Provider-agnostic remote ASR backend for episode matching

Status: draft for implementation. Branch: `feat/remote-asr` on `mliradelc/engram`.
Base commit: `f5c33be65aab48318bb6c5c4b7887a288b5764e9` (upstream `main`, 2026-11-03).
License: AGPL-3.0 (inherited). Private homelab fork; no network-service obligation today.

## 1. Goal

Let Engram transcribe episode-matching audio through **any external ASR service**, configured with
nothing more than **a URL and an API key**. No provider is hardcoded anywhere.

Granularity: **chunk-level** — one remote call per existing 30 s audio chunk. No batching.

The single decision that makes "any provider" achievable: **speak the OpenAI-compatible
`POST {base_url}/audio/transcriptions` contract**, which is the de facto standard. Verified
implementations of exactly this shape:

- OpenAI — `api.openai.com/v1/audio/transcriptions`
- Mistral — `api.mistral.ai/v1/audio/transcriptions`
- Self-hosted faster-whisper servers (hwdsl2/whisper-install, hwdsl2/docker-whisper,
  andyjrt/faster-whisper-server, v3DJG6GL/faster-whisper-backend) — same endpoint, same
  `{"text": ...}` response

One client therefore covers hosted and self-hosted, paid and free, cloud and LAN. A provider that
does not speak this contract is out of scope for v1; the escape hatch is the data-driven response
path in §5.4, not a per-provider code branch.

Non-goals (do not implement):
- Provider-specific adapters (no `if provider == "mistral"` anywhere).
- Whole-episode batching or transcript re-slicing.
- Changing matching, scoring, subtitle download, TMDB/TVDB, or the organizer.
- Changing the default provider. Local whisper stays default; remote is opt-in.

## 2. Files that may change

1. `backend/app/matcher/asr_models.py` — add `RemoteASRModel`; add factory branch; extend `model_output_key`.
2. `backend/app/matcher/asr_provider.py` — add `RemoteASRProvider` (thin wrapper over `create_asr_model`).
3. `backend/app/matcher/models.py` — widen `Config.asr_provider`; add remote config fields.
4. `backend/app/models/app_config.py` — add persisted config columns.
5. `backend/app/api/routes.py` — **redaction line only** for `asr_remote_api_key` (§6). No other change.
6. `backend/tests/unit/test_remote_asr.py` — new test module (hermetic).

Anything else needs a spec amendment first. Do **not** touch `episode_identification.py`,
`transcript_store.py`, `config_manager.py`, or the frontend.

## 3. Why these files

Both ASR consumers converge on two choke points, which is what keeps this patch small:

- `MatchEngineV2.__init__` (`matcher/core/engine.py:149`) → `get_asr_provider(...)`
- The real deep-identification path (`episode_identification.py:1852`, `:2171`) →
  `get_cached_model(self._model_config())` → `create_asr_model(...)`

`create_asr_model` is the single registration point. `asr_provider.py` is updated in parallel so
`MatchEngineV2` sees the same type.

## 4. Interface contract

`RemoteASRModel` subclasses `ASRModel` and implements:

```python
class RemoteASRModel(ASRModel):
    def load(self) -> None: ...                                 # no weights; validate config
    def transcribe(self, audio_path: str | Path) -> dict: ...    # returns {"text": str}
```

- `transcribe` MUST return a dict with a `"text"` key. Callers do
  `model.transcribe(path).get("text")` and treat `None` as empty, so return `{"text": ""}` for a
  successful call with no speech — never `None`.
- `calculate_match_score` is **inherited unchanged** (rapidfuzz token_sort 0.7 + partial 0.3).
  Do not override it.
- Set `self.device = "remote"`. `episode_identification._model_key_for` reads
  `getattr(model, "device", None)` and only overrides for `"cpu"`/`"cuda"`, so `"remote"` survives.
  Read that function and confirm before relying on it.
- `load()` sets `self._model` to a truthy sentinel so inherited `is_loaded` reports correctly.

## 5. Provider call

### 5.1 Request

- `POST {base_url}/audio/transcriptions`. Accept the configured URL with or without a trailing
  `/v1` and with or without a trailing slash; normalise once in `__init__` (strip trailing `/`,
  then append `/audio/transcriptions`). Document the accepted forms in the docstring.
- Header `Authorization: Bearer <api_key>`. If the key is blank, **omit the header entirely** —
  self-hosted servers often run without auth, and a bare `Bearer ` breaks some of them.
- Body **multipart/form-data**:
  - `file` — `(filename, bytes, "audio/wav")`
  - `model` — configured model name (required by the contract; many servers ignore the value but
    reject a missing field)
  - `language` — only when configured non-blank; omit to auto-detect
  - `response_format` — `"json"` (explicit; never rely on a server default)

Use `httpx` — the project's HTTP client (`backend/pyproject.toml` line 32, `httpx>=0.28.1`).
**`requests` is NOT a dependency**; do not add it.

### 5.2 Response

- JSON containing the configured text path → return that string.
- JSON without it → `{"text": ""}`.
- Non-JSON body with `Content-Type: text/plain` → treat the raw body as the transcript.
- Anything else → raise.

### 5.3 Errors

Raise for non-2xx, connection error and timeout. Include the status code and a **redacted** body
excerpt. **Never** log or include the API key or the Authorization header.

Timeouts: connect 10 s, read 120 s, both configurable. One attempt per chunk — **no retry loop**.
Engram already retries at the job layer; a second layer multiplies cost invisibly.

### 5.4 Response-path override (the only concession to non-standard servers)

Some services nest the text, e.g. `{"data": {"transcript": "..."}}`. Provide one optional config
field, `asr_remote_text_path` — a dotted path such as `data.transcript`, default `"text"`. Resolve
it generically. This is a data-driven escape hatch, **not** a provider branch. If the path does not
resolve, raise and name the actual top-level keys so the misconfiguration is diagnosable.

## 6. Factory and config wiring

In `create_asr_model(model_config)`, add a branch before the `else: raise ValueError`:

```python
elif model_type in ("remote", "openai-compatible"):
    return RemoteASRModel(
        base_url=model_config["base_url"],
        model_name=model_config.get("name") or "whisper-1",
        api_key=model_config.get("api_key") or "",
        language=model_config.get("language") or None,
        text_path=model_config.get("text_path") or "text",
        timeout_connect=model_config.get("timeout_connect", 10.0),
        timeout_read=model_config.get("timeout_read", 120.0),
    )
```

`base_url` is **required**: raise `ValueError("remote ASR requires base_url")` when missing. There
is no built-in default endpoint — the point is that the principal supplies it.

`Config.asr_provider` in `matcher/models.py` is currently `Literal["whisper", "parakeet"]`.
Widen to `Literal["whisper", "parakeet", "remote"]`. Leave the parakeet migration validator intact.

New persisted fields in `app_config.py` (safe defaults; existing DB migrates cleanly):

| Field | Type | Default | Notes |
|---|---|---|---|
| `asr_remote_base_url` | str | `""` | required when provider is remote |
| `asr_remote_model` | str | `"whisper-1"` | sent as `model`; server may ignore |
| `asr_remote_language` | str | `""` | blank → auto-detect |
| `asr_remote_api_key` | str | `""` | **redacted on GET** |
| `asr_remote_text_path` | str | `"text"` | dotted path into the JSON response |
| `asr_remote_timeout_read` | int | `120` | seconds |

Redaction: in `backend/app/api/routes.py` sensitive fields are returned as `"***"` when set and
`""` when empty (see `makemkv_key`, `tmdb_api_key`, `opensubtitles_api_key`, ~lines 1904–1981).
**`routes.py` is in the allowed list for this one line only** — add
`asr_remote_api_key="***" if config.asr_remote_api_key else ""` to the same response model.

Prefer the environment variable `ASR_REMOTE_API_KEY` when the DB field is blank, so the secret need
not live in SQLite. Document the precedence you implement in the module docstring.

## 7. Cache-key correctness (the trap)

`model_output_key(model_config)` returns `"{type}_{name}_{device}_{compute_type}"`, and that string
is the `model_key` of the **persistent transcript cache**
(`transcript_store.get/put(file_key, start_s, duration_s, model_key)`).

If a remote model produces the same key as a local one, cached local transcripts are served for
remote runs and vice versa — silently wrong, and it presents as a matching bug.

Required: for remote types the key must include the endpoint host and remote model name and must not
claim `int8`/`float16`:

```python
if model_type in ("remote", "openai-compatible"):
    host = urlparse(base_url).netloc or "remote"
    return f"remote_{host}_{model_name}_{language or 'auto'}"
```

Leave the whisper/parakeet branch untouched. Tests must prove remote and whisper keys never
collide, and that two different endpoints differ.

## 8. Tests (hermetic, stdlib unittest)

New file `backend/tests/unit/test_remote_asr.py`. Run:
`cd backend && uv run pytest tests/unit/test_remote_asr.py`

Required cases:
1. `create_asr_model({"type": "remote", "base_url": "http://x:9000/v1", ...})` returns `RemoteASRModel`.
2. Missing `base_url` raises `ValueError`.
3. `transcribe` with a fake transport returning `{"text": "hello world"}` returns that text.
4. `transcribe` with a fake 401 raises, and the message contains neither the key nor `Bearer`.
5. Response lacking the text path returns `{"text": ""}`.
6. `asr_remote_text_path="data.transcript"` resolves a nested response correctly.
7. An unresolvable text path raises and names the available top-level keys.
8. Blank API key omits the Authorization header (assert on the captured request).
9. `model_output_key` differs between remote and whisper for the same model name.
10. `model_output_key` differs between two different remote endpoints.
11. `calculate_match_score` is inherited and returns >0.9 for identical strings.

Mock the HTTP layer with `unittest.mock.patch` — **no live network in unit tests**.

## 9. Acceptance (run by the principal)

- `uv run pytest tests/unit/test_remote_asr.py` green.
- `uv run pytest` — no new failures elsewhere.
- `uv run ruff check .` clean on changed files.
- One episode transcribed end-to-end against a configured remote endpoint, comparing episode IDs
  with the curator's findings for the same disc, recording: wall time, API call count, cost.

## 10. Open questions

None. If a decision is genuinely ambiguous while implementing, stop and report it rather than
choosing silently — list the choice in your summary for review.
