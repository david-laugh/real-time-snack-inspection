"""C2 Preprocessing FastAPI 앱.

POST /preprocess : frame-envelope -> 전처리 -> processed-frame을 Nginx /infer로 전달
GET  /healthz    : 상태 확인
"""
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from . import schemas
from .codec import DecodeError
from .config import Settings
from .pipeline import DiameterTracker, build_processed_frame

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("c2")


class InferForwardError(Exception):
    pass


async def forward_to_infer(client: httpx.AsyncClient, url: str, msg: dict) -> dict:
    """processed-frame을 /infer로 전달하고 응답 요약을 반환한다.

    현재는 C3 응답을 기다리는 동기 방식이다. 즉시 202 + 백그라운드 전달로 바꾸려면 이 함수만 교체한다
    (PROPOSALS.md 참고)."""
    headers = {
        "X-Trace-Id": msg["trace_id"],
        "X-Frame-Id": str(msg["frame_id"]),
        "X-Session-Id": msg["session_id"],
    }
    try:
        resp = await client.post(url, json=msg, headers=headers)
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
        try:
            envelope = await request.json()
        except ValueError:
            raise HTTPException(422, "body is not valid JSON")
        if not isinstance(envelope, dict):
            raise HTTPException(422, "body must be a JSON object")
        errs = schemas.errors("frame-envelope", envelope)
        if errs:
            raise HTTPException(422, {"error": "frame-envelope schema violation", "details": errs})

        try:
            msg, det = await run_in_threadpool(
                build_processed_frame, envelope, settings, app.state.diameter_tracker)
        except DecodeError as e:
            raise HTTPException(422, str(e))
        log.debug("trace_id=%s frame_id=%s prompts=%d diameter=%s scale=%.3f roi=%s", msg["trace_id"],
                  msg["frame_id"], len(det.boxes), det.diameter_px, det.scale, det.roi)

        if settings.validate_output:
            errs = schemas.errors("processed-frame", msg)
            if errs:
                log.error("processed-frame schema violation trace_id=%s: %s", msg["trace_id"], errs)
                raise HTTPException(500, {"error": "processed-frame schema violation", "details": errs})

        try:
            infer = await forward_to_infer(app.state.http_client, settings.infer_url, msg)
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
