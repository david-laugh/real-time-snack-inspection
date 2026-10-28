"""Environment-driven settings for C3 SAM2 Inference.

All knobs are plain environment variables so that docker-compose / `.env` can
tune every replica without rebuilding the image. See README.md for the table.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping


def _get(env: Mapping[str, str], key: str, default: str) -> str:
    value = env.get(key)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = _get(env, key, str(default))
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be a number, got {raw!r}") from exc


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = _get(env, key, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from exc


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = _get(env, key, "1" if default else "0").lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{key} must be a boolean (1/0/true/false), got {raw!r}")


@dataclass(frozen=True)
class DefectThresholds:
    """Thresholds for the mask-based defect rules (src/defects.py)."""

    # --- UNKNOWN gating (mask is not trustworthy enough to judge) ---
    # SAM2 mask quality (predicted IoU). Below this -> status=unknown.
    min_mask_score: float = 0.5
    # Masks smaller than this (pixels) -> status=unknown.
    min_mask_area_px: int = 100

    # --- BROKEN: outer contour shape ---
    # 4*pi*A / P^2 of the largest component. Disc ~0.9 (raster), square ~0.78.
    broken_circularity_min: float = 0.70
    # A / convex_hull_area. Bites / chipped edges make the outline concave.
    broken_solidity_min: float = 0.90
    # short_side / long_side of the min-area rectangle. Halves of round snacks ~0.5.
    broken_aspect_ratio_min: float = 0.75
    # A component counts as a separate fragment if its area >= this fraction of the mask.
    broken_fragment_min_ratio: float = 0.05
    # Expected snack area in pixels (0 = disabled). Depends on camera distance / C2 resize.
    broken_expected_area_px: float = 0.0
    # Allowed relative deviation from expected area when enabled (0.25 = +/-25%).
    broken_area_tolerance: float = 0.25

    # --- SPOT_DEFECT: dark area inside the mask ---
    # A pixel is "dark" if its L (Lab lightness, 0-255) < median(L in mask) - delta.
    spot_dark_delta: float = 50.0
    # Maximum allowed dark pixel ratio inside the (eroded) mask.
    spot_ratio_max: float = 0.03
    # Erode the mask by this many pixels before measuring (ignores edge shadows).
    spot_erode_px: int = 3
    # Morphological opening kernel for the dark map (removes 1-2 px noise). 0/1 = off.
    spot_open_kernel: int = 3

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "DefectThresholds":
        env = os.environ if env is None else env
        d = cls()
        return cls(
            min_mask_score=_float(env, "C3_MIN_MASK_SCORE", d.min_mask_score),
            min_mask_area_px=_int(env, "C3_MIN_MASK_AREA_PX", d.min_mask_area_px),
            broken_circularity_min=_float(env, "C3_BROKEN_CIRCULARITY_MIN", d.broken_circularity_min),
            broken_solidity_min=_float(env, "C3_BROKEN_SOLIDITY_MIN", d.broken_solidity_min),
            broken_aspect_ratio_min=_float(env, "C3_BROKEN_ASPECT_RATIO_MIN", d.broken_aspect_ratio_min),
            broken_fragment_min_ratio=_float(env, "C3_BROKEN_FRAGMENT_MIN_RATIO", d.broken_fragment_min_ratio),
            broken_expected_area_px=_float(env, "C3_BROKEN_EXPECTED_AREA_PX", d.broken_expected_area_px),
            broken_area_tolerance=_float(env, "C3_BROKEN_AREA_TOLERANCE", d.broken_area_tolerance),
            spot_dark_delta=_float(env, "C3_SPOT_DARK_DELTA", d.spot_dark_delta),
            spot_ratio_max=_float(env, "C3_SPOT_RATIO_MAX", d.spot_ratio_max),
            spot_erode_px=_int(env, "C3_SPOT_ERODE_PX", d.spot_erode_px),
            spot_open_kernel=_int(env, "C3_SPOT_OPEN_KERNEL", d.spot_open_kernel),
        )


@dataclass(frozen=True)
class Settings:
    # --- service identity ---
    port: int = 8002
    inference_container_id: str = "c3-inference-local"
    schema_version: str = "1.0.0"
    log_level: str = "INFO"

    # --- predictor ---
    predictor: str = "sam2"  # sam2 | mock
    sam2_model_cfg: str = "configs/sam2.1/sam2.1_hiera_t.yaml"
    sam2_checkpoint_path: str = "/app/models/sam2.1_hiera_tiny.pt"
    device: str = "auto"  # auto | cpu | cuda | cuda:N
    amp_dtype: str = "auto"  # auto | none | bfloat16 | float16 (CUDA only)
    warmup_iters: int = 2
    warmup_image_size: int = 1024
    box_batch_size: int = 16

    # --- request limits / output ---
    max_prompts: int = 64
    return_mask: bool = True

    # --- metrics ---
    metrics_window: int = 1000

    # --- mock predictor (tests / pipeline integration without weights) ---
    mock_encoder_delay_ms: float = 0.0
    mock_segment_delay_ms: float = 0.0

    thresholds: DefectThresholds = field(default_factory=DefectThresholds)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        d = cls()
        predictor = _get(env, "C3_PREDICTOR", d.predictor).lower()
        if predictor not in ("sam2", "mock"):
            raise ValueError(f"C3_PREDICTOR must be 'sam2' or 'mock', got {predictor!r}")
        amp = _get(env, "C3_AMP_DTYPE", d.amp_dtype).lower()
        if amp not in ("auto", "none", "bfloat16", "float16"):
            raise ValueError(f"C3_AMP_DTYPE must be auto|none|bfloat16|float16, got {amp!r}")
        return cls(
            port=_int(env, "C3_PORT", d.port),
            inference_container_id=_get(env, "INFERENCE_CONTAINER_ID", d.inference_container_id),
            schema_version=_get(env, "SCHEMA_VERSION", d.schema_version),
            log_level=_get(env, "LOG_LEVEL", d.log_level).upper(),
            predictor=predictor,
            sam2_model_cfg=_get(env, "SAM2_MODEL_CFG", d.sam2_model_cfg),
            sam2_checkpoint_path=_get(env, "SAM2_CHECKPOINT_PATH", d.sam2_checkpoint_path),
            device=_get(env, "C3_DEVICE", d.device).lower(),
            amp_dtype=amp,
            warmup_iters=_int(env, "C3_WARMUP_ITERS", d.warmup_iters),
            warmup_image_size=_int(env, "C3_WARMUP_IMAGE_SIZE", d.warmup_image_size),
            box_batch_size=max(1, _int(env, "C3_BOX_BATCH_SIZE", d.box_batch_size)),
            max_prompts=_int(env, "C3_MAX_PROMPTS", d.max_prompts),
            return_mask=_bool(env, "C3_RETURN_MASK", d.return_mask),
            metrics_window=max(1, _int(env, "C3_METRICS_WINDOW", d.metrics_window)),
            mock_encoder_delay_ms=_float(env, "C3_MOCK_ENCODER_DELAY_MS", d.mock_encoder_delay_ms),
            mock_segment_delay_ms=_float(env, "C3_MOCK_SEGMENT_DELAY_MS", d.mock_segment_delay_ms),
            thresholds=DefectThresholds.from_env(env),
        )
