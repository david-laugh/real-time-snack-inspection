"""In-process metrics for one C3 replica, exposed via GET /metrics (JSON)."""

from __future__ import annotations

import threading
from collections import Counter, deque
from typing import Any, Deque, Dict, Optional

import numpy as np

STAGES = (
    "decode_ms",
    "encoder_ms",
    "segmentation_ms",
    "defect_analysis_ms",
    "mask_encode_ms",
    "total_inference_ms",
)


class Metrics:
    def __init__(self, window: int = 1000) -> None:
        self._lock = threading.Lock()
        self.processed_frame_count = 0
        self.processed_detection_count = 0
        self.error_count = 0
        self.errors_by_code: Counter = Counter()
        self.trace_header_mismatch_count = 0
        self.results_push_ok_count = 0
        self.results_push_error_count = 0
        self.results_push_errors: Counter = Counter()
        self.status_counts: Counter = Counter()
        self._windows: Dict[str, Deque[float]] = {s: deque(maxlen=window) for s in STAGES}
        self.last_timings: Dict[str, float] = {}

    def record_success(self, timings: Dict[str, float], statuses: Dict[str, int]) -> None:
        with self._lock:
            self.processed_frame_count += 1
            self.processed_detection_count += sum(statuses.values())
            self.status_counts.update(statuses)
            for stage in STAGES:
                if stage in timings:
                    self._windows[stage].append(timings[stage])
            self.last_timings = dict(timings)

    def record_error(self, code: str) -> None:
        with self._lock:
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
            latency = {}
            for stage, values in self._windows.items():
                if not values:
                    continue
                arr = np.fromiter(values, dtype=np.float64)
                latency[stage] = {
                    "count": int(arr.size),
                    "mean": round(float(arr.mean()), 3),
                    "p50": round(float(np.percentile(arr, 50)), 3),
                    "p95": round(float(np.percentile(arr, 95)), 3),
                    "max": round(float(arr.max()), 3),
                }
            out: Dict[str, Any] = {
                "processed_frame_count": self.processed_frame_count,
                "processed_detection_count": self.processed_detection_count,
                "error_count": self.error_count,
                "errors_by_code": dict(self.errors_by_code),
                "trace_header_mismatch_count": self.trace_header_mismatch_count,
                "results_push_ok_count": self.results_push_ok_count,
                "results_push_error_count": self.results_push_error_count,
                "results_push_errors": dict(self.results_push_errors),
                "status_counts": dict(self.status_counts),
                "last_timings_ms": self.last_timings,
                "latency_window_ms": latency,
            }
        if extra:
            out.update(extra)
        return out
