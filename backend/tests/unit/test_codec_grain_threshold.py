"""Blu-ray / DVD compression noise must not be read as film grain.

The classifier routes ``grain >= GRAIN_THRESHOLD`` to x265, because real film
grain costs a fortune in AV1 bits at the same quality.  The threshold was fitted
on synthetic sources and its own documentation warns that *compression* noise —
MPEG-2 mosquito artefacts on a DVD, light H.264 dither on a Blu-ray — reads as
grain.  Observed on the principal's own disc: ``Reef Diary - Builders`` scored
``grain=0.46`` and was routed to x265, while its sibling track from the very same
disc scored 0.28 and went to AV1.  Two tracks off one disc do not have different
film stock; only one of them tripped the artefact.

These are contract tests on the threshold and the decision, not on the probe
internals: any fix must keep real grain routing to x265.
"""

from __future__ import annotations

import pytest

from app.services import codec_classifier as cc


def _plan(grain: float, *, animation: float | None = 0.10, content_type: str | None = None):
    # A *measured progressive* source: without it the classifier is undecided on
    # interlacing and returns the deinterlaced plan before reaching the codec
    # choice at all, so the grain decision would never be exercised.
    probe = {
        "grain": grain,
        "animation": animation,
        "fps": 25.0,
        "interlaced": "no",
        "field_order": "progressive",
    }
    context = {"content_type": content_type} if content_type else None
    return cc.classify(probe, context or {})


def test_the_observed_misfire_is_routed_to_av1():
    """grain=0.46 with low animation is the measured Reef Diary case."""
    plan = _plan(0.46)
    assert plan.class_name == cc.LIVE_ACTION_CLEAN, (
        "0.46 was the observed Blu-ray compression artefact; routing it to x265 "
        "costs encodes for grain the disc does not have"
    )


def test_real_film_grain_still_routes_to_x265():
    """The costly class must still claim genuinely grainy material."""
    plan = _plan(0.72)
    assert plan.class_name == cc.LIVE_ACTION_GRAINY


def test_the_threshold_is_above_the_documented_artefact_band():
    assert cc.GRAIN_THRESHOLD > 0.46, (
        "the observed artefact scored 0.46; a threshold at or below it "
        "re-admits the misfire"
    )


def test_the_threshold_stays_below_genuine_grain():
    assert cc.GRAIN_THRESHOLD <= 0.60, (
        "raising it past genuine film-grain scores would send expensive "
        "material to AV1 and blow the encode budget"
    )


def test_animation_is_still_excluded_when_grain_is_high():
    """A high animation score does not override grain (existing contract)."""
    plan = _plan(0.85, animation=0.95)
    assert plan.class_name == cc.LIVE_ACTION_GRAINY


def test_the_threshold_constant_is_the_single_source_of_truth():
    """The decision must read the constant, not a duplicated literal."""
    import inspect

    source = inspect.getsource(cc._classify)
    assert "GRAIN_THRESHOLD" in source
    for literal in ("0.40", "0.46", "0.50", "0.55"):
        assert literal not in source, (
            f"a duplicated grain literal {literal} in classify() would drift "
            "from GRAIN_THRESHOLD"
        )
