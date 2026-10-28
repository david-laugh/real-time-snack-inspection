from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

SERVICE_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = SERVICE_DIR.parents[1]
SCHEMA_DIR = REPO_DIR / "shared" / "schemas"
sys.path.insert(0, str(SERVICE_DIR))

from src.app import create_app  # noqa: E402
from src.config import Settings  # noqa: E402
from src.predictor import MockPredictor  # noqa: E402

BACKGROUND = (70, 70, 70)
SNACK = (210, 170, 110)
SPOT = (45, 30, 20)


def load_schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def make_image(
    width: int = 640,
    height: int = 480,
    discs: Iterable[Sequence[int]] = ((200, 240, 80),),
    spots: Iterable[Sequence[int]] = (),
) -> np.ndarray:
    """RGB image with light snack discs (cx, cy, r) and dark spots (cx, cy, r)."""
    img = np.full((height, width, 3), BACKGROUND, dtype=np.uint8)
    for cx, cy, r in discs:
        cv2.circle(img, (cx, cy), r, SNACK, thickness=-1)
    for cx, cy, r in spots:
        cv2.circle(img, (cx, cy), r, SPOT, thickness=-1)
    return img


def jpeg_b64(image_rgb: np.ndarray, quality: int = 95) -> str:
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(image_rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, quality])
    assert ok
    return base64.b64encode(buf.tobytes()).decode("ascii")


def make_frame(
    image_rgb: Optional[np.ndarray] = None,
    boxes: Sequence[Sequence[float]] = ((120, 160, 280, 320),),
    frame_id: int = 7,
    **overrides,
) -> dict:
    image_rgb = make_image() if image_rgb is None else image_rgb
    h, w = image_rgb.shape[:2]
    frame = {
        "schema_version": "1.0.0",
        "trace_id": "trace-abc",
        "session_id": "session-1",
        "frame_id": frame_id,
        "captured_at_ms": 1_761_600_000_000,
        "preprocess_started_at_ms": 1_761_600_000_010,
        "preprocess_finished_at_ms": 1_761_600_000_020,
        "width": w,
        "height": h,
        "prompts": [{"candidate_id": f"cand-{i}", "type": "box", "bbox": list(b)} for i, b in enumerate(boxes)],
        "image_b64": jpeg_b64(image_rgb),
    }
    frame.update(overrides)
    return frame


def trace_headers(frame: dict) -> dict:
    return {
        "X-Trace-Id": frame["trace_id"],
        "X-Frame-Id": str(frame["frame_id"]),
        "X-Session-Id": frame["session_id"],
    }


def disc_mask(shape, cx, cy, r) -> np.ndarray:
    m = np.zeros(shape, dtype=np.uint8)
    cv2.circle(m, (cx, cy), r, 1, thickness=-1)
    return m.astype(bool)


@pytest.fixture
def settings() -> Settings:
    return Settings.from_env({"INFERENCE_CONTAINER_ID": "c3-inference-test", "C3_PREDICTOR": "mock"})


def _client(settings: Settings, predictor: MockPredictor, load: bool) -> TestClient:
    if load:
        predictor.load()
    app = create_app(settings=settings, predictor=predictor, load_model=False)
    return TestClient(app)


@pytest.fixture
def mock_predictor() -> MockPredictor:
    return MockPredictor()


@pytest.fixture
def client(settings, mock_predictor):
    with _client(settings, mock_predictor, load=True) as c:
        yield c


@pytest.fixture
def make_client(settings):
    """Factory: make_client(predictor, load=True, settings=None)."""
    clients: List[TestClient] = []

    def factory(predictor: MockPredictor, load: bool = True, settings_override: Optional[Settings] = None):
        c = _client(settings_override or settings, predictor, load)
        c.__enter__()
        clients.append(c)
        return c

    yield factory
    for c in clients:
        c.__exit__(None, None, None)
