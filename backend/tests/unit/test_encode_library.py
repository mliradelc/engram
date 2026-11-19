"""GET /api/encoding/library: the read-only, grouped view of every recorded encode."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services import encode_hook, encode_library


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(encode_hook, "_ENCODING", {})
    monkeypatch.setattr(
        encode_hook, "_FAILED", encode_hook.deque(maxlen=encode_hook.FAILURE_BUFFER_SIZE)
    )
    monkeypatch.setattr(encode_hook, "_RETRY_PENDING", set())
    encode_library.clear_cache()
    yield
    encode_library.clear_cache()


@pytest.fixture
def lib(tmp_path, monkeypatch):
    """A curator-staging domain with tv/ and movies/ roots and an encode output dir."""
    from app.services import config_service

    domain = tmp_path / "_staging"
    tv = domain / "tv"
    movies = domain / "movies"
    tv.mkdir(parents=True)
    movies.mkdir(parents=True)
    out = tmp_path / "encode-out"
    out.mkdir()
    monkeypatch.setattr(encode_hook, "_DOMAIN_ROOTS", (str(domain),))
    monkeypatch.setenv("ENGRAM_ENCODE_STAGING", str(out))

    async def fake_get_config():
        return SimpleNamespace(library_tv_path=str(tv), library_movies_path=str(movies))

    monkeypatch.setattr(config_service, "get_config", fake_get_config)
    return SimpleNamespace(domain=domain, tv=tv, movies=movies, out=out)


def _source(path: Path, payload: bytes = b"source-bytes") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _mark(src: Path, *, output: Path | None, codec_class: str | None = None, **extra) -> Path:
    """Write a marker for ``src``'s current revision, as the hook would."""
    st = src.stat()
    data: dict = {
        "size": st.st_size,
        "mtime": int(st.st_mtime),
        "av1": str(output) if output else None,
    }
    if codec_class:
        data["codec_class"] = codec_class
    data.update(extra)
    marker = encode_hook._marker_path(src)
    marker.write_text(json.dumps(data))
    return marker


def _output(lib, name: str, payload: bytes = b"av1") -> Path:
    p = lib.out / name
    p.write_bytes(payload)
    return p


def _entries(body: dict) -> dict[str, dict]:
    return {e["file"]: e for g in body["groups"] for e in g["entries"]}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Stargate SG-1 - S03E09 - Rules of Engagement.mkv", (3, 9)),
        ("Show - S00E01 - Behind the Scenes.mkv", (0, 1)),
        ("show.s01e102.mkv", (1, 102)),
        ("Inception (2010).mkv", (None, None)),
        ("Stargate SG-1 Disc 1 Extra t05.mkv", (None, None)),
        ("MS01E02X.mkv", (None, None)),  # not a token on its own
    ],
)
def test_parse_season_episode(name, expected):
    assert encode_library.parse_season_episode(name) == expected


def test_ttl_is_a_module_constant():
    assert encode_library.LIBRARY_SCAN_TTL_SECONDS == 10.0


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


async def test_empty_library(client, lib):
    resp = await client.get("/api/encoding/library")
    assert resp.status_code == 200
    body = resp.json()
    assert body["groups"] == []
    assert body["totals"]["entries"] == 0
    assert body["totals"]["groups"] == 0
    assert body["root_tv"] == str(lib.tv)
    assert body["root_movies"] == str(lib.movies)


