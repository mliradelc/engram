"""Per-title codec-class override (pin) and forced retry for the AV1 hook.

Covers SPEC-reclassify-v2 D0-D5: only four classes may be forced, the pin lives
in the marker and survives an encode and a retry, a stale pin is ignored and
dropped, the domain gate holds, and nothing ever encodes in place.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services import av1_hook

FOUR = ["passthrough", "animation", "live_action_grainy", "live_action_clean"]

SD_MPEG2 = {
    "streams": [{"codec_type": "video", "codec_name": "mpeg2video", "width": 720, "height": 576}]
}
HD_VC1 = {
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "vc1",
            "width": 1920,
            "height": 1080,
            "color_transfer": "bt709",
        }
    ]
}
HDR_HEVC = {
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "hevc",
            "width": 3840,
            "height": 2160,
            "color_transfer": "smpte2084",
        }
    ]
}
# Captured before any fixture swaps it for a fake.
_REAL_TRANSCODE = av1_hook.transcode_to_av1

AV1_SRC = {"streams": [{"codec_type": "video", "codec_name": "av1", "width": 1920, "height": 1080}]}


PROGRESSIVE_SRC = {"field_order": "progressive",
                   "idet": {"tff": 2, "bff": 1, "progressive": 200, "undetermined": 9}}
INTERLACED_SRC = {"field_order": "tt",
                  "idet": {"tff": 180, "bff": 4, "progressive": 20, "undetermined": 6}}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(av1_hook, "_ENCODING", {})
    monkeypatch.setattr(av1_hook, "_FAILED", av1_hook.deque(maxlen=av1_hook.FAILURE_BUFFER_SIZE))
    monkeypatch.setattr(av1_hook, "_RETRY_PENDING", set())


@pytest.fixture(autouse=True)
def interlace(monkeypatch):
    """Stub the classifier's idet probe (no ffmpeg on fake files).  Tests set
    ``interlace.result`` to choose what the source looks like; ``calls`` lists
    the paths probed."""
    state = SimpleNamespace(result={}, calls=[])

    def fake(path):
        state.calls.append(path)
        return dict(state.result)

    monkeypatch.setattr(av1_hook._load_classifier(), "probe_interlace", fake)
    return state


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Hook and classifier on; domain = tmp/_staging (with a tv library inside);
    AV1 output = tmp/out; a sibling tmp/media/tv stands in for the real library.
    ``transcode_to_av1`` is faked and records the plan it was given."""
    domain = tmp_path / "_staging"
    lib = domain / "tv"
    lib.mkdir(parents=True)
    library = tmp_path / "media" / "tv"
    library.mkdir(parents=True)
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(av1_hook, "_DOMAIN_ROOTS", (str(domain),))
    monkeypatch.setenv("ENGRAM_AV1_HOOK", "1")
    monkeypatch.delenv("ENGRAM_CODEC_CLASSIFIER", raising=False)
    monkeypatch.setenv("ENGRAM_AV1_STAGING", str(out))
    monkeypatch.setattr(av1_hook, "_probe", lambda p: SD_MPEG2)

    encodes: list[dict] = []

    def fake_transcode(src, staging, plan=None, *, loop=None):
        encodes.append({"src": Path(src), "plan": plan})
        return staging / Path(src).name

    monkeypatch.setattr(av1_hook, "transcode_to_av1", fake_transcode)

    classified: list[Path] = []

    def fake_classify(src, context):
        classified.append(src)
        mod = av1_hook._load_classifier()
        return mod.plan_for_class("live_action_clean", res="sd")

    monkeypatch.setattr(av1_hook, "_classify", fake_classify)
    return SimpleNamespace(
        domain=domain,
        lib=lib,
        library=library,
        out=out,
        encodes=encodes,
        classified=classified,
        roots=[domain, out],
    )


def _rip(folder: Path, name: str = "Show - S01E01.mkv", data: bytes = b"src") -> Path:
    p = folder / name
    p.write_bytes(data)
    return p


def _marker(src: Path) -> dict:
    return json.loads(av1_hook._marker_path(src).read_text())


