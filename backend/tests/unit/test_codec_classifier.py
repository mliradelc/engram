"""Tests for the content-aware codec classifier and its wiring into the encoding hook.

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
encode_hook = _load("encode_hook")

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
def test_unknown_or_malformed_probe_deinterlaces_and_never_raises(bad):
    # Nothing tells interlacing apart, so the source is treated as interlaced:
    # bwdif on progressive is near-lossless, combing in an encode is permanent.
    plan = cc.classify(bad, None)
    assert plan.class_name == cc.INTERLACED
    assert plan.codec == "av1"
    assert plan.video_filters == [cc.DEINTERLACE_FILTER]
    assert plan.evidence["interlaced"] == "unknown"
    assert cc.validate_plan(plan) == []


@pytest.mark.parametrize("ctx", [None, {}, "x", {"is_extra": "maybe", "duration_seconds": "n/a",
                                                 "content_type": 7}])
def test_malformed_context_never_raises(ctx):
    assert cc.classify(probe(), ctx).class_name == cc.LIVE_ACTION_CLEAN


def test_plan_for_file_on_a_missing_file_returns_the_default():
    plan = cc.plan_for_file("/nonexistent/definitely-not-here.mkv", {"content_type": "tv"})
    # An unprobeable file is undecided, which deinterlaces.
    assert plan.class_name == cc.INTERLACED
    assert plan.video_filters == [cc.DEINTERLACE_FILTER]


# ---------------------------------------------------------------------------
# Media probes win over identification context
# ---------------------------------------------------------------------------


def test_interlaced_detection_beats_an_animation_hint():
    p = probe(idet=INTERLACED_IDET, field_order="tt", animation=0.95, grain=0.0)
    plan = cc.classify(p, {"content_type": "tv", "is_extra": False})
    assert plan.class_name == cc.INTERLACED
    assert not plan.is_passthrough
    assert plan.video_filters == [cc.DEINTERLACE_FILTER]


def test_interlaced_beats_grain_and_keeps_hdr_with_bwdif():
    assert cc.classify(probe(idet=INTERLACED_IDET, grain=0.9)).class_name == cc.INTERLACED
    # Deinterlacing never costs an HDR source its HDR handling.
    plan = cc.classify(probe(idet=INTERLACED_IDET, color_transfer="smpte2084", grain=0.1))
    assert plan.class_name == cc.HDR and plan.hdr
    assert plan.video_filters == [cc.DEINTERLACE_FILTER]
    assert cc.validate_plan(plan) == []


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


def test_interlaced_source_is_deinterlaced_with_no_flag_and_forces_denoise_off(monkeypatch):
    # The old opt-in variable must not matter, either way.
    monkeypatch.delenv("ENGRAM_CODEC_INTERLACED", raising=False)
    p = probe(idet=INTERLACED_IDET)
    plan = cc.classify(p)
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
    plans.append(cc.classify(probe(idet=INTERLACED_IDET, width=720 if res == "sd" else 1920)))
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
                 cc.classify(probe(idet=INTERLACED_IDET, width=720 if res == "sd" else 1920))):
        if plan.codec != "av1":
            continue
        args = plan.video_args(4, optional=optional)
        params = dict(kv.split("=", 1) for kv in args[args.index("-svtav1-params") + 1].split(":"))
        assert params["film-grain-denoise"] == "0"
        assert params["tune"] == "0"
        assert params["lp"] == "4"


# ---------------------------------------------------------------------------
# Bit depth follows the source ("preserve bit")
# ---------------------------------------------------------------------------

SOURCE_PIX_FMTS = {  # source pix_fmt -> expected output pix_fmt
    "yuv420p": "yuv420p",
    "yuvj420p": "yuv420p",
    "nv12": "yuv420p",
    "yuv422p": "yuv420p",
    "yuv420p10le": "yuv420p10le",
    "yuv422p10le": "yuv420p10le",
    "p010le": "yuv420p10le",
    "yuv420p12le": "yuv420p10le",  # clamped: SVT-AV1 has no 12-bit profile
    None: "yuv420p10le",  # unknown depth: 10-bit cannot truncate
}


def test_10_bit_source_plans_10_bit_and_8_bit_source_plans_8_bit():
    for name in (cc.ANIMATION, cc.LIVE_ACTION_GRAINY, cc.LIVE_ACTION_CLEAN):
        ten = cc.plan_for_class(name, res="hd", source_pix_fmt="yuv420p10le")
        eight = cc.plan_for_class(name, res="hd", source_pix_fmt="yuv420p")
        assert ten.pix_fmt == "yuv420p10le"
        assert eight.pix_fmt == "yuv420p"
        for plan, want in ((ten, "yuv420p10le"), (eight, "yuv420p")):
            args = plan.video_args(4)
            assert args[args.index("-pix_fmt") + 1] == want
            assert cc.validate_plan(plan) == []


@pytest.mark.parametrize("src_fmt, want", list(SOURCE_PIX_FMTS.items()))
@pytest.mark.parametrize("name", list(CLASS_PROBES))
def test_classify_derives_pix_fmt_from_the_probe(name, src_fmt, want):
    p, ctx = CLASS_PROBES[name]
    plan = cc.classify({**p, "pix_fmt": src_fmt}, ctx)
    if plan.is_passthrough:
        return
    expected = "yuv420p10le" if plan.hdr else want
    assert plan.pix_fmt == expected
    assert cc.validate_plan(plan) == []


@pytest.mark.parametrize("src_fmt", [f for f in SOURCE_PIX_FMTS if f])
@pytest.mark.parametrize("name", ENCODING_CLASSES)
def test_no_class_ever_emits_a_pix_fmt_shallower_than_its_source(name, src_fmt):
    plans = [cc.plan_for_class(name, res="hd", hdr_meta=HDR_META, source_pix_fmt=src_fmt),
             cc.classify(probe(idet=INTERLACED_IDET, pix_fmt=src_fmt))]
    for plan in plans:
        out = cc.source_bit_depth(plan.pix_fmt)
        assert out >= min(cc.source_bit_depth(src_fmt), 10)
        assert plan.pix_fmt.startswith("yuv420p")  # 4:2:0 kept
        assert cc.validate_plan(plan) == []


def test_validate_plan_rejects_a_shallower_output(monkeypatch):
    plan = cc.plan_for_class(cc.LIVE_ACTION_CLEAN, source_pix_fmt="yuv420p10le")
    assert cc.validate_plan(plan) == []
    # Simulate the regression this guards: a fixed 8-bit output whatever the source.
    monkeypatch.setattr(cc, "output_pix_fmt", lambda src, hdr=False: "yuv420p")
    assert "output is shallower than the source" in cc.validate_plan(plan)


def test_hdr_is_10_bit_even_from_a_mislabelled_8_bit_source():
    assert cc.output_pix_fmt("yuv420p", hdr=True) == "yuv420p10le"


def test_legacy_video_args_still_have_no_pix_fmt():
    assert "-pix_fmt" not in encode_hook._legacy_video_args("4")


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
# encode_hook wiring (subprocess mocked)
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
    encode_hook._encoder_accepts.cache_clear()
    monkeypatch.setenv("ENGRAM_ENCODE_HOOK", "1")
    monkeypatch.setenv("ENGRAM_ENCODE_STAGING", str(tmp_path / "stage"))
    monkeypatch.delenv("ENGRAM_CODEC_CLASSIFIER", raising=False)
    # The fixture source stands in for a rip in Engram's staging root; the hook
    # refuses anything outside ``_DOMAIN_ROOTS``.
    monkeypatch.setattr(encode_hook, "_DOMAIN_ROOTS", (str(tmp_path),))
    src = tmp_path / "Movie (2000).mkv"
    src.write_bytes(b"source")
    yield src, tmp_path
    encode_hook._encoder_accepts.cache_clear()


def _encode_cmds(calls):
    return [c for c in calls if c[0] == "ffmpeg" and c[-1].endswith(".partial.mkv")]


def test_kill_switch_reproduces_the_original_command_exactly(hook, monkeypatch):
    src, tmp = hook
    monkeypatch.setenv("ENGRAM_CODEC_CLASSIFIER", "0")
    monkeypatch.setenv("ENGRAM_ENCODE_CRF", "30")
    monkeypatch.setenv("ENGRAM_ENCODE_PRESET", "8")
    monkeypatch.setenv("ENGRAM_ENCODE_THREADS", "2")
    calls: list = []
    monkeypatch.setattr(encode_hook.subprocess, "run", _Run(calls, lambda c: _streams()))
    # The classifier must not even be consulted.
    monkeypatch.setattr(encode_hook, "_load_classifier",
                        lambda: pytest.fail("classifier loaded despite kill switch"))
    encode_hook.after_organize(str(src), context={"content_type": "tv"})
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
    monkeypatch.setattr(encode_hook.subprocess, "run", _Run(calls, lambda c: _streams()))
    encode_hook.after_organize(str(src))
    (cmd,) = _encode_cmds(calls)
    assert cmd[cmd.index("-crf") + 1] == "22" and cmd[cmd.index("-preset") + 1] == "6"
    assert cmd[cmd.index("-svtav1-params") + 1] == "tune=0:lp=4"
    assert "-pix_fmt" not in cmd


class _FakeClassifier:
    """Stands in for the classifier module so the hook can be tested in isolation."""

    def __init__(self, plan):
        self._plan = plan

    def plan_for_file(self, path, context=None):
        self.seen = (path, context)
        return self._plan


def test_classifier_plan_drives_the_command_and_context_is_forwarded(hook, monkeypatch):
    src, tmp = hook
    plan = cc.plan_for_class(cc.ANIMATION, res="sd")
    fake = _FakeClassifier(plan)
    monkeypatch.setattr(encode_hook, "_load_classifier", lambda: fake)
    calls: list = []
    monkeypatch.setattr(encode_hook.subprocess, "run", _Run(calls, lambda c: _streams()))
    ctx = {"content_type": "tv", "is_extra": False}
    encode_hook.after_organize(str(src), context=ctx)
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
    marker = json.loads((src.parent / (src.name + encode_hook.MARKER_SUFFIX)).read_text())
    assert marker["codec_class"] == cc.ANIMATION


def test_hevc_plan_is_verified_as_hevc(hook, monkeypatch):
    src, tmp = hook
    monkeypatch.setattr(encode_hook, "_load_classifier",
                        lambda: _FakeClassifier(cc.plan_for_class(cc.LIVE_ACTION_GRAINY)))
    calls: list = []
    monkeypatch.setattr(encode_hook.subprocess, "run",
                        _Run(calls, lambda c: _streams(codec="hevc")))
    encode_hook.after_organize(str(src))
    (cmd,) = _encode_cmds(calls)
    assert cmd[cmd.index("-c:v") + 1] == "libx265"
    assert (tmp / "stage" / src.name).exists()


def test_wrong_codec_or_pixfmt_aborts_the_swap(hook, monkeypatch):
    src, tmp = hook
    monkeypatch.setattr(encode_hook, "_load_classifier",
                        lambda: _FakeClassifier(cc.plan_for_class(cc.LIVE_ACTION_CLEAN)))
    for streams in (_streams(codec="hevc"), _streams(pix="yuv420p")):
        calls: list = []
        monkeypatch.setattr(encode_hook.subprocess, "run", _Run(calls, lambda c, s=streams: s))
        encode_hook.after_organize(str(src))
        assert not (tmp / "stage" / src.name).exists()
        assert not list((tmp / "stage").glob("*.partial.mkv"))
        assert src.read_bytes() == b"source"


def test_track_count_mismatch_still_aborts_the_swap(hook, monkeypatch):
    src, tmp = hook
    monkeypatch.setattr(encode_hook, "_load_classifier",
                        lambda: _FakeClassifier(cc.plan_for_class(cc.LIVE_ACTION_CLEAN)))
    probes = iter([_streams(audio=2), _streams(audio=1)])  # source, then output
    calls: list = []

    def run(cmd, **kw):
        if cmd[0] == "ffprobe":
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, json.dumps(next(probes)), "")
        return _Run([], None)(cmd, **kw)

    monkeypatch.setattr(encode_hook.subprocess, "run", run)
    encode_hook.after_organize(str(src))
    assert not (tmp / "stage" / src.name).exists()
    assert src.read_bytes() == b"source"


def test_hdr_transfer_loss_aborts_the_swap(hook, monkeypatch):
    src, tmp = hook
    plan = cc.plan_for_class(cc.HDR)
    monkeypatch.setattr(encode_hook, "_load_classifier", lambda: _FakeClassifier(plan))
    probes = iter([_streams(transfer="smpte2084"), _streams()])
    calls: list = []

    def run(cmd, **kw):
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, json.dumps(next(probes)), "")
        return _Run(calls, None)(cmd, **kw)

    monkeypatch.setattr(encode_hook.subprocess, "run", run)
    encode_hook.after_organize(str(src))
    assert not (tmp / "stage" / src.name).exists()


def test_passthrough_plan_encodes_nothing_and_writes_an_idempotent_marker(hook, monkeypatch):
    src, tmp = hook
    plan = cc.plan_for_class(cc.PASSTHROUGH, reason="source already AV1")
    monkeypatch.setattr(encode_hook, "_load_classifier", lambda: _FakeClassifier(plan))
    calls: list = []
    monkeypatch.setattr(encode_hook.subprocess, "run", _Run(calls, lambda c: _streams()))
    encode_hook.after_organize(str(src))
    assert _encode_cmds(calls) == []
    assert not (tmp / "stage").exists() or not any((tmp / "stage").iterdir())
    marker = json.loads((src.parent / (src.name + encode_hook.MARKER_SUFFIX)).read_text())
    assert marker["av1"] is None and marker["codec_class"] == cc.PASSTHROUGH
    # Second call is a no-op (marker matches size+mtime).
    monkeypatch.setattr(encode_hook, "_load_classifier",
                        lambda: pytest.fail("re-classified an already-handled file"))
    encode_hook.after_organize(str(src))


def test_classifier_exception_falls_back_to_fixed_settings(hook, monkeypatch):
    src, tmp = hook

    class Boom:
        def plan_for_file(self, *a, **k):
            raise RuntimeError("probe exploded")

    monkeypatch.setattr(encode_hook, "_load_classifier", lambda: Boom())
    calls: list = []
    monkeypatch.setattr(encode_hook.subprocess, "run", _Run(calls, lambda c: _streams()))
    encode_hook.after_organize(str(src))
    (cmd,) = _encode_cmds(calls)
    assert cmd[cmd.index("-svtav1-params") + 1] == "tune=0:lp=4"
    assert (tmp / "stage" / src.name).exists()


def test_unloadable_classifier_falls_back_to_fixed_settings(hook, monkeypatch):
    src, tmp = hook
    monkeypatch.setattr(encode_hook, "_load_classifier", lambda: None)
    calls: list = []
    monkeypatch.setattr(encode_hook.subprocess, "run", _Run(calls, lambda c: _streams()))
    encode_hook.after_organize(str(src))
    (cmd,) = _encode_cmds(calls)
    assert cmd[cmd.index("-svtav1-params") + 1] == "tune=0:lp=4"


def test_encoder_rejecting_extras_degrades_to_essential_params_then_to_fixed(hook, monkeypatch):
    src, tmp = hook
    plan = cc.plan_for_class(cc.LIVE_ACTION_CLEAN)
    monkeypatch.setattr(encode_hook, "_load_classifier", lambda: _FakeClassifier(plan))

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
    monkeypatch.setattr(encode_hook.subprocess, "run", run)
    encode_hook.after_organize(str(src))
    (cmd,) = _encode_cmds(calls)
    p = cmd[cmd.index("-svtav1-params") + 1]
    assert "film-grain-denoise=0" in p and "variance-boost" not in p

    # Encoder rejects everything the plan emits: fixed legacy command.
    (tmp / "stage" / src.name).unlink()
    (src.parent / (src.name + encode_hook.MARKER_SUFFIX)).unlink()
    encode_hook._encoder_accepts.cache_clear()
    calls, run = run_factory(lambda c: "film-grain" not in " ".join(c))
    monkeypatch.setattr(encode_hook.subprocess, "run", run)
    encode_hook.after_organize(str(src))
    (cmd,) = _encode_cmds(calls)
    assert cmd[cmd.index("-svtav1-params") + 1] == "tune=0:lp=4"


def test_after_organize_context_is_optional_and_keyword_only():
    import inspect

    sig = inspect.signature(encode_hook.after_organize)
    p = sig.parameters["context"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is None
    with pytest.raises(TypeError):
        encode_hook.after_organize("/x.mkv", {"content_type": "tv"})  # positional is rejected


def test_hook_disabled_does_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv("ENGRAM_ENCODE_HOOK", raising=False)
    src = tmp_path / "a.mkv"
    src.write_bytes(b"x")
    monkeypatch.setattr(encode_hook.subprocess, "run",
                        lambda *a, **k: pytest.fail("ran a subprocess while disabled"))
    encode_hook.after_organize(str(src), context={"content_type": "tv"})


def test_load_classifier_works_when_imported_directly():
    # encode_hook was imported by path above, with no app.services in sight.
    assert encode_hook._load_classifier() is not None


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
    plan = cc.classify(probe(idet=INTERLACED_IDET))
    args = plan.video_args(2)
    args[args.index("-preset") + 1] = "12"
    out = tmp_path / "d.mkv"
    run = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(synth["test"]), *args,
                          str(out)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    v = _ffprobe_video(out)
    assert (v["codec_name"], v["pix_fmt"]) == ("av1", "yuv420p10le")


@needs_ffmpeg
@pytest.mark.parametrize("src_fmt", ["yuv420p", "yuv420p10le"])
@pytest.mark.parametrize("name", [cc.ANIMATION, cc.LIVE_ACTION_GRAINY])
def test_real_encode_preserves_the_source_bit_depth(name, src_fmt, tmp_path):
    src = _make_source(tmp_path / f"src-{src_fmt}.mkv", "testsrc=size=320x240:rate=24",
                       ["-c:v", "ffv1", "-pix_fmt", src_fmt])
    meta = cc._probe_stream(str(src))
    assert meta["pix_fmt"] == src_fmt
    plan = cc.plan_for_class(name, res="sd", source_pix_fmt=meta["pix_fmt"])
    args = plan.video_args(2)
    args[args.index("-preset") + 1] = "12" if plan.codec == "av1" else "ultrafast"
    out = tmp_path / "out.mkv"
    run = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", "0:v:0",
                          *args, str(out)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert _ffprobe_video(out)["pix_fmt"] == src_fmt


@needs_ffmpeg
def test_forced_class_on_a_real_combed_source_gets_bwdif(tmp_path):
    """End to end on a real interlaced MPEG-2 file: the cheap probe catches it."""
    combed = _make_source(
        tmp_path / "combed.mkv", "testsrc2=size=320x240:rate=60",
        ["-vf", "interlace=scan=tff,format=yuv420p", "-c:v", "mpeg2video",
         "-flags", "+ildct+ilme", "-top", "1", "-b:v", "4M"], seconds=3)
    facts = cc.probe_interlace(str(combed))
    assert "grain" not in facts and "animation" not in facts  # no pixel probes
    plan = cc.forced_class_plan(cc.ANIMATION, facts)
    assert plan.class_name == cc.ANIMATION
    assert plan.video_filters == [cc.DEINTERLACE_FILTER]
    assert plan.pix_fmt == "yuv420p"

    progressive = _make_source(tmp_path / "prog.mkv", "testsrc2=size=320x240:rate=24",
                               seconds=3)
    plan = cc.forced_class_plan(cc.ANIMATION, cc.probe_interlace(str(progressive)))
    assert plan.video_filters == []


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
    monkeypatch.setenv("ENGRAM_ENCODE_HOOK", "1")
    monkeypatch.setenv("ENGRAM_ENCODE_STAGING", str(tmp_path / "stage"))
    monkeypatch.setenv("ENGRAM_ENCODE_THREADS", "2")
    monkeypatch.setattr(encode_hook, "_DOMAIN_ROOTS", (str(tmp_path),))  # src is a staged rip
    # Force a fast preset on whatever class the media lands in; settings otherwise intact.
    real = encode_hook._load_classifier()
    orig = real.plan_for_file

    def fast(path, context=None, **kw):
        plan = orig(path, context, **kw)
        if plan.codec == "av1":
            plan.preset = "12"
        elif plan.codec == "hevc":
            plan.preset = "ultrafast"
        return plan

    monkeypatch.setattr(real, "plan_for_file", fast)
    encode_hook._encoder_accepts.cache_clear()
    encode_hook.after_organize(str(src), context={"content_type": "tv", "is_extra": False})
    out = tmp_path / "stage" / src.name
    assert out.exists()
    probe_out = encode_hook._probe(out)
    src_counts = encode_hook._track_counts(encode_hook._probe(src))
    assert encode_hook._track_counts(probe_out) == src_counts == (1, 1)
    assert (src.parent / (src.name + encode_hook.MARKER_SUFFIX)).exists()


# ---------------------------------------------------------------------------
# The deinterlace rule: interlaced source -> deinterlace; progressive -> don't.
# ---------------------------------------------------------------------------

_FRAME_RATE_FLAGS = ("-r", "-fps_mode", "-vsync", "-filter:v:r")


def _vf(args: list[str]) -> str | None:
    return args[args.index("-vf") + 1] if "-vf" in args else None


def test_deinterlace_filter_is_deint_all_send_frame():
    f = cc.DEINTERLACE_FILTER
    assert f == "bwdif=mode=send_frame:parity=auto:deint=all"
    assert "mode=send_frame" in f and "send_field" not in f
    assert "deint=interlaced" not in f


def test_automatic_and_forced_paths_use_the_identical_filter():
    """One shared constant: the two paths cannot diverge."""
    auto = cc.classify(probe(idet=INTERLACED_IDET))
    forced = cc.forced_class_plan(cc.LIVE_ACTION_CLEAN,
                                  {"width": 1920, "height": 1080, "idet": INTERLACED_IDET})
    undecided = cc.classify({})
    assert auto.video_filters == forced.video_filters == undecided.video_filters \
        == [cc.DEINTERLACE_FILTER]
    assert _vf(auto.video_args(4)) == _vf(forced.video_args(4)) == cc.DEINTERLACE_FILTER


@pytest.mark.parametrize("name", [n for n in cc.CLASSES if n != cc.PASSTHROUGH])
def test_progressive_source_never_gets_bwdif(name):
    prog = {"width": 1920, "height": 1080, "field_order": "progressive", "idet": PROGRESSIVE}
    if name == cc.INTERLACED:
        plan = cc.classify(probe())  # automatic path on a progressive source
        assert plan.class_name != cc.INTERLACED
    elif name == cc.HDR:
        plan = cc.classify(probe(color_transfer="smpte2084"))
    else:
        plan = cc.forced_class_plan(name, prog)
    assert plan.video_filters == []
    assert "bwdif" not in " ".join(plan.video_args(4))


def test_interlaced_class_is_a_real_encode_not_a_no_op():
    plan = cc.plan_for_class(cc.INTERLACED, res="sd", source_pix_fmt="yuv420p")
    assert not plan.is_passthrough and plan.codec == "av1"
    assert plan.video_filters == [cc.DEINTERLACE_FILTER]
    assert plan.pix_fmt == "yuv420p"  # bit depth still follows the source
    assert cc.validate_plan(plan) == []


def test_old_opt_in_variable_is_ignored(monkeypatch):
    for value in ("", "off", "deinterlace"):
        monkeypatch.setenv("ENGRAM_CODEC_INTERLACED", value)
        assert cc.classify(probe(idet=INTERLACED_IDET)).video_filters == [cc.DEINTERLACE_FILTER]
        assert cc.classify(probe()).video_filters == []


@pytest.mark.parametrize("p", [
    {},  # nothing at all
    {"field_order": "unknown"},
    {"idet": {"tff": 3, "bff": 2, "progressive": 4, "undetermined": 400}},  # too few decided
    # combed-looking but no consistent field order, and no usable field_order
    {"idet": {"tff": 149, "bff": 151, "progressive": 65, "undetermined": 0}},
])
def test_undecidable_probe_deinterlaces(p):
    inter, _, _ = cc.interlaced_verdict(p)
    assert inter is None
    deint, ev = cc.needs_deinterlace(p)
    assert deint is True and ev["interlaced"] == "unknown"
    assert cc.classify({**p, "width": 1920, "height": 1080}).video_filters \
        == [cc.DEINTERLACE_FILTER]
    assert cc.forced_class_plan(cc.ANIMATION, p).video_filters == [cc.DEINTERLACE_FILTER]


def test_mixed_parity_falls_back_to_field_order_never_straight_to_progressive():
    mixed = {"tff": 149, "bff": 151, "progressive": 65, "undetermined": 0}
    for fo in ("tt", "bb", "tb", "bt"):
        assert cc.interlaced_verdict({"idet": mixed, "field_order": fo})[0] is True
    assert cc.interlaced_verdict({"idet": mixed, "field_order": "progressive"})[0] is False
    assert cc.interlaced_verdict({"idet": mixed})[0] is None  # undecided -> deinterlace


def test_classify_error_deinterlaces(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(cc, "_classify", boom)
    plan = cc.classify(probe())
    assert plan.video_filters == [cc.DEINTERLACE_FILTER]
    assert plan.evidence["error"] == "bug"


def test_no_plan_ever_changes_the_frame_rate():
    plans = [cc.plan_for_class(n, res=r, hdr_meta=HDR_META, grainy_hdr=g)
             for n in cc.CLASSES for r in ("sd", "hd", "uhd") for g in (False, True)]
    plans += [cc.classify(probe(idet=INTERLACED_IDET)), cc.classify({}),
              cc.forced_class_plan(cc.ANIMATION, {"idet": INTERLACED_IDET})]
    for plan in plans:
        for optional in (True, False):
            args = plan.video_args(4, optional=optional)
            assert not set(args) & set(_FRAME_RATE_FLAGS), (plan.class_name, args)
            assert "send_field" not in " ".join(args)
            assert "fps=" not in (_vf(args) or "")


def test_hook_command_for_an_interlaced_source_has_bwdif_and_no_rate_flags(hook, monkeypatch):
    src, tmp = hook
    plan = cc.classify(probe(idet=INTERLACED_IDET, pix_fmt="yuv420p"))
    monkeypatch.setattr(encode_hook, "_load_classifier", lambda: _FakeClassifier(plan))
    calls: list = []
    monkeypatch.setattr(encode_hook.subprocess, "run", _Run(calls, lambda c: _streams(pix="yuv420p")))
    encode_hook.after_organize(str(src))
    (cmd,) = _encode_cmds(calls)
    assert cmd[cmd.index("-vf") + 1] == cc.DEINTERLACE_FILTER
    assert not set(cmd) & set(_FRAME_RATE_FLAGS)


def test_fixed_command_fallback_keeps_bwdif(hook, monkeypatch):
    """An encoder rejecting the class settings must not drop the deinterlacer."""
    src, tmp = hook
    plan = cc.classify(probe(idet=INTERLACED_IDET))
    monkeypatch.setattr(encode_hook, "_load_classifier", lambda: _FakeClassifier(plan))
    calls: list = []
    base = _Run(calls, lambda c: _streams())

    def run(cmd, **kw):
        if cmd[0] == "ffmpeg" and "testsrc=size=128x128" in " ".join(cmd):
            return subprocess.CompletedProcess(cmd, 1, "", "bad param")
        return base(cmd, **kw)

    monkeypatch.setattr(encode_hook.subprocess, "run", run)
    encode_hook.after_organize(str(src))
    (cmd,) = _encode_cmds(calls)
    assert cmd[cmd.index("-svtav1-params") + 1] == "tune=0:lp=4"  # the fixed command...
    assert cmd[cmd.index("-vf") + 1] == cc.DEINTERLACE_FILTER  # ...still deinterlaced


def test_kill_switch_command_is_still_byte_identical(hook, monkeypatch):
    assert "-vf" not in encode_hook._legacy_video_args("4")


def test_an_old_interlaced_skip_marker_does_not_count_as_done(hook, monkeypatch):
    """Files skipped under the old 'interlaced, left untouched' rule get encoded now."""
    src, tmp = hook
    st = src.stat()
    marker = src.parent / (src.name + encode_hook.MARKER_SUFFIX)
    marker.write_text(json.dumps({"size": st.st_size, "mtime": int(st.st_mtime),
                                  "av1": None, "codec_class": "interlaced"}))
    assert encode_hook._already_done(src) is False
    plan = cc.classify(probe(idet=INTERLACED_IDET))
    monkeypatch.setattr(encode_hook, "_load_classifier", lambda: _FakeClassifier(plan))
    calls: list = []
    monkeypatch.setattr(encode_hook.subprocess, "run", _Run(calls, lambda c: _streams()))
    encode_hook.after_organize(str(src))
    (cmd,) = _encode_cmds(calls)
    assert cmd[cmd.index("-vf") + 1] == cc.DEINTERLACE_FILTER
    done = json.loads(marker.read_text())
    assert done["codec_class"] == cc.INTERLACED and done["av1"] is not None
    assert encode_hook._already_done(src) is True
    # An ordinary passthrough record is still done.
    marker.write_text(json.dumps({"size": st.st_size, "mtime": int(st.st_mtime),
                                  "av1": None, "codec_class": "passthrough"}))
    assert encode_hook._already_done(src) is True


def _field_paired_1080i(path: Path, *, flagged: bool) -> Path:
    """Synthetic 1080i/25: each output frame interleaves the alternate field lines of
    two consecutive progressive 1080p50 frames (ffmpeg ``interlace``).  ``flagged``
    False re-tags the frames progressive, the case ``deint=interlaced`` missed."""
    vf = "interlace=scan=tff,format=yuv420p" + ("" if flagged else ",setfield=prog")
    enc = ["-vf", vf, "-c:v", "mpeg2video", "-q:v", "2"]
    if flagged:
        enc += ["-flags", "+ildct+ilme", "-top", "1"]
    return _make_source(path, "testsrc2=size=1920x1080:rate=50", enc, seconds=2)


def _idet_after(src: Path, vf: str) -> dict:
    run = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-v", "info", "-i", str(src),
                          "-vf", f"{vf},idet", "-f", "null", "-"], capture_output=True)
    tff, bff, prog, und = (int(x) for x in cc._IDET_RE.findall(run.stderr)[-1])
    return {"tff": tff, "bff": bff, "progressive": prog, "undetermined": und}


@needs_ffmpeg
@pytest.mark.parametrize("flagged", [True, False], ids=["flagged-tt", "tagged-progressive"])
def test_real_field_paired_1080i_is_detected_and_deinterlaced(tmp_path, flagged):
    src = _field_paired_1080i(tmp_path / "1080i.mkv", flagged=flagged)
    stream = cc._probe_stream(str(src))
    assert (stream["width"], stream["height"], stream["fps"]) == (1920, 1080, 25.0)

    # Automatic path (full probe) and forced path (cheap probe) both catch it.
    plan = cc.plan_for_file(str(src), {"content_type": "movie"})
    assert plan.evidence["interlaced"] == "yes", plan.evidence
    assert plan.class_name == cc.INTERLACED
    assert plan.video_filters == [cc.DEINTERLACE_FILTER]
    forced = cc.forced_class_plan(cc.LIVE_ACTION_CLEAN, cc.probe_interlace(str(src)))
    assert forced.video_filters == [cc.DEINTERLACE_FILTER]

    # The filter really removes the combing, on every frame, flagged or not --
    # and keeps 25 fps (send_frame: one frame out per frame in).
    after = _idet_after(src, cc.DEINTERLACE_FILTER)
    assert after["tff"] + after["bff"] == 0 and after["progressive"] == 50
    if not flagged:
        # The defect this guards: deint=interlaced leaves a progressive-tagged
        # combed source exactly as combed as it was.
        old = _idet_after(src, "bwdif=mode=send_frame:parity=auto:deint=interlaced")
        assert old["tff"] + old["bff"] >= 45

    out = tmp_path / "out.mkv"
    args = plan.video_args(2)
    args[args.index("-preset") + 1] = "12"
    run = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", "0:v:0",
                          *args, str(out)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    v = _ffprobe_video(out)
    assert v["codec_name"] == "av1" and v["pix_fmt"] == "yuv420p"
    assert cc._probe_stream(str(out))["fps"] == 25.0


@needs_ffmpeg
def test_real_progressive_1080p_gets_no_bwdif(tmp_path):
    src = _make_source(tmp_path / "1080p.mkv", "testsrc2=size=1920x1080:rate=25",
                       ["-c:v", "mpeg2video", "-q:v", "2"], seconds=2)
    plan = cc.plan_for_file(str(src))
    assert plan.evidence["interlaced"] == "no"
    assert plan.video_filters == []
    assert cc.forced_class_plan(cc.ANIMATION, cc.probe_interlace(str(src))).video_filters == []


# ---------------------------------------------------------------------------
# Telecine: IVTC (fieldmatch,decimate) only on a MEASURED telecine verdict.
#
# Measured on synthetic sources: telecined 29.97 -> fieldmatch,decimate gives
# 23.976 (correct); true interlaced 25 -> fieldmatch,decimate gives 20 fps with
# one frame in five destroyed (catastrophic).  idet's "Repeated Fields" line is
# the discriminator: progressive and true interlaced both read 0.
# ---------------------------------------------------------------------------

# idet's "Repeated Fields" counters as _probe_idet accumulates them.
TELECINE_REPEATS = {"repeated_neither": 108, "repeated_top": 36, "repeated_bottom": 36}
CLEAN_REPEATS = {"repeated_neither": 150, "repeated_top": 0, "repeated_bottom": 0}
TELECINED_IDET = {**INTERLACED_IDET, **TELECINE_REPEATS}

# Real ffmpeg 7.1 idet output (two summaries: the first instance is empty).
_IDET_STDERR_TELECINE = b"""\
[Parsed_idet_0 @ 0x1] Repeated Fields: Neither:     0 Top:     0 Bottom:     0
[Parsed_idet_0 @ 0x1] Single frame detection: TFF:     0 BFF:     0 Progressive:     0 Undetermined:     0
[Parsed_idet_0 @ 0x1] Multi frame detection: TFF:     0 BFF:     0 Progressive:     0 Undetermined:     0
[Parsed_idet_0 @ 0x2] Repeated Fields: Neither:    54 Top:    18 Bottom:    18
[Parsed_idet_0 @ 0x2] Single frame detection: TFF:    36 BFF:     0 Progressive:    50 Undetermined:     4
[Parsed_idet_0 @ 0x2] Multi frame detection: TFF:    90 BFF:     0 Progressive:     0 Undetermined:     0
"""
_IDET_STDERR_CLEAN = b"""\
[Parsed_idet_0 @ 0x2] Repeated Fields: Neither:    75 Top:     0 Bottom:     0
[Parsed_idet_0 @ 0x2] Single frame detection: TFF:    70 BFF:     0 Progressive:     5 Undetermined:     0
[Parsed_idet_0 @ 0x2] Multi frame detection: TFF:    75 BFF:     0 Progressive:     0 Undetermined:     0
"""


def test_repeated_fields_line_is_parsed_last_instance_wins():
    det, rep = cc._parse_idet(_IDET_STDERR_TELECINE)
    assert rep == {"repeated_neither": 54, "repeated_top": 18, "repeated_bottom": 18}
    # The detection counters are unchanged: still the LAST Multi frame row.
    assert det == {"tff": 90, "bff": 0, "progressive": 0, "undetermined": 0}


def test_a_clean_repeated_fields_line_yields_zero():
    det, rep = cc._parse_idet(_IDET_STDERR_CLEAN)
    assert rep == {"repeated_neither": 75, "repeated_top": 0, "repeated_bottom": 0}
    assert det["tff"] == 75
    assert cc._parse_idet(b"no idet here") == (None, None)


def test_probe_idet_sums_repeated_fields_across_samples(monkeypatch):
    outputs = iter([_IDET_STDERR_TELECINE, _IDET_STDERR_CLEAN, _IDET_STDERR_TELECINE])
    monkeypatch.setattr(cc, "_run", lambda cmd, timeout=None: subprocess.CompletedProcess(
        cmd, 0, b"", next(outputs)))
    total = cc._probe_idet("x.mkv", [1.0, 2.0, 3.0])
    assert total == {"tff": 90 + 75 + 90, "bff": 0, "progressive": 0, "undetermined": 0,
                     "repeated_neither": 54 + 75 + 54, "repeated_top": 36,
                     "repeated_bottom": 36}


def test_telecine_verdict_fraction_and_threshold():
    tele, frac, basis = cc.telecine_verdict({"idet": TELECINED_IDET, "fps": 29.97})
    assert tele is True and frac == pytest.approx(72 / 180) and basis == "repeated_fields"
    assert cc.telecine_verdict({"idet": {**INTERLACED_IDET, **CLEAN_REPEATS},
                                "fps": 29.97})[:2] == (False, 0.0)
    # Just below / at the threshold.
    below = {"repeated_neither": 87, "repeated_top": 7, "repeated_bottom": 6}  # 0.13
    at = {"repeated_neither": 85, "repeated_top": 8, "repeated_bottom": 7}  # 0.15
    assert cc.telecine_verdict({"idet": below, "fps": 29.97})[0] is False
    assert cc.telecine_verdict({"idet": at, "fps": 29.97})[0] is True
    # Too few counted frames, or no repeated-field counters at all: never telecined.
    few = {"repeated_neither": 5, "repeated_top": 3, "repeated_bottom": 3}
    assert cc.telecine_verdict({"idet": few, "fps": 29.97})[0] is False
    assert cc.telecine_verdict({"idet": INTERLACED_IDET, "fps": 29.97})[0] is False
    assert cc.telecine_verdict({"fps": 29.97})[0] is False


@pytest.mark.parametrize("fps", [25.0, 50.0, 23.976, None])
def test_repeated_fields_at_a_non_ntsc_rate_never_select_ivtc(fps):
    """3:2 pulldown only produces 29.97/30.  IVTC on PAL 25 is the 20 fps disaster."""
    p = {"idet": TELECINED_IDET, "field_order": "tt"}
    if fps is not None:
        p["fps"] = fps
    assert cc.telecine_verdict(p)[0] is False
    vf, ev = cc.deinterlace_filter(p)
    assert vf == cc.DEINTERLACE_FILTER and ev["telecined"] == "no"


def test_telecined_probe_selects_ivtc_on_every_path():
    p = probe(idet=TELECINED_IDET, fps=29.97, field_order="tt", width=720, height=480)
    vf, ev = cc.deinterlace_filter(p)
    assert vf == cc.IVTC_FILTER and ev["telecined"] == "yes"
    assert ev["repeated_frac"] == 0.4
    auto = cc.classify(p)
    assert auto.class_name == cc.INTERLACED
    assert auto.video_filters == [cc.IVTC_FILTER]
    assert "telecined=yes" in auto.describe()
    assert cc.validate_plan(auto) == []
    for name in (cc.ANIMATION, cc.LIVE_ACTION_CLEAN, cc.LIVE_ACTION_GRAINY, cc.INTERLACED):
        assert cc.forced_class_plan(name, p).video_filters == [cc.IVTC_FILTER], name


def test_telecine_wins_over_plain_interlacing():
    """A telecined source is ALSO interlaced; the order of the checks matters."""
    p = {"idet": TELECINED_IDET, "fps": 29.97, "field_order": "tt"}
    assert cc.interlaced_verdict(p)[0] is True
    assert cc.telecine_verdict(p)[0] is True
    assert cc.deinterlace_filter(p)[0] == cc.IVTC_FILTER


def test_true_interlaced_probe_gets_bwdif_not_fieldmatch():
    """Regression guard for the 20 fps disaster: zero repeated fields -> never IVTC."""
    for fps in (25.0, 29.97):
        p = probe(idet={**INTERLACED_IDET, **CLEAN_REPEATS}, fps=fps, field_order="tt")
        plan = cc.classify(p)
        assert plan.video_filters == [cc.DEINTERLACE_FILTER]
        assert "fieldmatch" not in " ".join(plan.video_args(4))
        forced = cc.forced_class_plan(cc.LIVE_ACTION_CLEAN, p)
        assert forced.video_filters == [cc.DEINTERLACE_FILTER]


def test_progressive_probe_gets_neither_filter():
    p = probe(idet={**PROGRESSIVE, "repeated_neither": 214, "repeated_top": 0,
                    "repeated_bottom": 0})
    assert cc.deinterlace_filter(p)[0] is None
    args = " ".join(cc.classify(p).video_args(4))
    assert "bwdif" not in args and "fieldmatch" not in args and "decimate" not in args


def test_undecided_probe_gets_bwdif_never_ivtc():
    for p in ({}, None, {"fps": 29.97}, {"idet": {"undetermined": 400}, "fps": 29.97}):
        vf, ev = cc.deinterlace_filter(p)
        assert vf == cc.DEINTERLACE_FILTER, p
        assert ev["interlaced"] == "unknown" and ev["telecined"] == "no"


def test_ivtc_filter_carries_no_bwdif_options():
    assert cc.IVTC_FILTER == "fieldmatch,decimate"
    assert "deint" not in cc.IVTC_FILTER and "mode=" not in cc.IVTC_FILTER
    assert "bwdif" not in cc.IVTC_FILTER
    # Telecined HDR keeps HDR handling with exactly one deinterlace filter.
    plan = cc.classify(probe(idet=TELECINED_IDET, fps=29.97, color_transfer="smpte2084"))
    assert plan.class_name == cc.HDR and plan.video_filters == [cc.IVTC_FILTER]


def test_no_telecine_plan_changes_the_frame_rate_by_flag():
    tele = probe(idet=TELECINED_IDET, fps=29.97)
    plans = [cc.classify(tele), cc.forced_class_plan(cc.ANIMATION, tele),
             cc.forced_class_plan(cc.LIVE_ACTION_GRAINY, tele)]
    for plan in plans:
        for optional in (True, False):
            args = plan.video_args(4, optional=optional)
            assert not set(args) & set(_FRAME_RATE_FLAGS), args
            assert _vf(args) == cc.IVTC_FILTER


def test_hook_command_for_a_telecined_source_ivtcs_and_keeps_it_on_fallback(hook, monkeypatch):
    src, tmp = hook
    plan = cc.classify(probe(idet=TELECINED_IDET, fps=29.97, pix_fmt="yuv420p"))
    monkeypatch.setattr(encode_hook, "_load_classifier", lambda: _FakeClassifier(plan))
    calls: list = []
    base = _Run(calls, lambda c: _streams(pix="yuv420p"))

    def run(cmd, **kw):  # the encoder rejects every class setting -> fixed command
        if cmd[0] == "ffmpeg" and "testsrc=size=128x128" in " ".join(cmd):
            return subprocess.CompletedProcess(cmd, 1, "", "bad param")
        return base(cmd, **kw)

    monkeypatch.setattr(encode_hook.subprocess, "run", run)
    encode_hook.after_organize(str(src))
    (cmd,) = _encode_cmds(calls)
    assert cmd[cmd.index("-vf") + 1] == cc.IVTC_FILTER
    assert not set(cmd) & set(_FRAME_RATE_FLAGS)


def _repeats_after(src: Path, vf: str | None) -> tuple[int, int]:
    """(repeated top+bottom, frames) measured by idet after ``vf``."""
    chain = f"{vf},idet" if vf else "idet"
    run = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-v", "info", "-i", str(src),
                          "-vf", chain, "-f", "null", "-"], capture_output=True)
    _, rep = cc._parse_idet(run.stderr)
    return rep["repeated_top"] + rep["repeated_bottom"], sum(rep.values())


def _encode_fps(src: Path, plan, out: Path) -> float:
    args = plan.video_args(2)
    args[args.index("-preset") + 1] = "12"
    run = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", "0:v:0",
                          *args, str(out)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    return cc._probe_stream(str(out))["fps"]


@needs_ffmpeg
def test_real_telecined_source_is_inverse_telecined_to_23976(tmp_path):
    src = _make_source(
        tmp_path / "telecined.mkv", "testsrc2=size=320x240:rate=24000/1001",
        ["-vf", "telecine=pattern=23,format=yuv420p", "-c:v", "mpeg2video", "-q:v", "2",
         "-flags", "+ildct+ilme"], seconds=3)
    assert cc._probe_stream(str(src))["fps"] == pytest.approx(30000 / 1001)

    p = cc.probe_interlace(str(src))
    tele, frac, _ = cc.telecine_verdict(p)
    assert tele is True and frac == pytest.approx(0.40, abs=0.05)
    plan = cc.plan_for_file(str(src))
    assert plan.video_filters == [cc.IVTC_FILTER], plan.evidence
    vf = _vf(plan.video_args(2))
    assert "fieldmatch" in vf and "decimate" in vf and "bwdif" not in vf
    assert cc.forced_class_plan(cc.LIVE_ACTION_CLEAN, p).video_filters == [cc.IVTC_FILTER]

    # Causal, not correlated: IVTC removes the repeated fields, bwdif does not.
    assert _repeats_after(src, None)[0] > 0
    assert _repeats_after(src, cc.IVTC_FILTER)[0] == 0
    assert _repeats_after(src, cc.DEINTERLACE_FILTER)[0] > 0

    assert _encode_fps(src, plan, tmp_path / "out.mkv") == pytest.approx(24000 / 1001)


@needs_ffmpeg
def test_real_true_interlaced_25_is_bwdif_not_fieldmatch(tmp_path):
    """THE regression guard: IVTC here would give 20 fps and destroy 1 frame in 5."""
    src = _make_source(
        tmp_path / "25i.mkv", "testsrc2=size=320x240:rate=50",
        ["-vf", "interlace=scan=tff,format=yuv420p", "-c:v", "mpeg2video", "-q:v", "2",
         "-flags", "+ildct+ilme", "-top", "1"], seconds=3)
    p = cc.probe_interlace(str(src))
    assert p["fps"] == 25.0
    assert p["idet"]["repeated_top"] + p["idet"]["repeated_bottom"] == 0
    assert cc.telecine_verdict(p)[0] is False
    plan = cc.plan_for_file(str(src))
    assert plan.video_filters == [cc.DEINTERLACE_FILTER], plan.evidence
    assert "fieldmatch" not in " ".join(plan.video_args(2))
    forced = cc.forced_class_plan(cc.LIVE_ACTION_CLEAN, p)
    assert forced.video_filters == [cc.DEINTERLACE_FILTER]
    assert _encode_fps(src, plan, tmp_path / "out.mkv") == 25.0


@needs_ffmpeg
def test_real_progressive_gets_neither_filter(tmp_path):
    src = _make_source(tmp_path / "24p.mkv", "testsrc2=size=320x240:rate=24000/1001",
                       ["-c:v", "mpeg2video", "-q:v", "2"], seconds=3)
    p = cc.probe_interlace(str(src))
    assert cc.telecine_verdict(p)[0] is False
    assert cc.plan_for_file(str(src)).video_filters == []
    assert cc.forced_class_plan(cc.ANIMATION, p).video_filters == []
