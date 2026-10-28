"""C2 -> C4 리사이즈 프레임 전송: 합의 형식, C3 요청과의 일치, 메인 경로 격리, 끄기."""
import asyncio
import base64
import json
import socket
import sys
import threading
import time
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest
import uvicorn

from c2.app import create_app
from c2.c4_frames import C4_FIELDS
from c2.config import Settings
from helpers import load_fixture, make_envelope

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
from fake_c4 import check_frame, create_app as create_fake_c4  # noqa: E402

INFER_URL = "http://fake-infer/infer"
C4_URL = "http://fake-c4/api/camera/frame"


class C3Recorder:
    """/infer mock: 받은 processed-frame을 기록하고 delay_s 뒤에 200."""

    def __init__(self, delay_s=0.0):
        self.delay_s, self.bodies = delay_s, []

    async def __call__(self, request: httpx.Request):
        self.bodies.append(json.loads(request.content))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        return httpx.Response(200, json={"ok": True})


async def run(settings_kw, scenario, c3=None, c4_app=None, c4_transport=None):
    c3 = c3 or C3Recorder()
    if c4_app is not None:
        c4_transport = httpx.ASGITransport(app=c4_app)
    kw = dict(infer_url=INFER_URL, c4_frame_url=C4_URL)
    kw.update(settings_kw)
    app = create_app(Settings(**kw), transport=httpx.MockTransport(c3), c4_transport=c4_transport)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c2",
                                     timeout=30) as client:
            result = await scenario(client)
            for _ in range(300):               # C4 전송이 끝날 때까지 (최대 3초)
                if (await client.get("/stats")).json()["c4_inflight"] == 0:
                    break
                await asyncio.sleep(0.01)
            stats = (await client.get("/stats")).json()
    return result, stats, c3


def frames():
    img, _ = load_fixture()
    blank = np.full_like(img, 40)              # 쿠키 없음 -> prompts 0개
    return [make_envelope(img, frame_id=0, fmt="jpeg"), make_envelope(img, frame_id=1, fmt="jpeg"),
            make_envelope(blank, frame_id=2, fmt="jpeg")]


def test_c4_body_matches_agreed_format_and_c3_request():
    fake_c4 = create_fake_c4()

    async def scenario(client):
        for e in frames():
            r = await client.post("/preprocess", json=e)
            assert r.status_code == 200, r.text

    _, stats, c3 = asyncio.run(run({}, scenario, c4_app=fake_c4))
    assert stats["c4_sent"] == 3 and stats["c4_skipped"] == 0 and stats["c4_error"] == 0
    c4_frames = fake_c4.state.frames
    assert len(c4_frames) == 3

    for sent in c3.bodies:
        body = c4_frames[sent["trace_id"]]
        assert set(body) == set(C4_FIELDS)                      # 합의된 7개 필드만
        assert check_frame(body) == []                          # JPEG 디코딩, 크기 = width x height
        raw = base64.b64decode(body["image_b64"])
        img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        assert (img.shape[1], img.shape[0]) == (body["width"], body["height"])
        # 같은 프레임의 C3 요청과 같은 값, 이미지는 같은 JPEG(다시 인코딩하지 않음)
        for k in ("trace_id", "session_id", "frame_id", "width", "height", "prompts"):
            assert body[k] == sent[k], k
        assert body["image_b64"] == sent["image_b64"]
    assert [len(c4_frames[f"trace-{i:06d}"]["prompts"]) for i in range(3)] == [15, 15, 0]   # 0개도 보냄


def test_disabled_when_url_empty():
    calls = []

    def c4_handler(request):
        calls.append(request)
        return httpx.Response(200)

    async def scenario(client):
        for e in frames():
            assert (await client.post("/preprocess", json=e)).status_code == 200

    _, stats, c3 = asyncio.run(run({"c4_frame_url": ""}, scenario, c4_transport=httpx.MockTransport(c4_handler)))
    assert calls == [] and len(c3.bodies) == 3
    assert stats["c4_enabled"] is False and stats["c4_sent"] == 0


def test_invalid_frames_are_not_sent_to_c4():
    fake_c4 = create_fake_c4()

    async def scenario(client):
        bad = {**frames()[0], "image_b64": base64.b64encode(b"not an image").decode()}
        assert (await client.post("/preprocess", json=bad)).status_code == 422

    _, stats, c3 = asyncio.run(run({}, scenario, c4_app=fake_c4))
    assert c3.bodies == [] and stats["c4_sent"] == 0 and len(fake_c4.state.frames) == 0


class RealServer:
    """fake C4를 실제 HTTP 서버로 띄운다. ASGITransport는 httpx 타임아웃이 걸리지 않으므로
    느린 C4의 타임아웃은 실제 소켓으로 확인해야 한다."""

    def __init__(self, app):
        self.port = _free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning", ws="none"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(500):
            if self.server.started:
                return self
            time.sleep(0.01)
        raise RuntimeError("fake C4 server did not start")

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=5)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _load(client, n=40, concurrency=4):
    """동시 요청 concurrency개로 n프레임을 보내고 (처리량 fps, 평균 지연 ms)."""
    img, _ = load_fixture()
    body = make_envelope(img, fmt="jpeg")
    lat, ids = [], iter(range(n))

    async def worker():
        for i in ids:
            t = time.perf_counter()
            r = await client.post("/preprocess", json={**body, "frame_id": i, "trace_id": f"load-{i:04d}"})
            assert r.status_code == 200, r.text
            lat.append((time.perf_counter() - t) * 1000)

    t0 = time.perf_counter()
    await asyncio.gather(*[worker() for _ in range(concurrency)])
    return n / (time.perf_counter() - t0), sum(lat) / len(lat)


