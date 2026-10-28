"""CPU 작업이 이벤트 루프 밖(스레드풀)에서 실행되는지, 세션 상태가 스레드에서 안전한지, 워밍업이 도는지."""
import asyncio
import threading
import time

import httpx
from fastapi.testclient import TestClient

import c2.app as c2_app
from c2.config import Settings
from c2.pipeline import DiameterTracker
from helpers import load_fixture, make_envelope

INFER_URL = "http://nginx-inference-lb:8080/infer"


def _ok(request):
    return httpx.Response(200, json={"ok": True})


async def _with_app(settings, fn):
    app = c2_app.create_app(settings, transport=httpx.MockTransport(_ok))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c2") as client:
            return await fn(app, client)


def test_event_loop_not_blocked_by_preprocessing(monkeypatch):
    """전처리가 오래 걸리는 동안에도 /healthz가 바로 응답하고, 전처리 요청들은 병렬로 처리된다."""
    real = c2_app.build_processed_frame
    delay = 0.4

    def slow(*args, **kwargs):
        time.sleep(delay)                 # 이벤트 루프에서 실행되면 루프 전체가 멈춘다
        return real(*args, **kwargs)

    monkeypatch.setattr(c2_app, "build_processed_frame", slow)
    img, _ = load_fixture()
    envelope = make_envelope(img, fmt="jpeg")

    async def scenario(app, client):
        t0 = time.perf_counter()
        jobs = [asyncio.create_task(client.post("/preprocess", json={**envelope, "frame_id": i}))
                for i in range(4)]
        await asyncio.sleep(0.05)
        h = await client.get("/healthz")
        health_s = time.perf_counter() - t0
        results = await asyncio.gather(*jobs)
        return h, health_s, results, time.perf_counter() - t0

    h, health_s, results, total_s = asyncio.run(_with_app(Settings(infer_url=INFER_URL), scenario))
    assert h.status_code == 200 and health_s < delay, f"/healthz waited {health_s:.2f}s"
    assert all(r.status_code == 200 for r in results)
    assert total_s < 2 * delay, f"4 requests took {total_s:.2f}s (serial would be {4 * delay:.1f}s)"


def test_diameter_tracker_thread_safe():
    t = DiameterTracker(alpha=0.2, max_sessions=8)
    errors = []

    def worker(k):
        try:
            for i in range(2000):
                t.update(f"s{(k + i) % 16}", 50.0 + (i % 3))
        except Exception as e:      # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors
    assert len(t._values) <= 8
    assert all(50.0 <= v <= 52.0 for v in t._values.values())


def test_warmup_runs_on_startup_without_touching_sessions():
    app = c2_app.create_app(Settings(infer_url=INFER_URL), transport=httpx.MockTransport(_ok))
    with TestClient(app):
        assert app.state.warmup_ms > 0
        assert app.state.diameter_tracker.update("__warmup__", None) is None
