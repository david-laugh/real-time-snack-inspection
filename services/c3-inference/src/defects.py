"""Mask-based defect rules (independent of SAM2).

Input : RGB frame, one boolean mask, the SAM2 mask quality score.
Output: status (normal / defect / unknown), defect_type, defect_score.

defect_score vs. SAM2 mask score
--------------------------------
`mask_score` is SAM2's predicted IoU ("how good is this mask"). It is used ONLY
to decide whether the mask is trustworthy enough to judge (else -> unknown).
`defect_score` is "how strongly the rules indicate a defect", in [0, 1]:

    each rule maps its measurement to a severity in [0, 1] such that
      * ideal value          -> 0.0
      * exactly at threshold -> 0.5
      * far past threshold   -> 1.0   (linear in between)
    defect_score = max severity over all rules.

So status == defect  <=>  some rule is violated  <=>  defect_score > 0.5.
For status == unknown, defect_score is 0.0 (no judgement was made).

Rules
-----
BROKEN (outer shape of the mask)
    concavity    = deepest notch / convex-hull diameter            > max -> broken  (bite, wedge)
                   (notches shallower than concavity_min_px are ignored as edge noise)
    aspect_ratio = short / long side of min-area rectangle         < min -> broken  (cut, half)
    circularity  = 4*pi*A / P^2            (largest component)   < min -> broken
    solidity     = A / convex_hull_area                            < min -> broken
    aspect_ratio = short / long side of min-area rectangle         < min -> broken
    fragments    = #components with area >= ratio * mask area      > 1   -> broken
    area         = |A - expected| / expected (only if expected > 0) > tol -> broken
SPOT_DEFECT (inside the mask; disabled by default, see DefectThresholds.spot_enabled)
    dark_ratio   = #dark px / #px in eroded mask                   > max -> spot_defect
    dark px      : Lab L < median(L in mask) - delta, then morphological opening
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Optional

import cv2
import numpy as np

from .config import DefectThresholds
from .schemas import DefectType, Status


@dataclass
class DefectDecision:
    status: Status
    defect_type: Optional[DefectType]
    defect_score: float
    reason: str = ""
    features: Dict[str, float] = field(default_factory=dict)


def _severity_below(value: float, threshold: float, ideal: float = 1.0) -> float:
    """Severity for metrics that must stay ABOVE `threshold` (ideal is the best value)."""
    if value >= ideal:
        return 0.0
    if value >= threshold:
        span = max(ideal - threshold, 1e-9)
        return 0.5 * (ideal - value) / span
    return 0.5 + 0.5 * min(1.0, (threshold - value) / max(threshold, 1e-9))


def _severity_above(value: float, threshold: float) -> float:
    """Severity for metrics that must stay BELOW `threshold` (0 is ideal)."""
    if threshold <= 0:
        return 1.0 if value > 0 else 0.0
    if value <= threshold:
        return 0.5 * value / threshold
    return 0.5 + 0.5 * min(1.0, (value - threshold) / threshold)


def _max_notch_depth(cnt: np.ndarray) -> float:
    """Depth (px) of the deepest concavity between the outline and its convex hull."""
    try:
        hull_idx = cv2.convexHull(cnt, returnPoints=False)
        defects = cv2.convexityDefects(cnt, hull_idx) if len(hull_idx) > 3 else None
    except cv2.error:  # rare non-monotonous hull indices on self-touching outlines
        return 0.0
    if defects is None:
        return 0.0
    return float(defects[:, 0, 3].max()) / 256.0  # fixed-point 8.8


class DefectAnalyzer:
    def __init__(self, thresholds: DefectThresholds) -> None:
        self.t = thresholds

    def analyze(self, image_rgb: np.ndarray, mask: np.ndarray, mask_score: float) -> DefectDecision:
        t = self.t
        features: Dict[str, float] = {"mask_score": round(float(mask_score), 4)}

        if not math.isfinite(mask_score) or mask_score < t.min_mask_score:
            return DefectDecision(Status.UNKNOWN, None, 0.0, "low_mask_score", features)

        # OpenCV SIMD scans of the full-frame mask; everything after works on the crop.
        mask_u8 = np.ascontiguousarray(mask).view(np.uint8)
        area = int(cv2.countNonZero(mask_u8))
        features["area_px"] = area
        if area < max(1, t.min_mask_area_px):
            return DefectDecision(Status.UNKNOWN, None, 0.0, "mask_too_small", features)

        # Work on the mask's bounding region only (much cheaper than full frame).
        bx, by, bw, bh = cv2.boundingRect(mask_u8)
        pad = max(2, t.spot_erode_px + 2)
        h, w = mask.shape
        y0, y1 = max(0, by - pad), min(h, by + bh + pad)
        x0, x1 = max(0, bx - pad), min(w, bx + bw + pad)
        m = mask[y0:y1, x0:x1].astype(np.uint8)
        rgb = image_rgb[y0:y1, x0:x1]

        broken_scores = self._broken_scores(m, area, features)
        if broken_scores is None:
            return DefectDecision(Status.UNKNOWN, None, 0.0, "no_contour", features)
        spot_score = self._spot_score(rgb, m, features) if t.spot_enabled else 0.0

        broken_score = max(broken_scores.values())
        features.update({f"severity_{k}": round(v, 4) for k, v in broken_scores.items()})
        features["severity_spot"] = round(spot_score, 4)

        broken_violated = broken_score > 0.5
        spot_violated = spot_score > 0.5
        score = round(float(max(broken_score, spot_score)), 4)
        if broken_violated or spot_violated:
            if broken_violated and (broken_score >= spot_score or not spot_violated):
                worst = max(broken_scores, key=broken_scores.get)
                return DefectDecision(Status.DEFECT, DefectType.BROKEN, score, f"broken:{worst}", features)
            return DefectDecision(Status.DEFECT, DefectType.SPOT_DEFECT, score, "spot:dark_ratio", features)
        return DefectDecision(Status.NORMAL, None, score, "ok", features)

    # ---------------------------------------------------------------- BROKEN
    def _broken_scores(self, m: np.ndarray, area: int, features: Dict[str, float]) -> Optional[Dict[str, float]]:
        t = self.t
        n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
        comp_areas = stats[1:, cv2.CC_STAT_AREA]
        if len(comp_areas) == 0:
            return None
        fragments = int((comp_areas >= t.broken_fragment_min_ratio * area).sum())
        largest = 1 + int(np.argmax(comp_areas))
        main = (labels == largest).astype(np.uint8)

        contours, _ = cv2.findContours(main, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if not contours:
            return None
        cnt = max(contours, key=cv2.contourArea)
        c_area = float(cv2.contourArea(cnt))
        perimeter = float(cv2.arcLength(cnt, True))
        if c_area <= 0 or perimeter <= 0:
            return None
        hull_area = float(cv2.contourArea(cv2.convexHull(cnt)))
        (_, _), (rw, rh), _ = cv2.minAreaRect(cnt)
        hull_diameter = 2.0 * math.sqrt(hull_area / math.pi) if hull_area > 0 else 0.0
        depth_px = _max_notch_depth(cnt)

        circularity = min(1.0, 4.0 * math.pi * c_area / (perimeter * perimeter))
        solidity = min(1.0, c_area / hull_area) if hull_area > 0 else 0.0
        aspect = (min(rw, rh) / max(rw, rh)) if max(rw, rh) > 0 else 0.0

        concavity = depth_px / hull_diameter if hull_diameter > 0 else 0.0
        # A notch below the pixel floor cannot be told apart from mask edge noise.
        effective_concavity = concavity if depth_px >= t.broken_concavity_min_px else min(
            concavity, t.broken_concavity_max)

        features.update(
            concavity=round(concavity, 4),
            notch_depth_px=round(depth_px, 2),
            circularity=round(circularity, 4),
            solidity=round(solidity, 4),
            aspect_ratio=round(aspect, 4),
            fragments=fragments,
        )
        scores = {
            "concavity": _severity_above(effective_concavity, t.broken_concavity_max),
            "circularity": _severity_below(circularity, t.broken_circularity_min),
            "solidity": _severity_below(solidity, t.broken_solidity_min),
            "aspect_ratio": _severity_below(aspect, t.broken_aspect_ratio_min),
            # 1 fragment -> 0, 2 -> 0.75, >=3 -> 1.0
            "fragments": 0.0 if fragments <= 1 else min(1.0, 0.5 + 0.25 * (fragments - 1)),
        }
        if t.broken_expected_area_px > 0:
            deviation = abs(area - t.broken_expected_area_px) / t.broken_expected_area_px
            features["area_deviation"] = round(deviation, 4)
            scores["area"] = _severity_above(deviation, t.broken_area_tolerance)
        return scores

    # ----------------------------------------------------------- SPOT_DEFECT
    def _spot_score(self, rgb: np.ndarray, m: np.ndarray, features: Dict[str, float]) -> float:
        t = self.t
        # SAM2 often carves a strongly contrasting spot out of the mask as a hole.
        # Measure inside the filled outer outline so such spots are still counted.
        contours, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        inner = np.zeros_like(m)
        cv2.drawContours(inner, contours, -1, 1, thickness=-1)
        features["hole_px"] = int(inner.sum()) - int(m.sum())
        if t.spot_erode_px > 0:
            k = 2 * t.spot_erode_px + 1
            inner = cv2.erode(inner, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
        inner_px = int(inner.sum())
        if inner_px == 0:
            features["dark_ratio"] = 0.0
            return 0.0
        lightness = cv2.cvtColor(np.ascontiguousarray(rgb), cv2.COLOR_RGB2LAB)[:, :, 0]
        inside = inner.astype(bool)
        median_l = float(np.median(lightness[inside]))
        dark = ((lightness.astype(np.float32) < (median_l - t.spot_dark_delta)) & inside).astype(np.uint8)
        if t.spot_open_kernel > 1:
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (t.spot_open_kernel, t.spot_open_kernel))
            dark = cv2.morphologyEx(dark, cv2.MORPH_OPEN, kernel)
        ratio = float(dark.sum()) / inner_px
        features.update(dark_ratio=round(ratio, 4), median_lightness=round(median_l, 1))
        return _severity_above(ratio, t.spot_ratio_max)
