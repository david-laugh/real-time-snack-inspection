"""resize -> 후보 bbox 검출 -> processed-frame 메시지 생성. 네트워크 I/O 없음."""
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np

from . import codec, detect
from .config import Settings

COMMON_FIELDS = ("schema_version", "trace_id", "session_id", "frame_id", "captured_at_ms")


def now_ms() -> int:
    return int(time.time() * 1000)


def resize_long_side(bgr: np.ndarray, long_side: int) -> tuple[np.ndarray, float]:
    """긴 변이 long_side가 되도록 축소. 확대는 하지 않는다. (이미지, 원본 대비 배율) 반환."""
    h, w = bgr.shape[:2]
    if long_side <= 0 or max(h, w) <= long_side:
        return bgr, 1.0
    scale = long_side / max(h, w)
    size = (max(1, round(w * scale)), max(1, round(h * scale)))
    return cv2.resize(bgr, size, interpolation=cv2.INTER_AREA), scale


class DiameterTracker:
    """session_id별 쿠키 지름 추정값을 EMA로 평활화. 추정 실패 프레임은 이전 값을 유지한다."""

    def __init__(self, alpha: float = 0.2, max_sessions: int = 256):
        self.alpha = alpha
        self.max_sessions = max_sessions
        self._values: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def update(self, session_id: str, estimate: Optional[float]) -> Optional[float]:
        with self._lock:
            prev = self._values.get(session_id)
            if estimate is None:
                value = prev
            elif prev is None:
                value = estimate
            else:
                value = self.alpha * estimate + (1 - self.alpha) * prev
            if value is not None:
                self._values[session_id] = value
                self._values.move_to_end(session_id)
                while len(self._values) > self.max_sessions:
                    self._values.popitem(last=False)
            return value


@dataclass
class Detection:
    boxes: list
    scale: float               # 원본 대비 배율
    diameter_px: Optional[float]   # 전처리 후 이미지 기준 지름 (검출에 실제 사용한 값)
    roi: Optional[list]


def resolve_roi(settings: Settings, scale: float):
    """detect.filter_candidates에 넘길 roi 값."""
    if settings.roi_mode == "auto":
        return "auto"
    if settings.roi_mode == "fixed" and settings.roi:
        return [round(v * scale) for v in settings.roi]
    return None


def detect_frame(bgr: np.ndarray, settings: Settings, session_id: str,
                 tracker: Optional[DiameterTracker]) -> tuple[np.ndarray, Detection]:
    resized, scale = resize_long_side(bgr, settings.resize_long_side)
    mask = detect.build_mask(resized, settings.hsv_low, settings.hsv_high)
    if settings.expected_diameter_px is not None:
        diameter = settings.expected_diameter_px * scale
    else:
        diameter = detect.estimate_diameter(mask)
        if tracker is not None:
            diameter = tracker.update(session_id, diameter)
    roi = resolve_roi(settings, scale)
    if roi == "auto":
        roi = detect.find_stage_roi(resized)
    boxes = [] if diameter is None else detect.filter_candidates(resized, mask, diameter, roi=roi)
    return resized, Detection(boxes, scale, diameter, roi)


def build_processed_frame(envelope: dict, settings: Settings,
                          tracker: Optional[DiameterTracker] = None) -> tuple[dict, Detection]:
    """frame-envelope -> processed-frame. 스키마에 정의된 필드만 넣는다."""
    started = now_ms()
    bgr = codec.decode_frame(envelope)
    resized, det = detect_frame(bgr, settings, envelope["session_id"], tracker)
    image_fields = codec.encode_frame(resized, settings.output_image_format, settings.jpeg_quality)
    h, w = resized.shape[:2]
    msg = {k: envelope[k] for k in COMMON_FIELDS}
    msg.update({
        "preprocess_started_at_ms": started,
        "preprocess_finished_at_ms": now_ms(),
        "width": w,
        "height": h,
        "prompts": detect.to_prompts(det.boxes),
        **image_fields,
    })
    return msg, det
