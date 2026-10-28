"""
C4 Visualizer — 과자 크랙 검사 대시보드 + 모니터링
Port 8003 | POST /results (C3 → C4) | WS /ws/dashboard | GET / (UI)
"""

import asyncio
import os
import time
from contextlib import asynccontextmanager, suppress
from urllib.request import urlopen

import json
import math
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from src.models import CameraFrame, DetectionResult
from src.metrics import PipelineMetrics
from src.store import InspectionStore, InspectionRecord

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("c4")

metrics = PipelineMetrics()
C2_STATS_URL = os.getenv("C4_C2_STATS_URL", "http://c2-preprocessing:8001/stats")


def fetch_c2_stats():
    with urlopen(C2_STATS_URL, timeout=1) as response:
        return json.load(response)


def dashboard_stats():
    return {**store.get_stats(), **metrics.get_stats()}


async def poll_metrics():
    while True:
        if C2_STATS_URL:
            try:
                metrics.update_c2(await asyncio.to_thread(fetch_c2_stats))
            except Exception:
                metrics.c2_available = False
        await broadcast({"type": "stats", "data": dashboard_stats()})
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(poll_metrics())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="C4 Visualizer", lifespan=lifespan)
store = InspectionStore(max_records=500)
ws_clients: list[WebSocket] = []

CIRCULARITY_THRESHOLD = 0.85


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


def compute_circularity(area: float, perimeter: float) -> float:
    if perimeter <= 0:
        return 0.0
    return (4 * math.pi * area) / (perimeter * perimeter)


@app.post("/results")
async def receive_result(result: DetectionResult):
    """C3 Inference → C4: detection-result 수신."""
    metrics.record(result, time.time() * 1000)
    overlays = []
    for det in result.detections:
        bbox = det.bbox
        w = abs(bbox[2] - bbox[0])
        h = abs(bbox[3] - bbox[1])
        area = w * h
        perimeter = 2 * (w + h)
        circ = compute_circularity(area, perimeter)

        status = "pass" if circ >= CIRCULARITY_THRESHOLD else "fail"

        rec = InspectionRecord(
            item_id=det.object_id,
            trace_id=result.trace_id,
            frame_id=result.frame_id,
            status=status,
            circularity=circ,
            area=area,
            perimeter=perimeter,
            defect_score=det.defect_score,
            inference_latency_ms=result.inference_latency_ms,
            container_id=result.inference_container_id,
        )
        store.add(rec)
        overlays.append({
            "item_id": det.object_id, "bbox": det.bbox, "mask": det.mask,
            "status": status, "circularity": round(circ, 4),
        })

        await broadcast({
            "type": "inspection_result",
            "data": {
                "item_id": det.object_id,
                "trace_id": result.trace_id,
                "session_id": result.session_id,
                "status": status,
                "circularity": round(circ, 4),
                "area": round(area, 1),
                "perimeter": round(perimeter, 1),
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
        "frame_id": result.frame_id, "detections": overlays,
    }})
    stats = dashboard_stats()
    await broadcast({"type": "stats", "data": stats})

    return {"status": "ok", "detections_processed": len(result.detections)}


@app.post("/api/camera/frame")
async def receive_camera_frame(frame: CameraFrame):
    """C2 → C4: relay the same resized image and prompts sent to C3."""
    data = frame.model_dump()
    data["frame"] = data.pop("image_b64")
    await broadcast({"type": "camera_frame", "data": data})
    return {"status": "ok"}


@app.get("/api/stats")
async def get_stats():
    return dashboard_stats()


@app.get("/api/inspections")
async def list_inspections(limit: int = 50):
    return store.get_recent(limit)


@app.get("/api/defects")
async def list_defects(limit: int = 50):
    return store.get_defects(limit)


@app.get("/api/export/csv")
async def export_csv():
    """검사 결과를 CSV로 다운로드."""
    csv_data = store.export_csv()
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=inspection-results.csv"},
    )


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
