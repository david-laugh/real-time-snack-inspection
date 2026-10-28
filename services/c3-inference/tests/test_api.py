"""API integration tests with MockPredictor (no SAM2 / torch required)."""

from __future__ import annotations

import base64
import time

import cv2
import jsonschema
import numpy as np
import pytest

from conftest import disc_mask, jpeg_b64, load_schema, make_frame, make_image, trace_headers
from src.mask_codec import rle_decode
from src.predictor import MockPredictor

RESULT_SCHEMA = load_schema("detection-result.schema.json")
FRAME_SCHEMA = load_schema("processed-frame.schema.json")


def assert_valid_result(body: dict) -> None:
    jsonschema.Draft202012Validator(RESULT_SCHEMA).validate(body)


# --------------------------------------------------------------- valid frame
def test_valid_frame_returns_schema_valid_detection_result(client, mock_predictor):
    image = make_image(discs=[(200, 240, 80), (450, 240, 70)])
    frame = make_frame(image, boxes=[(120, 160, 280, 320), (380, 170, 520, 310)])
    jsonschema.Draft202012Validator(FRAME_SCHEMA).validate(frame)  # request is a real ProcessedFrame

    r = client.post("/infer", json=frame, headers=trace_headers(frame))

    assert r.status_code == 200, r.text
    body = r.json()
    assert_valid_result(body)
    # Common fields propagated unchanged.
    for key in ("schema_version", "trace_id", "session_id", "frame_id", "captured_at_ms"):
        assert body[key] == frame[key]
    assert body["inference_container_id"] == "c3-inference-test"
    assert body["inference_finished_at_ms"] >= body["inference_started_at_ms"]
    assert [d["object_id"] for d in body["detections"]] == ["cand-0", "cand-1"]
    assert [d["status"] for d in body["detections"]] == ["normal", "normal"]
    assert all(d["defect_type"] is None for d in body["detections"])
    assert all(0.0 <= d["defect_score"] < 0.5 for d in body["detections"])

    # Mask round-trips to the mock's ellipse == the drawn disc (+/- raster edge).
    mask = rle_decode(body["detections"][0]["mask"])
    assert mask.shape == (480, 640)
    iou = (mask & disc_mask(mask.shape, 200, 240, 80)).sum() / (mask | disc_mask(mask.shape, 200, 240, 80)).sum()
    assert iou > 0.95

    # Trace headers echoed, per-stage timings exposed as headers.
    assert r.headers["X-Trace-Id"] == "trace-abc"
    assert r.headers["X-Frame-Id"] == "7"
    assert r.headers["X-Session-Id"] == "session-1"
    assert r.headers["X-Inference-Container-Id"] == "c3-inference-test"
    for h in ("X-C3-Decode-Ms", "X-C3-Encoder-Ms", "X-C3-Segmentation-Ms", "X-C3-Defect-Analysis-Ms", "X-C3-Total-Inference-Ms"):
        assert float(r.headers[h]) >= 0.0
    assert "encoder;dur=" in r.headers["Server-Timing"]

    # One image embedding per frame, all boxes in one decoder call.
    assert mock_predictor.set_image_calls == 1
    assert mock_predictor.predict_calls == 1


def test_spot_defect_detected_when_enabled(make_client):
    from src.config import Settings

    client = make_client(MockPredictor(), settings_override=Settings.from_env(
        {"C3_PREDICTOR": "mock", "C3_RESULTS_URL": "", "C3_SPOT_ENABLED": "1"}))
    image = make_image(discs=[(200, 240, 80)], spots=[(200, 240, 22)])
    r = client.post("/infer", json=make_frame(image))
    assert r.status_code == 200, r.text
    det = r.json()["detections"][0]
    assert det["status"] == "defect"
    assert det["defect_type"] == "spot_defect"
    assert det["defect_score"] > 0.5


def test_broken_detected(make_client):
    def pacman(image, box):
        h, w = image.shape[:2]
        m = np.zeros((h, w), np.uint8)
        cv2.ellipse(m, (200, 240), (80, 80), 0, 40, 320, 1, thickness=-1)  # 80 deg bite
        return m.astype(bool), 0.93

    client = make_client(MockPredictor(mask_fn=pacman))
    r = client.post("/infer", json=make_frame())
    assert r.status_code == 200, r.text
    det = r.json()["detections"][0]
    assert_valid_result(r.json())
    assert (det["status"], det["defect_type"]) == ("defect", "broken")


