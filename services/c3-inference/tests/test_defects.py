"""Unit tests for the mask-based defect rules and the RLE codec."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from conftest import disc_mask, fixture_mask, load_c2_fixture, make_image
from src.config import DefectThresholds
from src.defects import DefectAnalyzer
from src.mask_codec import rle_decode, rle_encode
from src.schemas import DefectType, Status

SHAPE = (480, 640)


@pytest.fixture
def analyzer() -> DefectAnalyzer:
    return DefectAnalyzer(DefectThresholds())


@pytest.fixture
def spot_analyzer() -> DefectAnalyzer:
    """SPOT_DEFECT is off by default (chocolate chips); these tests cover the opt-in rule."""
    return DefectAnalyzer(DefectThresholds.from_env({"C3_SPOT_ENABLED": "1"}))


def _mask(draw) -> np.ndarray:
    m = np.zeros(SHAPE, np.uint8)
    draw(m)
    return m.astype(bool)


def test_round_snack_is_normal(analyzer):
    d = analyzer.analyze(make_image(), disc_mask(SHAPE, 200, 240, 80), 0.95)
    assert d.status is Status.NORMAL and d.defect_type is None
    assert 0.0 <= d.defect_score < 0.5
    assert d.features["circularity"] > 0.85 and d.features["solidity"] > 0.95


def test_square_cracker_needs_relaxed_circularity():
    # Defaults target round cookies; a square (circularity ~0.785) needs the backstop relaxed.
    image = np.full((*SHAPE, 3), 200, np.uint8)
    square = _mask(lambda m: cv2.rectangle(m, (100, 100), (220, 220), 1, -1))
    assert DefectAnalyzer(DefectThresholds()).analyze(image, square, 0.95).status is Status.DEFECT
    relaxed = DefectAnalyzer(DefectThresholds.from_env({"C3_BROKEN_CIRCULARITY_MIN": "0.7"}))
    d = relaxed.analyze(image, square, 0.95)
    assert d.status is Status.NORMAL, d.features


@pytest.mark.parametrize(
    "name, draw",
    [
        ("wedge", lambda m: cv2.ellipse(m, (200, 240), (80, 80), 0, 40, 320, 1, -1)),
        ("bite", lambda m: (cv2.circle(m, (200, 240), 80, 1, -1), cv2.circle(m, (200, 155), 22, 0, -1))),
        ("cut", lambda m: (cv2.circle(m, (200, 240), 80, 1, -1), cv2.rectangle(m, (255, 100), (300, 400), 0, -1))),
        ("half", lambda m: cv2.ellipse(m, (200, 240), (80, 80), 0, 0, 180, 1, -1)),
        ("two_pieces", lambda m: (cv2.circle(m, (150, 240), 40, 1, -1), cv2.circle(m, (300, 240), 35, 1, -1))),
    ],
)
def test_broken_shapes(analyzer, name, draw):
    image = np.full((*SHAPE, 3), 200, np.uint8)  # uniform snack colour: isolate the shape rules
    d = analyzer.analyze(image, _mask(draw), 0.9)
    assert d.status is Status.DEFECT, (name, d.features)
    assert d.defect_type is DefectType.BROKEN
    assert 0.5 < d.defect_score <= 1.0


def test_chocolate_chips_are_normal_by_default(analyzer):
    image = make_image(spots=[(180, 220, 8), (230, 250, 7), (190, 280, 9), (240, 210, 6)])  # ~4% dark
    d = analyzer.analyze(image, disc_mask(SHAPE, 200, 240, 80), 0.95)
    assert d.status is Status.NORMAL, d.features
    assert "dark_ratio" not in d.features  # spot rule not evaluated


@pytest.mark.parametrize("radius", [15, 25, 40])
def test_small_round_cookie_is_normal(analyzer, radius):
    # Low-resolution streams (C1 /cam 640px): raster noise must not look like a defect.
    d = analyzer.analyze(make_image(discs=[(200, 240, radius)]), disc_mask(SHAPE, 200, 240, radius), 0.95)
    assert d.status is Status.NORMAL, d.features


def test_small_cookie_bite_is_broken(analyzer):
    m = _mask(lambda m: (cv2.circle(m, (200, 240), 25, 1, -1), cv2.circle(m, (200, 213), 8, 0, -1)))
    d = analyzer.analyze(np.full((*SHAPE, 3), 200, np.uint8), m, 0.95)
    assert (d.status, d.defect_type) == (Status.DEFECT, DefectType.BROKEN), d.features


def test_dark_spot_is_spot_defect(spot_analyzer):
    analyzer = spot_analyzer
    image = make_image(spots=[(200, 240, 20)])  # ~6% of the disc
    d = analyzer.analyze(image, disc_mask(SHAPE, 200, 240, 80), 0.95)
    assert (d.status, d.defect_type) == (Status.DEFECT, DefectType.SPOT_DEFECT)
    assert d.features["dark_ratio"] > 0.03


def test_tiny_spot_below_threshold_is_normal(spot_analyzer):
    analyzer = spot_analyzer
    image = make_image(spots=[(200, 240, 6)])  # ~0.6% of the disc
    d = analyzer.analyze(image, disc_mask(SHAPE, 200, 240, 80), 0.95)
    assert d.status is Status.NORMAL, d.features
    assert 0 < d.features["dark_ratio"] < 0.03


def test_dark_edge_shadow_ignored_by_erosion(spot_analyzer):
    analyzer = spot_analyzer
    image = make_image()
    cv2.circle(image, (200, 240), 80, (40, 30, 20), thickness=2)  # dark rim only
    d = analyzer.analyze(image, disc_mask(SHAPE, 200, 240, 80), 0.95)
    assert d.status is Status.NORMAL, d.features


@pytest.mark.parametrize(
    "mask, score, reason",
    [
        (disc_mask(SHAPE, 200, 240, 80), 0.3, "low_mask_score"),
        (disc_mask(SHAPE, 200, 240, 4), 0.95, "mask_too_small"),
        (np.zeros(SHAPE, bool), 0.95, "mask_too_small"),
        (disc_mask(SHAPE, 200, 240, 80), float("nan"), "low_mask_score"),
    ],
)
def test_unknown_when_mask_untrustworthy(analyzer, mask, score, reason):
    d = analyzer.analyze(make_image(), mask, score)
    assert (d.status, d.defect_type, d.defect_score, d.reason) == (Status.UNKNOWN, None, 0.0, reason)


def test_thresholds_from_env_change_decision():
    image = make_image(spots=[(200, 240, 20)])
    mask = disc_mask(SHAPE, 200, 240, 80)
    strict = DefectAnalyzer(DefectThresholds.from_env({"C3_SPOT_ENABLED": "1"}))
    lenient = DefectAnalyzer(DefectThresholds.from_env({"C3_SPOT_ENABLED": "1", "C3_SPOT_RATIO_MAX": "0.2"}))
    assert strict.analyze(image, mask, 0.95).status is Status.DEFECT
    assert lenient.analyze(image, mask, 0.95).status is Status.NORMAL

    expected = DefectAnalyzer(DefectThresholds.from_env({"C3_BROKEN_EXPECTED_AREA_PX": "40000"}))
    d = expected.analyze(make_image(), mask, 0.95)  # area ~20100 px, 50% below expected
    assert (d.status, d.defect_type) == (Status.DEFECT, DefectType.BROKEN)


def test_defect_score_independent_of_mask_score(analyzer):
    mask = disc_mask(SHAPE, 200, 240, 80)
    a = analyzer.analyze(make_image(), mask, 0.6)
    b = analyzer.analyze(make_image(), mask, 0.99)
    assert a.defect_score == b.defect_score


@pytest.mark.parametrize("seed", range(5))
def test_rle_roundtrip(seed):
    rng = np.random.default_rng(seed)
    m = rng.random((37, 53)) > 0.5
    enc = rle_encode(m)
    assert sum(enc["data"]["counts"]) == 37 * 53
    assert np.array_equal(rle_decode(enc), m)


def test_rle_edge_cases():
    for m in (np.zeros((4, 5), bool), np.ones((4, 5), bool)):
        assert np.array_equal(rle_decode(rle_encode(m)), m)
    assert rle_encode(np.ones((2, 2), bool))["data"]["counts"] == [0, 4]


def test_spot_carved_out_as_mask_hole_is_still_detected(spot_analyzer):
    analyzer = spot_analyzer
    # Real SAM2 behaviour: a dark spot is often excluded from the mask as a hole.
    image = make_image(spots=[(200, 240, 25)])
    mask = disc_mask(SHAPE, 200, 240, 80) & ~disc_mask(SHAPE, 200, 240, 25)
    d = analyzer.analyze(image, mask, 0.95)
    assert (d.status, d.defect_type) == (Status.DEFECT, DefectType.SPOT_DEFECT), d.features
    assert d.features["hole_px"] > 0


# ---------------------------------------------------------------------------
# Regression on real data: C2-generated prompts on C2's fixtures, real SAM2 masks.
# ---------------------------------------------------------------------------
C2_FIXTURE = load_c2_fixture()
C2_OBJECTS = [(f, o) for f in C2_FIXTURE["frames"] for o in f["objects"]]


@pytest.mark.parametrize("frame, obj", C2_OBJECTS, ids=[f"{f['name'][:11]}-{o['candidate_id']}-{o['label']}" for f, o in C2_OBJECTS])
def test_c2_fixture_real_masks(analyzer, frame, obj):
    image = np.full((frame["height"], frame["width"], 3), 200, np.uint8)  # shape rules only (spot is off)
    d = analyzer.analyze(image, fixture_mask(frame, obj), obj["mask_score"])
    if obj["label"] == "round":
        assert (d.status, d.defect_type) == (Status.NORMAL, None), d.features
    else:  # bite / wedge / cut / half
        assert (d.status, d.defect_type) == (Status.DEFECT, DefectType.BROKEN), d.features


def test_c2_fixture_covers_all_defect_types():
    labels = {o["label"] for _, o in C2_OBJECTS}
    assert labels == {"round", "bite", "wedge", "cut", "half"}
    assert len(C2_OBJECTS) == 42