async def test_groups_tv_film_and_ungrouped(client, lib):
    show = lib.tv / "Stargate SG-1 (1997) [tmdbid-4629]"
    ep = _source(show / "Season 03" / "Stargate SG-1 - S03E09 - Rules of Engagement.mkv")
    _mark(ep, output=_output(lib, ep.name), codec_class="live_action_grainy")
    film = lib.movies / "Inception (2010)"
    mov = _source(film / "Inception (2010).mkv")
    _mark(mov, output=_output(lib, mov.name), codec_class="live_action_clean")
    # Directly in the tv root (no show folder) and in the domain but outside both roots.
    loose = _source(lib.tv / "Loose File.mkv")
    _mark(loose, output=None, codec_class="passthrough")
    elsewhere = _source(lib.domain / "imports" / "Mystery.mkv")
    _mark(elsewhere, output=None, codec_class="passthrough")
    # An .mkv with no marker is not an encode record.
    _source(show / "Season 03" / "Stargate SG-1 - S03E10 - Unmarked.mkv")

    resp = await client.get("/api/encoding/library")
    assert resp.status_code == 200
    body = resp.json()
    kinds = [(g["kind"], g["title"]) for g in body["groups"]]
    assert kinds == [
        ("series", "Stargate SG-1 (1997) [tmdbid-4629]"),
        ("film", "Inception (2010)"),
        ("other", "Ungrouped"),
    ]
    series, movie, other = body["groups"]
    assert series["folder"] == str(show)
    [e] = series["entries"]
    assert e["source"] == str(ep)
    assert e["folder"] == str(show / "Season 03")
    assert (e["season"], e["episode"]) == (3, 9)
    assert e["output"] == str(lib.out / ep.name)
    assert e["codec_class"] == "live_action_grainy"
    assert e["state"] == "encoded"
    assert e["source_bytes"] == ep.stat().st_size
    assert e["output_bytes"] == 3
    assert e["encoded_at"] == int(encode_hook._marker_path(ep).stat().st_mtime)
    assert movie["folder"] == str(film)
    assert movie["entries"][0]["season"] is None
    assert sorted(x["file"] for x in other["entries"]) == ["Loose File.mkv", "Mystery.mkv"]
    assert other["folder"] == ""
    assert body["totals"]["groups"] == 3
    assert body["totals"]["entries"] == 4
    assert body["totals"]["encoded"] == 2
    assert body["totals"]["passthrough"] == 2


async def test_extras_season_zero_and_episode_order(client, lib):
    show = lib.tv / "Show (2001)"
    names = ["Show - S01E02.mkv", "Show - S00E01 - Featurette.mkv", "Show - S01E01.mkv"]
    for n in names:
        src = _source(show / ("Specials" if "S00" in n else "Season 01") / n)
        _mark(src, output=None, codec_class="passthrough")
    body = (await client.get("/api/encoding/library")).json()
    [group] = body["groups"]
    got = [(e["season"], e["episode"]) for e in group["entries"]]
    assert got == [(0, 1), (1, 1), (1, 2)]


async def test_passthrough_is_its_own_state(client, lib):
    src = _source(lib.tv / "Show" / "Show - S01E01.mkv")
    _mark(src, output=None)  # no codec_class: passthrough
    e = _entries((await client.get("/api/encoding/library")).json())[src.name]
    assert e["state"] == "passthrough"
    assert e["codec_class"] == "passthrough"
    assert e["output"] is None
    assert e["output_bytes"] is None


@pytest.mark.parametrize("field", ["size", "mtime"])
async def test_stale_when_source_changed(client, lib, field):
    src = _source(lib.tv / "Show" / "Show - S01E01.mkv")
    marker = _mark(src, output=_output(lib, src.name), codec_class="animation")
    data = json.loads(marker.read_text())
    data[field] += 1
    marker.write_text(json.dumps(data))
    body = (await client.get("/api/encoding/library")).json()
    e = _entries(body)[src.name]
    assert e["state"] == "stale"
    assert e["note"]
    assert body["totals"]["stale"] == 1


async def test_old_interlaced_skip_is_not_reported_done(client, lib):
    src = _source(lib.tv / "Show" / "Show - S01E01.mkv")
    _mark(src, output=None, codec_class="interlaced")
    e = _entries((await client.get("/api/encoding/library")).json())[src.name]
    assert e["state"] == "stale"


async def test_output_missing(client, lib):
    src = _source(lib.tv / "Show" / "Show - S01E01.mkv")
    gone = lib.out / "pruned" / src.name
    _mark(src, output=gone, codec_class="live_action_clean")
    body = (await client.get("/api/encoding/library")).json()
    e = _entries(body)[src.name]
    assert e["state"] == "output_missing"
    assert e["output"] == str(gone)  # reported as recorded, not reconciled
    assert e["output_bytes"] is None
    assert body["totals"]["output_missing"] == 1


async def test_pin_only_marker_is_pending(client, lib):
    src = _source(lib.tv / "Show" / "Show - S01E01.mkv")
    st = src.stat()
    encode_hook._marker_path(src).write_text(
        json.dumps({"override": {"codec_class": "animation", "size": st.st_size, "mtime": 1}})
    )
    e = _entries((await client.get("/api/encoding/library")).json())[src.name]
    assert e["state"] == "pending"
    assert e["encoded_at"] is None


