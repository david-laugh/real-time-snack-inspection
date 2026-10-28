"""Frame vs. observation accounting in /metrics (C3 counts observations, never unique snacks).

Uses C2's fixture images with the stored real-SAM2 masks (tests/fixtures/c2_snack_masks.json)
served through MockPredictor, so the numbers are those of the real pipeline without a GPU.
"""

from __future__ import annotations

import base64
from collections import Counter

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from conftest import REPO_DIR, fixture_mask, load_c2_fixture, make_frame, trace_headers
from src.app import create_app
from src.config import Settings
from src.predictor import MockPredictor

FIXTURE = load_c2_fixture()
EXPECTED_STATUS = Counter("normal" if o["label"] == "round" else "defect" for f in FIXTURE["frames"] for o in f["objects"])


def _fixture_requests():
    """C2-equivalent ProcessedFrames for the 6 fixture images (resize to C2 size, JPEG q90)."""
    out = []
    for i, f in enumerate(FIXTURE["frames"]):
        src = REPO_DIR / f["source"]
        if not src.is_file():
            pytest.skip(f"C2 fixture image not found: {src}")
        bgr = cv2.imdecode(np.fromfile(str(src), np.uint8), cv2.IMREAD_COLOR)
        bgr = cv2.resize(bgr, (f["width"], f["height"]), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 90])
        msg = make_frame(np.zeros((1, 1, 3), np.uint8), boxes=[], frame_id=i, trace_id=f"fx-{i}",
                         width=f["width"], height=f["height"], image_b64=base64.b64encode(buf.tobytes()).decode())
        msg["prompts"] = [{"candidate_id": o["candidate_id"], "type": "box", "bbox": o["bbox"]} for o in f["objects"]]
        out.append((f, msg))
    return out


def _fixture_mask_fn():
    """Return the stored SAM2 mask for a prompt bbox (any fixture frame)."""
    table = {}
    for f in FIXTURE["frames"]:
        for o in f["objects"]:
            table[(f["width"], f["height"], *(round(v, 1) for v in o["bbox"]))] = (fixture_mask(f, o), o["mask_score"])

    def mask_fn(image, box):
        h, w = image.shape[:2]
        return table[(w, h, *(round(float(v), 1) for v in box))]

    return mask_fn


def _client(container_id: str) -> TestClient:
    settings = Settings.from_env({"C3_PREDICTOR": "mock", "C3_RESULTS_URL": "", "INFERENCE_CONTAINER_ID": container_id})
    predictor = MockPredictor(mask_fn=_fixture_mask_fn())
    predictor.load()
    return TestClient(create_app(settings=settings, predictor=predictor, load_model=False))


def test_c2_fixture_frames_observations_and_status_counts():
    requests = _fixture_requests()
    with _client("c3-inference-1") as client:
        per_frame = []
        for f, msg in requests:
            r = client.post("/infer", json=msg, headers=trace_headers(msg))
            assert r.status_code == 200, r.text
            body = r.json()
            # 2. per-frame count = number of C2 prompts = detections in the body = header
            assert len(body["detections"]) == len(f["objects"])
            assert int(r.headers["X-C3-Detection-Count"]) == len(body["detections"])
            per_frame.append(len(body["detections"]))
        m = client.get("/metrics").json()

    # 1. processed frames == requests sent
    assert m["infer_request_count"] == len(requests) == m["processed_frame_count"] == 6
    # 3. cumulative observations == sum of per-frame detections (42 objects over 6 frames)
    assert m["detected_observation_count"] == sum(per_frame) == 42
    assert m["processed_detection_count"] == m["detected_observation_count"]  # deprecated alias
    # 4. status observation counts == labels (23 normal, 19 broken), and sum to observations
    assert m["status_counts"] == dict(EXPECTED_STATUS) == {"normal": 23, "defect": 19}
    assert sum(m["status_counts"].values()) == m["detected_observation_count"]
    # current frame = last request
    last_f, last_msg = requests[-1]
    assert m["current_frame_detection_count"] == len(last_f["objects"]) == 4
    assert m["current_frame"]["trace_id"] == last_msg["trace_id"]
    assert m["frames_with_detections"] == 6 and m["frames_without_detections"] == 0
    assert m["detections_per_frame"] == {"window_frames": 6, "mean": 7.0, "max": 15}
    assert m["inference_latency_ms"]["count"] == 6
    assert "NOT the number of unique snacks" in m["metric_definitions"]["detected_observation_count"]


def test_same_frame_sent_repeatedly_counts_observations_not_unique_snacks():
    """The same 6 cookies in 10 frames = 60 observations. C3 must not pretend they are 60 snacks."""
    f, msg = _fixture_requests()[1]  # phone frame with 6 cookies
    with _client("c3-inference-1") as client:
        for i in range(10):
            assert client.post("/infer", json=dict(msg, frame_id=i, trace_id=f"rep-{i}")).status_code == 200
        m = client.get("/metrics").json()
    assert m["processed_frame_count"] == 10
    assert m["detected_observation_count"] == 60
    assert m["current_frame_detection_count"] == 6
    assert "unique" not in " ".join(k for k in m if k != "metric_definitions")  # no fake unique counter


def test_errors_and_empty_frames_are_accounted_separately(client):
    ok = [make_frame(boxes=[(120, 160, 280, 320), (380, 170, 520, 310)], frame_id=0),
          make_frame(boxes=[], frame_id=1),
          make_frame(boxes=[(120, 160, 280, 320)], frame_id=2)]
    for frame in ok:
        assert client.post("/infer", json=frame).status_code == 200
    assert client.post("/infer", json=make_frame(boxes=[(1, 1, 0, 0)], frame_id=3)).status_code == 422
    m = client.get("/metrics").json()
    assert m["infer_request_count"] == 4 == m["processed_frame_count"] + m["error_count"]
    assert (m["processed_frame_count"], m["error_count"]) == (3, 1)
    assert m["detected_observation_count"] == 3
    assert (m["frames_with_detections"], m["frames_without_detections"]) == (2, 1)
    assert m["current_frame_detection_count"] == 1  # last successful frame; errors do not reset it
    assert m["detections_per_frame"]["max"] == 2


def test_replicas_report_their_own_share():
    """Round-robin over 2 replicas: per-replica stats are disjoint and add up to the totals."""
    requests = _fixture_requests()
    with _client("c3-inference-1") as r1, _client("c3-inference-2") as r2:
        for i, (_, msg) in enumerate(requests):
            assert (r1 if i % 2 == 0 else r2).post("/infer", json=msg).status_code == 200
        m1, m2 = r1.get("/metrics").json(), r2.get("/metrics").json()
    assert (m1["inference_container_id"], m2["inference_container_id"]) == ("c3-inference-1", "c3-inference-2")
    assert m1["processed_frame_count"] + m2["processed_frame_count"] == 6
    assert m1["detected_observation_count"] == sum(len(FIXTURE["frames"][i]["objects"]) for i in (0, 2, 4))
    assert m2["detected_observation_count"] == sum(len(FIXTURE["frames"][i]["objects"]) for i in (1, 3, 5))
    assert m1["detected_observation_count"] + m2["detected_observation_count"] == 42
    total = Counter(m1["status_counts"]) + Counter(m2["status_counts"])
    assert dict(total) == {"normal": 23, "defect": 19}
    assert m1["current_frame"]["frame_id"] == 4 and m2["current_frame"]["frame_id"] == 5
