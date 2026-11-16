"""GET /api/encoding and the encode_progress WebSocket message contract."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.websocket import ConnectionManager
from app.main import app
from app.services import av1_hook, finalization_coordinator  # noqa: F401


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_encoding_endpoint_empty(client, monkeypatch):
    monkeypatch.setattr(av1_hook, "_ENCODING", {})
    resp = await client.get("/api/encoding")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_encoding_endpoint_returns_snapshot(client, monkeypatch):
    row = {
        "file": "Show - S01E01.mkv",
        "destination_folder": "/staging/av1-out",
        "percent": 42.5,
        "eta": 1800,
        "speed": "0.25x",
        "codec": "av1",
        "started_at": 1.0,
    }
    monkeypatch.setattr(av1_hook, "_ENCODING", {"/staging/av1-out/Show - S01E01.mkv": row})
    resp = await client.get("/api/encoding")
    assert resp.status_code == 200
    assert resp.json() == [row]


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

    assert av1_hook._progress_publisher == manager.broadcast_encode_progress