async def test_live_encoding_wins_over_marker(client, lib, monkeypatch):
    src = _source(lib.tv / "Show" / "Show - S01E01.mkv")
    _mark(src, output=_output(lib, src.name), codec_class="animation")
    row = {
        "file": src.name,
        "destination_folder": str(lib.out),
        "percent": 42.5,
        "eta": 1800,
        "speed": "0.25x",
        "codec": "hevc",
        "codec_class": "live_action_grainy",
        "forced": True,
        "started_at": 1.0,
    }
    monkeypatch.setattr(encode_hook, "_ENCODING", {str(lib.out / src.name): row})
    body = (await client.get("/api/encoding/library")).json()
    e = _entries(body)[src.name]
    assert e["state"] == "encoding"
    assert e["progress"]["percent"] == 42.5
    assert e["progress"]["eta"] == 1800
    assert e["codec_class"] == "live_action_grainy"
    assert e["forced"] is True
    assert body["totals"]["encoding"] == 1
    assert body["totals"]["encoded"] == 0


async def test_live_failure_wins_over_marker(client, lib):
    src = _source(lib.tv / "Show" / "Show - S01E01.mkv")
    _mark(src, output=_output(lib, src.name), codec_class="animation")
    encode_hook._record_failure(src, lib.out / src.name, "ffmpeg exited 1: boom")
    e = _entries((await client.get("/api/encoding/library")).json())[src.name]
    assert e["state"] == "failed"
    assert e["failure"]["reason"] == "ffmpeg exited 1: boom"


@pytest.mark.parametrize("junk", ["{not json", "[1, 2]", '"a string"', ""])
async def test_invalid_marker_is_skipped_not_500(client, lib, junk):
    bad = _source(lib.tv / "Show" / "Show - S01E01.mkv")
    encode_hook._marker_path(bad).write_text(junk)
    good = _source(lib.tv / "Show" / "Show - S01E02.mkv")
    _mark(good, output=None)
    resp = await client.get("/api/encoding/library")
    assert resp.status_code == 200
    assert list(_entries(resp.json())) == [good.name]


async def test_overlapping_roots_list_each_source_once(client, lib):
    # The domain root contains library_tv_path: both are roots, the file is listed once.
    src = _source(lib.tv / "Show" / "Show - S01E01.mkv")
    _mark(src, output=None)
    body = (await client.get("/api/encoding/library")).json()
    assert body["totals"]["entries"] == 1


async def test_outside_the_domain_is_not_scanned(client, lib, tmp_path):
    outside = _source(tmp_path / "elsewhere" / "Show" / "Show - S01E01.mkv")
    _mark(outside, output=None)
    body = (await client.get("/api/encoding/library")).json()
    assert body["totals"]["entries"] == 0


async def test_library_outside_the_encode_domain_is_still_catalogued(
    client, tmp_path, monkeypatch
):
    """The bug this guards: markers end up in the *library*, not the domain.

    The hook writes a source's marker next to the source and the curator then
    files that source — marker and all — into the library.  ``domain_roots``
    rightly refuses to encode from the library, so a cataloguing endpoint built
    on it would find nothing at all on a real box.  Scanning is read-only and
    must therefore use ``library_scan_roots``.
    """
    from app.services import config_service

    domain = tmp_path / "_staging"
    domain.mkdir()
    library = tmp_path / "library"  # NOT under domain
    tv = library / "Videos" / "TV"
    movies = library / "Videos" / "Movies"
    tv.mkdir(parents=True)
    movies.mkdir(parents=True)
    monkeypatch.setattr(encode_hook, "_DOMAIN_ROOTS", (str(domain),))

    async def fake_get_config():
        return SimpleNamespace(library_tv_path=str(tv), library_movies_path=str(movies))

    monkeypatch.setattr(config_service, "get_config", fake_get_config)

    # The encode domain must not have widened: this is a read-only scan.
    assert encode_hook.domain_roots([str(tv), str(movies)]) == [domain.resolve()] or (
        str(tv) not in [str(p) for p in encode_hook.domain_roots([str(tv), str(movies)])]
    )

    src = _source(tv / "Show (2001) [tmdbid-1]" / "Season 01" / "Show - S01E01.mkv")
    _mark(src, output=None, codec_class="passthrough")

    body = (await client.get("/api/encoding/library")).json()
    assert body["totals"]["entries"] == 1
    group = body["groups"][0]
    assert group["kind"] == "series"
    assert str(tv) in group["folder"]


async def test_scan_is_cached_for_the_ttl(client, lib, monkeypatch):
    src = _source(lib.tv / "Show" / "Show - S01E01.mkv")
    _mark(src, output=None)
    calls = []
    real = encode_library.scan_markers
    monkeypatch.setattr(
        encode_library, "scan_markers", lambda roots: calls.append(1) or real(roots)
    )
    clock = [1000.0]
    monkeypatch.setattr(encode_library, "_monotonic", lambda: clock[0])
    await client.get("/api/encoding/library")
    await client.get("/api/encoding/library")
    assert len(calls) == 1
    clock[0] += encode_library.LIBRARY_SCAN_TTL_SECONDS + 0.1
    await client.get("/api/encoding/library")
    assert len(calls) == 2


