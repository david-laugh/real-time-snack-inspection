"""C2 Preprocessing FastAPI 앱.

POST /preprocess : frame-envelope -> 전처리 -> processed-frame을 Nginx /infer로 전달
GET  /healthz    : 상태 확인
"""
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
from .config import Settings
from .pipeline import Detection, DiameterTracker, build_processed_frame

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("c2")


class RequestError(Exception):
    def __init__(self, status_code: int, detail):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


class InferForwardError(Exception):
    pass


def process_request(body: bytes, settings: Settings,
                    tracker: Optional[DiameterTracker]) -> tuple[dict, bytes, Detection]:
    """/preprocess의 CPU 작업 전체. 이벤트 루프를 막지 않도록 스레드풀에서 실행한다.
    JSON 파싱 -> frame-envelope 검증 -> 디코딩·리사이즈·검출·인코딩 -> processed-frame 검증 -> 직렬화.

    스레드 안전성: settings는 읽기 전용, 스키마 validator는 lru_cache(동시 첫 호출 시 중복 생성만 가능, 무해),
    세션별 지름 EMA는 DiameterTracker 내부 lock으로 보호한다. OpenCV 함수는 GIL을 풀고 실행되므로 요청이 병렬로 처리된다."""
    try:
        envelope = json.loads(body)
    except ValueError:
        raise RequestError(422, "body is not valid JSON")
    if not isinstance(envelope, dict):
        raise RequestError(422, "body must be a JSON object")
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
    """processed-frame(content: 직렬화된 JSON)을 /infer로 전달하고 응답 요약을 반환한다.

    현재는 C3 응답을 기다리는 동기 방식이다. 즉시 202 + 백그라운드 전달로 바꾸려면 이 함수만 교체한다
    (PROPOSALS.md 참고)."""
    headers = {
        "Content-Type": "application/json",
        "X-Trace-Id": msg["trace_id"],
        "X-Frame-Id": str(msg["frame_id"]),
        "X-Session-Id": msg["session_id"],
    }
    try:
        resp = await client.post(url, content=content, headers=headers)
    except httpx.TimeoutException as e:
        raise InferForwardError(f"/infer timeout: {e!r}") from e
    except httpx.HTTPError as e:
        raise InferForwardError(f"/infer request failed: {e!r}") from e
    if resp.status_code >= 400:
        raise InferForwardError(f"/infer returned {resp.status_code}")
    return {"status_code": resp.status_code}


def create_app(settings: Optional[Settings] = None,
               transport: Optional[httpx.AsyncBaseTransport] = None) -> FastAPI:
    """transport는 테스트에서 /infer를 mock할 때 쓴다."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.warmup_ms = await run_in_threadpool(warmup, settings)
        log.info("warm-up done in %.1f ms", app.state.warmup_ms)
        async with httpx.AsyncClient(timeout=settings.infer_timeout_s, transport=transport) as client:
            app.state.http_client = client
            yield

    app = FastAPI(title="C2 Preprocessing", lifespan=lifespan)
    app.state.settings = settings
    app.state.diameter_tracker = DiameterTracker(settings.diameter_ema_alpha, settings.diameter_max_sessions)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.post("/preprocess")
    async def preprocess(request: Request):
        body = await request.body()
        try:
            msg, content, det = await run_in_threadpool(
                process_request, body, settings, app.state.diameter_tracker)
        except RequestError as e:
            raise HTTPException(e.status_code, e.detail)
        log.debug("trace_id=%s frame_id=%s prompts=%d diameter=%s scale=%.3f roi=%s", msg["trace_id"],
                  msg["frame_id"], len(det.boxes), det.diameter_px, det.scale, det.roi)

        try:
            infer = await forward_to_infer(app.state.http_client, settings.infer_url, msg, content)
        except InferForwardError as e:
            log.warning("trace_id=%s frame_id=%s %s", msg["trace_id"], msg["frame_id"], e)
            raise HTTPException(502, str(e))

        return {
            "trace_id": msg["trace_id"],
            "frame_id": msg["frame_id"],
            "num_prompts": len(msg["prompts"]),
            "preprocess_ms": msg["preprocess_finished_at_ms"] - msg["preprocess_started_at_ms"],
            "infer_status": infer["status_code"],
        }

    return app


app = create_app()
