"""
C4 Visualizer — 과자 크랙 검사 대시보드 + 모니터링
Port 8003 | POST /results (C3 → C4) | POST /api/camera/frame (C2 → C4) | WS /ws/dashboard | GET / (UI)
"""

import asyncio
import time
from contextlib import asynccontextmanager, suppress
from urllib.request import urlopen

import json
import logging
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from src.models import CameraFrame, DetectionResult, ScanLine
from src.metrics import PipelineMetrics
from src.config import (C2_STATS_URL, C2_STATS_TIMEOUT_S, C2_STATS_INTERVAL_S, C1_METRICS_URL,
                        SCAN_LINE_RATIO, TRAVEL_DIRECTION, TRACK_VOTE_FRAMES, TRACK_REORDER_MS)
from src.store import InspectionStore, InspectionRecord
from src.tracker import CookieTracker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("c4")

metrics = PipelineMetrics()
tracker = CookieTracker(scan_line_ratio=SCAN_LINE_RATIO, direction=TRAVEL_DIRECTION,
                        vote_frames=TRACK_VOTE_FRAMES, reorder_ms=TRACK_REORDER_MS)
TRACK_FLUSH_INTERVAL_S = 0.2


def fetch_c2_stats():
    with urlopen(C2_STATS_URL, timeout=C2_STATS_TIMEOUT_S) as response:
        return json.load(response)


def fetch_c1_metrics():
    with urlopen(C1_METRICS_URL, timeout=C2_STATS_TIMEOUT_S) as response:
        return json.load(response)


def dashboard_stats():
    return {**store.get_stats(), **metrics.get_stats(), "tracking": tracker.config()}


async def publish_cookies(counted):
    """Store and announce cookies the tracker has just counted (once per cookie)."""
    for cookie in counted:
        await broadcast({"type": "cookie_counted", "data": store.add_cookie(cookie)})
    if counted:
        await broadcast({"type": "stats", "data": dashboard_stats()})


async def flush_tracker():
    # Results wait in the tracker's reorder buffer; release them even when no new result arrives.
    while True:
        await publish_cookies(tracker.flush(time.time() * 1000))
        await asyncio.sleep(TRACK_FLUSH_INTERVAL_S)


async def poll_metrics():
    while True:
        if C2_STATS_URL:
            try:
                metrics.update_c2(await asyncio.to_thread(fetch_c2_stats))
            except Exception:
                metrics.c2_available = False
        if C1_METRICS_URL:
            try:
                metrics.update_c1(await asyncio.to_thread(fetch_c1_metrics))
            except Exception:
                metrics.c1_available = False
        metrics.sample_drop_window()
        await broadcast({"type": "stats", "data": dashboard_stats()})
        await asyncio.sleep(C2_STATS_INTERVAL_S)


@asynccontextmanager
async def lifespan(app):
    tasks = [asyncio.create_task(poll_metrics()), asyncio.create_task(flush_tracker())]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="C4 Visualizer", lifespan=lifespan)
store = InspectionStore(max_records=500)
ws_clients: list[WebSocket] = []

async def broadcast(payload: dict):
    msg = json.dumps(payload)
    dead = []
    for ws in list(ws_clients):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in ws_clients:
            ws_clients.remove(ws)


@app.post("/results")
async def receive_result(result: DetectionResult):
    """C3 Inference → C4: detection-result 수신."""
    now_ms = time.time() * 1000
    metrics.record(result, now_ms)
    overlays = []
    for det in result.detections:
        status = det.status

        rec = InspectionRecord(
            item_id=det.object_id,
            trace_id=result.trace_id,
            frame_id=result.frame_id,
            status=status,
            session_id=result.session_id,
            defect_type=det.defect_type,
            defect_score=det.defect_score,
            inference_latency_ms=result.inference_latency_ms,
            container_id=result.inference_container_id,
        )
        store.add(rec)
        overlays.append({
            "item_id": det.object_id, "bbox": det.bbox, "mask": det.mask,
            "status": status, "defect_type": det.defect_type, "defect_score": det.defect_score,
        })

        await broadcast({
            "type": "inspection_result",
            "data": {
                "item_id": det.object_id,
                "trace_id": result.trace_id,
                "session_id": result.session_id,
                "status": status,
                "defect_score": round(det.defect_score, 4),
                "inference_latency_ms": round(result.inference_latency_ms, 1),
                "container_id": result.inference_container_id,
                "frame_id": result.frame_id,
                "bbox": det.bbox,
                "defect_type": det.defect_type,
            },
        })

    # Send one complete frame, including empty detections, for atomic overlays.
    await broadcast({"type": "frame_result", "data": {
        "trace_id": result.trace_id, "session_id": result.session_id,
        "frame_id": result.frame_id, "captured_at_ms": result.captured_at_ms,
        "inference_latency_ms": result.inference_latency_ms,
        "latency_breakdown_ms": result.latency_breakdown_ms,
        "inference_timings_ms": result.inference_timings_ms, "detections": overlays,
    }})

    # Masks are encoded at the C2-resized size; otherwise the tracker uses the camera frame width.
    width = next((d.mask.get("width") for d in result.detections
                  if d.mask and isinstance(d.mask.get("width"), int)), None)
    tracker.add_result(result.session_id, result.frame_id, overlays, now_ms, width=width)
    counted = tracker.flush(now_ms)
    for cookie in counted:
        await broadcast({"type": "cookie_counted", "data": store.add_cookie(cookie)})
    stats = dashboard_stats()
    await broadcast({"type": "stats", "data": stats})

    return {"status": "ok", "detections_processed": len(result.detections)}


