"""In-process metrics for one C3 replica, exposed via GET /metrics (JSON).

Counting units (do not mix them up):
    frame        one /infer request that produced a DetectionResult
    observation  one detection of an object inside one frame
C3 is stateless and frame-independent: the same physical snack seen in N
consecutive frames is N observations. C3 never counts unique snacks; that needs
cross-frame tracking, which belongs to C4 (it receives every replica's results).
Every counter here is per replica; sum across replicas for the whole C3 tier.
"""

from __future__ import annotations

import threading
from collections import Counter, deque
from typing import Any, Deque, Dict, Optional

import numpy as np

STAGES = (
    # Time a request waited for this replica's SAM2 predictor (queueing, not compute).
    # total_inference_ms includes it; total - lock_wait = service time.
    "lock_wait_ms",
    "decode_ms",
    "encoder_ms",
    "segmentation_ms",
    "defect_analysis_ms",
    "mask_encode_ms",
    "total_inference_ms",
)

METRIC_DEFINITIONS = {
    "infer_request_count": "/infer requests received by this replica (= processed_frame_count + error_count)",
    "processed_frame_count": "frames this replica processed successfully (one DetectionResult each)",
    "detected_observation_count": "sum of detections over processed frames; the same snack in N frames counts N times. "
                                  "NOT the number of unique snacks",
    "processed_detection_count": "deprecated alias of detected_observation_count (kept for compatibility)",
    "current_frame_detection_count": "detections in the most recent frame processed by this replica",
    "status_counts": "per-frame observation counts by status (normal/defect/unknown); NOT unique snacks",
    "frames_with_detections": "processed frames with at least one detection",
    "frames_without_detections": "processed frames with zero detections",
    "inference_latency_ms": "per-frame C3 inference time (= DetectionResult.inference_latency_ms), recent window",
    "unique_snacks": "not computed by C3 (stateless, replicas see disjoint frames); track across frames in C4",
}


def _summary(values) -> Dict[str, Any]:
    arr = np.fromiter(values, dtype=np.float64)
    return {
        "count": int(arr.size),
        "mean": round(float(arr.mean()), 3),
        "p50": round(float(np.percentile(arr, 50)), 3),
        "p95": round(float(np.percentile(arr, 95)), 3),
        "max": round(float(arr.max()), 3),
    }


class Metrics:
    def __init__(self, window: int = 1000) -> None:
        self._lock = threading.Lock()
        self.infer_request_count = 0
        self.processed_frame_count = 0
        self.detected_observation_count = 0
        self.frames_with_detections = 0
        self.error_count = 0
        self.errors_by_code: Counter = Counter()
        self.trace_header_mismatch_count = 0
        self.results_push_ok_count = 0
        self.results_push_error_count = 0
        self.results_push_errors: Counter = Counter()
        self.status_counts: Counter = Counter()
        self._windows: Dict[str, Deque[float]] = {s: deque(maxlen=window) for s in STAGES}
        self._detections_window: Deque[int] = deque(maxlen=window)
        self.last_timings: Dict[str, float] = {}
        self.current_frame: Optional[Dict[str, Any]] = None

    def record_success(
        self, timings: Dict[str, float], statuses: Dict[str, int], frame: Optional[Dict[str, Any]] = None
    ) -> None:
        """statuses: {status: count} for ONE frame. frame: trace/session/frame ids and inference_latency_ms."""
        detections = sum(statuses.values())
        with self._lock:
            self.infer_request_count += 1
            self.processed_frame_count += 1
            self.detected_observation_count += detections
            self.frames_with_detections += detections > 0
            self.status_counts.update(statuses)
            self._detections_window.append(detections)
            for stage in STAGES:
                if stage in timings:
                    self._windows[stage].append(timings[stage])
            self.last_timings = dict(timings)
            self.current_frame = {**(frame or {}), "detection_count": detections, "status_counts": dict(statuses)}

    def record_error(self, code: str) -> None:
        with self._lock:
            self.infer_request_count += 1
            self.error_count += 1
            self.errors_by_code[code] += 1

    def record_header_mismatch(self) -> None:
        with self._lock:
            self.trace_header_mismatch_count += 1

    def record_push(self, error: Optional[str] = None) -> None:
        with self._lock:
            if error is None:
                self.results_push_ok_count += 1
            else:
                self.results_push_error_count += 1
                self.results_push_errors[error] += 1

    def snapshot(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self._lock:
            latency = {stage: _summary(values) for stage, values in self._windows.items() if values}
            total = self._windows["total_inference_ms"]
            inference_latency = {"last": self.last_timings.get("total_inference_ms"), **_summary(total)} if total else None
            per_frame = None
            if self._detections_window:
                d = self._detections_window
                per_frame = {"window_frames": len(d), "mean": round(sum(d) / len(d), 3), "max": max(d)}
            out: Dict[str, Any] = {
                "infer_request_count": self.infer_request_count,
                "processed_frame_count": self.processed_frame_count,
                "detected_observation_count": self.detected_observation_count,
                "processed_detection_count": self.detected_observation_count,  # deprecated alias
                "current_frame_detection_count": self.current_frame["detection_count"] if self.current_frame else 0,
                "current_frame": self.current_frame,
                "frames_with_detections": self.frames_with_detections,
                "frames_without_detections": self.processed_frame_count - self.frames_with_detections,
                "detections_per_frame": per_frame,
                "status_counts": dict(self.status_counts),
                "inference_latency_ms": inference_latency,
                "error_count": self.error_count,
                "errors_by_code": dict(self.errors_by_code),
                "trace_header_mismatch_count": self.trace_header_mismatch_count,
                "results_push_ok_count": self.results_push_ok_count,
                "results_push_error_count": self.results_push_error_count,
                "results_push_errors": dict(self.results_push_errors),
                "last_timings_ms": self.last_timings,
                "latency_window_ms": latency,
                "metric_definitions": METRIC_DEFINITIONS,
            }
        if extra:
            out.update(extra)
        return out
