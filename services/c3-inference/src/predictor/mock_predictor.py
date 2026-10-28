"""Mock predictor: same interface as SAM2, no torch / weights required.

Default behaviour: the mask for each box is the filled ellipse inscribed in the
box (clipped to the image), with a fixed mask score. Tests can inject
`mask_fn(image_rgb, box) -> (mask, score)` to produce arbitrary masks, and
configure artificial latency / load failure.
"""

from __future__ import annotations

import time
from typing import Callable, Optional, Tuple

import cv2
import numpy as np

from .base import LoadInfo, SegmentationOutput, SegmentationPredictor

MaskFn = Callable[[np.ndarray, np.ndarray], Tuple[np.ndarray, float]]


def ellipse_mask(image_rgb: np.ndarray, box: np.ndarray) -> Tuple[np.ndarray, float]:
    h, w = image_rgb.shape[:2]
    mask = np.zeros((h, w), dtype=np.uint8)
    x1, y1, x2, y2 = box
    center = (int(round((x1 + x2) / 2)), int(round((y1 + y2) / 2)))
    axes = (max(1, int(round((x2 - x1) / 2))), max(1, int(round((y2 - y1) / 2))))
    cv2.ellipse(mask, center, axes, 0, 0, 360, 1, thickness=-1)
    return mask.astype(bool), 0.95


class MockPredictor(SegmentationPredictor):
    name = "mock"

    def __init__(
        self,
        encoder_delay_ms: float = 0.0,
        segment_delay_ms: float = 0.0,
        load_delay_s: float = 0.0,
        fail_load: Optional[Exception] = None,
        mask_fn: MaskFn = ellipse_mask,
    ) -> None:
        self.encoder_delay_ms = encoder_delay_ms
        self.segment_delay_ms = segment_delay_ms
        self.load_delay_s = load_delay_s
        self.fail_load = fail_load
        self.mask_fn = mask_fn
        self._ready = False
        self._image: Optional[np.ndarray] = None
        # Call counters let tests assert "one embedding per frame".
        self.set_image_calls = 0
        self.predict_calls = 0

    def load(self) -> LoadInfo:
        t0 = time.perf_counter()
        if self.load_delay_s:
            time.sleep(self.load_delay_s)
        if self.fail_load is not None:
            raise self.fail_load
        self._ready = True
        return LoadInfo(model_load_ms=(time.perf_counter() - t0) * 1000.0, warmup_ms=0.0)

    @property
    def is_ready(self) -> bool:
        return self._ready

    def set_image(self, image_rgb: np.ndarray) -> None:
        self.set_image_calls += 1
        if self.encoder_delay_ms:
            time.sleep(self.encoder_delay_ms / 1000.0)
        self._image = image_rgb

    def predict_boxes(self, boxes_xyxy: np.ndarray) -> SegmentationOutput:
        if self._image is None:
            raise RuntimeError("set_image() must be called before predict_boxes()")
        self.predict_calls += 1
        if self.segment_delay_ms:
            time.sleep(self.segment_delay_ms / 1000.0)
        h, w = self._image.shape[:2]
        masks = np.zeros((len(boxes_xyxy), h, w), dtype=bool)
        scores = np.zeros((len(boxes_xyxy),), dtype=np.float32)
        for i, box in enumerate(boxes_xyxy):
            masks[i], scores[i] = self.mask_fn(self._image, box)
        return SegmentationOutput(masks=masks, mask_scores=scores)

    def describe(self) -> dict:
        return {"predictor": self.name, "device": "cpu"}
