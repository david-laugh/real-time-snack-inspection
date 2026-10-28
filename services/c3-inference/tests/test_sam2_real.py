"""Optional: the same API tests against the REAL SAM2 backend.

Skipped unless torch + sam2 are importable and the checkpoint exists
(default: services/c3-inference/models/sam2.1_hiera_tiny.pt, override with
C3_TEST_SAM2_CHECKPOINT). Run with `-s` to see load / latency numbers.
"""

from __future__ import annotations

import os
import statistics

import jsonschema
import pytest

from conftest import BACKGROUND, REPO_DIR, SERVICE_DIR, disc_mask, load_c2_fixture, load_schema, make_frame, make_image
from src.config import Settings

sam2 = pytest.importorskip("sam2")
torch = pytest.importorskip("torch")

CKPT = os.environ.get("C3_TEST_SAM2_CHECKPOINT", str(SERVICE_DIR / "models" / "sam2.1_hiera_tiny.pt"))
pytestmark = pytest.mark.skipif(not os.path.isfile(CKPT), reason=f"SAM2 checkpoint not found: {CKPT}")


@pytest.fixture(scope="module", params=[os.environ.get("C3_TEST_DEVICE", "auto")])
def real_client(request):
    from fastapi.testclient import TestClient

    from src.app import create_app
    from src.predictor import build_predictor

    settings = Settings.from_env(
        {
            "C3_PREDICTOR": "sam2",
            "SAM2_CHECKPOINT_PATH": CKPT,
            "C3_DEVICE": request.param,
            "INFERENCE_CONTAINER_ID": "c3-inference-real",
            "C3_RESULTS_URL": "",
            "C3_WARMUP_ITERS": "2",
        }
    )
    predictor = build_predictor(settings)
    info = predictor.load()  # synchronous: measure load separately from inference
    print(f"\n[sam2] {predictor.describe()}")
    print(f"[sam2] model_load_ms={info.model_load_ms:.1f} warmup_ms={info.warmup_ms:.1f}")
    app = create_app(settings=settings, predictor=predictor, load_model=False)
    with TestClient(app) as client:
        yield client


def test_real_sam2_segments_and_judges(real_client):
    image = make_image(width=1024, height=1024, discs=[(300, 300, 120), (700, 650, 110)])
    import cv2

    cv2.circle(image, (700, 540), 30, BACKGROUND, thickness=-1)  # bite out of the second cookie
    frame = make_frame(image, boxes=[(170, 170, 430, 430), (580, 530, 820, 770)])
    r = real_client.post("/infer", json=frame)
    assert r.status_code == 200, r.text
    body = r.json()
    jsonschema.Draft202012Validator(load_schema("detection-result.schema.json")).validate(body)

    from src.mask_codec import rle_decode

    mask = rle_decode(body["detections"][0]["mask"])
    gt = disc_mask(mask.shape, 300, 300, 120)
    iou = (mask & gt).sum() / (mask | gt).sum()
    print(f"[sam2] disc IoU={iou:.3f} detections={[(d['status'], d['defect_type'], d['defect_score']) for d in body['detections']]}")
    assert iou > 0.85
    assert body["detections"][0]["status"] == "normal"
    assert (body["detections"][1]["status"], body["detections"][1]["defect_type"]) == ("defect", "broken")


@pytest.mark.parametrize("n_boxes", [1, 4, 8])
def test_real_sam2_latency(real_client, n_boxes):
    centers = [(160 + (i % 4) * 230, 300 + (i // 4) * 400) for i in range(n_boxes)]
    image = make_image(width=1024, height=1024, discs=[(x, y, 90) for x, y in centers])
    frame = make_frame(image, boxes=[(x - 100, y - 100, x + 100, y + 100) for x, y in centers])
    keys = ["X-C3-Decode-Ms", "X-C3-Encoder-Ms", "X-C3-Segmentation-Ms", "X-C3-Defect-Analysis-Ms", "X-C3-Total-Inference-Ms"]
    samples = {k: [] for k in keys}
    for i in range(20):
        r = real_client.post("/infer", json=dict(frame, frame_id=i))
        assert r.status_code == 200, r.text
        for k in keys:
            samples[k].append(float(r.headers[k]))
    summary = {k.removeprefix("X-C3-").removesuffix("-Ms"): round(statistics.median(v), 2) for k, v in samples.items()}
    gpu = real_client.get("/metrics").json()["gpu_memory"]
    print(f"[sam2] boxes={n_boxes} median ms over 20 frames: {summary} gpu_memory={gpu}")


def test_real_sam2_on_c2_fixture_frames(real_client):
    """End-to-end on real snack images: C2 fixture image -> C2-like resize/JPEG -> C3 -> labels."""
    import base64

    import cv2
    import numpy as np

    fixture = load_c2_fixture()
    correct = total = 0
    for frame in fixture["frames"]:
        src = REPO_DIR / frame["source"]
        if not src.is_file():
            pytest.skip(f"C2 fixture image not found: {src}")
        bgr = cv2.imdecode(np.fromfile(str(src), np.uint8), cv2.IMREAD_COLOR)  # unicode-safe path
        bgr = cv2.resize(bgr, (frame["width"], frame["height"]), interpolation=cv2.INTER_AREA)  # C2: long side 1024
        ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 90])
        msg = make_frame(np.zeros((1, 1, 3), np.uint8), boxes=[], width=frame["width"], height=frame["height"],
                         image_b64=base64.b64encode(buf.tobytes()).decode())
        msg["prompts"] = [{"candidate_id": o["candidate_id"], "type": "box", "bbox": o["bbox"]} for o in frame["objects"]]
        r = real_client.post("/infer", json=msg)
        assert r.status_code == 200, r.text
        for o, d in zip(frame["objects"], r.json()["detections"]):
            expected = ("normal", None) if o["label"] == "round" else ("defect", "broken")
            total += 1
            correct += (d["status"], d["defect_type"]) == expected
            if (d["status"], d["defect_type"]) != expected:
                print(f"[sam2] MISS {frame['name']} {o['candidate_id']} label={o['label']} got={d['status']}/{d['defect_type']}")
    print(f"[sam2] C2 fixture frames: {correct}/{total} correct")
    assert correct == total