def test_low_mask_score_is_unknown(make_client):
    def weak(image, box):
        return disc_mask(image.shape[:2], 200, 240, 80), 0.2  # SAM2 unsure

    client = make_client(MockPredictor(mask_fn=weak))
    det = client.post("/infer", json=make_frame()).json()["detections"][0]
    assert (det["status"], det["defect_type"], det["defect_score"]) == ("unknown", None, 0.0)


# ----------------------------------------------------------------- no bbox
def test_frame_without_prompts_skips_encoder(client, mock_predictor):
    frame = make_frame(boxes=[])
    r = client.post("/infer", json=frame)
    assert r.status_code == 200, r.text
    body = r.json()
    assert_valid_result(body)
    assert body["detections"] == []
    assert mock_predictor.set_image_calls == 0
    assert float(r.headers["X-C3-Encoder-Ms"]) == 0.0
    assert float(r.headers["X-C3-Decode-Ms"]) > 0.0  # image is still validated


# ------------------------------------------------------------- invalid bbox
@pytest.mark.parametrize(
    "boxes, code",
    [
        ([(280, 160, 120, 320)], "invalid_bbox"),  # x2 < x1
        ([(120, 160, 120, 320)], "invalid_bbox"),  # zero width
        ([(700, 500, 800, 600)], "invalid_bbox"),  # entirely outside 640x480
        ([(-50, -50, -10, -10)], "invalid_bbox"),
        ([(120, 160, 280)], "invalid_request"),  # schema: exactly 4 numbers
        ([("a", 160, 280, 320)], "invalid_request"),
    ],
)
def test_invalid_bbox_rejected(client, boxes, code):
    r = client.post("/infer", json=make_frame(boxes=boxes))
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == code


def test_duplicate_candidate_id_rejected(client):
    frame = make_frame(boxes=[(120, 160, 280, 320), (130, 170, 270, 310)])
    frame["prompts"][1]["candidate_id"] = frame["prompts"][0]["candidate_id"]
    r = client.post("/infer", json=frame)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "duplicate_candidate_id"


def test_bbox_partially_outside_is_clipped(client):
    image = make_image(discs=[(600, 240, 60)])
    r = client.post("/infer", json=make_frame(image, boxes=[(540, 180, 700, 300)]))
    assert r.status_code == 200, r.text
    assert r.json()["detections"][0]["bbox"] == [540.0, 180.0, 640.0, 300.0]


# ------------------------------------------------------------ broken image
def test_corrupt_jpeg_rejected(client):
    raw = base64.b64decode(jpeg_b64(make_image()))
    corrupt = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00garbage" * 50).decode()
    truncated = base64.b64encode(raw[:200]).decode()
    for payload in (corrupt, truncated, "not base64 !!!"):
        r = client.post("/infer", json=make_frame(image_b64=payload), headers={"X-Trace-Id": "t-bad"})
        assert r.status_code == 422, r.text
        err = r.json()["error"]
        assert err["code"] == "image_decode_failed"
        assert err["trace_id"] == "trace-abc"  # body trace id wins once the body is parsed
        assert r.headers["X-Trace-Id"] == "trace-abc"


def test_missing_image_rejected(client):
    frame = make_frame()
    del frame["image_b64"]
    r = client.post("/infer", json=frame)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "missing_image"


def test_image_size_mismatch_rejected(client):
    r = client.post("/infer", json=make_frame(width=1024, height=1024))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "image_size_mismatch"