@pytest.mark.parametrize("case", ["c4_slow_2s", "c4_down"])
def test_c4_problems_do_not_affect_c3_path(case):
    """C4가 2초씩 지연되거나 꺼져 있어도 C3 전달 처리량과 지연은 C4를 끈 경우와 같다."""
    n = 40

    def measure(settings_kw, **kw):
        c3 = C3Recorder(delay_s=0.05)                                   # C3 추론 50ms 흉내
        (fps, lat_ms), stats, c3 = asyncio.run(run(settings_kw, _load, c3=c3, **kw))
        assert len(c3.bodies) == n                                     # C3는 모든 프레임을 받음
        return fps, lat_ms, stats

    base_fps, base_lat, _ = measure({"c4_frame_url": ""})
    if case == "c4_slow_2s":
        with RealServer(create_fake_c4(delay_ms=2000)) as srv:
            fps, lat, stats = measure({"c4_frame_url": f"http://127.0.0.1:{srv.port}/api/camera/frame"})
        assert stats["c4_skipped"] > 0                                 # 한도(2) 초과분은 보내지 않음
        assert stats["c4_error"] > 0                                   # 1초 타임아웃
    else:
        fps, lat, stats = measure({"c4_frame_url": f"http://127.0.0.1:{_free_port()}/api/camera/frame"})  # 아무도 안 듣는 포트
        assert stats["c4_sent"] == 0 and stats["c4_error"] + stats["c4_skipped"] == n
    print(f"\n[{case}] baseline {base_fps:.1f} fps / {base_lat:.1f} ms  ->  {fps:.1f} fps / {lat:.1f} ms  "
          f"c4: sent={stats['c4_sent']} skipped={stats['c4_skipped']} error={stats['c4_error']}")
    # 처리량 측정은 환경 잡음이 커서(특히 Windows) 기준을 넉넉히 둔다. 이벤트 루프 차단 회귀는 아래 테스트가 직접 잡는다.
    assert fps >= base_fps * 0.8, (fps, base_fps)
    assert lat <= base_lat * 1.25 + 15, (lat, base_lat)


def test_sniffio_installed():
    """anyio 4.x는 sniffio를 설치하지 않지만 httpcore는 요청마다 `import sniffio`를 시도한다.
    없으면 실패한 import(sys.path 전체 stat)가 요청마다 이벤트 루프에서 돈다 -> requirements.txt에 고정."""
    import sniffio  # noqa: F401


def test_failed_c4_connections_do_not_block_event_loop():
    """C4가 꺼져 연결이 계속 실패하는 동안에도 이벤트 루프(C3 전송이 도는 곳)가 막히지 않는다.
    sniffio가 없을 때 Linux에서 실패 1건당 루프가 수~십수 ms 막혀 C3 처리량이 약 20% 떨어졌다."""
    from c2.c4_frames import C4FrameSender
    from c2.events import EventLog

    async def ticker(seconds, lag):
        """1ms sleep이 실제로 얼마나 늦게 깨어나는지 = 이벤트 루프가 막힌 시간."""
        stop = time.perf_counter() + seconds
        while time.perf_counter() < stop:
            t = time.perf_counter()
            await asyncio.sleep(0.001)
            lag.append((time.perf_counter() - t) * 1000)

    async def main():
        sender = C4FrameSender(Settings(c4_frame_url=f"http://127.0.0.1:{_free_port()}/api/camera/frame",
                                        c4_max_inflight=2, c4_timeout_s=1.0), EventLog())
        msg = {"trace_id": "t", "session_id": "s", "frame_id": 0, "width": 2, "height": 2, "prompts": [],
               "image_b64": "/9j/"}
        base, loaded = [], []
        await ticker(0.5, base)                    # 기준: C4 부하 없음 (Windows는 타이머 해상도 때문에 원래 ~15ms)

        async def flood(seconds):
            stop = time.perf_counter() + seconds
            while time.perf_counter() < stop:
                sender.submit(msg, msg, "test")
                await asyncio.sleep(0.002)

        await asyncio.gather(ticker(1.0, loaded), flood(1.0))
        stats = sender.stats()
        await sender.aclose()
        return sorted(base), sorted(loaded), stats

    base, loaded, stats = asyncio.run(main())
    p95_base, p95 = base[int(len(base) * 0.95)], loaded[int(len(loaded) * 0.95)]
    print(f"\n[C4 down loop lag] attempts={stats['c4_error'] + stats['c4_inflight']} "
          f"sleep(1ms) p95: idle {p95_base:.2f}ms -> during C4 failures {p95:.2f}ms")
    assert p95 < p95_base + 3.0, f"event loop blocked during C4 failures: p95 {p95:.2f} ms vs idle {p95_base:.2f} ms"