def _wait_for(pred, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


# ---------------------------------------------------------------------------
# D0: the four classes, and forced_plan
# ---------------------------------------------------------------------------


def test_offered_classes_are_exactly_the_four():
    assert list(av1_hook.OFFERED_CLASSES) == FOUR
    assert "hdr" not in av1_hook.OFFERED_CLASSES
    assert "interlaced" not in av1_hook.OFFERED_CLASSES


@pytest.mark.parametrize(
    "probe, res, cls, crf",
    [
        (SD_MPEG2, "sd", "animation", 27),
        (SD_MPEG2, "sd", "live_action_clean", 23),
        (HD_VC1, "hd", "live_action_clean", 24),
        (SD_MPEG2, "sd", "live_action_grainy", 20),
        (HD_VC1, "hd", "live_action_grainy", 21),
    ],
)
def test_forced_plan_keeps_the_source_resolution(tmp_path, probe, res, cls, crf):
    plan = av1_hook.forced_plan(tmp_path / "x.mkv", cls, probe)
    assert plan.class_name == cls
    assert plan.evidence["res"] == res
    assert plan.evidence["forced"] is True
    assert plan.crf == crf


@pytest.mark.parametrize("cls", FOUR)
def test_forced_plan_never_yields_hdr_or_interlaced(tmp_path, cls, interlace):
    interlace.result = PROGRESSIVE_SRC
    plan = av1_hook.forced_plan(tmp_path / "x.mkv", cls, HD_VC1)
    assert plan.class_name == cls
    assert plan.class_name not in ("hdr", "interlaced")
    assert not plan.hdr
    assert not plan.video_filters  # no deinterlace sneaks in


@pytest.mark.parametrize("cls", ["hdr", "interlaced", "bogus"])
def test_forced_plan_refuses_classes_outside_the_four(tmp_path, cls):
    with pytest.raises(ValueError):
        av1_hook.forced_plan(tmp_path / "x.mkv", cls, HD_VC1)


# ---------------------------------------------------------------------------
# Deinterlacing belongs to the source: a forced or pinned class cannot skip it
# ---------------------------------------------------------------------------

ENCODING_FOUR = [c for c in FOUR if c != "passthrough"]
BWDIF = "bwdif=mode=send_frame:parity=auto:deint=all"


@pytest.mark.parametrize("cls", ENCODING_FOUR)
def test_forced_class_on_an_interlaced_source_still_gets_bwdif(tmp_path, cls, interlace):
    interlace.result = INTERLACED_SRC
    plan = av1_hook.forced_plan(tmp_path / "x.mkv", cls, SD_MPEG2)
    assert plan.class_name == cls  # the class still picks codec/CRF/preset
    assert plan.video_filters == [BWDIF]
    assert plan.evidence["interlaced"] == "yes"
    args = plan.video_args(4)
    assert args[args.index("-vf") + 1] == BWDIF
    assert interlace.calls == [str(tmp_path / "x.mkv")]


@pytest.mark.parametrize("cls", ENCODING_FOUR)
def test_forced_class_on_a_progressive_source_does_not_get_bwdif(tmp_path, cls, interlace):
    interlace.result = PROGRESSIVE_SRC
    plan = av1_hook.forced_plan(tmp_path / "x.mkv", cls, SD_MPEG2)
    assert plan.video_filters == []
    assert "-vf" not in plan.video_args(4)
    assert plan.evidence["interlaced"] == "no"


def test_forced_plan_uses_idet_over_a_progressive_field_order_flag(tmp_path, interlace):
    """A DVD flagged progressive but combed in fact: idet decides, not the flag."""
    interlace.result = {**INTERLACED_SRC, "field_order": "progressive"}
    plan = av1_hook.forced_plan(tmp_path / "x.mkv", "animation", SD_MPEG2)
    assert plan.video_filters == [BWDIF]


def test_forced_passthrough_skips_the_idet_probe(tmp_path, interlace):
    interlace.result = INTERLACED_SRC
    plan = av1_hook.forced_plan(tmp_path / "x.mkv", "passthrough", SD_MPEG2)
    assert plan.is_passthrough and plan.video_filters == []
    assert interlace.calls == []


def test_a_pin_on_an_interlaced_source_encodes_deinterlaced(env, interlace):
    src = _rip(env.lib)
    interlace.result = INTERLACED_SRC
    av1_hook.set_override(src, "animation", roots=env.roots)
    av1_hook.after_organize(str(src))
    [enc] = env.encodes
    assert enc["plan"].class_name == "animation"
    assert enc["plan"].video_filters == [BWDIF]
    assert interlace.calls == [str(src)]


def test_a_forced_retry_class_on_an_interlaced_source_encodes_deinterlaced(env, interlace):
    src = _rip(env.lib)
    interlace.result = INTERLACED_SRC
    av1_hook.after_organize(str(src), forced_class="live_action_grainy")
    [enc] = env.encodes
    assert enc["plan"].codec == "hevc"
    assert enc["plan"].video_filters == [BWDIF]


def test_forced_plan_carries_the_source_bit_depth(tmp_path):
    ten = {"streams": [{**SD_MPEG2["streams"][0], "pix_fmt": "yuv420p10le"}]}
    eight = {"streams": [{**SD_MPEG2["streams"][0], "pix_fmt": "yuv420p"}]}
    for cls in ENCODING_FOUR:
        assert av1_hook.forced_plan(tmp_path / "x.mkv", cls, ten).pix_fmt == "yuv420p10le"
        assert av1_hook.forced_plan(tmp_path / "x.mkv", cls, eight).pix_fmt == "yuv420p"


def test_forced_class_problem():
    assert av1_hook.forced_class_problem("animation", SD_MPEG2) is None
    assert "cannot be forced" in av1_hook.forced_class_problem("hdr", HDR_HEVC)
    assert "HDR" in av1_hook.forced_class_problem("live_action_clean", HDR_HEVC)
    assert "AV1" in av1_hook.forced_class_problem("animation", AV1_SRC)
    assert av1_hook.forced_class_problem("passthrough", AV1_SRC) is None
    assert av1_hook.forced_class_problem("passthrough", HDR_HEVC) is None
    assert av1_hook.forced_class_problem("animation", {}) is not None  # unprobeable


# ---------------------------------------------------------------------------
# D1/D2: the pin in the marker
# ---------------------------------------------------------------------------


def test_setting_a_pin_records_only(env):
    src = _rip(env.lib)
    status = av1_hook.set_override(src, "animation", roots=env.roots)
    assert env.encodes == [] and env.classified == []
    assert status["override"]["codec_class"] == "animation"
    assert status["override"]["stale"] is False
    assert status["done"] is False
    marker = _marker(src)
    assert set(marker) == {"override"}
    assert not av1_hook._already_done(src)  # a pin is not a done record


def test_pin_is_applied_not_skipped_and_does_not_loop(env):
    src = _rip(env.lib)
    av1_hook.set_override(src, "animation", roots=env.roots)

    av1_hook.after_organize(str(src))
    [enc] = env.encodes
    assert enc["plan"].class_name == "animation"
    assert enc["plan"].evidence["forced"] is True
    assert env.classified == []  # the pin replaced the classifier
    marker = _marker(src)
    assert marker["codec_class"] == "animation" and marker["forced"] is True
    assert marker["override"]["codec_class"] == "animation"  # merge kept the pin

    av1_hook.after_organize(str(src))  # second sweep: done, so no re-encode
    assert len(env.encodes) == 1


def test_pin_survives_a_restart(env):
    """The pin is read from the marker on disk, never from process memory."""
    src = _rip(env.lib)
    st = src.stat()
    # As left by a previous process: nothing in memory knows about it.
    av1_hook._marker_path(src).write_text(
        json.dumps(
            {
                "override": {
                    "codec_class": "live_action_grainy",
                    "set_at": 1.0,
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                }
            }
        )
    )
    av1_hook.after_organize(str(src))
    [enc] = env.encodes
    assert enc["plan"].class_name == "live_action_grainy"


def test_forced_class_takes_precedence_over_a_stored_pin(env):
    src = _rip(env.lib)
    av1_hook.set_override(src, "animation", roots=env.roots)
    av1_hook.after_organize(str(src), forced_class="live_action_clean")
    [enc] = env.encodes
    assert enc["plan"].class_name == "live_action_clean"


def test_forced_passthrough_writes_a_done_marker_and_encodes_nothing(env):
    src = _rip(env.lib)
    av1_hook.after_organize(str(src), forced_class="passthrough")
    assert env.encodes == []
    marker = _marker(src)
    assert marker["av1"] is None and marker["codec_class"] == "passthrough"
    assert marker["forced"] is True


def test_write_marker_merges_the_pin(env):
    src = _rip(env.lib)
    av1_hook.set_override(src, "animation", roots=env.roots)
    av1_hook._write_marker(src, env.out / src.name, "animation", forced=True)
    marker = _marker(src)
    assert marker["override"]["codec_class"] == "animation"
    assert marker["av1"] == str(env.out / src.name)
    assert not list(src.parent.glob("*.tmp"))  # atomic write leaves no temp file


# ---------------------------------------------------------------------------
# D5: stale pins
# ---------------------------------------------------------------------------


def test_stale_pin_is_ignored_reported_and_dropped(env):
    src = _rip(env.lib)
    av1_hook.set_override(src, "animation", roots=env.roots)
    src.write_bytes(b"a different, longer revision of the rip")  # size changes

    status = av1_hook.override_status(src, roots=env.roots)
    assert status["override"]["stale"] is True

    av1_hook.after_organize(str(src))
    assert env.classified == [src]  # the classifier decided
    [enc] = env.encodes
    assert enc["plan"].class_name == "live_action_clean"
    marker = _marker(src)
    assert "override" not in marker  # the next _write_marker dropped it
    assert "forced" not in marker


# ---------------------------------------------------------------------------
# Clear
# ---------------------------------------------------------------------------


def test_clear_deletes_a_pin_only_marker_and_returns_to_the_classifier(env):
    src = _rip(env.lib)
    av1_hook.set_override(src, "animation", roots=env.roots)
    assert av1_hook.clear_override(src, roots=env.roots) is True
    assert not av1_hook._marker_path(src).exists()
    assert av1_hook.clear_override(src, roots=env.roots) is False
    av1_hook.after_organize(str(src))
    assert env.classified == [src]
    assert env.encodes[0]["plan"].class_name == "live_action_clean"


def test_clear_keeps_the_done_record(env):
    src = _rip(env.lib)
    av1_hook.set_override(src, "animation", roots=env.roots)
    av1_hook._write_marker(src, env.out / src.name, "animation", forced=True)
    assert av1_hook.clear_override(src, roots=env.roots) is True
    marker = _marker(src)
    assert "override" not in marker
    assert av1_hook._already_done(src)


# ---------------------------------------------------------------------------
# Retry keeps the pin
# ---------------------------------------------------------------------------


def test_retry_strips_done_fields_but_keeps_the_pin(env, monkeypatch):
    src = _rip(env.lib)
    av1_hook.set_override(src, "animation", roots=env.roots)
    av1_hook._write_marker(src, env.out / src.name, "animation", forced=True)
    assert av1_hook._already_done(src)

    seen_markers: list[dict] = []
    started = threading.Event()

    def fake_transcode(s, staging, plan=None, *, loop=None):
        seen_markers.append(_marker(Path(s)))
        env.encodes.append({"src": s, "plan": plan})
        started.set()
        return staging / Path(s).name

    monkeypatch.setattr(av1_hook, "transcode_to_av1", fake_transcode)
    av1_hook.retry_encode(src, roots=env.roots)
    assert started.wait(5)
    assert _wait_for(lambda: not av1_hook._RETRY_PENDING)
    assert seen_markers == [{"override": seen_markers[0]["override"]}]
    assert env.encodes[-1]["plan"].class_name == "animation"
    marker = _marker(src)
    assert marker["override"]["codec_class"] == "animation"
    assert marker["forced"] is True


def test_retry_with_a_class_persists_the_pin_before_encoding(env):
    src = _rip(env.lib)
    av1_hook._write_marker(src, env.out / src.name, "live_action_clean")
    av1_hook.retry_encode(src, roots=env.roots, codec_class="live_action_grainy")
    assert _wait_for(lambda: not av1_hook._RETRY_PENDING)
    [enc] = env.encodes
    assert enc["plan"].class_name == "live_action_grainy"
    marker = _marker(src)
    assert marker["override"]["codec_class"] == "live_action_grainy"
    assert marker["codec_class"] == "live_action_grainy" and marker["forced"] is True


@pytest.mark.parametrize("cls", ["hdr", "interlaced", "bogus"])
def test_retry_refuses_classes_outside_the_four(env, cls):
    src = _rip(env.lib)
    with pytest.raises(av1_hook.RetryInvalid) as exc:
        av1_hook.retry_encode(src, roots=env.roots, codec_class=cls)
    assert exc.value.status == 422
    assert not av1_hook._RETRY_PENDING
    assert not av1_hook._marker_path(src).exists()


def test_retry_refuses_a_lossy_class_on_av1_and_hdr_sources(env, monkeypatch):
    src = _rip(env.lib)
    for probe in (AV1_SRC, HDR_HEVC):
        monkeypatch.setattr(av1_hook, "_probe", lambda p, probe=probe: probe)
        with pytest.raises(av1_hook.RetryInvalid):
            av1_hook.retry_encode(src, roots=env.roots, codec_class="animation")
    assert not av1_hook._marker_path(src).exists()


# ---------------------------------------------------------------------------
# Domain gate: library paths refused everywhere
# ---------------------------------------------------------------------------


def test_library_path_is_refused_by_retry_put_and_after_organize(env):
    src = _rip(env.library)
    with pytest.raises(av1_hook.RetryNotFound):
        av1_hook.retry_encode(src, roots=env.roots, codec_class="animation")
    with pytest.raises(av1_hook.RetryNotFound):
        av1_hook.set_override(src, "animation", roots=env.roots)
    with pytest.raises(av1_hook.RetryNotFound):
        av1_hook.clear_override(src, roots=env.roots)
    av1_hook.after_organize(str(src), forced_class="animation")
    assert env.encodes == []
    assert not av1_hook._marker_path(src).exists()
    status = av1_hook.override_status(src, roots=env.roots)
    assert status["in_domain"] is False and status["override"] is None


def test_status_outside_the_domain_never_reads_the_marker(env, monkeypatch):
    src = _rip(env.library)
    monkeypatch.setattr(
        av1_hook, "_read_marker", lambda p: pytest.fail("read a marker outside the domain")
    )
    assert av1_hook.override_status(src, roots=env.roots)["in_domain"] is False


# ---------------------------------------------------------------------------
# Never in place
# ---------------------------------------------------------------------------


def test_transcode_refuses_when_dest_is_src(env, monkeypatch):
    out = env.out
    src = _rip(out)
    calls: list = []
    monkeypatch.setattr(av1_hook.subprocess, "run", lambda *a, **k: calls.append(a))
    assert _REAL_TRANSCODE(src, out, None) is None
    assert calls == []
    assert src.read_bytes() == b"src"


def test_kill_switch_path_refuses_in_place(env, monkeypatch):
    """The hole that existed on ENGRAM_CODEC_CLASSIFIER=0: a file in the AV1
    staging dir is in the domain, and dest == src there."""
    monkeypatch.setenv("ENGRAM_CODEC_CLASSIFIER", "0")
    src = _rip(env.out)
    av1_hook.after_organize(str(src))
    assert env.encodes == []
    assert src.read_bytes() == b"src"
    assert not av1_hook._marker_path(src).exists()


def test_retry_and_put_refuse_a_source_in_av1_staging(env):
    src = _rip(env.out)
    with pytest.raises(av1_hook.RetryInvalid):
        av1_hook.retry_encode(src, roots=env.roots)
    with pytest.raises(av1_hook.RetryInvalid):
        av1_hook.set_override(src, "animation", roots=env.roots)
    assert not av1_hook._RETRY_PENDING
    assert not av1_hook._marker_path(src).exists()


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


def test_kill_switch_classify_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAM_CODEC_CLASSIFIER", "0")
    monkeypatch.setattr(
        av1_hook, "_load_classifier", lambda: pytest.fail("classifier loaded despite kill switch")
    )
    assert av1_hook._classify(tmp_path / "x.mkv", None) is None


def test_kill_switch_refuses_a_forced_retry_and_ignores_a_pin(env, monkeypatch):
    src = _rip(env.lib)
    av1_hook.set_override(src, "animation", roots=env.roots)
    monkeypatch.setenv("ENGRAM_CODEC_CLASSIFIER", "0")
    monkeypatch.setattr(av1_hook, "_classify", lambda s, c: None)  # the real kill-switch result
    with pytest.raises(av1_hook.RetryConflict) as exc:
        av1_hook.retry_encode(src, roots=env.roots, codec_class="animation")
    assert exc.value.status == 409
    av1_hook.after_organize(str(src))
    [enc] = env.encodes
    assert enc["plan"] is None  # fixed settings, the pin did not apply
    assert av1_hook.override_status(src, roots=env.roots)["classifier_enabled"] is False


# ---------------------------------------------------------------------------
# Panel rows
# ---------------------------------------------------------------------------


def test_failed_rows_carry_class_and_forced(tmp_path):
    src = tmp_path / "a.mkv"
    av1_hook._record_failure(
        src, tmp_path / "out" / "a.mkv", "boom", codec_class="animation", forced=True
    )
    [row] = av1_hook.encoding_status()["failed"]
    assert row["codec_class"] == "animation" and row["forced"] is True


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def api_env(env, monkeypatch):
    from app.services import config_service

    async def fake_get_config():
        return SimpleNamespace(library_tv_path=str(env.lib), library_movies_path="")

    monkeypatch.setattr(config_service, "get_config", fake_get_config)
    runs: list = []
    monkeypatch.setattr(av1_hook, "_run_retry", lambda src, key, loop: runs.append(src))
    env.runs = runs
    return env


async def test_api_get_lists_only_the_four_classes(client, api_env):
    src = _rip(api_env.lib)
    resp = await client.get("/api/encoding/override", params={"file": str(src)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["classes"] == FOUR
    assert body["in_domain"] is True and body["override"] is None
    assert set(body) == {
        "file",
        "in_domain",
        "done",
        "applied_class",
        "forced",
        "override",
        "classifier_enabled",
        "classes",
    }


async def test_api_put_get_delete_round_trip(client, api_env):
    src = _rip(api_env.lib)
    resp = await client.put(
        "/api/encoding/override", json={"file": str(src), "codec_class": "animation"}
    )
    assert resp.status_code == 200
    assert resp.json()["override"]["codec_class"] == "animation"
    assert api_env.encodes == [] and api_env.runs == []  # D2: records only
    resp = await client.get("/api/encoding/override", params={"file": str(src)})
    assert resp.json()["override"]["stale"] is False
    resp = await client.delete("/api/encoding/override", params={"file": str(src)})
    assert resp.status_code == 200 and resp.json() == {"cleared": True}
    assert not av1_hook._marker_path(src).exists()


@pytest.mark.parametrize("cls", ["hdr", "interlaced", "HDR", ""])
async def test_api_refuses_hdr_and_interlaced_with_422(client, api_env, cls):
    src = _rip(api_env.lib)
    resp = await client.put("/api/encoding/override", json={"file": str(src), "codec_class": cls})
    assert resp.status_code == 422
    resp = await client.post("/api/encoding/retry", json={"file": str(src), "codec_class": cls})
    assert resp.status_code == 422
    assert api_env.runs == []
    assert not av1_hook._marker_path(src).exists()


async def test_api_library_path_404_for_put_delete_and_retry(client, api_env):
    src = _rip(api_env.library)
    resp = await client.put(
        "/api/encoding/override", json={"file": str(src), "codec_class": "animation"}
    )
    assert resp.status_code == 404
    resp = await client.delete("/api/encoding/override", params={"file": str(src)})
    assert resp.status_code == 404
    resp = await client.post(
        "/api/encoding/retry", json={"file": str(src), "codec_class": "animation"}
    )
    assert resp.status_code == 404
    resp = await client.get("/api/encoding/override", params={"file": str(src)})
    assert resp.status_code == 200 and resp.json()["in_domain"] is False
    assert not av1_hook._marker_path(src).exists()


async def test_api_retry_with_class_202_and_pin_on_disk(client, api_env):
    src = _rip(api_env.lib)
    resp = await client.post(
        "/api/encoding/retry", json={"file": str(src), "codec_class": "live_action_clean"}
    )
    assert resp.status_code == 202
    assert resp.json()["codec_class"] == "live_action_clean"
    assert _marker(src)["override"]["codec_class"] == "live_action_clean"
    assert _wait_for(lambda: api_env.runs == [src.resolve()])


async def test_api_retry_with_class_409_under_kill_switch(client, api_env, monkeypatch):
    monkeypatch.setenv("ENGRAM_CODEC_CLASSIFIER", "0")
    src = _rip(api_env.lib)
    resp = await client.post(
        "/api/encoding/retry", json={"file": str(src), "codec_class": "animation"}
    )
    assert resp.status_code == 409
    assert api_env.runs == []
