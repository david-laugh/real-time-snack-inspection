"""C3 SAM2 Inference HTTP service.

    POST /infer    processed-frame -> detection-result (shared/schemas)
    GET  /healthz  model readiness (200 ready / 503 loading|error)
    GET  /metrics  counters, per-stage latency window, model load time, GPU memory

The model is loaded ONCE in a background thread at startup so /healthz can
report "loading" while weights load and warm up.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from .config import Settings
from .metrics import Metrics
from .pipeline import InferenceError, InferencePipeline
from .predictor import LoadInfo, SegmentationPredictor, build_predictor
from .schemas import ProcessedFrame

log = logging.getLogger("c3.inference")

TRACE_HEADERS = ("X-Trace-Id", "X-Frame-Id", "X-Session-Id")
TIMING_HEADERS = {
    "decode_ms": "X-C3-Decode-Ms",
    "encoder_ms": "X-C3-Encoder-Ms",
    "segmentation_ms": "X-C3-Segmentation-Ms",
    "defect_analysis_ms": "X-C3-Defect-Analysis-Ms",
    "total_inference_ms": "X-C3-Total-Inference-Ms",
}


class ModelState:
    def __init__(self) -> None:
        self.status = "not_started"  # not_started | loading | ready | error
        self.error: Optional[str] = None
        self.load_info: Optional[LoadInfo] = None
        self.started_at_ms: Optional[int] = None
        self.loaded_at_ms: Optional[int] = None


def _load_model(predictor: SegmentationPredictor, state: ModelState) -> None:
    state.status = "loading"
    state.started_at_ms = int(time.time() * 1000)
    try:
        state.load_info = predictor.load()
        state.loaded_at_ms = int(time.time() * 1000)
        state.status = "ready"
        log.info(
            "model ready predictor=%s model_load_ms=%.1f warmup_ms=%.1f",
            predictor.name, state.load_info.model_load_ms, state.load_info.warmup_ms,
        )
    except Exception as exc:  # keep serving /healthz with the error
        state.status = "error"
        state.error = f"{type(exc).__name__}: {exc}"
        log.exception("model load failed")


def _trace_from_headers(request: Request) -> Dict[str, str]:
    return {h: request.headers[h] for h in TRACE_HEADERS if h in request.headers}


def create_app(
    settings: Optional[Settings] = None,
    predictor: Optional[SegmentationPredictor] = None,
    load_model: bool = True,
) -> FastAPI:
    settings = settings or Settings.from_env()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    predictor = predictor or build_predictor(settings)
    pipeline = InferencePipeline(settings, predictor)
    metrics = Metrics(window=settings.metrics_window)
    state = ModelState()
    if not load_model and predictor.is_ready:  # caller loaded it already (tests / scripts)
        state.status = "ready"

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if load_model:
            threading.Thread(target=_load_model, args=(predictor, state), name="model-loader", daemon=True).start()
        yield

    app = FastAPI(title="C3 SAM2 Inference", version=settings.schema_version, lifespan=lifespan)
    app.state.settings = settings
    app.state.predictor = predictor
    app.state.pipeline = pipeline
    app.state.metrics = metrics
    app.state.model_state = state

    def model_info() -> Dict[str, Any]:
        info: Dict[str, Any] = {
            "status": state.status,
            "model_ready": predictor.is_ready,
            "inference_container_id": settings.inference_container_id,
            "error": state.error,
            "model_load_ms": round(state.load_info.model_load_ms, 1) if state.load_info else None,
            "warmup_ms": round(state.load_info.warmup_ms, 1) if state.load_info else None,
            "load_started_at_ms": state.started_at_ms,
            "loaded_at_ms": state.loaded_at_ms,
        }
        info.update(predictor.describe())
        return info

    def error_response(code: str, message: str, status: int, trace: Dict[str, str]) -> JSONResponse:
        metrics.record_error(code)
        headers = dict(trace)
        headers["X-Inference-Container-Id"] = settings.inference_container_id
        if status == 503:
            headers["Retry-After"] = "1"
        body = {
            "error": {
                "code": code,
                "message": message,
                "inference_container_id": settings.inference_container_id,
                "trace_id": trace.get("X-Trace-Id"),
                "frame_id": trace.get("X-Frame-Id"),
                "session_id": trace.get("X-Session-Id"),
            }
        }
        log.warning("infer error code=%s status=%d trace=%s msg=%s", code, status, trace, message)
        return JSONResponse(body, status_code=status, headers=headers)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        info = model_info()
        return JSONResponse(info, status_code=200 if predictor.is_ready else 503)

    @app.get("/metrics")
    async def get_metrics() -> JSONResponse:
        extra = {
            "inference_container_id": settings.inference_container_id,
            "model": model_info(),
            "gpu_memory": predictor.gpu_memory(),
        }
        return JSONResponse(metrics.snapshot(extra))

    @app.post("/infer")
    async def infer(request: Request) -> JSONResponse:
        header_trace = _trace_from_headers(request)
        if not predictor.is_ready:
            return error_response("model_not_ready", f"model status: {state.status}", 503, header_trace)

        body = await request.body()
        try:
            frame = ProcessedFrame.model_validate_json(body)
        except ValidationError as exc:
            details = exc.errors(include_input=False, include_url=False)
            return error_response("invalid_request", json.dumps(details, default=str), 422, header_trace)

        # Body fields are authoritative (schema); headers are echoed and cross-checked.
        trace = {"X-Trace-Id": frame.trace_id, "X-Frame-Id": str(frame.frame_id), "X-Session-Id": frame.session_id}
        mismatched = {h: v for h, v in header_trace.items() if v != trace[h]}
        if mismatched:
            metrics.record_header_mismatch()
            log.warning("trace header/body mismatch headers=%s body=%s", mismatched, trace)

        try:
            result, timings, decisions = await run_in_threadpool(pipeline.run, frame)
        except InferenceError as exc:
            return error_response(exc.code, exc.message, exc.http_status, trace)
        except Exception as exc:
            log.exception("inference failed trace=%s", trace)
            return error_response("internal_error", f"{type(exc).__name__}: {exc}", 500, trace)

        metrics.record_success(timings, pipeline.status_counts(result))
        headers = dict(trace)
        headers["X-Inference-Container-Id"] = settings.inference_container_id
        for key, name in TIMING_HEADERS.items():
            headers[name] = f"{timings[key]:.3f}"
        headers["Server-Timing"] = ", ".join(
            f"{k.removesuffix('_ms')};dur={v:.3f}" for k, v in timings.items()
        )
        log.info(
            json.dumps(
                {
                    "event": "infer",
                    "trace_id": frame.trace_id,
                    "session_id": frame.session_id,
                    "frame_id": frame.frame_id,
                    "inference_container_id": settings.inference_container_id,
                    "detections": len(result.detections),
                    "timings_ms": timings,
                    "decisions": [
                        {"object_id": d.object_id, "status": d.status.value, "reason": dec.reason, **dec.features}
                        for d, dec in zip(result.detections, decisions)
                    ],
                }
            )
        )
        return JSONResponse(result.model_dump(mode="json"), headers=headers)

    return app