@app.post("/api/camera/frame", status_code=202)
async def receive_camera_frame(frame: CameraFrame, background: BackgroundTasks):
    """C2 → C4: relay the same resized image and prompts sent to C3.

    Answers 202 before the WebSocket relay so a slow browser never delays C2.
    """
    metrics.record_camera(frame)
    tracker.note_width(frame.session_id, frame.width)
    data = frame.model_dump()
    data["frame"] = data.pop("image_b64")
    background.add_task(broadcast, {"type": "camera_frame", "data": data})
    return {"status": "accepted"}


@app.get("/api/stats")
async def get_stats():
    return dashboard_stats()


@app.get("/api/inspections")
async def list_inspections(limit: int = 50):
    return store.get_recent(limit)


@app.get("/api/defects")
async def list_defects(limit: int = 50):
    return store.get_defects(limit)


@app.get("/api/cookies")
async def list_cookies(limit: int = 50):
    """검사선을 통과해 1회 집계된 쿠키 목록 (최신순)."""
    return store.get_cookies(limit)


@app.get("/api/export/csv")
async def export_csv():
    """추적 기준 CSV: 쿠키 1개당 1행."""
    return Response(
        content=store.export_cookies_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=inspection-cookies.csv"},
    )


@app.get("/api/export/detections.csv")
async def export_detections_csv():
    """검출 단위 CSV: C3 결과의 프레임별 검출 1건당 1행 (같은 쿠키가 여러 행에 나옴)."""
    return Response(
        content=store.export_detections_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=inspection-detections.csv"},
    )


@app.get("/api/scan-line")
async def get_scan_line():
    return tracker.config()


@app.post("/api/scan-line")
async def set_scan_line(body: ScanLine):
    """대시보드 클릭으로 검사선 x(이미지 폭 대비 비율)를 바꾼다. 재시작하면 C4_SCAN_LINE_RATIO로 돌아간다."""
    tracker.set_scan_line(body.ratio)
    await broadcast({"type": "scan_line", "data": tracker.config()})
    return tracker.config()


@app.get("/api/connections")
async def connections():
    """Observed connectivity; absence of input is not proof that upstream is down."""
    return {
        "c1_metrics": {"enabled": bool(C1_METRICS_URL), "available": metrics.c1_available,
                       "last_success_at_ms": metrics.c1_updated_at_ms},
        "c2_stats": {"enabled": bool(C2_STATS_URL), "available": metrics.c2_available,
                     "last_success_at_ms": metrics.c2_updated_at_ms},
        "camera": {"last_received_at_ms": metrics.last_camera_at_ms},
        "results": {"last_received_at_ms": metrics.last_result_at_ms},
        "websocket_clients": len(ws_clients),
        "mqtt_required": False,
    }


@app.get("/healthz")
async def healthz():
    return {"status": "healthy"}


@app.websocket("/ws/dashboard")
async def dashboard_ws(ws: WebSocket):
    await ws.accept()
    ws_clients.append(ws)
    try:
        await ws.send_text(json.dumps({"type": "stats", "data": dashboard_stats()}))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if ws in ws_clients:
            ws_clients.remove(ws)


static_dir = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
async def index():
    return FileResponse(static_dir / "index.html")


@app.get("/viewer")
async def viewer():
    return FileResponse(static_dir / "viewer.html")
