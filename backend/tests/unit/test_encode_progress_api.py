"""GET /api/encoding and the encode_progress WebSocket message contract."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.websocket import ConnectionManager
from app.main import app
from app.services import encode_hook, finalization_coordinator  # noqa: F401


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
def _reset_registries(monkeypatch):
    monkeypatch.setattr(encode_hook, "_ENCODING", {})
    monkeypatch.setattr(
        encode_hook, "_FAILED", encode_hook.deque(maxlen=encode_hook.FAILURE_BUFFER_SIZE)
    )
    monkeypatch.setattr(encode_hook, "_RETRY_PENDING", set())


async def test_encoding_endpoint_empty(client):
    resp = await client.get("/api/encoding")
    assert resp.status_code == 200
    assert resp.json() == {"active": [], "failed": []}


async def test_encoding_endpoint_returns_snapshot(client, monkeypatch):
    row = {
        "file": "Show - S01E01.mkv",
        "destination_folder": "/staging/encode-out",
        "percent": 42.5,
        "eta": 1800,
        "speed": "0.25x",
        "codec": "av1",
        "started_at": 1.0,
    }
    monkeypatch.setattr(encode_hook, "_ENCODING", {"/staging/encode-out/Show - S01E01.mkv": row})
    resp = await client.get("/api/encoding")
    assert resp.status_code == 200
    assert resp.json() == {"active": [row], "failed": []}


async def test_encoding_endpoint_lists_failures(client, tmp_path):
    src = tmp_path / "Show - S01E02.mkv"
    encode_hook._record_failure(src, tmp_path / "out" / src.name, "ffmpeg exited 1")
    resp = await client.get("/api/encoding")
    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] == []
    [failed] = body["failed"]
    assert failed["file"] == src.name
    assert failed["source"] == str(src)
    assert failed["destination_folder"] == str(tmp_path / "out")
    assert failed["reason"] == "ffmpeg exited 1"
    assert isinstance(failed["failed_at"], float)


# ---------------------------------------------------------------------------
# POST /api/encoding/retry
# ---------------------------------------------------------------------------


@pytest.fixture
def retry_env(tmp_path, monkeypatch):
    """Hook on, config read stubbed, no real encode.

    Engram's domain is pointed at a fixture staging root and ``library_tv_path``
    is configured inside it (the curator's staging layout), so the route's
    ``default_retry_roots`` honours it.  A path outside the domain still 404s.
    """
    from app.services import config_service

    domain = tmp_path / "_staging"
    lib = domain / "tv"
    lib.mkdir(parents=True)
    monkeypatch.setattr(encode_hook, "_DOMAIN_ROOTS", (str(domain),))
    monkeypatch.setenv("ENGRAM_ENCODE_HOOK", "1")
    monkeypatch.setenv("ENGRAM_ENCODE_STAGING", str(tmp_path / "staging" / "encode-out"))

    async def fake_get_config():
        return SimpleNamespace(library_tv_path=str(lib), library_movies_path="")

    monkeypatch.setattr(config_service, "get_config", fake_get_config)
    runs: list[tuple] = []
    monkeypatch.setattr(encode_hook, "_run_retry", lambda src, key, loop: runs.append((src, loop)))
    return SimpleNamespace(lib=lib, runs=runs)


async def test_retry_returns_202_and_queues(client, retry_env):
    src = retry_env.lib / "Show - S01E01.mkv"
    src.write_bytes(b"x")
    marker = encode_hook._marker_path(src)
    marker.write_text("{}")
    resp = await client.post("/api/encoding/retry", json={"file": str(src)})
    assert resp.status_code == 202
    assert resp.json() == {"queued": True, "file": str(src)}
    assert not marker.exists()
    for _ in range(100):
        if retry_env.runs:
            break
        await asyncio.sleep(0.01)
    [(ran_src, loop)] = retry_env.runs
    assert ran_src == src.resolve()
    assert loop is not None  # the handler's running loop, for progress publishing


async def test_retry_returns_404_for_missing_file(client, retry_env):
    resp = await client.post(
        "/api/encoding/retry", json={"file": str(retry_env.lib / "nope.mkv")}
    )
    assert resp.status_code == 404
    assert retry_env.runs == []


async def test_retry_returns_404_outside_library_roots(client, retry_env, tmp_path):
    outside = tmp_path / "etc" / "secret.mkv"
    outside.parent.mkdir()
    outside.write_bytes(b"x")
    resp = await client.post("/api/encoding/retry", json={"file": str(outside)})
    assert resp.status_code == 404
    assert retry_env.runs == []


async def test_retry_returns_409_while_encoding(client, retry_env):
    src = retry_env.lib / "Show - S01E01.mkv"
    src.write_bytes(b"x")
    key = str(encode_hook._staging_dir() / src.name)
    encode_hook._ENCODING[key] = {"file": src.name, "started_at": 1.0}
    resp = await client.post("/api/encoding/retry", json={"file": str(src)})
    assert resp.status_code == 409
    assert "Already encoding" in resp.json()["detail"]
    assert retry_env.runs == []


async def test_retry_rejects_an_empty_body(client, retry_env):
    resp = await client.post("/api/encoding/retry", json={"file": ""})
    assert resp.status_code == 422


async def test_broadcast_encode_progress_is_flat_with_every_field():
    mgr = ConnectionManager()
    mgr.broadcast = AsyncMock()
    await mgr.broadcast_encode_progress(
        file="a.mkv", destination_folder="/out", percent=None, eta=None
    )
    mgr.broadcast.assert_awaited_once_with(
        {
            "type": "encode_progress",
            "file": "a.mkv",
            "destination_folder": "/out",
            "percent": None,
            "eta": None,
            "speed": None,
            "codec": None,
            "done": False,
        }
    )


def test_coordinator_registers_the_ws_publisher():
    from app.api.websocket import manager

    assert encode_hook._progress_publisher == manager.broadcast_encode_progress
