"""
ASR Model Abstraction Layer

This module provides a unified interface for different Automatic Speech Recognition models,
supporting OpenAI Whisper models via faster-whisper for efficient inference, and any
external service speaking the OpenAI-compatible ``/audio/transcriptions`` contract.

Remote API key precedence: the configured ``api_key`` (``AppConfig.asr_remote_api_key``)
wins; when it is blank the ``ASR_REMOTE_API_KEY`` environment variable is used, so the
secret need not live in SQLite; when both are blank no Authorization header is sent.
"""

import abc
import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import ctranslate2
import httpx
import librosa
import numpy as np
import psutil
from loguru import logger
from rapidfuzz import fuzz

from app.core.errors import MatchingError
from app.matcher.srt_utils import clean_text

# Cache for loaded models to avoid reloading. Guarded by _model_cache_lock so that
# concurrent matching threads (N semaphore slots → N asyncio.to_thread workers) don't
# each build their own model on a cold cache — without the lock, N threads race past the
# membership check and construct N models with N workers each (N² threads).
_model_cache = {}
_model_cache_lock = threading.Lock()

# Conservative parallel-stream cap on GPU (VRAM auto-sizing is a future enhancement).
GPU_WORKER_CAP = 4

REMOTE_MODEL_TYPES = ("remote", "openai-compatible")
DEFAULT_REMOTE_MODEL = "whisper-1"
DEFAULT_REMOTE_TEXT_PATH = "text"
REMOTE_API_KEY_ENV = "ASR_REMOTE_API_KEY"
_REMOTE_ERROR_EXCERPT_CHARS = 200
_BEARER_RE = re.compile(r"\bbearer\b\s*\S*", re.IGNORECASE)

# Process-wide resolved ASR device, decided ONCE at startup (job_manager.start) after the
# CUDA runtime is registered. Before this is set, callers fall back to the raw driver probe.
# Centralizing the decision here is what keeps the semaphore sizing, the /api/asr-status
# badge, and the model loader from disagreeing — they all read detect_asr_device().
_asr_device_override: str | None = None

# Why startup stayed on CPU although GPU acceleration was enabled, or None when it was not
# enabled at startup (or the GPU came up). Lets /api/asr-status tell "enabled but the libs
# failed to load" apart from "not enabled", which otherwise look identical (#694).
#   "runtime_missing"  - flag on, but no complete CUDA runtime on disk
#   "register_failed"  - runtime on disk, but its libraries could not be loaded
#   "load_failed"      - libraries registered, but the first CUDA model load/run failed
_gpu_fallback_reason: str | None = None


def set_asr_device(device: str | None, *, gpu_fallback_reason: str | None = None) -> None:
    """Pin the effective ASR device for the rest of the process (``None`` re-enables probing)."""
    global _asr_device_override, _gpu_fallback_reason
    _asr_device_override = device
    _gpu_fallback_reason = gpu_fallback_reason


def gpu_fallback_reason() -> str | None:
    """Why an enabled GPU fell back to CPU at startup (see ``_gpu_fallback_reason``)."""
    return _gpu_fallback_reason


def gpu_detected() -> bool:
    """True when an NVIDIA CUDA device is visible to the driver — ignores libs and override.

    This is the hardware-capability probe (does a GPU exist?), distinct from
    ``detect_asr_device()`` which reports the *usable* device. Used to decide whether to even
    offer the GPU-acceleration toggle.
    """
    try:
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001 — any probe failure means no usable GPU
        return False


def cuda_compute_type() -> str:
    """Pick the best CUDA compute type CTranslate2 reports as supported.

    Hardcoding ``float16`` fails on GPUs without Compute Capability ≥ 7.0 (e.g. Pascal), so
    fall back to ``int8_float16`` then ``float32``. Degrades to ``float16`` if the probe is
    unavailable (it always lists float16 on a working CUDA build).
    """
    try:
        supported = set(ctranslate2.get_supported_compute_types("cuda"))
    except Exception:  # noqa: BLE001 — probe failure (no CUDA): caller only reaches here on cuda
        return "float16"
    for candidate in ("float16", "int8_float16", "float32"):
        if candidate in supported:
            return candidate
    return "float16"


