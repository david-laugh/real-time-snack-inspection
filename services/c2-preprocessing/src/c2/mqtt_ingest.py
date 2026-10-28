"""C1 -> C2 MQTT 수신.

- paho-mqtt 2.x 네트워크 스레드가 브로커 연결·재접속·구독을 맡는다. 재접속할 때마다 on_connect에서 다시 구독한다.
- 수신 콜백(paho 스레드)은 JSON만 파싱해 이벤트 루프로 넘긴다. 무거운 작업(검증, 디코딩, 검출)은 하지 않는다.
- FrameSlots: 세션별 최신 프레임 1장만 보관한다. 처리 전에 새 프레임이 오면 이전 것은 dropped_stale.
  captured_at_ms 기준 max_frame_age_ms보다 오래된 프레임은 보관할 때와 꺼낼 때 모두 확인해 dropped_old.
- 워커 max_inflight개가 FrameSlots에서 프레임을 꺼내 처리하고 /infer로 보낸다 (app.handle_frame).
"""
import asyncio
import json
import logging
import socket
from typing import Callable, Optional

import paho.mqtt.client as mqtt

from .config import Settings
from .events import EventLog
from .pipeline import now_ms

log = logging.getLogger("c2.mqtt")


class FrameSlots:
    """세션별 최신 프레임 1장 보관함. 이벤트 루프 스레드에서만 사용한다."""

    def __init__(self, events: EventLog, max_age_ms: int):
        self.events = events
        self.max_age_ms = max_age_ms
        self._pending: dict[str, dict] = {}
        self._ready: asyncio.Queue[str] = asyncio.Queue()   # 대기 프레임이 있는 세션 (도착 순)

    def __len__(self):
        return len(self._pending)

    def _age_ms(self, envelope) -> int:
        return now_ms() - envelope["captured_at_ms"]

    def _too_old(self, envelope) -> bool:
        if self.max_age_ms <= 0:
            return False
        age = self._age_ms(envelope)
        if age > self.max_age_ms:
            self.events.write(envelope, "dropped_old", "mqtt", detail=f"age={age}ms")
            return True
        return False

    def put(self, envelope: dict):
        if self._too_old(envelope):
            return
        sid = envelope["session_id"]
        prev = self._pending.get(sid)
        self._pending[sid] = envelope
        if prev is None:
            self._ready.put_nowait(sid)
        else:   # 세션 순서는 유지하고 프레임만 최신으로 교체
            self.events.write(prev, "dropped_stale", "mqtt", detail=f"superseded by frame_id={envelope['frame_id']}")

    async def get(self) -> dict:
        while True:
            sid = await self._ready.get()
            envelope = self._pending.pop(sid, None)
            if envelope is not None and not self._too_old(envelope):
                return envelope


def _looks_like_envelope(obj) -> bool:
    """보관에 필요한 최소 필드만 확인. 전체 스키마 검증은 처리 단계에서 한다."""
    return (isinstance(obj, dict) and isinstance(obj.get("session_id"), str)
            and isinstance(obj.get("captured_at_ms"), int) and not isinstance(obj.get("captured_at_ms"), bool))


class MqttIngest:
    def __init__(self, settings: Settings, loop: asyncio.AbstractEventLoop,
                 slots: FrameSlots, events: EventLog):
        self.s = settings
        self.loop = loop
        self.slots = slots
        self.events = events
        self.connected = False
        self.received = 0
        self.connects = 0
        self.client: Optional[mqtt.Client] = None

    # ---- paho 스레드 콜백 ----
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.warning("MQTT connect refused: %s", reason_code)
            return
        client.subscribe(self.s.mqtt_topic, qos=self.s.mqtt_qos)
        self._to_loop(self._set_connected, True)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self._to_loop(self._set_connected, False, str(reason_code))

    def _on_connect_fail(self, client, userdata):
        log.warning("MQTT connect to %s:%s failed; retrying", self.s.mqtt_host, self.s.mqtt_port)

    def _on_message(self, client, userdata, msg):
        try:
            obj, err = json.loads(msg.payload), None
        except ValueError as e:
            obj, err = None, f"invalid JSON: {e}"
        self._to_loop(self._accept, obj, msg.topic, err)

    def _to_loop(self, fn: Callable, *args):
        try:
            self.loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:        # 종료 중 이벤트 루프가 닫힘
            pass

    # ---- 이벤트 루프 ----
    def _set_connected(self, value: bool, reason: str = ""):
        if value:
            self.connects += 1
            log.info("MQTT connected to %s:%s, subscribed %s (qos %d)",
                     self.s.mqtt_host, self.s.mqtt_port, self.s.mqtt_topic, self.s.mqtt_qos)
        elif self.connected:
            log.warning("MQTT disconnected: %s; reconnecting", reason)
        self.connected = value

    def _accept(self, obj, topic: str, err: Optional[str]):
        self.received += 1
        if err:
            self._invalid(None, topic, err)
        elif not _looks_like_envelope(obj):
            self._invalid(obj, topic, "not a frame-envelope (session_id / captured_at_ms)")
            return
        self.slots.put(obj)

    def _invalid(self, obj, topic: str, detail: str):
        log.warning("invalid MQTT message on %s: %s", topic, detail)
        self.events.write(obj, "error", "mqtt", detail=detail)

    # ---- 시작/종료 ----
    def start(self):
        client_id = self.s.mqtt_client_id or f"c2-preprocessing-{socket.gethostname()}"
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, protocol=mqtt.MQTTv311)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_connect_fail = self._on_connect_fail
        c.on_message = self._on_message
        c.reconnect_delay_set(min_delay=self.s.mqtt_reconnect_min_s, max_delay=self.s.mqtt_reconnect_max_s)
        c.connect_async(self.s.mqtt_host, self.s.mqtt_port, keepalive=self.s.mqtt_keepalive_s)
        c.loop_start()          # 네트워크 스레드: 연결 실패·끊김 시 자동 재접속
        self.client = c

    def stop(self):
        if self.client:
            self.client.disconnect()
            self.client.loop_stop()
            self.client = None
        self.connected = False

    def status(self) -> dict:
        return {"enabled": True, "connected": self.connected, "broker": f"{self.s.mqtt_host}:{self.s.mqtt_port}",
                "topic": self.s.mqtt_topic, "qos": self.s.mqtt_qos, "received": self.received,
                "connects": self.connects}
