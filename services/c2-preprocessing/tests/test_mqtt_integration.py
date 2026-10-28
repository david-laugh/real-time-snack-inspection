"""실제 MQTT 브로커(mosquitto)를 쓰는 통합 테스트: 발행 -> C2 수신 -> 가짜 /infer 도착.

브로커 선택 (호스트의 localhost:1883은 절대 쓰지 않는다. 다른 프로젝트 브로커일 수 있다):
  1) C2_TEST_MQTT_HOST / C2_TEST_MQTT_PORT 지정 -> 그 브로커 (Docker 테스트 실행 시 사이드카 컨테이너)
     단, localhost/127.0.0.1의 1883은 거부한다.
  2) 지정이 없으면 테스트가 mosquitto 컨테이너를 직접 띄운다 (127.0.0.1의 무작위 포트, 끝나면 삭제).
  3) docker도 없으면 skip.
"""
import asyncio
import base64
import json
import os
import shutil
import subprocess
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

DEV = Path(__file__).resolve().parents[1] / "dev"
sys.path.insert(0, str(DEV))
from fake_c4 import create_app as create_fake_c4  # noqa: E402
from fake_infer import create_app as create_fake  # noqa: E402

MOSQUITTO_IMAGE = "eclipse-mosquitto:2.0.20"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def assert_not_host_default_broker(host, port):
    if host in LOCAL_HOSTS and int(port) == 1883:
        raise pytest.UsageError(f"refusing to use {host}:1883 (may be another project's broker); "
                                "use a dedicated test broker port")


