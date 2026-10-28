import base64
import csv
import json

import cv2
import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from c2 import schemas
from c2.app import create_app
from c2.config import Settings
from c2.pipeline import COMMON_FIELDS
from helpers import evaluate, load_fixture, make_envelope

INFER_URL = "http://nginx-inference-lb:8080/infer"


class FakeInfer:
    """/infer mock. 받은 요청을 기록하고 지정한 상태 코드로 응답한다."""

    def __init__(self, status=200, exc=None):
        self.status, self.exc, self.requests = status, exc, []

    def __call__(self, request: httpx.Request):
        self.requests.append(request)
        if self.exc:
            raise self.exc
        return httpx.Response(self.status, json={"ok": True})


def _client(fake, **settings):
    app = create_app(Settings(infer_url=INFER_URL, **settings), transport=httpx.MockTransport(fake))
    return TestClient(app)


def test_healthz():
    with _client(FakeInfer()) as c:
        r = c.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok", "mqtt": {"enabled": False}}


@pytest.mark.parametrize("fmt", ["png", "jpeg"])
def test_preprocess_forwards_valid_processed_frame(fmt):
    img, gt = load_fixture()
    envelope = make_envelope(img, fmt=fmt)
    fake = FakeInfer()
    with _client(fake) as c:
        r = c.post("/preprocess", json=envelope)
    assert r.status_code == 200, r.text
    assert len(fake.requests) == 1

    req = fake.requests[0]
    assert str(req.url) == INFER_URL and req.method == "POST"
    sent = json.loads(req.content)

    # processed-frame 스키마 통과 + 스키마에 없는 필드 없음(additionalProperties: false)
    assert schemas.errors("processed-frame", sent) == []

    # 공통 필드는 그대로 전파
    for k in COMMON_FIELDS:
        assert sent[k] == envelope[k], k

    # 헤더 전파
    assert req.headers["X-Trace-Id"] == envelope["trace_id"]
    assert req.headers["X-Frame-Id"] == str(envelope["frame_id"])
    assert req.headers["X-Session-Id"] == envelope["session_id"]

    # 긴 변 1024 기준 리사이즈, 전달한 이미지 크기와 width/height 일치
    scale = 1024 / max(img.shape[:2])
    assert (sent["width"], sent["height"]) == (1024, round(img.shape[0] * scale))
    out = cv2.imdecode(np.frombuffer(base64.b64decode(sent["image_b64"]), np.uint8), cv2.IMREAD_COLOR)
    assert out.shape[:2] == (sent["height"], sent["width"])

    # 타임스탬프
    assert sent["preprocess_started_at_ms"] <= sent["preprocess_finished_at_ms"]

    # prompts: 리사이즈 후 좌표 기준 정확도
    assert [p["candidate_id"] for p in sent["prompts"]] == [f"c{i}" for i in range(len(sent["prompts"]))]
    assert all(p["type"] == "box" for p in sent["prompts"])
    res = evaluate([p["bbox"] for p in sent["prompts"]], [[v * scale for v in b] for b in gt])
    assert res["recall"] == 1.0 and res["mean_iou"] >= 0.95, res

    body = r.json()
    assert body["trace_id"] == envelope["trace_id"] and body["num_prompts"] == len(sent["prompts"])


def test_session_diameter_is_smoothed_across_frames():
    img, _ = load_fixture()
    with _client(FakeInfer()) as c:
        c.post("/preprocess", json=make_envelope(img, frame_id=1))
        blank = np.full_like(img, 40)                    # 쿠키가 없어 추정 실패하는 프레임
        r = c.post("/preprocess", json=make_envelope(blank, frame_id=2))
        assert r.status_code == 200
        tracker = c.app.state.diameter_tracker
        assert tracker.update("sess-test", None) is not None   # 이전 값을 유지


def test_empty_frame_is_not_forwarded(tmp_path):
    """prompts 0개 프레임은 /infer로 보내지 않고 skipped_empty로 기록한다 (기본 C2_SKIP_EMPTY=true)."""
    img, _ = load_fixture()
    blank = np.full_like(img, 40)
    log_path = tmp_path / "c2_events.csv"
    fake = FakeInfer()
    with _client(fake, event_log=str(log_path)) as c:
        r = c.post("/preprocess", json=make_envelope(blank, frame_id=1))
        assert r.status_code == 200, r.text
        assert r.json()["num_prompts"] == 0 and r.json()["infer_status"] is None
        assert not fake.requests
        assert c.post("/preprocess", json=make_envelope(img, frame_id=2)).status_code == 200
        assert len(fake.requests) == 1                   # 쿠키가 있는 프레임은 그대로 전달
        stats = c.get("/stats").json()
    assert stats["events"]["skipped_empty"] == 1 and stats["events"]["forwarded"] == 1
    assert stats["skip_empty"] is True
    rows = list(csv.DictReader(log_path.open(encoding="utf-8")))
    assert [(r["trace_id"], r["event"]) for r in rows] == [("trace-000001", "skipped_empty"),
                                                          ("trace-000002", "forwarded")]
    assert rows[0]["preprocess_ms"] != "" and rows[0]["infer_rtt_ms"] == ""


def test_skip_empty_setting_from_env(monkeypatch):
    assert Settings().skip_empty is True
    monkeypatch.setenv("C2_SKIP_EMPTY", "false")
    assert Settings().skip_empty is False


def test_invalid_envelope_rejected():
    img, _ = load_fixture()
    envelope = make_envelope(img)
    del envelope["trace_id"]
    fake = FakeInfer()
    with _client(fake) as c:
        r = c.post("/preprocess", json=envelope)
    assert r.status_code == 422 and not fake.requests


@pytest.mark.parametrize("patch", [
    {"image_b64": "!!!not base64"},
    {"image_b64": base64.b64encode(b"not an image").decode()},
    {"format": "raw_bgr"},
])
def test_undecodable_frame_rejected(patch):
    img, _ = load_fixture()
    envelope = {**make_envelope(img), **patch}
    fake = FakeInfer()
    with _client(fake) as c:
        r = c.post("/preprocess", json=envelope)
    assert r.status_code == 422 and not fake.requests


@pytest.mark.parametrize("fake", [
    FakeInfer(status=503),
    FakeInfer(exc=httpx.ReadTimeout("timeout")),
    FakeInfer(exc=httpx.ConnectError("refused")),
])
def test_infer_failure_returns_502(fake):
    img, _ = load_fixture()
    with _client(fake) as c:
        r = c.post("/preprocess", json=make_envelope(img))
    assert r.status_code == 502