def test_unknown_field_and_bad_schema_version_rejected(client):
    r = client.post("/infer", json=make_frame(extra_field=1))
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"
    r = client.post("/infer", json=make_frame(schema_version="2.0.0"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "unsupported_schema_version"


def test_malformed_json_uses_header_trace(client):
    r = client.post("/infer", content=b"{not json", headers={"X-Trace-Id": "t-1", "X-Frame-Id": "3", "X-Session-Id": "s"})
    assert r.status_code == 422
    assert r.json()["error"]["trace_id"] == "t-1"
    assert r.headers["X-Frame-Id"] == "3"


# ------------------------------------------------------------ model state
def test_model_not_ready(make_client):
    client = make_client(MockPredictor(), load=False)
    h = client.get("/healthz")
    assert h.status_code == 503
    assert h.json()["model_ready"] is False
    r = client.post("/infer", json=make_frame(), headers={"X-Trace-Id": "t-1"})
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "1"
    assert r.json()["error"]["code"] == "model_not_ready"
    assert client.get("/metrics").json()["error_count"] == 1


def test_background_loading_then_ready(settings):
    from fastapi.testclient import TestClient

    from src.app import create_app

    app = create_app(settings=settings, predictor=MockPredictor(load_delay_s=0.3))
    with TestClient(app) as client:
        first = client.get("/healthz")
        assert first.status_code == 503 and first.json()["status"] == "loading"
        deadline = time.time() + 5
        while client.get("/healthz").status_code != 200 and time.time() < deadline:
            time.sleep(0.05)
        ready = client.get("/healthz").json()
        assert ready["status"] == "ready" and ready["model_ready"] is True
        assert ready["model_load_ms"] >= 300
        assert client.post("/infer", json=make_frame()).status_code == 200


def test_model_load_failure_reported(settings):
    from fastapi.testclient import TestClient

    from src.app import create_app

    app = create_app(settings=settings, predictor=MockPredictor(fail_load=FileNotFoundError("ckpt missing")))
    with TestClient(app) as client:
        deadline = time.time() + 5
        while client.get("/healthz").json()["status"] == "loading" and time.time() < deadline:
            time.sleep(0.02)
        h = client.get("/healthz")
        assert h.status_code == 503
        assert h.json()["status"] == "error"
        assert "ckpt missing" in h.json()["error"]


# -------------------------------------------------------- latency/metrics
def test_latency_measurement_and_metrics(make_client):
    client = make_client(MockPredictor(encoder_delay_ms=30, segment_delay_ms=20))
    frame = make_frame()
    timings = []
    for i in range(5):
        r = client.post("/infer", json=dict(frame, frame_id=i))
        assert r.status_code == 200
        t = {h: float(r.headers[h]) for h in ("X-C3-Decode-Ms", "X-C3-Encoder-Ms", "X-C3-Segmentation-Ms",
                                                "X-C3-Defect-Analysis-Ms", "X-C3-Total-Inference-Ms")}
        assert t["X-C3-Encoder-Ms"] >= 30
        assert t["X-C3-Segmentation-Ms"] >= 20
        stage_sum = t["X-C3-Decode-Ms"] + t["X-C3-Encoder-Ms"] + t["X-C3-Segmentation-Ms"] + t["X-C3-Defect-Analysis-Ms"]
        assert t["X-C3-Total-Inference-Ms"] >= stage_sum
        assert r.json()["inference_latency_ms"] == pytest.approx(t["X-C3-Total-Inference-Ms"], abs=0.01)
        timings.append(t)
    client.post("/infer", json=make_frame(boxes=[(1, 1, 0, 0)]))  # one error

    m = client.get("/metrics").json()
    assert m["processed_frame_count"] == 5
    assert m["error_count"] == 1
    assert m["errors_by_code"] == {"invalid_bbox": 1}
    assert m["inference_container_id"] == "c3-inference-test"
    for stage in ("lock_wait_ms", "decode_ms", "encoder_ms", "segmentation_ms", "defect_analysis_ms", "total_inference_ms"):
        assert m["latency_window_ms"][stage]["count"] == 5
    assert m["latency_window_ms"]["encoder_ms"]["p50"] >= 30
    assert m["model"]["status"] == "ready" and m["model"]["model_ready"] is True
    assert m["gpu_memory"] is None  # mock runs on CPU
    print("\nmock latency per frame (ms):", [round(t["X-C3-Total-Inference-Ms"], 2) for t in timings])


def test_trace_header_mismatch_counted(client):
    frame = make_frame()
    r = client.post("/infer", json=frame, headers={"X-Trace-Id": "other", "X-Frame-Id": "7"})
    assert r.status_code == 200
    assert r.headers["X-Trace-Id"] == "trace-abc"
    assert client.get("/metrics").json()["trace_header_mismatch_count"] == 1


# ------------------------------------------------------------ C3 -> C4
def _c4_settings(url="http://c4-visualizer:8003/results"):
    from src.config import Settings

    return Settings.from_env({"INFERENCE_CONTAINER_ID": "c3-inference-test", "C3_PREDICTOR": "mock", "C3_RESULTS_URL": url})


def _c4_client(handler, url="http://c4-visualizer:8003/results"):
    import httpx
    from fastapi.testclient import TestClient

    from src.app import create_app

    predictor = MockPredictor()
    predictor.load()
    app = create_app(settings=_c4_settings(url), predictor=predictor, load_model=False,
                     results_transport=httpx.MockTransport(handler))
    return TestClient(app)


def test_result_is_delivered_to_c4():
    import json

    received = []

    def c4(request):
        received.append(request)
        return __import__("httpx").Response(200, json={"ok": True})

    with _c4_client(c4) as client:
        frame = make_frame()
        r = client.post("/infer", json=frame, headers=trace_headers(frame))
        assert r.status_code == 200
        assert len(received) == 1
        req = received[0]
        assert str(req.url) == "http://c4-visualizer:8003/results"
        assert req.method == "POST"
        assert json.loads(req.content) == r.json()  # exactly the DetectionResult returned to C2
        assert_valid_result(json.loads(req.content))
        assert req.headers["X-Trace-Id"] == "trace-abc" and req.headers["X-Frame-Id"] == "7"
        m = client.get("/metrics").json()
        assert (m["results_push_ok_count"], m["results_push_error_count"]) == (1, 0)
        assert m["results_url"] == "http://c4-visualizer:8003/results"


@pytest.mark.parametrize("failure, code", [("http_500", "http_500"), ("connect", "ConnectError"), ("timeout", "timeout")])
def test_c4_failure_does_not_fail_infer(failure, code):
    import httpx

    def c4(request):
        if failure == "http_500":
            return httpx.Response(500)
        if failure == "connect":
            raise httpx.ConnectError("refused", request=request)
        raise httpx.ReadTimeout("slow", request=request)

    with _c4_client(c4) as client:
        r = client.post("/infer", json=make_frame())
        assert r.status_code == 200
        assert r.json()["detections"][0]["status"] == "normal"
        m = client.get("/metrics").json()
        assert m["results_push_error_count"] == 1
        assert m["results_push_errors"] == {code: 1}
        assert m["error_count"] == 0  # /infer itself succeeded


def test_c4_delivery_disabled_with_empty_url(client):
    assert client.post("/infer", json=make_frame()).status_code == 200
    m = client.get("/metrics").json()
    assert (m["results_push_ok_count"], m["results_push_error_count"], m["results_url"]) == (0, 0, None)


def test_errors_are_not_delivered_to_c4():
    calls = []
    with _c4_client(lambda req: calls.append(req) or __import__("httpx").Response(200)) as client:
        assert client.post("/infer", json=make_frame(boxes=[(1, 1, 0, 0)])).status_code == 422
        assert calls == []


# ------------------------------------------------- latency breakdown (1.1.0)
def test_latency_breakdown_in_result(make_client):
    client = make_client(MockPredictor(encoder_delay_ms=20, segment_delay_ms=10))
    now = int(time.time() * 1000)
    frame = make_frame(captured_at_ms=now - 300, preprocess_started_at_ms=now - 250, preprocess_finished_at_ms=now - 230)
    r = client.post("/infer", json=frame, headers=trace_headers(frame))
    assert r.status_code == 200, r.text
    body = r.json()
    assert_valid_result(body)  # shared schema with the 1.1.0 optional fields
    assert (body["preprocess_started_at_ms"], body["preprocess_finished_at_ms"]) == (now - 250, now - 230)
    lb = body["latency_breakdown_ms"]
    assert lb["capture_to_preprocess"] == 50.0
    assert lb["preprocess"] == 20.0
    # C2 finished 230 ms ago -> the C2 -> C3 hop is ~230 ms (request handled right away)
    assert lb["c2_to_c3"] == body["inference_received_at_ms"] - (now - 230)
    assert 230 <= lb["c2_to_c3"] < 1000
    assert lb["c3_service"] >= 30  # mock encoder 20 + segment 10
    assert lb["c3_queue"] == body["inference_timings_ms"]["lock_wait"]
    assert lb["c3_total"] == body["inference_finished_at_ms"] - body["inference_received_at_ms"]
    assert lb["capture_to_c3_done"] == body["inference_finished_at_ms"] - (now - 300)
    t = body["inference_timings_ms"]
    assert t["encoder"] == pytest.approx(float(r.headers["X-C3-Encoder-Ms"]), abs=0.01)
    assert t["total"] == pytest.approx(body["inference_latency_ms"], abs=0.01)


def test_latency_breakdown_clock_skew_is_null(client):
    future = int(time.time() * 1000) + 60_000  # C2 clock ahead of C3 by a minute
    frame = make_frame(captured_at_ms=future - 30, preprocess_started_at_ms=future - 20, preprocess_finished_at_ms=future)
    body = client.post("/infer", json=frame).json()
    assert_valid_result(body)
    lb = body["latency_breakdown_ms"]
    assert lb["c2_to_c3"] is None and lb["capture_to_c3_done"] is None
    assert lb["preprocess"] == 20.0  # same-clock segments still reported


def test_c4_receives_latency_breakdown():
    import json

    received = []
    with _c4_client(lambda req: received.append(json.loads(req.content)) or __import__("httpx").Response(200)) as client:
        client.post("/infer", json=make_frame())
    assert set(received[0]["latency_breakdown_ms"]) == {
        "capture_to_preprocess", "preprocess", "c2_to_c3", "c3_queue", "c3_service", "c3_total", "capture_to_c3_done"}
