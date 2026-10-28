"""실제 MQTT 브로커(mosquitto)를 쓰는 통합 테스트: 발행 -> C2 수신 -> 가짜 /infer 도착.

브로커 주소: C2_TEST_MQTT_HOST / C2_TEST_MQTT_PORT (기본 127.0.0.1:18830). 접속할 수 없으면 skip.
  docker run -d --rm --name c2-test-mosquitto -p 18830:1883 \\
    -v "$PWD/dev/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro" eclipse-mosquitto:2.0.20
"""
import asyncio
import base64
import json
import os
import socket
import sys
import time
import uuid
from pathlib import Path

import cv2
import httpx
import paho.mqtt.client as mqtt
import pytest

from c2.app import create_app
from c2.config import Settings
from c2.pipeline import now_ms
from helpers import load_fixture

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
from fake_infer import create_app as create_fake  # noqa: E402

HOST = os.environ.get("C2_TEST_MQTT_HOST", "127.0.0.1")
PORT = int(os.environ.get("C2_TEST_MQTT_PORT", "18830"))


def _broker_up():
    try:
        with socket.create_connection((HOST, PORT), timeout=1):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _broker_up(), reason=f"MQTT broker not reachable at {HOST}:{PORT}")


@pytest.fixture(scope="module")
def frame_b64():
    img, _ = load_fixture()                         # 시뮬레이터 1045x450
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return base64.b64encode(buf.tobytes()).decode()


def envelope(frame_b64, session, frame_id, age_ms=0):
    now = now_ms()
    return {"schema_version": "1.0.0", "trace_id": f"{session}-{frame_id:05d}", "session_id": session,
            "frame_id": frame_id, "captured_at_ms": now - age_ms, "source_width": 1045, "source_height": 450,
            "format": "jpeg", "gateway_received_at_ms": now, "image_b64": frame_b64}


class Publisher:
    def __init__(self, host=HOST, port=PORT):
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"test-pub-{uuid.uuid4().hex[:6]}")
        self.c.connect(host, port)
        self.c.loop_start()

    def publish(self, topic, payload, qos=0):
        self.c.publish(topic, payload if isinstance(payload, (bytes, str)) else json.dumps(payload), qos=qos)

    def close(self):
        self.c.disconnect()
        self.c.loop_stop()


async def wait_for(cond, timeout=10.0, interval=0.05):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if await cond():
            return True
        await asyncio.sleep(interval)
    return False


async def run_c2(settings_kw, fake_delay_ms, scenario):
    """C2(lifespan 포함) + 가짜 /infer를 같은 이벤트 루프에서 띄우고 scenario(c2, fake)를 실행."""
    fake = create_fake(fake_delay_ms)
    prefix = f"psf/test-{uuid.uuid4().hex[:8]}/frames"
    kw = dict(mqtt_enabled=True, mqtt_host=HOST, mqtt_port=PORT, mqtt_topic=f"{prefix}/+",
              mqtt_reconnect_min_s=0.5, mqtt_reconnect_max_s=2, infer_url="http://fake-infer/infer")
    kw.update(settings_kw)
    if "mqtt_proxy" in kw:                          # 재접속 테스트: C2는 프록시로 접속
        kw["mqtt_host"], kw["mqtt_port"] = kw.pop("mqtt_proxy")
    app = create_app(Settings(**kw), transport=httpx.ASGITransport(app=fake))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c2") as c2, \
                httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url="http://fake") as fk:
            async def connected():
                return (await c2.get("/healthz")).json()["mqtt"]["connected"]
            assert await wait_for(connected), "C2 did not connect to the broker"
            return await scenario(c2, fk, prefix)


def test_publish_reaches_fake_infer(frame_b64, tmp_path):
    log_path = tmp_path / "c2_events.csv"

    async def scenario(c2, fake, prefix):
        pub = Publisher()
        try:
            pub.publish(f"{prefix}/sess-1", envelope(frame_b64, "sess-1", 0))

            async def arrived():
                return (await fake.get("/stats")).json().get("received", 0) == 1
            assert await wait_for(arrived), "frame did not reach /infer"
        finally:
            pub.close()
        return (await c2.get("/stats")).json(), (await c2.get("/healthz")).json(), (await fake.get("/stats")).json()

    stats, health, fstats = asyncio.run(run_c2({"event_log": str(log_path)}, 0, scenario))
    assert health["status"] == "ok" and health["mqtt"]["connected"]
    assert stats["events"]["forwarded"] == 1 and stats["mqtt"]["received"] == 1
    assert fstats["last_frame_id"] == {"sess-1": 0} and fstats.get("header_mismatch", 0) == 0
    rows = log_path.read_text(encoding="utf-8").splitlines()
    assert rows[0].startswith("trace_id,") and ",mqtt,forwarded," in rows[1] and "sess-1-00000" in rows[1]


