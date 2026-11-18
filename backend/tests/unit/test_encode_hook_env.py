"""Tests for the ENGRAM_AV1_* -> ENGRAM_ENCODE_* rename guard and ``ENGRAM_ENCODE_HOOK``.

``encode_hook`` is loaded by file path, never through ``app.services`` (same
convention as ``test_codec_classifier.py``): that package's ``__init__`` imports
the whole ASR stack.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path

import pytest

SERVICES = Path(__file__).resolve().parents[2] / "app" / "services"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SERVICES / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


encode_hook = _load("encode_hook")

OLD_NAMES = ("HOOK", "STAGING", "CRF", "PRESET", "THREADS", "MAX_CONCURRENT")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("ENGRAM_AV1_", "ENGRAM_ENCODE_")):
            monkeypatch.delenv(key)


def test_guard_raises_on_stale_key_and_names_replacement(monkeypatch, caplog):
    monkeypatch.setenv("ENGRAM_AV1_HOOK", "1")
    with caplog.at_level(logging.ERROR), pytest.raises(encode_hook.StaleEncodeEnvError) as exc:
        encode_hook.check_stale_env()
    msg = str(exc.value)
    assert "ENGRAM_AV1_HOOK -> ENGRAM_ENCODE_HOOK" in msg
    assert "encoding disabled" in msg
    assert any(
        "ENGRAM_AV1_HOOK was renamed to ENGRAM_ENCODE_HOOK" in r.getMessage()
        for r in caplog.records
    )


def test_guard_names_every_stale_key(monkeypatch):
    for suffix in OLD_NAMES:
        monkeypatch.setenv(f"ENGRAM_AV1_{suffix}", "x")
    with pytest.raises(encode_hook.StaleEncodeEnvError) as exc:
        encode_hook.check_stale_env()
    for suffix in OLD_NAMES:
        assert f"ENGRAM_AV1_{suffix} -> ENGRAM_ENCODE_{suffix}" in str(exc.value)


def test_guard_raises_even_when_new_name_is_also_set(monkeypatch):
    # A half-migrated .env is still ambiguous; make the operator remove the old key.
    monkeypatch.setenv("ENGRAM_AV1_HOOK", "1")
    monkeypatch.setenv("ENGRAM_ENCODE_HOOK", "1")
    with pytest.raises(encode_hook.StaleEncodeEnvError):
        encode_hook.check_stale_env()


def test_guard_accepts_explicit_mapping():
    with pytest.raises(encode_hook.StaleEncodeEnvError):
        encode_hook.check_stale_env({"ENGRAM_AV1_CRF": "22"})
    encode_hook.check_stale_env({"ENGRAM_ENCODE_CRF": "22"})


def test_no_stale_key_means_no_error(monkeypatch):
    monkeypatch.setenv("ENGRAM_ENCODE_HOOK", "1")
    monkeypatch.setenv("ENGRAM_ENCODE_CRF", "22")
    encode_hook.check_stale_env()  # does not raise
    assert encode_hook.stale_env_keys() == []


def test_guard_not_run_at_import(monkeypatch):
    monkeypatch.setenv("ENGRAM_AV1_HOOK", "1")
    _load("encode_hook")  # importing with a stale key must not raise


def test_cfg_reads_new_name_not_old(monkeypatch):
    monkeypatch.setenv("ENGRAM_AV1_HOOK", "1")
    assert encode_hook._cfg("ENGRAM_ENCODE_HOOK", "0") == "0"
    assert encode_hook.is_enabled() is False
    monkeypatch.setenv("ENGRAM_ENCODE_HOOK", "1")
    assert encode_hook._cfg("ENGRAM_ENCODE_HOOK", "0") == "1"


@pytest.mark.parametrize(
    ("value", "enabled"),
    [
        (None, False),
        ("1", True),
        ("0", False),
        ("true", False),
        ("yes", False),
        ("", False),
        (" 1", False),
    ],
)
def test_is_enabled_only_on_exact_one(monkeypatch, value, enabled):
    if value is not None:
        monkeypatch.setenv("ENGRAM_ENCODE_HOOK", value)
    assert encode_hook.is_enabled() is enabled


async def test_app_startup_refuses_on_stale_key(monkeypatch):
    """The guard is wired into the lifespan: the app refuses to start, before any DB work."""
    import app.main as main_mod
    from app.main import app, lifespan

    # Keep the global logging config untouched for the rest of the suite.
    monkeypatch.setattr(main_mod, "setup_logging", lambda: None)
    monkeypatch.setenv("ENGRAM_AV1_HOOK", "1")
    with pytest.raises(RuntimeError, match="ENGRAM_AV1_HOOK -> ENGRAM_ENCODE_HOOK"):
        async with lifespan(app):
            pass