@dataclass(frozen=True)
class AsrRuntime:
    """Resolved ASR execution parameters — the single source of truth for sizing.

    Consumed by the shared WhisperModel (num_workers/cpu_threads), the JobManager
    match semaphore (workers == admission slots, so the dashboard cannot overstate
    MATCHING), and the /api/asr-status endpoint.
    """

    device: str  # "cuda" | "cpu"
    compute_type: str  # "float16" (cuda) | "int8" (cpu)
    workers: int
    cpu_threads: int | None  # None on GPU (not applicable)


def detect_asr_device() -> str:
    """Return the effective ASR device ('cuda' only when actually usable, else 'cpu').

    Once ``set_asr_device()`` has run at startup this returns that resolved decision — which
    accounts for whether the user enabled the GPU *and* the CUDA math libraries are installed,
    not merely whether a GPU exists. Before startup it falls back to the raw driver probe.
    """
    if _asr_device_override is not None:
        return _asr_device_override
    return "cuda" if gpu_detected() else "cpu"


def resolve_asr_runtime(device: str, requested_workers: int | None) -> AsrRuntime:
    """Resolve (workers, cpu_threads) from a requested worker count, clamped to hardware.

    CPU: workers clamp to physical cores; cpu_threads = cores // workers so the total
    thread count stays ~= cores (avoids the oversubscription that makes naive
    parallelism slower). GPU: workers clamp to GPU_WORKER_CAP; cpu_threads is N/A.
    """
    requested = max(1, int(requested_workers or 1))
    if device == "cuda":
        return AsrRuntime(
            device="cuda",
            compute_type=cuda_compute_type(),
            workers=min(requested, GPU_WORKER_CAP),
            cpu_threads=None,
        )
    cores = psutil.cpu_count(logical=False) or psutil.cpu_count(logical=True) or 1
    workers = max(1, min(requested, cores))
    cpu_threads = max(1, cores // workers)
    return AsrRuntime(device="cpu", compute_type="int8", workers=workers, cpu_threads=cpu_threads)


class ASRModel(abc.ABC):
    """Abstract base class for ASR models."""

    def __init__(self, model_name: str, device: str | None = None):
        """
        Initialize ASR model.

        Args:
            model_name: Name/identifier of the model
            device: Device to run on ('cpu', 'cuda', or None for auto-detect)
        """
        self.model_name = model_name
        self.device = device or self._get_default_device()
        self._model = None

    def _get_default_device(self) -> str:
        """Get default device for this model type (honors the startup-resolved override)."""
        return detect_asr_device()

    @abc.abstractmethod
    def load(self):
        """Load the model. Should be called before transcription."""
        pass

    @abc.abstractmethod
    def transcribe(self, audio_path: str | Path) -> dict:
        """
        Transcribe audio file.

        Args:
            audio_path: Path to audio file

        Returns:
            Dictionary with at least 'text' key containing transcription
        """
        pass

    def calculate_match_score(self, transcription: str, reference: str) -> float:
        """
        Calculate similarity score between transcription and reference.

        Args:
            transcription: Transcribed text
            reference: Reference subtitle text

        Returns:
            Float score between 0.0 and 1.0
        """
        # Default implementation: Standard weights
        # Token sort ratio (70%) + Partial ratio (30%)
        token_weight = 0.7
        partial_weight = 0.3

        score = (
            fuzz.token_sort_ratio(transcription, reference) * token_weight
            + fuzz.partial_ratio(transcription, reference) * partial_weight
        ) / 100.0

        return score

    @property
    def is_loaded(self) -> bool:
        """Check if model is loaded."""
        return self._model is not None

    def unload(self):
        """Unload model to free memory."""
        self._model = None


class FasterWhisperModel(ASRModel):
    """
    OpenAI Whisper ASR model implementation using faster-whisper.

    This uses CTranslate2 for efficient inference, providing:
    - Faster inference than original Whisper
    - Lower memory usage
    - Easy CPU and GPU support
    - No complex dependencies (unlike NVIDIA NeMo)
    """

    # Available model sizes with their approximate properties
    MODEL_SIZES = {
        "tiny": {"params": "39M", "vram": "~1GB", "speed": "fastest"},
        "tiny.en": {"params": "39M", "vram": "~1GB", "speed": "fastest"},
        "base": {"params": "74M", "vram": "~1GB", "speed": "fast"},
        "base.en": {"params": "74M", "vram": "~1GB", "speed": "fast"},
        "small": {"params": "244M", "vram": "~2GB", "speed": "medium"},
        "small.en": {"params": "244M", "vram": "~2GB", "speed": "medium"},
        "medium": {"params": "769M", "vram": "~5GB", "speed": "slow"},
        "medium.en": {"params": "769M", "vram": "~5GB", "speed": "slow"},
        "large-v3": {"params": "1550M", "vram": "~10GB", "speed": "slowest"},
    }

    def __init__(
        self, model_name: str = "small", device: str | None = None, requested_workers: int = 1
    ):
        """
        Initialize Faster Whisper model.

        Args:
            model_name: Whisper model size (tiny, base, small, medium, large-v3)
            device: Device to run on ('cpu', 'cuda', or None for auto-detect)
            requested_workers: Desired parallel ASR workers; resolve_asr_runtime
                clamps this to hardware at load time.
        """
        if model_name.startswith("openai/whisper-"):
            model_name = model_name.replace("openai/whisper-", "")

        self.requested_workers = max(1, int(requested_workers or 1))

        # Register the CUDA math libraries (downloaded cache or dev pip packages) so
        # CTranslate2 can dlopen them. Idempotent — startup already did this; this covers
        # any model built before/without that path.
        if device == "cuda" or (device is None and detect_asr_device() == "cuda"):
            _ensure_nvidia_libraries()

        super().__init__(model_name, device)

    def _get_compute_type(self) -> str:
        """Get optimal compute type for the device."""
        if self.device == "cuda":
            # Best CUDA type the hardware supports (float16, or int8_float16/float32 on
            # older GPUs) — must match resolve_asr_runtime so the cache key and badge agree.
            return cuda_compute_type()
        else:
            # Use int8 for CPU (good balance of speed and accuracy)
            return "int8"

    def load(self):
        """Load Faster Whisper model with caching."""
        if self.is_loaded:
            return

        runtime = resolve_asr_runtime(self.device, self.requested_workers)
        cache_key = (
            f"faster_whisper_{self.model_name}_{self.device}"
            f"_w{runtime.workers}_t{runtime.cpu_threads}"
        )

        if cache_key in _model_cache:
            self._model = _model_cache[cache_key]
            logger.debug(f"Using cached Faster Whisper model: {self.model_name} on {self.device}")
            return

        try:
            from faster_whisper import WhisperModel

            compute_type = self._get_compute_type()

            logger.info(
                f"Loading Faster Whisper model: {self.model_name} on {self.device} "
                f"(compute_type={compute_type})"
            )

            try:
                model_kwargs = {
                    "device": self.device,
                    "compute_type": compute_type,
                    "download_root": None,  # Use default cache location
                    "num_workers": runtime.workers,
                }
                if runtime.cpu_threads is not None:
                    model_kwargs["cpu_threads"] = runtime.cpu_threads
                self._model = WhisperModel(self.model_name, **model_kwargs)

                # Eagerly transcribe 1s of silence to surface missing CUDA
                # DLL errors here instead of on the first real transcription.
                if self.device == "cuda":
                    try:
                        logger.debug("Verifying CUDA availability by running dummy encoding...")
                        dummy_audio = np.zeros(16000, dtype=np.float32)
                        next(self._model.transcribe(dummy_audio, language="en")[0], None)
                    except Exception as e:
                        # If verifying fails, raise it to be caught by the outer try/except
                        logger.warning(f"CUDA verification failed: {e}")
                        raise RuntimeError(f"CUDA verification failed: {e}") from e

            except RuntimeError as e:
                # Fallback to CPU if CUDA libraries are missing
                if self.device == "cuda" and (
                    "Library" in str(e) or "verification failed" in str(e)
                ):
                    logger.warning(
                        f"Failed to load/run on CUDA due to missing libraries: {e}. "
                        "Falling back to CPU."
                    )
                    # Startup pinned "cuda" because the libraries registered; this load just
                    # proved they don't run. Repin so the badge, the settings panel and later
                    # models report CPU instead of "CUDA float16" over a CPU model.
                    set_asr_device("cpu", gpu_fallback_reason="load_failed")
                    self.device = "cpu"
                    compute_type = "int8"
                    cpu_runtime = resolve_asr_runtime("cpu", self.requested_workers)
                    self._model = WhisperModel(
                        self.model_name,
                        device=self.device,
                        compute_type=compute_type,
                        download_root=None,
                        num_workers=cpu_runtime.workers,
                        cpu_threads=cpu_runtime.cpu_threads,
                    )
                    # Stored under the CPU key, not the stale CUDA key computed above.
                    cache_key = (
                        f"faster_whisper_{self.model_name}_{self.device}"
                        f"_w{cpu_runtime.workers}_t{cpu_runtime.cpu_threads}"
                    )
                else:
                    raise

            _model_cache[cache_key] = self._model
            logger.info(f"Loaded Faster Whisper model: {self.model_name} on {self.device}")

        except ImportError as e:
            message = describe_faster_whisper_import_error(e)
            logger.error(f"Faster Whisper import failed: {message}", exc_info=True)
            raise ImportError(message) from e
        except Exception as e:
            logger.error(f"Failed to load Faster Whisper model {self.model_name}: {e}")
            raise

    def _preprocess_audio(self, audio_path: str | Path) -> np.ndarray:
        """
        Load audio as the 16kHz mono float32 array faster-whisper expects.

        The array is handed to faster-whisper directly. Given a path instead, it
        decodes with its own ``decode_audio``, which passes ``metadata_errors`` to
        ``av.open``; PyAV 19 removed that keyword and every chunk failed (#709).

        Args:
            audio_path: Path to input audio file

        Returns:
            Peak-normalized 16kHz mono float32 samples

        Raises:
            Exception: If the file cannot be decoded (``transcribe`` handles it)
        """
        # Target sample rate for Whisper models (16kHz)
        target_sr = 16000

        audio, original_sr = librosa.load(str(audio_path), sr=None, mono=True)

        # Resample if necessary
        if original_sr != target_sr:
            audio = librosa.resample(audio, orig_sr=original_sr, target_sr=target_sr)
            logger.debug(f"Resampled audio from {original_sr}Hz to {target_sr}Hz")

        # Normalize audio to [-1, 1] range
        peak = np.max(np.abs(audio)) if audio.size else 0.0
        if peak > 0:
            audio = audio / peak

        return audio.astype(np.float32, copy=False)

    def _clean_transcription_text(self, text: str) -> str:
        """
        Clean and normalize transcription text.

        Args:
            text: Raw transcription text

        Returns:
            Cleaned text
        """
        if not text:
            return ""

        return clean_text(text)

    def transcribe(self, audio_path: str | Path) -> dict:
        """
        Transcribe audio using Faster Whisper.

        Args:
            audio_path: Path to audio file

        Returns:
            Dictionary with 'text', 'raw_text', 'segments', and 'language'
        """
        if not self.is_loaded:
            self.load()

        try:
            logger.debug(f"Starting Faster Whisper transcription for {audio_path}")

            # Decode here and pass samples, never the path (see _preprocess_audio)
            audio = self._preprocess_audio(audio_path)

            # Transcribe with faster-whisper
            segments, info = self._model.transcribe(
                audio,
                language="en",  # Force English for TV episode matching
                beam_size=5,
                best_of=5,
                temperature=0.0,  # Greedy decoding for consistency
                condition_on_previous_text=False,
                vad_filter=True,  # Filter out non-speech
            )

            # Collect all segment texts
            segment_list = []
            full_text_parts = []

            for segment in segments:
                segment_list.append(
                    {
                        "start": segment.start,
                        "end": segment.end,
                        "text": segment.text,
                    }
                )
                full_text_parts.append(segment.text)

            raw_text = " ".join(full_text_parts).strip()
            cleaned_text = self._clean_transcription_text(raw_text)

            logger.debug(f"Raw transcription: '{raw_text}'")
            logger.debug(f"Cleaned transcription: '{cleaned_text}'")

            return {
                "text": cleaned_text,
                "raw_text": raw_text,
                "segments": segment_list,
                "language": info.language if hasattr(info, "language") else "en",
            }

        except Exception as e:
            # opt(exception=) puts the traceback in the job-tagged log; print_exc()
            # only reached stderr, which the diagnostics bundle never sees.
            logger.opt(exception=e).error(
                f"Faster Whisper transcription failed for {audio_path}: {type(e).__name__}: {e}"
            )
            # Return empty result instead of raising to allow fallback
            return {"text": "", "raw_text": "", "segments": [], "language": "en"}


class RemoteASRModel(ASRModel):
    """
    ASR through an external service's OpenAI-compatible ``POST .../audio/transcriptions``.

    One call per audio chunk and one attempt per call: Engram already retries at the job
    layer, and a second layer would multiply cost invisibly.

    ``base_url`` is used as given apart from surrounding whitespace and a trailing slash,
    and ``/audio/transcriptions`` is appended. All of these are accepted, and nothing is
    guessed, so a server that serves under ``/v1`` must be configured with it:

    - ``https://api.example.com/v1`` -> ``https://api.example.com/v1/audio/transcriptions``
    - ``https://api.example.com/v1/`` -> ``https://api.example.com/v1/audio/transcriptions``
    - ``http://192.168.1.5:9000`` -> ``http://192.168.1.5:9000/audio/transcriptions``
    - ``http://192.168.1.5:9000/`` -> ``http://192.168.1.5:9000/audio/transcriptions``
    """

    def __init__(
        self,
        base_url: str,
        model_name: str = DEFAULT_REMOTE_MODEL,
        api_key: str = "",
        language: str | None = None,
        text_path: str = DEFAULT_REMOTE_TEXT_PATH,
        timeout_connect: float = 10.0,
        timeout_read: float = 120.0,
    ):
        """
        Initialize the remote model.

        Args:
            base_url: Service base URL (see the class docstring); required
            model_name: Sent as the ``model`` form field
            api_key: Bearer token; blank falls back to ``ASR_REMOTE_API_KEY``
            language: Sent as the ``language`` form field; blank lets the server auto-detect
            text_path: Dotted path to the transcript in the JSON response
            timeout_connect: Connect timeout in seconds
            timeout_read: Read timeout in seconds
        """
        normalized_url = (base_url or "").strip().rstrip("/")
        if not normalized_url:
            raise ValueError("remote ASR requires base_url")

        super().__init__(model_name, device="remote")
        self.base_url = normalized_url
        self.endpoint = f"{normalized_url}/audio/transcriptions"
        self.language = (language or "").strip() or None
        self.text_path = text_path
        self._api_key = (api_key or "").strip() or os.environ.get(REMOTE_API_KEY_ENV, "").strip()
        self._timeout = httpx.Timeout(timeout_read, connect=timeout_connect)

    def load(self) -> None:
        """No weights to load: validate the config and mark the model ready."""
        parsed = urlparse(self.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("remote ASR base_url must be an absolute http(s) URL")
        self._model = True

    def _redact(self, text: str) -> str:
        """Strip the API key and any bearer credential from text bound for an error."""
        if self._api_key:
            text = text.replace(self._api_key, "***")
        return _BEARER_RE.sub("***", text)

    def _excerpt(self, response: httpx.Response) -> str:
        return self._redact(response.text)[:_REMOTE_ERROR_EXCERPT_CHARS]

    def _auth_headers(self) -> dict[str, str]:
        # Omitted, not blank: a bare "Bearer " breaks some unauthenticated servers.
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    def _resolve_text(self, payload: dict) -> str:
        """Read the transcript at ``text_path``.

        A response without the default ``text`` field is a call that produced no text,
        but an explicitly configured path that does not resolve is a misconfiguration,
        so that raises and names what the response actually holds.
        """
        value = payload
        for part in self.text_path.split("."):
            if not isinstance(value, dict) or part not in value:
                keys = sorted(str(key) for key in payload)
                if self.text_path == DEFAULT_REMOTE_TEXT_PATH:
                    logger.warning(f"Remote ASR response has no 'text' field; keys: {keys}")
                    return ""
                raise MatchingError(
                    f"Remote ASR text path '{self.text_path}' not found in response; "
                    f"top-level keys: {keys}"
                )
            value = value[part]

        if value is None:
            return ""
        if not isinstance(value, str):
            raise MatchingError(
                f"Remote ASR text path '{self.text_path}' holds "
                f"{type(value).__name__}, not a string"
            )
        return value

    def _parse_response(self, response: httpx.Response) -> str:
        if not response.is_success:
            raise MatchingError(
                f"Remote ASR returned HTTP {response.status_code}: {self._excerpt(response)}"
            )

        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            return self._resolve_text(payload)

        # A plain-text transcript may happen to parse as JSON ("123"), hence the dict check.
        content_type = response.headers.get("content-type", "")
        if content_type.lower().startswith("text/plain"):
            return response.text
        raise MatchingError(
            f"Remote ASR returned an unusable response (Content-Type "
            f"'{content_type}'): {self._excerpt(response)}"
        )

    def transcribe(self, audio_path: str | Path) -> dict:
        """
        Transcribe audio through the remote service.

        Args:
            audio_path: Path to audio file

        Returns:
            Dictionary with 'text'; "" for a successful call with no speech

        Raises:
            MatchingError: On a non-2xx status, connection error, timeout or unusable body
        """
        if not self.is_loaded:
            self.load()

        path = Path(audio_path)
        form = {"model": self.model_name, "response_format": "json"}
        if self.language:
            form["language"] = self.language
        files = {"file": (path.name, path.read_bytes(), "audio/wav")}

        # Raised outside the except block on purpose. The file log sink runs with
        # diagnose=True, which prints the values of variables in traceback frames, and an
        # httpx exception chain carries the headers (h11 quotes an illegal header value in
        # its message). Only the redacted string may reach a traceback.
        response = None
        failure = ""
        try:
            response = httpx.post(
                self.endpoint,
                headers=self._auth_headers(),
                data=form,
                files=files,
                timeout=self._timeout,
            )
        except (httpx.HTTPError, httpx.InvalidURL, UnicodeError) as e:
            failure = f"Remote ASR request failed ({type(e).__name__}): {self._redact(str(e))}"
        if response is None:
            raise MatchingError(failure)

        return {"text": self._parse_response(response)}


def _remote_endpoint_id(base_url: str) -> str:
    """Host of a remote ASR URL, plus any path that distinguishes it from other services.

    Credentials embedded in the URL are dropped because this ends up in a persisted key.
    A trailing ``/v1`` is dropped so the common forms of one URL share a key, while the
    rest of the path is kept: two services behind one gateway differ only there.
    """
    parsed = urlparse(base_url.strip().rstrip("/"))
    host = parsed.netloc.rpartition("@")[2] or "remote"
    return f"{host}{parsed.path.removesuffix('/v1')}"


def model_output_key(model_config: dict) -> str:
    """Return a stable string identifying the *output-affecting* ASR model configuration.

    This key is designed for use as the ``model_key`` component of a persistent
    transcript-cache lookup (file_key, start_s, duration_s, model_key).

    **Assumption — caller owns device honesty.** The key is only as accurate as the
    config it receives.  An explicit ``device="cuda"`` is taken at face value; if the
    model actually loads on CPU (e.g. the CUDA→CPU fallback in
    ``FasterWhisperModel.__init__`` when cuDNN libraries are missing), the key will be
    stale.  Callers that build a config from a raw GPU probe must resolve the *effective*
    device (via ``set_asr_device`` / ``detect_asr_device()``) before calling this
    function.  "auto" / None / missing are safe — they are resolved here via
    ``detect_asr_device()``.

    **Included fields** (all change Whisper's output text):
    - ``type`` — model family/architecture (e.g. "whisper", "faster-whisper")
    - ``name`` — model size/checkpoint (e.g. "small", "large-v3"); different sizes have
      different vocabularies, encoder capacities, and word-error rates.
    - ``device`` — resolved effective device ("cpu" or "cuda"). Transcripts produced on
      CUDA with float16 differ from CPU int8 transcripts due to floating-point rounding.
      An unresolved value ("auto", None, or missing) is resolved via ``detect_asr_device()``
      so the key reflects the actual runtime, not the config placeholder.
    - ``compute_type`` — quantization scheme (e.g. "int8", "float16", "int8_float16").
      Quantization changes numeric precision throughout the encoder, directly affecting
      transcription output.  Derived from ``device`` via ``cuda_compute_type()`` /
      the fixed CPU value "int8".

    **Excluded fields** (affect only speed/parallelism, not transcript content):
    - ``requested_workers`` / ``num_workers`` — controls how many parallel transcription
      streams the WhisperModel object runs; does not change what any individual stream
      produces.
    - ``cpu_threads`` — thread-pool size for BLAS ops inside CTranslate2; same model
      weights, same output.

    **Remote models** have no local device or quantization. What changes their output is
    the service, the model it runs and the language, so they key on those instead, and
    can never share a transcript with a local model or with another endpoint.

    Returns:
        ``"{type}_{name}_{device}_{compute_type}"`` — underscore-joined, deterministic
        across process restarts for the same effective configuration. Remote models return
        ``"remote_{host}_{name}_{language}"``.
    """
    model_type = model_config.get("type", "")
    model_name = model_config.get("name", "")

    if model_type.lower() in REMOTE_MODEL_TYPES:
        host = _remote_endpoint_id(model_config.get("base_url") or "")
        remote_name = model_name or DEFAULT_REMOTE_MODEL
        language = (model_config.get("language") or "").strip()
        return f"remote_{host}_{remote_name}_{language or 'auto'}"

    # Resolve the effective device: honor explicit "cpu"/"cuda", fall back to the
    # startup-pinned value for "auto", None, or missing so that a config placeholder
    # never produces a key that disagrees with the actual runtime.
    raw_device = model_config.get("device")
    if raw_device in ("cpu", "cuda"):
        device = raw_device
    else:
        device = detect_asr_device()

    # Derive compute_type from the resolved device, matching the logic in
    # FasterWhisperModel._get_compute_type() and resolve_asr_runtime().
    if device == "cuda":
        compute_type = cuda_compute_type()
    else:
        compute_type = "int8"

    return f"{model_type}_{model_name}_{device}_{compute_type}"


def create_asr_model(model_config: dict) -> ASRModel:
    """
    Factory function to create ASR models from configuration.

    Args:
        model_config: Dictionary with 'type' and 'name' keys

    Returns:
        Configured ASRModel instance

    Example:
        model_config = {"type": "whisper", "name": "small"}
        model = create_asr_model(model_config)
    """
    model_type = model_config.get("type", "").lower()
    model_name = model_config.get("name", "")
    device = model_config.get("device")
    requested_workers = model_config.get("requested_workers", 1)

    # Handle whisper and faster-whisper types
    if model_type in ("whisper", "faster-whisper", "openai-whisper"):
        if not model_name:
            model_name = "small"

        logger.info(f"Creating Faster Whisper model: {model_name}")
        return FasterWhisperModel(model_name, device, requested_workers=requested_workers)

    # Legacy parakeet support - redirect to whisper
    elif model_type == "parakeet":
        logger.warning(
            "Parakeet models are no longer supported. Using Whisper 'small' model instead."
        )
        return FasterWhisperModel("small", device, requested_workers=requested_workers)

    elif model_type in REMOTE_MODEL_TYPES:
        return RemoteASRModel(
            base_url=model_config.get("base_url"),
            model_name=model_name or DEFAULT_REMOTE_MODEL,
            api_key=model_config.get("api_key") or "",
            language=model_config.get("language") or None,
            text_path=model_config.get("text_path") or DEFAULT_REMOTE_TEXT_PATH,
            timeout_connect=model_config.get("timeout_connect", 10.0),
            timeout_read=model_config.get("timeout_read", 120.0),
        )

    else:
        raise ValueError(
            f"Unsupported model type: {model_type}. "
            "Supported types: 'whisper', 'faster-whisper', 'remote', 'openai-compatible'"
        )


def get_cached_model(model_config: dict) -> ASRModel:
    """
    Get a cached model instance, creating it if necessary.

    Args:
        model_config: Dictionary with model configuration

    Returns:
        ASRModel instance (loaded and ready for use)
    """
    if str(model_config.get("type", "")).lower() in REMOTE_MODEL_TYPES:
        # Every field selects a different service or credential, and the type/name key
        # below would hand one endpoint's model to another. Hashed to keep the key out of
        # anything that prints this cache.
        identity = json.dumps(model_config, sort_keys=True, default=str)
        cache_key = f"remote_{hashlib.sha256(identity.encode()).hexdigest()}"
    else:
        device = model_config.get("device") or detect_asr_device()
        runtime = resolve_asr_runtime(device, model_config.get("requested_workers", 1))
        cache_key = (
            f"{model_config.get('type', '')}_{model_config.get('name', '')}"
            f"_{device}_w{runtime.workers}_t{runtime.cpu_threads}"
        )

    # Hold the lock across check-create-store so exactly one thread builds the shared
    # model per cache_key; the rest block, then reuse it. First-load contention is the
    # whole point — it's what makes "one shared model with N workers" actually one model.
    with _model_cache_lock:
        if cache_key not in _model_cache:
            model = create_asr_model(model_config)
            model.load()  # Load immediately for caching
            _model_cache[cache_key] = model

        return _model_cache[cache_key]


def clear_model_cache():
    """Clear all cached models to free memory."""
    global _model_cache
    for model in _model_cache.values():
        if hasattr(model, "unload"):
            model.unload()
    _model_cache.clear()
    logger.info("Cleared ASR model cache")


def describe_faster_whisper_import_error(error: ImportError) -> str:
    """Explain a failed ``faster_whisper`` import without blaming the wrong thing.

    "Not installed" is only true when the ``faster_whisper`` package itself is absent.
    Anything else (a CUDA DLL that will not load, a broken ctranslate2 or av) means the
    package is present but unusable, and hiding that behind an install hint sent #760
    chasing a bundled dependency that was never missing.
    """
    root = (getattr(error, "name", None) or "").split(".")[0]
    if isinstance(error, ModuleNotFoundError) and root == "faster_whisper":
        return "faster-whisper not installed. Run: pip install faster-whisper"
    return f"faster-whisper failed to import ({type(error).__name__}: {error})"


def list_available_models() -> dict:
    """
    List available model types and their requirements.

    Returns:
        Dictionary with model types and their availability status
    """
    availability = {}

    # Check Faster Whisper availability
    try:
        from faster_whisper import WhisperModel  # noqa: F401

        availability["whisper"] = {
            "available": True,
            "models": list(FasterWhisperModel.MODEL_SIZES.keys()),
            "default": "small",
            "description": "OpenAI Whisper models via faster-whisper (CTranslate2)",
        }
    except ImportError as e:
        availability["whisper"] = {
            "available": False,
            "error": describe_faster_whisper_import_error(e),
        }

    return availability


def _ensure_nvidia_libraries():
    """Register the CUDA math libraries (downloaded cache, else dev pip packages).

    Cross-platform: on Windows this adds the DLL directory; on Linux it preloads the .so
    files into the global symbol namespace. Delegates to ``cuda_runtime.register_cuda_runtime``
    so the on-demand download cache and the dev ``uv sync -E gpu`` packages are both honored.
    Safe to call when nothing is installed (no-op).
    """
    try:
        from app.matcher.cuda_runtime import register_cuda_runtime

        register_cuda_runtime()
    except Exception as e:  # noqa: BLE001 — registration is best-effort; load-time fallback covers it
        logger.warning(f"Failed to register CUDA libraries: {e}", exc_info=True)