def test_30fps_with_slow_infer_does_not_accumulate_latency(frame_b64):
    """30fps 발행, /infer 지연 200ms, 워커 4개: 처리량(약 4/0.2s = 20fps)보다 빨리 들어오므로
    세션별 최신 1장만 남기고 버린다(dropped_stale). 도착 시점 나이가 시간이 지나도 늘지 않아야 한다."""
    n, fps = 90, 30

    async def scenario(c2, fake, prefix):
        pub = Publisher()
        try:
            t0 = time.monotonic()
            for i in range(n):
                await asyncio.sleep(max(0.0, t0 + i / fps - time.monotonic()))
                pub.publish(f"{prefix}/cam", envelope(frame_b64, "cam", i))

            async def drained():
                s = (await c2.get("/stats")).json()
                return s["mqtt"]["received"] == n and s["inflight"] == 0 and s["pending_sessions"] == 0
            assert await wait_for(drained), (await c2.get("/stats")).json()
        finally:
            pub.close()
        return (await c2.get("/stats")).json(), (await fake.get("/stats")).json()

    stats, fstats = asyncio.run(run_c2({"max_inflight": 4, "max_frame_age_ms": 500}, 200, scenario))
    ev = stats["events"]
    ages = [a for _, a in fstats["age_ms"]["recent"]]
    print(f"\n[30fps / infer 200ms] events={ev} fake_received={fstats['received']} "
          f"arrival age max={fstats['age_ms']['max']}ms last20={ages}")
    assert sum(ev.values()) == n                                   # 모든 프레임이 어느 이벤트로든 기록됨
    assert ev["dropped_stale"] > 0
    assert ev["forwarded"] == fstats["received"] >= 30
    assert ev["error"] == 0 and ev["timeout"] == 0
    assert fstats["age_ms"]["max"] < 500                            # 지연이 쌓이지 않음
    assert sum(ages[-10:]) / 10 < sum(ages[:10]) / 10 + 100        # 뒤로 갈수록 늘지 않음


def test_old_and_invalid_messages_are_dropped(frame_b64):
    async def scenario(c2, fake, prefix):
        pub = Publisher()
        try:
            for i in range(3):
                pub.publish(f"{prefix}/old", envelope(frame_b64, "old", i, age_ms=2000))
            pub.publish(f"{prefix}/bad", b"not json")
            pub.publish(f"{prefix}/bad", {"hello": "world"})

            async def seen():
                return (await c2.get("/stats")).json()["mqtt"]["received"] == 5
            assert await wait_for(seen)
            await asyncio.sleep(0.2)
        finally:
            pub.close()
        return (await c2.get("/stats")).json(), (await fake.get("/stats")).json()

    stats, fstats = asyncio.run(run_c2({}, 0, scenario))
    assert stats["events"]["dropped_old"] == 3
    assert stats["events"]["error"] == 2            # invalid JSON 1 + frame-envelope 아님 1
    assert fstats.get("received", 0) == 0


async def _proxy(target_host, target_port):
    """연결을 강제로 끊을 수 있는 TCP 프록시."""
    conns = []

    async def pipe(r, w):
        try:
            while data := await r.read(65536):
                w.write(data)
                await w.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            w.close()

    async def handle(cr, cw):
        br, bw = await asyncio.open_connection(target_host, target_port)
        conns.extend([cw, bw])
        await asyncio.gather(pipe(cr, bw), pipe(br, cw))

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], conns


def test_reconnects_after_broker_connection_loss(frame_b64):
    async def main():
        server, port, conns = await _proxy(HOST, PORT)

        async def scenario(c2, fake, prefix):
            for w in conns:                         # 브로커 연결 끊기
                w.close()
            conns.clear()

            down = []

            async def disconnected():
                h = (await c2.get("/healthz")).json()
                if not h["mqtt"]["connected"]:
                    down.append(h)
                return bool(down)

            async def reconnected():
                return (await c2.get("/healthz")).json()["mqtt"]["connected"]
            await wait_for(disconnected, timeout=3, interval=0.01)
            assert await wait_for(reconnected, timeout=10), "C2 did not reconnect"

            pub = Publisher()
            try:
                pub.publish(f"{prefix}/after", envelope(frame_b64, "after", 0))

                async def arrived():
                    return (await fake.get("/stats")).json().get("received", 0) == 1
                assert await wait_for(arrived), "no delivery after reconnect (resubscribe?)"
            finally:
                pub.close()
            return down, (await c2.get("/stats")).json()

        try:
            return await run_c2({"mqtt_proxy": ("127.0.0.1", port)}, 0, scenario)
        finally:
            server.close()

    down, stats = asyncio.run(main())
    assert down and down[0]["status"] == "degraded"                # 끊긴 동안 /healthz에 반영

    assert stats["mqtt"]["connects"] >= 2 and stats["events"]["forwarded"] == 1
