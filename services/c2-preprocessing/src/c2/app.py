"""C2 Preprocessing FastAPI 앱.

입력
  MQTT  : C2_MQTT_TOPIC(psf/v1/frames/+) 구독. 페이로드는 frame-envelope JSON. 운영 경로.
  HTTP  : POST /preprocess (같은 frame-envelope JSON). 테스트·디버깅용.
두 입력 모두 handle_frame() 하나로 처리한다: 전처리(스레드풀) -> processed-frame을 Nginx /infer로 전달 -> 이벤트 기록.
C4 전송(C2_C4_FRAME_URL)을 켜면 같은 프레임(리사이즈 이미지 + prompts)을 C3 전송과 동시에 C4로도 보낸다 (c4_frames.py).

GET /healthz : 상태 + MQTT 연결 상태
GET /stats   : 이벤트 누적 수, MQTT 수신 수, 처리 중/대기 프레임 수, C4 전송 수(c4_sent/c4_skipped/c4_error)
"""
import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

import cv2
import httpx
import numpy as np
from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from . import codec, schemas
from .codec import DecodeError
from .c4_frames import C4FrameSender
from .config import Settings
from .events import EventLog
from .mqtt_ingest import FrameSlots, MqttIngest
from .pipeline import Detection, DiameterTracker, build_processed_frame

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("c2")


class RequestError(Exception):
    def __init__(self, status_code: int, detail):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


class InferForwardError(Exception):
    def __init__(self, detail: str, timeout: bool = False):
        super().__init__(detail)
        self.timeout = timeout


def parse_body(body: bytes) -> dict:
    try:
        envelope = json.loads(body)
    except ValueError:
        raise RequestError(422, "body is not valid JSON")
    if not isinstance(envelope, dict):
        raise RequestError(422, "body must be a JSON object")
    return envelope


def process_envelope(envelope: dict, settings: Settings,
                     tracker: Optional[DiameterTracker]) -> tuple[dict, bytes, Detection]:
    """CPU 작업 전체. 이벤트 루프를 막지 않도록 스레드풀에서 실행한다.
    frame-envelope 검증 -> 디코딩·리사이즈·검출·인코딩 -> processed-frame 검증 -> 직렬화.

    스레드 안전성: settings는 읽기 전용, 스키마 validator는 lru_cache(동시 첫 호출 시 중복 생성만 가능, 무해),
    세션별 지름 EMA는 DiameterTracker 내부 lock으로 보호한다. OpenCV 함수는 GIL을 풀고 실행되므로 요청이 병렬로 처리된다."""
    errs = schemas.errors("frame-envelope", envelope)
    if errs:
        raise RequestError(422, {"error": "frame-envelope schema violation", "details": errs})

    try:
        msg, det = build_processed_frame(envelope, settings, tracker)
    except DecodeError as e:
        raise RequestError(422, str(e))

    if settings.validate_output:
        errs = schemas.errors("processed-frame", msg)
        if errs:
            log.error("processed-frame schema violation trace_id=%s: %s", msg["trace_id"], errs)
            raise RequestError(500, {"error": "processed-frame schema violation", "details": errs})
    return msg, json.dumps(msg, separators=(",", ":")).encode(), det


def process_request(body: bytes, settings: Settings,
                    tracker: Optional[DiameterTracker]) -> tuple[dict, bytes, Detection]:
    """JSON 본문 -> process_envelope. (워밍업·벤치마크용)"""
    return process_envelope(parse_body(body), settings, tracker)