async def test_live_overlay_is_fresh_even_when_scan_is_cached(client, lib, monkeypatch):
    src = _source(lib.tv / "Show" / "Show - S01E01.mkv")
    _mark(src, output=None)
    first = _entries((await client.get("/api/encoding/library")).json())[src.name]
    assert first["state"] == "passthrough"
    encode_hook._record_failure(src, lib.out / src.name, "boom")
    second = _entries((await client.get("/api/encoding/library")).json())[src.name]
    assert second["state"] == "failed"


async def test_two_shows_with_the_same_episode_name_do_not_cross_over(client, lib, monkeypatch):
    """A live encode is matched by source path, never by bare file name.

    Two shows can both hold "Show - S01E01.mkv".  Keying the overlay on the file
    name alone would paint one show's in-flight encode onto the other show's row,
    and both rows would report ``encoding``.
    """
    a = _source(lib.tv / "Alpha (2001)" / "Alpha - S01E01.mkv")
    _mark(a, output=None)
    b = _source(lib.tv / "Beta (2002)" / "Beta - S01E01.mkv")
    _mark(b, output=None)
    # Same file name in both shows; only Alpha is encoding.
    same = "Show - S01E01.mkv"
    a = a.rename(a.with_name(same))
    b = b.rename(b.with_name(same))
    _mark(a, output=None)
    _mark(b, output=None)

    live = {
        "file": same,
        "source": str(a),
        "destination_folder": str(lib.out),
        "percent": 12.0,
        "eta": 60,
        "speed": "1.0x",
        "codec": "hevc",
        "codec_class": "live_action_clean",
        "started_at": 1.0,
    }
    monkeypatch.setattr(
        encode_hook, "encoding_status", lambda: {"active": [live], "failed": []}
    )

    body = (await client.get("/api/encoding/library")).json()
    # Keyed by source: both files share a name, which is the point of the test.
    by_source = {e["source"]: e for g in body["groups"] for e in g["entries"]}
    assert by_source[str(a)]["state"] == "encoding"
    assert by_source[str(a)]["progress"]["percent"] == 12.0
    assert by_source[str(b)]["state"] == "passthrough"
    assert by_source[str(b)]["progress"] is None


async def test_endpoint_is_read_only(client, lib, monkeypatch):
    """No write, rename, delete or encode happens; the tree is byte-identical after."""
    show = lib.tv / "Show"
    a = _source(show / "Show - S01E01.mkv")
    _mark(a, output=_output(lib, a.name), codec_class="animation")
    b = _source(show / "Show - S01E02.mkv")
    _mark(b, output=lib.out / "missing.mkv")
    c = _source(show / "Show - S01E03.mkv")
    encode_hook._marker_path(c).write_text("{broken")

    def snapshot() -> dict[str, tuple[int, int, bytes]]:
        out = {}
        for root, _dirs, files in os.walk(lib.domain.parent):
            for f in files:
                p = Path(root) / f
                st = p.stat()
                out[str(p)] = (st.st_size, st.st_mtime_ns, p.read_bytes())
        return out

    before = snapshot()

    def forbidden(*_a, **_k):
        raise AssertionError("the library endpoint must not write")

    hook_writers = (
        "_store_marker",
        "_write_marker",
        "_strip_done",
        "_store_override",
        "after_organize",
        "transcode_file",
        "retry_encode",
    )
    for name in hook_writers:
        monkeypatch.setattr(encode_hook, name, forbidden)
    for name in ("replace", "rename", "remove", "unlink", "rmdir", "mkdir", "makedirs"):
        monkeypatch.setattr(os, name, forbidden)
    for name in ("write_text", "write_bytes", "unlink", "rename", "replace", "mkdir", "touch"):
        monkeypatch.setattr(Path, name, forbidden)

    real_open = open

    def guarded_open(file, mode="r", *args, **kwargs):
        if any(ch in mode for ch in "wax+"):
            raise AssertionError(f"opened {file!r} for writing")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", guarded_open)

    resp = await client.get("/api/encoding/library")
    assert resp.status_code == 200
    assert resp.json()["totals"]["entries"] == 2
    monkeypatch.undo()
    assert snapshot() == before
