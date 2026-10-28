"""Segmentation predictor interface.

The inference pipeline only talks to this interface, so the real SAM2 model
(sam2_predictor.Sam2ImagePredictorBackend) and the MockPredictor are
interchangeable.

Contract (per frame, called under a lock by the pipeline):
    1. set_image(image_rgb)        -> computes the image embedding ONCE
    2. predict_boxes(boxes_xyxy)   -> masks for all boxes, reusing that embedding

Both calls must be synchronous: when they return, the GPU work they launched is
finished, so the caller's wall-clock timing is accurate.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Dict

import numpy as np


@dataclass
class SegmentationOutput:
    # (N, H, W) bool masks, one per input box, in input order.
    masks: np.ndarray
    # (N,) float SAM2 mask quality score (predicted IoU). NOT a defect score.
    mask_scores: np.ndarray


@dataclass
class LoadInfo:
    model_load_ms: float = 0.0
    warmup_ms: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)


class SegmentationPredictor(abc.ABC):
    """Frame-independent box-prompted segmentation model."""

    name: str = "base"

    @abc.abstractmethod
    def load(self) -> LoadInfo:
        """Load weights and warm up. Called once at startup (may be slow)."""

    @property
    @abc.abstractmethod
    def is_ready(self) -> bool:
        """True once load() succeeded and the model can serve requests."""

    @abc.abstractmethod
    def set_image(self, image_rgb: np.ndarray) -> None:
        """Compute and cache the image embedding for one HxWx3 uint8 RGB frame."""

    @abc.abstractmethod
    def predict_boxes(self, boxes_xyxy: np.ndarray) -> SegmentationOutput:
        """Segment (N, 4) float32 xyxy boxes against the cached embedding."""

    def describe(self) -> Dict[str, Any]:
        """Static info for /healthz (device, model cfg, ...)."""
        return {"predictor": self.name}

    def gpu_memory(self) -> Dict[str, Any] | None:
        """GPU memory usage, or None when not running on a GPU."""
        return None