def warmup(settings: Settings) -> float:
    """더미 프레임으로 파이프라인을 한 번 돌린다 (OpenCV 초기화, 스키마 로드). 세션 EMA는 건드리지 않는다."""
    img = np.full((1920, 1080, 3), (40, 40, 40), np.uint8)
    for i in range(6):
        cv2.circle(img, (270 + 540 * (i % 2), 400 + 450 * (i // 2)), 85, (75, 138, 201), -1)
    envelope = {
        "schema_version": "0.0.0", "trace_id": "warmup", "session_id": "__warmup__", "frame_id": 0,
        "captured_at_ms": 0, "source_width": 1080, "source_height": 1920, "format": "jpeg",
        "gateway_received_at_ms": 0, **codec.encode_frame(img, "jpeg", 70),
    }
    t0 = time.perf_counter()
    process_request(json.dumps(envelope).encode(), settings, None)
    return (time.perf_counter() - t0) * 1000


async def forward_to_infer(client: httpx.AsyncClient, url: str, msg: dict, content: bytes) -> dict:
    """processed-frame(content: 직렬화된 JSON)을 /infer로 전달하고 응답 요약을 반환한다."""
    headers = {
        "Content-Type": "application/json",
        "X-Trace-Id": msg["trace_id"],
        "X-Frame-Id": str(msg["frame_id"]),
        "X-Session-Id": msg["session_id"],
    }
    try:
        resp = await client.post(url, content=content, headers=headers)
    except httpx.TimeoutException as e:
        raise InferForwardError(f"/infer timeout: {e!r}", timeout=True) from e
    except httpx.HTTPError as e:
        raise InferForwardError(f"/infer request failed: {e!r}") from e
    if resp.status_code >= 400:
        raise InferForwardError(f"/infer returned {resp.status_code}")
    return {"status_code": resp.status_code}


async def handle_frame(app: FastAPI, envelope: dict, source: str) -> dict:
    """HTTP와 MQTT 공통 처리: 전처리(스레드풀) -> /infer 전달 -> 이벤트 기록.
    실패하면 이벤트를 남기고 RequestError / InferForwardError를 다시 던진다."""
    st = app.state
    try:
        msg, content, det = await run_in_threadpool(process_envelope, envelope, st.settings, st.diameter_tracker)
    except RequestError as e:
        st.events.write(envelope, "error", source, detail=e.detail)
        raise
    preprocess_ms = msg["preprocess_finished_at_ms"] - msg["preprocess_started_at_ms"]
    log.debug("trace_id=%s frame_id=%s prompts=%d diameter=%s scale=%.3f roi=%s", msg["trace_id"],
              msg["frame_id"], len(det.boxes), det.diameter_px, det.scale, det.roi)

    if st.c4 is not None:       # C3로 보내는 프레임만, C3 전송과 동시에 (기다리지 않음, prompts 0개여도 보냄)
        st.c4.submit(msg, envelope, source)

    t0 = time.perf_counter()
    try:
        infer = await forward_to_infer(st.http_client, st.settings.infer_url, msg, content)
    except InferForwardError as e:
        log.warning("trace_id=%s frame_id=%s %s", msg["trace_id"], msg["frame_id"], e)
        st.events.write(envelope, "timeout" if e.timeout else "error", source,
                        preprocess_ms=preprocess_ms, infer_rtt_ms=(time.perf_counter() - t0) * 1000, detail=e)
        raise
    st.events.write(envelope, "forwarded", source, preprocess_ms=preprocess_ms,
                    infer_rtt_ms=(time.perf_counter() - t0) * 1000)
    return {
        "trace_id": msg["trace_id"],
        "frame_id": msg["frame_id"],
        "num_prompts": len(msg["prompts"]),
        "preprocess_ms": preprocess_ms,
        "infer_status": infer["status_code"],
    }


async def mqtt_worker(app: FastAPI, slots: FrameSlots):
    """보관된 프레임을 하나씩 꺼내 처리한다. 워커 수 = 동시 처리 수(C2_MAX_INFLIGHT)."""
    while True:
        envelope = await slots.get()
        app.state.inflight += 1
        try:
            await handle_frame(app, envelope, "mqtt")
        except (RequestError, InferForwardError):
            pass                                  # handle_frame이 이벤트를 남김
        except Exception:                         # noqa: BLE001 - 워커는 죽지 않는다
            log.exception("unexpected error trace_id=%s", envelope.get("trace_id"))
            app.state.events.write(envelope, "error", "mqtt", detail="internal error")
        finally:
            app.state.inflight -= 1


def create_app(settings: Optional[Settings] = None,
               transport: Optional[httpx.AsyncBaseTransport] = None,
               c4_transport: Optional[httpx.AsyncBaseTransport] = None) -> FastAPI:
    """transport / c4_transport는 테스트에서 /infer, C4를 mock할 때 쓴다."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.warmup_ms = await run_in_threadpool(warmup, settings)
        log.info("warm-up done in %.1f ms", app.state.warmup_ms)
        app.state.events = EventLog(settings.event_log)
        if settings.c4_frame_url:
            app.state.c4 = C4FrameSender(settings, app.state.events, c4_transport)
        workers = []
        async with httpx.AsyncClient(timeout=settings.infer_timeout_s, transport=transport) as client:
            app.state.http_client = client
            if settings.mqtt_enabled:
                slots = FrameSlots(app.state.events, settings.max_frame_age_ms)
                app.state.slots = slots
                app.state.mqtt = MqttIngest(settings, asyncio.get_running_loop(), slots, app.state.events)
                workers = [asyncio.create_task(mqtt_worker(app, slots)) for _ in range(settings.max_inflight)]
                app.state.mqtt.start()
            try:
                yield
            finally:
                if app.state.mqtt:
                    app.state.mqtt.stop()
                for w in workers:
                    w.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                if app.state.c4 is not None:
                    await app.state.c4.aclose()
                app.state.events.close()

    app = FastAPI(title="C2 Preprocessing", lifespan=lifespan)
    app.state.settings = settings
    app.state.diameter_tracker = DiameterTracker(settings.diameter_ema_alpha, settings.diameter_max_sessions)
    app.state.mqtt = None
    app.state.slots = None
    app.state.inflight = 0
    app.state.events = EventLog(None)
    app.state.c4 = None

    def mqtt_status() -> dict:
        return app.state.mqtt.status() if app.state.mqtt else {"enabled": False}

    @app.get("/healthz")
    async def healthz():
        mqtt = mqtt_status()
        ok = not mqtt["enabled"] or mqtt["connected"]
        return {"status": "ok" if ok else "degraded", "mqtt": mqtt}

    @app.get("/stats")
    async def stats():
        return {
            "events": dict(app.state.events.counts),
            "mqtt": mqtt_status(),
            "inflight": app.state.inflight,
            "max_inflight": settings.max_inflight,
            "pending_sessions": len(app.state.slots) if app.state.slots else 0,
            "c4_enabled": app.state.c4 is not None,
            **(app.state.c4.stats() if app.state.c4 is not None
               else {"c4_sent": 0, "c4_skipped": 0, "c4_error": 0, "c4_inflight": 0}),
        }

    @app.post("/preprocess")
    async def preprocess(request: Request):
        """디버깅·테스트용 HTTP 입력. MQTT와 같은 handle_frame을 쓰며 /infer 결과까지 기다렸다가 응답한다."""
        body = await request.body()
        try:
            envelope = parse_body(body)
            return await handle_frame(app, envelope, "http")
        except RequestError as e:
            raise HTTPException(e.status_code, e.detail)
        except InferForwardError as e:
            raise HTTPException(502, str(e))

    return app


app = create_app()
