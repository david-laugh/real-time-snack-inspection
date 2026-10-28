"""dev/fake_infer.py 동작 확인 + C2 -> 가짜 /infer 연결 테스트 (네트워크 없이 ASGI로 연결)."""
import sys
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from c2.app import create_app as create_c2
from c2.config import Settings
from helpers import load_fixture, make_envelope

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
from fake_infer import create_app as create_fake  # noqa: E402

INFER_URL = "http://fake-infer:8080/infer"


def test_c2_to_fake_infer_end_to_end():
    fake = create_fake()
    c2 = create_c2(Settings(infer_url=INFER_URL), transport=httpx.ASGITransport(app=fake))
    img, _ = load_fixture()
    with TestClient(c2) as c:
        for i in range(3):
            r = c.post("/preprocess", json=make_envelope(img, frame_id=i, fmt="jpeg"))
            assert r.status_code == 200, r.text
            assert r.json()["infer_status"] == 200 and r.json()["num_prompts"] == 15
    stats = TestClient(fake).get("/stats").json()
    assert stats["received"] == 3 and stats.get("invalid", 0) == 0 and stats.get("header_mismatch", 0) == 0
    assert stats["last_frame_id"] == {"sess-test": 2}


def test_fake_infer_rejects_invalid_message():
    with TestClient(create_fake()) as c:
        r = c.post("/infer", json={"trace_id": "x", "unexpected": 1})
        assert r.status_code == 422
        assert c.get("/stats").json()["invalid"] == 1
        assert c.get("/healthz").json() == {"status": "ok"}
