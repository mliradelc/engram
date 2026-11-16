"""Tests for the content-aware codec classifier and its wiring into the AV1 hook.

Both modules are loaded by file path, never through ``app.services``: that
package's ``__init__`` imports the whole ASR stack, and the point of the
classifier's import discipline is that a codec decision does not depend on it.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SERVICES = Path(__file__).resolve().parents[2] / "app" / "services"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SERVICES / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses resolves the module through sys.modules
    spec.loader.exec_module(mod)
    return mod


cc = _load("codec_classifier")
av1_hook = _load("av1_hook")

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed")


# ---------------------------------------------------------------------------
# Synthetic probe dicts, one per class
# ---------------------------------------------------------------------------

PROGRESSIVE = {"progressive": 200, "tff": 3, "bff": 1, "undetermined": 10}
INTERLACED_IDET = {"tff": 180, "bff": 5, "progressive": 20, "undetermined": 5}


def probe(**kw) -> dict:
    base = {
        "width": 1920, "height": 1080, "fps": 23.976, "field_order": "progressive",
        "color_transfer": "bt709", "codec_name": "vc1", "duration": 2700.0,
        "idet": PROGRESSIVE, "grain": 0.05, "animation": 0.10,
    }
    base.update(kw)
    return base


CLASS_PROBES = {
    cc.PASSTHROUGH: (probe(codec_name="av1"), None),
    cc.INTERLACED: (probe(idet=INTERLACED_IDET, field_order="tt"), None),
    cc.HDR: (probe(color_transfer="smpte2084", color_primaries="bt2020"), None),
    cc.ANIMATION: (probe(animation=0.90, grain=0.02), None),
    cc.LIVE_ACTION_GRAINY: (probe(grain=0.80), None),
    cc.LIVE_ACTION_CLEAN: (probe(), None),
}


@pytest.mark.parametrize("expected", cc.CLASSES)
def test_every_class_is_reachable_from_a_probe_dict(expected):
    p, ctx = CLASS_PROBES[expected]
    plan = cc.classify(p, ctx)
    assert plan.class_name == expected
    assert cc.validate_plan(plan) == []


def test_short_extra_is_passthrough_only_when_flagged_as_extra():
    p = probe(duration=60.0)
    assert cc.classify(p, {"is_extra": True}).class_name == cc.PASSTHROUGH
    # The same short file with no extra flag is encoded: the flag is the hint.
    assert cc.classify(p, {"is_extra": False}).class_name == cc.LIVE_ACTION_CLEAN
    # A long extra is a real feature-length bonus: encode it.
    assert cc.classify(probe(duration=1800.0), {"is_extra": True}).class_name != cc.PASSTHROUGH


# ---------------------------------------------------------------------------
# Unknown / empty probes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [None, {}, [], "garbage", 42,
                                 {"width": "wide", "grain": "lots", "idet": "yes", "fps": []},
                                 {"idet": {"tff": None, "bff": "x"}, "color_transfer": 5}])
def test_unknown_or_malformed_probe_falls_back_and_never_raises(bad):
    plan = cc.classify(bad, None)
    assert plan.class_name == cc.DEFAULT_CLASS
    assert plan.codec == "av1"
    assert cc.validate_plan(plan) == []


@pytest.mark.parametrize("ctx", [None, {}, "x", {"is_extra": "maybe", "duration_seconds": "n/a",
                                                 "content_type": 7}])
def test_malformed_context_never_raises(ctx):
    assert cc.classify(probe(), ctx).class_name == cc.LIVE_ACTION_CLEAN


def test_plan_for_file_on_a_missing_file_returns_the_default():
    plan = cc.plan_for_file("/nonexistent/definitely-not-here.mkv", {"content_type": "tv"})
    assert plan.class_name == cc.DEFAULT_CLASS


# ---------------------------------------------------------------------------
# Media probes win over identification context
# ---------------------------------------------------------------------------


def test_interlaced_detection_beats_an_animation_hint():
    p = probe(idet=INTERLACED_IDET, field_order="tt", animation=0.95, grain=0.0)
    plan = cc.classify(p, {"content_type": "tv", "is_extra": False})
    assert plan.class_name == cc.INTERLACED
    assert plan.is_passthrough


def test_interlaced_beats_hdr_and_grain_too():
    p = probe(idet=INTERLACED_IDET, color_transfer="smpte2084", grain=0.9)
    assert cc.classify(p).class_name == cc.INTERLACED


def test_idet_overrules_a_misleading_field_order_flag():
    # Progressive film flagged "tt" is common on DVD; idet says progressive.
    p = probe(field_order="tt", idet=PROGRESSIVE)
    assert cc.classify(p).class_name == cc.LIVE_ACTION_CLEAN
    # And the reverse: flagged progressive but clearly combed.
    p = probe(field_order="progressive", idet=INTERLACED_IDET)
    assert cc.classify(p).class_name == cc.INTERLACED


def test_idet_with_no_consistent_field_order_is_not_trusted_over_the_container():
    # Measured on synthetic progressive test patterns: TFF/BFF split like a coin flip.
    coin_flip = {"tff": 149, "bff": 151, "progressive": 65, "undetermined": 0}
    assert cc.classify(probe(field_order="progressive", idet=coin_flip)).class_name \
        == cc.LIVE_ACTION_CLEAN
    # If the container also says interlaced, the combination is believed.
    assert cc.classify(probe(field_order="tt", idet=coin_flip)).class_name == cc.INTERLACED


def test_field_order_is_used_when_idet_is_inconclusive():
    inconclusive = {"tff": 0, "bff": 0, "progressive": 0, "undetermined": 73}
    assert cc.classify(probe(field_order="tt", idet=inconclusive)).class_name == cc.INTERLACED
    assert (cc.classify(probe(field_order="progressive", idet=inconclusive)).class_name
            == cc.LIVE_ACTION_CLEAN)


def test_interlaced_can_opt_in_to_deinterlace_and_still_forces_denoise_off():
    p = probe(idet=INTERLACED_IDET)
    plan = cc.classify(p, deinterlace=True)
    assert plan.class_name == cc.INTERLACED and plan.codec == "av1"
    args = plan.video_args(4)
    assert "bwdif" in args[args.index("-vf") + 1]
    assert "film-grain-denoise=0" in args[args.index("-svtav1-params") + 1]


def test_context_cannot_override_the_probe_resolution():
    plan = cc.classify(probe(width=720, height=480), {"video_resolution": "4K"})
    assert plan.evidence["res"] == "sd"
    assert any("overruled_by_probe" in h for h in plan.evidence["hints"])


def test_context_resolution_is_used_only_when_the_probe_has_none():
    plan = cc.classify({"codec_name": "mpeg2video"}, {"video_resolution": "480p"})
    assert plan.evidence["res"] == "sd"
    assert "resolution_from_context" in plan.evidence["hints"]


def test_content_type_hint_only_tips_a_borderline_measurement():
    borderline = cc.ANIMATION_THRESHOLD - cc.HINT_NUDGE / 2
    assert cc.classify(probe(animation=borderline, grain=0.0),
                       {"content_type": "tv"}).class_name == cc.ANIMATION
    assert cc.classify(probe(animation=borderline, grain=0.0),
                       {"content_type": "movie"}).class_name == cc.LIVE_ACTION_CLEAN
    # A clear measurement is not moved by any hint.
    assert cc.classify(probe(animation=0.05, grain=0.0),
                       {"content_type": "tv"}).class_name == cc.LIVE_ACTION_CLEAN
    assert cc.classify(probe(animation=0.95, grain=0.0),
                       {"content_type": "movie"}).class_name == cc.ANIMATION


def test_measured_grain_overrules_an_animation_score():
    # Flat-looking but demonstrably noisy: not cel animation.
    plan = cc.classify(probe(animation=0.9, grain=0.7))
    assert plan.class_name == cc.LIVE_ACTION_GRAINY


# ---------------------------------------------------------------------------
# HDR
# ---------------------------------------------------------------------------

HDR_META = {
    "mastering_display": {
        "red": (0.68, 0.32), "green": (0.265, 0.69), "blue": (0.15, 0.06),
        "white_point": (0.3127, 0.329), "max_luminance": 1000.0, "min_luminance": 0.0001,
    },
    "content_light": {"max_content": 1000, "max_average": 400},
}


@pytest.mark.parametrize("transfer", ["smpte2084", "arib-std-b67"])
@pytest.mark.parametrize("grain", [0.0, 0.9])
@pytest.mark.parametrize("res", ["hd", "uhd"])
def test_hdr_forces_10_bit_and_suppresses_luma_bias(transfer, grain, res):
    p = probe(color_transfer=transfer, grain=grain, width=3840 if res == "uhd" else 1920,
              hdr_meta=HDR_META)
    plan = cc.classify(p)
    assert plan.class_name == cc.HDR and plan.hdr
    assert plan.pix_fmt == "yuv420p10le"
    args = plan.video_args(4)
    assert args[args.index("-pix_fmt") + 1] == "yuv420p10le"
    assert "luminance-qp-bias" not in " ".join(args)
    assert cc.validate_plan(plan) == []


def test_sdr_live_action_does_get_luma_bias():
    args = cc.classify(probe()).video_args(4)
    assert "luminance-qp-bias=25" in args[args.index("-svtav1-params") + 1]


def test_hdr_metadata_is_passed_explicitly_to_each_encoder():
    av1 = cc.classify(probe(color_transfer="smpte2084", hdr_meta=HDR_META)).video_args(4)
    s = av1[av1.index("-svtav1-params") + 1]
    assert "mastering-display=G(0.265,0.69)B(0.15,0.06)R(0.68,0.32)WP(0.3127,0.329)" in s
    assert "L(1000,0.0001)" in s and "content-light=1000,400" in s

    hevc = cc.classify(probe(color_transfer="smpte2084", grain=0.9, hdr_meta=HDR_META))
    assert hevc.codec == "hevc"
    x = hevc.video_args(4)
    s = x[x.index("-x265-params") + 1]
    assert "master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)" in s
    assert "L(10000000,1)" in s and "max-cll=1000,400" in s


def test_missing_hdr_metadata_is_omitted_not_invented():
    args = cc.classify(probe(color_transfer="smpte2084")).video_args(4)
    s = args[args.index("-svtav1-params") + 1]
    assert "mastering-display" not in s and "content-light" not in s


# ---------------------------------------------------------------------------
# Settings completeness
# ---------------------------------------------------------------------------

ENCODING_CLASSES = [c for c in cc.CLASSES if c not in (cc.PASSTHROUGH, cc.INTERLACED)]


@pytest.mark.parametrize("res", ["sd", "hd", "uhd"])
@pytest.mark.parametrize("name", cc.CLASSES)
def test_every_class_returns_a_complete_valid_settings_object(name, res):
    plan = cc.plan_for_class(name, res=res, hdr_meta=HDR_META)
    assert plan.class_name == name
    assert cc.validate_plan(plan) == []
    if plan.is_passthrough:
        assert plan.video_args(4) == []
        assert plan.reason
        return
    assert plan.codec in ("av1", "hevc")
    assert plan.crf is not None and plan.preset
    assert plan.pix_fmt == "yuv420p10le"
    args = plan.video_args(4)
    assert args[args.index("-pix_fmt") + 1] == "yuv420p10le"
    assert args[args.index("-crf") + 1] == str(plan.crf)
    assert args[args.index("-preset") + 1] == plan.preset


def test_av1_presets_are_never_slower_than_6_and_x265_is_never_slower_than_medium():
    for name in ENCODING_CLASSES:
        for res in ("sd", "hd", "uhd"):
            plan = cc.plan_for_class(name, res=res)
            if plan.codec == "av1":
                assert int(plan.preset) >= 6  # i3-7100T: preset 2-4 is not viable
            else:
                assert plan.preset in ("medium", "slow", "fast")


def test_unknown_class_name_yields_the_default_plan():
    assert cc.plan_for_class("no-such-class").class_name == cc.DEFAULT_CLASS


def test_class_table_matches_documented_settings():
    clean = cc.plan_for_class(cc.LIVE_ACTION_CLEAN, res="hd")
    assert (clean.codec, clean.crf, clean.preset) == ("av1", 24, "6")
    assert dict(clean.svt_required)["film-grain"] == "4"
    anim = cc.plan_for_class(cc.ANIMATION, res="hd")
    assert (anim.codec, anim.crf, anim.preset) == ("av1", 27, "6")
    assert dict(anim.svt_required)["film-grain"] == "0"
    grainy = cc.plan_for_class(cc.LIVE_ACTION_GRAINY, res="hd")
    assert (grainy.codec, grainy.crf, grainy.preset) == ("hevc", 21, "slow")


@pytest.mark.parametrize("optional", [True, False])
@pytest.mark.parametrize("res", ["sd", "hd", "uhd"])
def test_every_x265_plan_requires_anti_smoothing_params(res, optional):
    plans = [cc.plan_for_class(n, res=res, hdr_meta=HDR_META, grainy_hdr=True)
             for n in cc.CLASSES]
    hevc = [p for p in plans if p.codec == "hevc"]
    assert {p.class_name for p in hevc} >= {cc.LIVE_ACTION_GRAINY, cc.HDR}
    for plan in hevc:
        req = dict(plan.x265_required)
        assert req["no-sao"] == "1" and req["strong-intra-smoothing"] == "0"
        args = plan.video_args(4, optional=optional)  # optional=False is the fallback path
        params = args[args.index("-x265-params") + 1].split(":")
        assert "no-sao=1" in params and "strong-intra-smoothing=0" in params


@pytest.mark.parametrize("res", ["sd", "hd", "uhd"])
def test_no_plan_emits_av1_preset_8(res):
    plans = [cc.plan_for_class(n, res=res, hdr_meta=HDR_META) for n in cc.CLASSES]
    plans.append(cc.classify(probe(idet=INTERLACED_IDET, width=720 if res == "sd" else 1920),
                             deinterlace=True))
    assert any(p.codec == "av1" for p in plans)
    for p in plans:
        if p.codec == "av1":
            assert p.preset == "6"


def test_x265_grainy_preset_by_resolution():
    got = {r: cc.plan_for_class(cc.LIVE_ACTION_GRAINY, res=r).preset for r in ("sd", "hd", "uhd")}
    assert got == {"sd": "medium", "hd": "slow", "uhd": "fast"}


def test_uhd_av1_has_no_grain_synthesis_but_hd_does():
    assert dict(cc.plan_for_class(cc.LIVE_ACTION_CLEAN, res="uhd").svt_required)["film-grain"] == "0"
    assert dict(cc.plan_for_class(cc.LIVE_ACTION_CLEAN, res="hd").svt_required)["film-grain"] == "4"


def test_clean_crf_is_24_hd_and_23_sd():
    assert cc.plan_for_class(cc.LIVE_ACTION_CLEAN, res="hd").crf == 24
    assert cc.plan_for_class(cc.LIVE_ACTION_CLEAN, res="sd").crf == 23


@pytest.mark.parametrize("optional", [True, False])
@pytest.mark.parametrize("res", ["sd", "hd", "uhd"])
@pytest.mark.parametrize("name", list(cc.CLASSES))
def test_film_grain_denoise_zero_in_every_av1_param_string(name, res, optional):
    for plan in (cc.plan_for_class(name, res=res, hdr_meta=HDR_META),
                 cc.classify(probe(idet=INTERLACED_IDET, width=720 if res == "sd" else 1920),
                             deinterlace=True)):
        if plan.codec != "av1":
            continue
        args = plan.video_args(4, optional=optional)
        params = dict(kv.split("=", 1) for kv in args[args.index("-svtav1-params") + 1].split(":"))
        assert params["film-grain-denoise"] == "0"
        assert params["tune"] == "0"
        assert params["lp"] == "4"


def test_essential_params_are_a_subset_so_degrading_never_drops_denoise_or_grain():
    for name in ENCODING_CLASSES:
        plan = cc.plan_for_class(name, res="hd")
        if plan.codec == "av1":
            essential = dict(plan.svt_required)
            assert {"tune", "film-grain", "film-grain-denoise"} <= set(essential)


# ---------------------------------------------------------------------------
# Pure measurement helpers
# ---------------------------------------------------------------------------


def test_grain_helper_separates_flat_from_noisy_and_ignores_black():
    import random

    w = h = 64
    flat = bytes([128]) * (w * h)
    rng = random.Random(1)
    noisy = bytes(min(255, max(0, 128 + rng.randint(-6, 6))) for _ in range(w * h))
    assert cc.grain_of_gray(flat, w, h) == 0
    assert cc.normalise_grain(cc.grain_of_gray(noisy, w, h)) > cc.GRAIN_THRESHOLD
    assert cc.grain_of_gray(bytes(w * h), w, h) is None  # black frame says nothing


def test_animation_helper_scores_flat_few_colour_frames_above_textured_ones():
    import random

    w, h = cc.SMALL_W, cc.SMALL_H
    flat = bytearray()
    for y in range(h):
        flat += (bytes([200, 40, 40]) if y < h // 2 else bytes([40, 40, 200])) * w
    rng = random.Random(2)
    textured = bytes(rng.randint(30, 240) for _ in range(w * h * 3))
    assert cc.animation_of_rgb(bytes(flat), w, h) > cc.ANIMATION_THRESHOLD
    assert cc.animation_of_rgb(textured, w, h) < cc.ANIMATION_THRESHOLD
    assert cc.animation_of_rgb(bytes(w * h * 3), w, h) is None  # all black


def test_sample_times_are_spread_across_the_title_not_the_first_seconds():
    times = cc.sample_times(3600.0)
    assert len(times) >= 5 and times[0] > 300 and times[-1] < 3600 * 0.95
    assert times == sorted(times)
    assert cc.sample_times(None) == [0.0] and cc.sample_times(10.0) == [0.0]


def test_describe_logs_class_evidence_and_outcome():
    line = cc.classify(probe(grain=0.8)).describe()
    assert "class=live_action_grainy" in line and "grain=0.80" in line
    assert "x265 crf=21 preset=slow" in line


def test_module_imports_with_stdlib_only():
    src = (SERVICES / "codec_classifier.py").read_text()
    assert "import app" not in src and "from app" not in src
    assert "numpy" not in src


# ---------------------------------------------------------------------------
# av1_hook wiring (subprocess mocked)
# ---------------------------------------------------------------------------


class _Run:
    def __init__(self, calls, probe_json):
        self.calls, self.probe_json = calls, probe_json

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, json.dumps(self.probe_json(cmd)), "")
        if cmd[0] == "ffmpeg" and cmd[-1].endswith(".partial.mkv"):
            Path(cmd[-1]).write_bytes(b"x" * 10)
        return subprocess.CompletedProcess(cmd, 0, "", "")


def _streams(codec="av1", pix="yuv420p10le", audio=2, subs=1, transfer=None):
    v = {"index": 0, "codec_type": "video", "codec_name": codec, "pix_fmt": pix}
    if transfer:
        v["color_transfer"] = transfer
    out = [v] + [{"codec_type": "audio"}] * audio + [{"codec_type": "subtitle"}] * subs
    return {"streams": out}


@pytest.fixture
def hook(monkeypatch, tmp_path):
    av1_hook._encoder_accepts.cache_clear()
    monkeypatch.setenv("ENGRAM_AV1_HOOK", "1")
    monkeypatch.setenv("ENGRAM_AV1_STAGING", str(tmp_path / "stage"))
    monkeypatch.delenv("ENGRAM_CODEC_CLASSIFIER", raising=False)
    src = tmp_path / "Movie (2000).mkv"
    src.write_bytes(b"source")
    yield src, tmp_path
    av1_hook._encoder_accepts.cache_clear()


def _encode_cmds(calls):
    return [c for c in calls if c[0] == "ffmpeg" and c[-1].endswith(".partial.mkv")]


def test_kill_switch_reproduces_the_original_command_exactly(hook, monkeypatch):
    src, tmp = hook
    monkeypatch.setenv("ENGRAM_CODEC_CLASSIFIER", "0")
    monkeypatch.setenv("ENGRAM_AV1_CRF", "30")
    monkeypatch.setenv("ENGRAM_AV1_PRESET", "8")
    monkeypatch.setenv("ENGRAM_AV1_THREADS", "2")
    calls: list = []
    monkeypatch.setattr(av1_hook.subprocess, "run", _Run(calls, lambda c: _streams()))
    # The classifier must not even be consulted.
    monkeypatch.setattr(av1_hook, "_load_classifier",
                        lambda: pytest.fail("classifier loaded despite kill switch"))
    av1_hook.after_organize(str(src), context={"content_type": "tv"})
    (cmd,) = _encode_cmds(calls)
    staging = tmp / "stage"
    # Encoding arguments are the original ones; only the progress-reporting flags
    # (-nostdin -nostats -progress pipe:1) were added, which do not affect the output.
    assert cmd == [
        "ffmpeg", "-v", "error", "-nostdin", "-nostats", "-progress", "pipe:1", "-y",
        "-i", str(src),
        "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?",
        "-c:v", "libsvtav1", "-crf", "30", "-preset", "8",
        "-svtav1-params", "tune=0:lp=2",
        "-c:a", "copy", "-c:s", "copy",
        str(staging / ".Movie (2000).partial.mkv"),
    ]
    assert not any(c[0] == "ffmpeg" and "testsrc" in " ".join(c) for c in calls)


def test_default_fixed_settings_are_unchanged(hook, monkeypatch):
    src, tmp = hook
    monkeypatch.setenv("ENGRAM_CODEC_CLASSIFIER", "0")
    calls: list = []
    monkeypatch.setattr(av1_hook.subprocess, "run", _Run(calls, lambda c: _streams()))
    av1_hook.after_organize(str(src))
    (cmd,) = _encode_cmds(calls)
    assert cmd[cmd.index("-crf") + 1] == "22" and cmd[cmd.index("-preset") + 1] == "6"
    assert cmd[cmd.index("-svtav1-params") + 1] == "tune=0:lp=4"
    assert "-pix_fmt" not in cmd


class _FakeClassifier:
    """Stands in for the classifier module so the hook can be tested in isolation."""

    def __init__(self, plan):
        self._plan = plan

    def plan_for_file(self, path, context=None, *, deinterlace=False):
        self.seen = (path, context, deinterlace)
        return self._plan


def test_classifier_plan_drives_the_command_and_context_is_forwarded(hook, monkeypatch):
    src, tmp = hook
    plan = cc.plan_for_class(cc.ANIMATION, res="sd")
    fake = _FakeClassifier(plan)
    monkeypatch.setattr(av1_hook, "_load_classifier", lambda: fake)
    calls: list = []
    monkeypatch.setattr(av1_hook.subprocess, "run", _Run(calls, lambda c: _streams()))
    ctx = {"content_type": "tv", "is_extra": False}
    av1_hook.after_organize(str(src), context=ctx)
    assert fake.seen[1] == ctx
    (cmd,) = _encode_cmds(calls)
    assert cmd[cmd.index("-crf") + 1] == "27"
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p10le"
    assert "film-grain-denoise=0" in cmd[cmd.index("-svtav1-params") + 1]
    # Track preservation is untouched by the classifier.
    assert cmd[cmd.index("-map"):cmd.index("-map") + 6] == [
        "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?"]
    assert cmd[-5:-1] == ["-c:a", "copy", "-c:s", "copy"]
    assert (tmp / "stage" / src.name).exists()
    marker = json.loads((src.parent / (src.name + av1_hook.MARKER_SUFFIX)).read_text())
    assert marker["codec_class"] == cc.ANIMATION


def test_hevc_plan_is_verified_as_hevc(hook, monkeypatch):
    src, tmp = hook
    monkeypatch.setattr(av1_hook, "_load_classifier",
                        lambda: _FakeClassifier(cc.plan_for_class(cc.LIVE_ACTION_GRAINY)))
    calls: list = []
    monkeypatch.setattr(av1_hook.subprocess, "run",
                        _Run(calls, lambda c: _streams(codec="hevc")))
    av1_hook.after_organize(str(src))
    (cmd,) = _encode_cmds(calls)
    assert cmd[cmd.index("-c:v") + 1] == "libx265"
    assert (tmp / "stage" / src.name).exists()


def test_wrong_codec_or_pixfmt_aborts_the_swap(hook, monkeypatch):
    src, tmp = hook
    monkeypatch.setattr(av1_hook, "_load_classifier",
                        lambda: _FakeClassifier(cc.plan_for_class(cc.LIVE_ACTION_CLEAN)))
    for streams in (_streams(codec="hevc"), _streams(pix="yuv420p")):
        calls: list = []
        monkeypatch.setattr(av1_hook.subprocess, "run", _Run(calls, lambda c, s=streams: s))
        av1_hook.after_organize(str(src))
        assert not (tmp / "stage" / src.name).exists()
        assert not list((tmp / "stage").glob("*.partial.mkv"))
        assert src.read_bytes() == b"source"


def test_track_count_mismatch_still_aborts_the_swap(hook, monkeypatch):
    src, tmp = hook
    monkeypatch.setattr(av1_hook, "_load_classifier",
                        lambda: _FakeClassifier(cc.plan_for_class(cc.LIVE_ACTION_CLEAN)))
    probes = iter([_streams(audio=2), _streams(audio=1)])  # source, then output
    calls: list = []

    def run(cmd, **kw):
        if cmd[0] == "ffprobe":
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, json.dumps(next(probes)), "")
        return _Run([], None)(cmd, **kw)

    monkeypatch.setattr(av1_hook.subprocess, "run", run)
    av1_hook.after_organize(str(src))
    assert not (tmp / "stage" / src.name).exists()
    assert src.read_bytes() == b"source"


def test_hdr_transfer_loss_aborts_the_swap(hook, monkeypatch):
    src, tmp = hook
    plan = cc.plan_for_class(cc.HDR)
    monkeypatch.setattr(av1_hook, "_load_classifier", lambda: _FakeClassifier(plan))
    probes = iter([_streams(transfer="smpte2084"), _streams()])
    calls: list = []

    def run(cmd, **kw):
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, json.dumps(next(probes)), "")
        return _Run(calls, None)(cmd, **kw)

    monkeypatch.setattr(av1_hook.subprocess, "run", run)
    av1_hook.after_organize(str(src))
    assert not (tmp / "stage" / src.name).exists()


def test_passthrough_plan_encodes_nothing_and_writes_an_idempotent_marker(hook, monkeypatch):
    src, tmp = hook
    plan = cc.plan_for_class(cc.INTERLACED, reason="interlaced")
    monkeypatch.setattr(av1_hook, "_load_classifier", lambda: _FakeClassifier(plan))
    calls: list = []
    monkeypatch.setattr(av1_hook.subprocess, "run", _Run(calls, lambda c: _streams()))
    av1_hook.after_organize(str(src))
    assert _encode_cmds(calls) == []
    assert not (tmp / "stage").exists() or not any((tmp / "stage").iterdir())
    marker = json.loads((src.parent / (src.name + av1_hook.MARKER_SUFFIX)).read_text())
    assert marker["av1"] is None and marker["codec_class"] == cc.INTERLACED
    # Second call is a no-op (marker matches size+mtime).
    monkeypatch.setattr(av1_hook, "_load_classifier",
                        lambda: pytest.fail("re-classified an already-handled file"))
    av1_hook.after_organize(str(src))


def test_classifier_exception_falls_back_to_fixed_settings(hook, monkeypatch):
    src, tmp = hook

    class Boom:
        def plan_for_file(self, *a, **k):
            raise RuntimeError("probe exploded")

    monkeypatch.setattr(av1_hook, "_load_classifier", lambda: Boom())
    calls: list = []
    monkeypatch.setattr(av1_hook.subprocess, "run", _Run(calls, lambda c: _streams()))
    av1_hook.after_organize(str(src))
    (cmd,) = _encode_cmds(calls)
    assert cmd[cmd.index("-svtav1-params") + 1] == "tune=0:lp=4"
    assert (tmp / "stage" / src.name).exists()


def test_unloadable_classifier_falls_back_to_fixed_settings(hook, monkeypatch):
    src, tmp = hook
    monkeypatch.setattr(av1_hook, "_load_classifier", lambda: None)
    calls: list = []
    monkeypatch.setattr(av1_hook.subprocess, "run", _Run(calls, lambda c: _streams()))
    av1_hook.after_organize(str(src))
    (cmd,) = _encode_cmds(calls)
    assert cmd[cmd.index("-svtav1-params") + 1] == "tune=0:lp=4"


def test_encoder_rejecting_extras_degrades_to_essential_params_then_to_fixed(hook, monkeypatch):
    src, tmp = hook
    plan = cc.plan_for_class(cc.LIVE_ACTION_CLEAN)
    monkeypatch.setattr(av1_hook, "_load_classifier", lambda: _FakeClassifier(plan))

    def run_factory(accept):
        calls: list = []
        base = _Run(calls, lambda c: _streams())

        def run(cmd, **kw):
            if cmd[0] == "ffmpeg" and "testsrc=size=128x128" in " ".join(cmd):
                calls.append(list(cmd))
                return subprocess.CompletedProcess(cmd, 0 if accept(cmd) else 1, "", "bad param")
            return base(cmd, **kw)

        return calls, run

    # Old encoder: knows only the essential parameters.
    calls, run = run_factory(lambda c: "variance-boost" not in " ".join(c))
    monkeypatch.setattr(av1_hook.subprocess, "run", run)
    av1_hook.after_organize(str(src))
    (cmd,) = _encode_cmds(calls)
    p = cmd[cmd.index("-svtav1-params") + 1]
    assert "film-grain-denoise=0" in p and "variance-boost" not in p

    # Encoder rejects everything the plan emits: fixed legacy command.
    (tmp / "stage" / src.name).unlink()
    (src.parent / (src.name + av1_hook.MARKER_SUFFIX)).unlink()
    av1_hook._encoder_accepts.cache_clear()
    calls, run = run_factory(lambda c: "film-grain" not in " ".join(c))
    monkeypatch.setattr(av1_hook.subprocess, "run", run)
    av1_hook.after_organize(str(src))
    (cmd,) = _encode_cmds(calls)
    assert cmd[cmd.index("-svtav1-params") + 1] == "tune=0:lp=4"


def test_after_organize_context_is_optional_and_keyword_only():
    import inspect

    sig = inspect.signature(av1_hook.after_organize)
    p = sig.parameters["context"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is None
    with pytest.raises(TypeError):
        av1_hook.after_organize("/x.mkv", {"content_type": "tv"})  # positional is rejected


def test_hook_disabled_does_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv("ENGRAM_AV1_HOOK", raising=False)
    src = tmp_path / "a.mkv"
    src.write_bytes(b"x")
    monkeypatch.setattr(av1_hook.subprocess, "run",
                        lambda *a, **k: pytest.fail("ran a subprocess while disabled"))
    av1_hook.after_organize(str(src), context={"content_type": "tv"})


def test_load_classifier_works_when_imported_directly():
    # av1_hook was imported by path above, with no app.services in sight.
    assert av1_hook._load_classifier() is not None


# ---------------------------------------------------------------------------
# Real ffmpeg: every emitted command parses and yields the intended codec/pix_fmt.
# Synthetic sources only; nothing here touches disc content.
# ---------------------------------------------------------------------------


def _ffprobe_video(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-print_format", "json",
         "-show_entries", "stream=codec_name,pix_fmt,color_transfer,color_primaries,field_order",
         str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)["streams"][0]


def _make_source(path: Path, lavfi: str, extra: list[str] | None = None, seconds=1) -> Path:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", lavfi, "-t", str(seconds),
         *(extra or ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "12"]), str(path)],
        check=True)
    return path


@pytest.fixture(scope="module")
def synth(tmp_path_factory):
    d = tmp_path_factory.mktemp("synth")
    return {
        "bars": _make_source(d / "bars.mkv", "smptebars=size=320x240:rate=24"),
        "test": _make_source(d / "test.mkv", "testsrc=size=320x240:rate=24"),
    }


@needs_ffmpeg
@pytest.mark.parametrize("res", ["sd", "hd"])
@pytest.mark.parametrize("name", ENCODING_CLASSES)
def test_emitted_command_encodes_to_the_intended_codec_and_pix_fmt(name, res, synth, tmp_path):
    plan = cc.plan_for_class(name, res=res, hdr_meta=HDR_META)
    # The plan's fast preset is for real titles; swap in the fastest so the test is quick,
    # but keep every other emitted parameter exactly as the classifier produced it.
    args = plan.video_args(2)
    args[args.index("-preset") + 1] = "12" if plan.codec == "av1" else "ultrafast"
    out = tmp_path / "out.mkv"
    run = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(synth["test"]), "-map", "0:v:0", *args,
         str(out)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    v = _ffprobe_video(out)
    assert v["codec_name"] == plan.output_codec_name
    assert v["pix_fmt"] == "yuv420p10le"


@needs_ffmpeg
def test_deinterlace_command_parses_and_encodes_av1_10_bit(synth, tmp_path):
    plan = cc.classify(probe(idet=INTERLACED_IDET), deinterlace=True)
    args = plan.video_args(2)
    args[args.index("-preset") + 1] = "12"
    out = tmp_path / "d.mkv"
    run = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(synth["test"]), *args,
                          str(out)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    v = _ffprobe_video(out)
    assert (v["codec_name"], v["pix_fmt"]) == ("av1", "yuv420p10le")


@needs_ffmpeg
def test_hdr_tags_survive_an_encode_from_a_pq_source(tmp_path):
    src = _make_source(
        tmp_path / "pq.mkv", "testsrc=size=320x240:rate=24",
        ["-vf", "setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc",
         "-c:v", "libx265", "-pix_fmt", "yuv420p10le", "-preset", "ultrafast",
         "-x265-params",
         "log-level=error:master-display=G(13250,34500)B(7500,3000)R(34000,16000)"
         "WP(15635,16450)L(10000000,1):max-cll=1000,400"])
    meta = cc.probe_media(str(src))
    assert meta["color_transfer"] == "smpte2084"
    assert meta.get("hdr_meta"), "mastering/content-light metadata should be read from the source"
    assert meta["hdr_meta"]["content_light"] == {"max_content": 1000, "max_average": 400}
    plan = cc.classify(meta)
    assert plan.class_name == cc.HDR
    args = plan.video_args(2)
    args[args.index("-preset") + 1] = "12"
    out = tmp_path / "pq-out.mkv"
    run = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", "0:v:0", *args,
                          str(out)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    v = _ffprobe_video(out)
    assert (v["codec_name"], v["pix_fmt"]) == ("av1", "yuv420p10le")
    assert (v["color_transfer"], v["color_primaries"]) == ("smpte2084", "bt2020")


@needs_ffmpeg
def test_probe_media_on_a_synthetic_file_is_complete_and_classifies(synth):
    p = cc.probe_media(str(synth["bars"]))
    assert p["width"] == 320 and p["height"] == 240 and p["codec_name"] == "h264"
    assert p["fps"] == 24.0
    plan = cc.plan_for_file(str(synth["bars"]))
    assert plan.class_name in cc.CLASSES and cc.validate_plan(plan) == []


@needs_ffmpeg
def test_media_probes_classify_real_synthetic_files(tmp_path):
    """End to end: probe real files and land in the expected class, with no context at all."""
    noisy = _make_source(tmp_path / "noisy.mkv", "gradients=size=320x240:rate=24:speed=0.02",
                         ["-vf", "noise=alls=8:allf=t+u,format=yuv420p", "-c:v", "libx264",
                          "-crf", "10"], seconds=2)
    pattern = _make_source(tmp_path / "pattern.mkv", "smptebars=size=320x240:rate=24", seconds=2)
    combed = _make_source(
        tmp_path / "combed.mkv", "testsrc2=size=320x240:rate=60",
        ["-vf", "interlace=scan=tff,format=yuv420p", "-c:v", "mpeg2video",
         "-flags", "+ildct+ilme", "-top", "1", "-b:v", "4M"], seconds=3)
    assert cc.plan_for_file(str(noisy)).class_name == cc.LIVE_ACTION_GRAINY
    assert cc.plan_for_file(str(pattern)).class_name == cc.ANIMATION
    plan = cc.plan_for_file(str(combed), {"content_type": "tv"})  # tv hint leans animation
    assert plan.class_name == cc.INTERLACED and plan.evidence["interlaced"] == "yes"
    # Context that contradicts the file does not win: a "movie" hint on flat pattern art.
    assert cc.plan_for_file(str(pattern), {"content_type": "movie"}).class_name == cc.ANIMATION


@needs_ffmpeg
def test_full_hook_path_with_real_ffmpeg_on_a_synthetic_file(tmp_path, monkeypatch):
    src = _make_source(tmp_path / "Show - S01E01.mkv", "testsrc=size=320x240:rate=24",
                       seconds=2)
    # Add an audio + subtitle track so track preservation is exercised for real.
    withtracks = tmp_path / "with.mkv"
    sub = tmp_path / "s.srt"
    sub.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-f", "lavfi", "-i",
                    "sine=d=2", "-i", str(sub), "-map", "0:v", "-map", "1:a", "-map", "2:s",
                    "-c:v", "copy", "-c:a", "aac", "-c:s", "srt", str(withtracks)], check=True)
    withtracks.replace(src)
    monkeypatch.setenv("ENGRAM_AV1_HOOK", "1")
    monkeypatch.setenv("ENGRAM_AV1_STAGING", str(tmp_path / "stage"))
    monkeypatch.setenv("ENGRAM_AV1_THREADS", "2")
    # Force a fast preset on whatever class the media lands in; settings otherwise intact.
    real = av1_hook._load_classifier()
    orig = real.plan_for_file

    def fast(path, context=None, **kw):
        plan = orig(path, context, **kw)
        if plan.codec == "av1":
            plan.preset = "12"
        elif plan.codec == "hevc":
            plan.preset = "ultrafast"
        return plan

    monkeypatch.setattr(real, "plan_for_file", fast)
    av1_hook._encoder_accepts.cache_clear()
    av1_hook.after_organize(str(src), context={"content_type": "tv", "is_extra": False})
    out = tmp_path / "stage" / src.name
    assert out.exists()
    probe_out = av1_hook._probe(out)
    src_counts = av1_hook._track_counts(av1_hook._probe(src))
    assert av1_hook._track_counts(probe_out) == src_counts == (1, 1)
    assert (src.parent / (src.name + av1_hook.MARKER_SUFFIX)).exists()
