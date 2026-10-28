"""
C4 Visualizer — 과자 크랙 검사 대시보드 + 모니터링
Port 8003 | POST /results (C3 → C4) | WS /ws/dashboard | GET / (UI)
"""

import json
import math
import time
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from src.models import DetectionResult
from src.store import InspectionStore, InspectionRecord

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("c4")

app = FastAPI(title="C4 Visualizer")
store = InspectionStore(max_records=500)
ws_clients: list[WebSocket] = []

CIRCULARITY_THRESHOLD = 0.85


async def broadcast(payload: dict):
    msg = json.dumps(payload)
    dead = []
    for ws in ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        ws_clients.remove(ws)


def compute_circularity(area: float, perimeter: float) -> float:
    if perimeter <= 0:
        return 0.0
    return (4 * math.pi * area) / (perimeter * perimeter)


@app.post("/results")
async def receive_result(result: DetectionResult):
    """C3 Inference → C4: detection-result 수신."""
    now_ms = int(time.time() * 1000)
    e2e = now_ms - result.captured_at_ms

    if store._last_frame_id >= 0 and result.frame_id > store._last_frame_id + 1:
        dropped = result.frame_id - store._last_frame_id - 1
        logger.warning("frame drop detected: %d frames missing (frame %d → %d)",
                       dropped, store._last_frame_id, result.frame_id)

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
            e2e_latency_ms=e2e,
            container_id=result.inference_container_id,
        )
        store.add(rec)

        await broadcast({
            "type": "inspection_result",
            "data": {
                "item_id": det.object_id,
                "status": status,
                "circularity": round(circ, 4),
                "area": round(area, 1),
                "perimeter": round(perimeter, 1),
                "defect_score": round(det.defect_score, 4),
                "inference_latency_ms": round(result.inference_latency_ms, 1),
                "e2e_latency_ms": e2e,
                "container_id": result.inference_container_id,
                "frame_id": result.frame_id,
                "bbox": det.bbox,
                "defect_type": det.defect_type,
            },
        })

    stats = store.get_stats()
    await broadcast({"type": "stats", "data": stats})

    return {"status": "ok", "detections_processed": len(result.detections)}


@app.get("/api/stats")
async def get_stats():
    return store.get_stats()


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
        await ws.send_text(json.dumps({"type": "stats", "data": store.get_stats()}))
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