def _mqtt_ready(host, port, timeout=15.0):
    """TCP 포트가 열려도(Docker 포트 포워딩) 브로커가 아직 안 떴을 수 있으니 MQTT 연결로 확인."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"probe-{uuid.uuid4().hex[:6]}")
        try:
            c.connect(host, port, keepalive=5)
            c.disconnect()
            return True
        except OSError:
            time.sleep(0.2)
    return False


@pytest.fixture(scope="module")
def broker():
    host = os.environ.get("C2_TEST_MQTT_HOST")
    if host:
        port = int(os.environ.get("C2_TEST_MQTT_PORT", "1883"))
        assert_not_host_default_broker(host, port)
        if not _mqtt_ready(host, port, timeout=5):
            pytest.skip(f"MQTT broker not reachable at {host}:{port}")
        yield host, port
        return

    if not shutil.which("docker"):
        pytest.skip("no C2_TEST_MQTT_HOST and docker is not available to start a test broker")
    name = f"c2-test-mosquitto-{uuid.uuid4().hex[:8]}"
    conf = DEV / "mosquitto.conf"
    run = subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-p", "127.0.0.1::1883",
                          "-v", f"{conf}:/mosquitto/config/mosquitto.conf:ro", MOSQUITTO_IMAGE],
                         capture_output=True, text=True)
    if run.returncode != 0:
        pytest.skip(f"could not start test broker: {run.stderr.strip()[:200]}")
    try:
        mapped = subprocess.run(["docker", "port", name, "1883/tcp"], capture_output=True, text=True,
                                check=True).stdout.split()[0]               # 127.0.0.1:XXXXX
        port = int(mapped.rsplit(":", 1)[1])
        assert port != 1883
        assert _mqtt_ready("127.0.0.1", port), "test broker did not come up"
        yield "127.0.0.1", port
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


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
    def __init__(self, host, port):
        assert_not_host_default_broker(host, port)
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


async def run_c2(broker, settings_kw, fake_delay_ms, scenario, fake=None, c4_app=None):
    """C2(lifespan 포함) + 가짜 /infer(+ 가짜 C4)를 같은 이벤트 루프에서 띄우고 scenario(c2, fake)를 실행."""
    fake = fake or create_fake(fake_delay_ms)
    prefix = f"psf/test-{uuid.uuid4().hex[:8]}/frames"
    host, port = broker
    kw = dict(mqtt_enabled=True, mqtt_host=host, mqtt_port=port, mqtt_topic=f"{prefix}/+",
              mqtt_reconnect_min_s=0.5, mqtt_reconnect_max_s=2, infer_url="http://fake-infer/infer")
    kw.update(settings_kw)
    if "mqtt_proxy" in kw:                          # 재접속 테스트: C2는 프록시로 접속
        kw["mqtt_host"], kw["mqtt_port"] = kw.pop("mqtt_proxy")
    c4_transport = None
    if c4_app is not None:
        kw.setdefault("c4_frame_url", "http://fake-c4/api/camera/frame")
        c4_transport = httpx.ASGITransport(app=c4_app)
    app = create_app(Settings(**kw), transport=httpx.ASGITransport(app=fake), c4_transport=c4_transport)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c2") as c2, \
                httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url="http://fake") as fk:
            async def connected():
                return (await c2.get("/healthz")).json()["mqtt"]["connected"]
            assert await wait_for(connected), "C2 did not connect to the broker"
            return await scenario(c2, fk, prefix)


def test_publish_reaches_fake_infer(broker, frame_b64, tmp_path):
    log_path = tmp_path / "c2_events.csv"

    async def scenario(c2, fake, prefix):
        pub = Publisher(*broker)
        try:
            pub.publish(f"{prefix}/sess-1", envelope(frame_b64, "sess-1", 0))

            async def arrived():
                return (await fake.get("/stats")).json().get("received", 0) == 1
            assert await wait_for(arrived), "frame did not reach /infer"
        finally:
            pub.close()
        return (await c2.get("/stats")).json(), (await c2.get("/healthz")).json(), (await fake.get("/stats")).json()

    stats, health, fstats = asyncio.run(run_c2(broker, {"event_log": str(log_path)}, 0, scenario))
    assert health["status"] == "ok" and health["mqtt"]["connected"]
    assert stats["events"]["forwarded"] == 1 and stats["mqtt"]["received"] == 1
    assert fstats["last_frame_id"] == {"sess-1": 0} and fstats.get("header_mismatch", 0) == 0
    rows = log_path.read_text(encoding="utf-8").splitlines()
    assert rows[0].startswith("trace_id,") and ",mqtt,forwarded," in rows[1] and "sess-1-00000" in rows[1]


def test_30fps_with_slow_infer_does_not_accumulate_latency(broker, frame_b64):
    """30fps 발행, /infer 지연 200ms, 워커 4개: 처리량(약 4/0.2s = 20fps)보다 빨리 들어오므로
    세션별 최신 1장만 남기고 버린다(dropped_stale). 도착 시점 나이가 시간이 지나도 늘지 않아야 한다."""
    n, fps = 90, 30

    async def scenario(c2, fake, prefix):
        pub = Publisher(*broker)
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

    stats, fstats = asyncio.run(run_c2(broker, {"max_inflight": 4, "max_frame_age_ms": 500}, 200, scenario))
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


def test_old_and_invalid_messages_are_dropped(broker, frame_b64):
    async def scenario(c2, fake, prefix):
        pub = Publisher(*broker)
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

    stats, fstats = asyncio.run(run_c2(broker, {}, 0, scenario))
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


def test_reconnects_after_broker_connection_loss(broker, frame_b64):
    async def main():
        server, port, conns = await _proxy(*broker)

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

            pub = Publisher(*broker)
            try:
                pub.publish(f"{prefix}/after", envelope(frame_b64, "after", 0))

                async def arrived():
                    return (await fake.get("/stats")).json().get("received", 0) == 1
                assert await wait_for(arrived), "no delivery after reconnect (resubscribe?)"
            finally:
                pub.close()
            return down, (await c2.get("/stats")).json()

        try:
            return await run_c2(broker, {"mqtt_proxy": ("127.0.0.1", port)}, 0, scenario)
        finally:
            server.close()

    down, stats = asyncio.run(main())
    assert down and down[0]["status"] == "degraded"                # 끊긴 동안 /healthz에 반영

    assert stats["mqtt"]["connects"] >= 2 and stats["events"]["forwarded"] == 1


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_refuses_host_default_broker_port(host):
    """호스트의 1883(다른 프로젝트 브로커일 수 있음)에는 붙지 않는다. 브로커 없이 실행된다."""
    with pytest.raises(pytest.UsageError):
        assert_not_host_default_broker(host, 1883)
    with pytest.raises(pytest.UsageError):
        Publisher(host, 1883)
    assert_not_host_default_broker(host, 11883)              # 전용 포트는 허용
    assert_not_host_default_broker("mqtt-test-broker", 1883)  # 컨테이너 네트워크 안의 브로커는 허용


def test_c4_receives_only_frames_forwarded_to_c3(broker, frame_b64):
    """30fps + /infer 200ms: drop된 프레임(dropped_stale/old)은 C4로 가지 않고, C3로 간 프레임만 C4로 간다."""
    n, fps = 60, 30
    fake, fake_c4 = create_fake(200), create_fake_c4()

    async def scenario(c2, _fake, prefix):
        pub = Publisher(*broker)
        try:
            t0 = time.monotonic()
            for i in range(n):
                await asyncio.sleep(max(0.0, t0 + i / fps - time.monotonic()))
                pub.publish(f"{prefix}/cam", envelope(frame_b64, "cam", i))

            async def drained():
                s = (await c2.get("/stats")).json()
                return (s["mqtt"]["received"] == n and s["inflight"] == 0 and s["pending_sessions"] == 0
                        and s["c4_inflight"] == 0)
            assert await wait_for(drained), (await c2.get("/stats")).json()
        finally:
            pub.close()
        return (await c2.get("/stats")).json()

    stats = asyncio.run(run_c2(broker, {"c4_max_inflight": 4}, 0, scenario, fake=fake, c4_app=fake_c4))
    ev = stats["events"]
    c3_ids, c4_ids = set(fake.state.trace_ids), set(fake_c4.state.frames)
    print(f"\n[C4 via MQTT] events={ev} c4_sent={stats['c4_sent']} c4_skipped={stats['c4_skipped']} "
          f"c4_error={stats['c4_error']}")
    assert ev["dropped_stale"] > 0 and len(c3_ids) == ev["forwarded"] < n
    assert c4_ids <= c3_ids                                        # drop된 프레임은 C4로 가지 않음
    assert stats["c4_sent"] == len(c4_ids) > 0
    assert stats["c4_sent"] + stats["c4_skipped"] + stats["c4_error"] == ev["forwarded"]
    assert "c4_error" not in ev                                    # 프레임 이벤트 집계와 분리
