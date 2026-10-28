"""Per-frame inference pipeline (plan A: frame-independent).

    1. request validation      (prompts / bbox / image presence)
    2. JPEG decode             -> decode_ms
    3. SAM2 image embedding    -> encoder_ms        (once per frame)
    4. bbox-prompted masks     -> segmentation_ms   (all boxes, batched)
    5. defect analysis         -> defect_analysis_ms
    6. mask encoding + result  -> mask_encode_ms
                                  total_inference_ms (1..6)
"""

from __future__ import annotations

import base64
import binascii
import math
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Dict, List, Tuple

import cv2
import numpy as np

from .config import Settings
from .defects import DefectAnalyzer, DefectDecision
from .mask_codec import rle_encode
from .predictor import SegmentationPredictor
from .schemas import Detection, DetectionResult, InferenceTimings, LatencyBreakdown, Mask, ProcessedFrame


class InferenceError(Exception):
    """Request-level failure mapped to an HTTP error response."""

    def __init__(self, code: str, message: str, http_status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


@dataclass
class _ValidPrompt:
    candidate_id: str
    bbox: List[float]  # clipped to the image


def _now_ms() -> int:
    return int(time.time() * 1000)


def _elapsed_ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000.0


def _span(end_ms: int, start_ms: int):
    """end - start in ms across services; None if a clock went backwards (e.g. client clock skew)."""
    value = end_ms - start_ms
    return float(value) if value >= 0 else None


class InferencePipeline:
    def __init__(self, settings: Settings, predictor: SegmentationPredictor) -> None:
        self.settings = settings
        self.predictor = predictor
        self.analyzer = DefectAnalyzer(settings.thresholds)
        # SAM2ImagePredictor keeps the current image embedding as instance state,
        # so set_image + predict_boxes of one frame must not interleave with another.
        self._predictor_lock = threading.Lock()
        self._schema_major = settings.schema_version.split(".")[0]

    # ------------------------------------------------------------ validation
    def _validate(self, frame: ProcessedFrame) -> List[_ValidPrompt]:
        if frame.schema_version.split(".")[0] != self._schema_major:
            raise InferenceError(
                "unsupported_schema_version",
                f"schema_version {frame.schema_version!r} is not compatible with {self.settings.schema_version!r}",
            )
        if len(frame.prompts) > self.settings.max_prompts:
            raise InferenceError(
                "too_many_prompts", f"{len(frame.prompts)} prompts > C3_MAX_PROMPTS={self.settings.max_prompts}"
            )
        seen = set()
        valid: List[_ValidPrompt] = []
        for i, p in enumerate(frame.prompts):
            if p.candidate_id in seen:
                raise InferenceError("duplicate_candidate_id", f"prompts[{i}].candidate_id {p.candidate_id!r} is duplicated")
            seen.add(p.candidate_id)
            x1, y1, x2, y2 = p.bbox
            if not all(math.isfinite(v) for v in p.bbox):
                raise InferenceError("invalid_bbox", f"prompts[{i}].bbox has non-finite values: {p.bbox}")
            if x2 <= x1 or y2 <= y1:
                raise InferenceError("invalid_bbox", f"prompts[{i}].bbox must satisfy x1<x2 and y1<y2: {p.bbox}")
            # Clip to the preprocessed image; a box entirely outside the image is invalid.
            cx1, cy1 = min(max(x1, 0.0), frame.width), min(max(y1, 0.0), frame.height)
            cx2, cy2 = min(max(x2, 0.0), frame.width), min(max(y2, 0.0), frame.height)
            if cx2 - cx1 < 1.0 or cy2 - cy1 < 1.0:
                raise InferenceError(
                    "invalid_bbox", f"prompts[{i}].bbox {p.bbox} lies outside the {frame.width}x{frame.height} image"
                )
            valid.append(_ValidPrompt(p.candidate_id, [cx1, cy1, cx2, cy2]))
        if frame.image_b64 is None:
            raise InferenceError("missing_image", "image_b64 is required by C3 (base64-encoded JPEG)")
        return valid

    def _decode(self, frame: ProcessedFrame) -> np.ndarray:
        try:
            raw = base64.b64decode(frame.image_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise InferenceError("image_decode_failed", f"image_b64 is not valid base64: {exc}") from exc
        bgr = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR) if raw else None
        if bgr is None:
            raise InferenceError("image_decode_failed", "image_b64 could not be decoded as an image (corrupt JPEG?)")
        h, w = bgr.shape[:2]
        if (w, h) != (frame.width, frame.height):
            raise InferenceError(
                "image_size_mismatch", f"decoded image is {w}x{h} but width/height say {frame.width}x{frame.height}"
            )
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

    # ------------------------------------------------------------------- run
    def run(
        self, frame: ProcessedFrame, received_at_ms: int | None = None
    ) -> Tuple[DetectionResult, Dict[str, float], List[DefectDecision]]:
        """received_at_ms: epoch ms when the HTTP request reached C3 (before body parsing / threadpool)."""
        if not self.predictor.is_ready:
            raise InferenceError("model_not_ready", "SAM2 model is not loaded yet", http_status=503)

        started_at_ms = _now_ms()
        received_at_ms = started_at_ms if received_at_ms is None else received_at_ms
        t_total = time.perf_counter()
        timings: Dict[str, float] = {}

        prompts = self._validate(frame)

        t = time.perf_counter()
        image = self._decode(frame)
        timings["decode_ms"] = _elapsed_ms(t)

        timings["lock_wait_ms"] = timings["encoder_ms"] = timings["segmentation_ms"] = 0.0
        seg = None
        if prompts:  # no prompts -> skip the encoder entirely
            boxes = np.asarray([p.bbox for p in prompts], dtype=np.float32)
            t = time.perf_counter()
            with self._predictor_lock:
                timings["lock_wait_ms"] = _elapsed_ms(t)
                t = time.perf_counter()
                self.predictor.set_image(image)
                timings["encoder_ms"] = _elapsed_ms(t)
                t = time.perf_counter()
                seg = self.predictor.predict_boxes(boxes)
                timings["segmentation_ms"] = _elapsed_ms(t)

        t = time.perf_counter()
        decisions = [
            self.analyzer.analyze(image, seg.masks[i], float(seg.mask_scores[i])) for i in range(len(prompts))
        ]
        timings["defect_analysis_ms"] = _elapsed_ms(t)

        t = time.perf_counter()
        detections: List[Detection] = []
        for i, (prompt, decision) in enumerate(zip(prompts, decisions)):
            mask = None
            if self.settings.return_mask and seg.masks[i].any():
                mask = Mask(**rle_encode(seg.masks[i]))
            detections.append(
                Detection(
                    object_id=prompt.candidate_id,
                    bbox=prompt.bbox,
                    status=decision.status,
                    defect_type=decision.defect_type,
                    defect_score=decision.defect_score,
                    mask=mask,
                )
            )
        timings["mask_encode_ms"] = _elapsed_ms(t)

        total_ms = _elapsed_ms(t_total)
        timings["total_inference_ms"] = total_ms
        finished_at_ms = _now_ms()
        r = lambda v: None if v is None else round(v, 3)  # noqa: E731
        breakdown = LatencyBreakdown(
            capture_to_preprocess=r(_span(frame.preprocess_started_at_ms, frame.captured_at_ms)),
            preprocess=r(_span(frame.preprocess_finished_at_ms, frame.preprocess_started_at_ms)),
            c2_to_c3=r(_span(received_at_ms, frame.preprocess_finished_at_ms)),
            c3_queue=r(timings["lock_wait_ms"]),
            c3_service=r(total_ms - timings["lock_wait_ms"]),
            c3_total=r(_span(finished_at_ms, received_at_ms)),
            capture_to_c3_done=r(_span(finished_at_ms, frame.captured_at_ms)),
        )
        result = DetectionResult(
            schema_version=frame.schema_version,
            trace_id=frame.trace_id,
            session_id=frame.session_id,
            frame_id=frame.frame_id,
            captured_at_ms=frame.captured_at_ms,
            inference_container_id=self.settings.inference_container_id,
            inference_started_at_ms=started_at_ms,
            inference_finished_at_ms=finished_at_ms,
            inference_latency_ms=round(total_ms, 3),
            detections=detections,
            preprocess_started_at_ms=frame.preprocess_started_at_ms,
            preprocess_finished_at_ms=frame.preprocess_finished_at_ms,
            inference_received_at_ms=received_at_ms,
            latency_breakdown_ms=breakdown,
            inference_timings_ms=InferenceTimings(
                **{k.removesuffix("_ms").replace("total_inference", "total"): round(v, 3) for k, v in timings.items()}
            ),
        )
        timings = {k: round(v, 3) for k, v in timings.items()}
        return result, timings, decisions

    @staticmethod
    def status_counts(result: DetectionResult) -> Dict[str, int]:
        return dict(Counter(d.status.value for d in result.detections))
