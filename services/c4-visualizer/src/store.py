"""
검사 결과 인메모리 저장 + 통계 집계.
경량 컨테이너 용도 — SQLite 불필요, 메모리에서 최근 N건만 유지.
"""

import csv
import io
import time
from collections import deque
from dataclasses import dataclass


@dataclass
class InspectionRecord:
    item_id: str
    trace_id: str
    frame_id: int
    status: str             # normal | defect | unknown
    session_id: str = ""
    defect_type: str | None = None
    circularity: float | None = None
    area: float | None = None
    perimeter: float | None = None
    defect_score: float = 0.0
    inference_latency_ms: float = 0.0
    container_id: str = ""
    timestamp: float = 0.0


class InspectionStore:
    def __init__(self, max_records: int = 500):
        self._records: deque[InspectionRecord] = deque(maxlen=max_records)
        self._total = 0
        self._fail_count = 0
        self._pass_count = 0
        self._unknown_count = 0

    def add(self, rec: InspectionRecord):
        rec.timestamp = time.time()
        self._records.appendleft(rec)
        self._total += 1
        if rec.status == "defect":
            self._fail_count += 1
        elif rec.status == "normal":
            self._pass_count += 1
        else:
            self._unknown_count += 1

    def get_recent(self, limit: int = 50) -> list[dict]:
        out = []
        for r in list(self._records)[:limit]:
            out.append({
                "item_id": r.item_id,
                "trace_id": r.trace_id,
                "frame_id": r.frame_id,
                "status": r.status,
                "session_id": r.session_id,
                "defect_type": r.defect_type,
                "circularity": None,
                "area": r.area,
                "perimeter": r.perimeter,
                "defect_score": round(r.defect_score, 4),
                "inference_latency_ms": round(r.inference_latency_ms, 1),
                "container_id": r.container_id,
                "timestamp": round(r.timestamp, 3),
            })
        return out

    def get_defects(self, limit: int = 50) -> list[dict]:
        return [r for r in self.get_recent(limit * 3) if r["status"] == "defect"][:limit]

    def export_csv(self) -> str:
        buf = io.StringIO()
        fields = [
            "item_id", "trace_id", "frame_id", "status", "session_id", "defect_type", "circularity",
            "area", "perimeter", "defect_score", "inference_latency_ms",
            "container_id", "timestamp",
        ]
        writer = csv.DictWriter(buf, fieldnames=fields)
        writer.writeheader()
        for r in self._records:
            writer.writerow({
                "item_id": r.item_id,
                "trace_id": r.trace_id,
                "frame_id": r.frame_id,
                "status": r.status,
                "session_id": r.session_id,
                "defect_type": r.defect_type,
                "circularity": None,
                "area": r.area,
                "perimeter": r.perimeter,
                "defect_score": round(r.defect_score, 4),
                "inference_latency_ms": round(r.inference_latency_ms, 1),
                "container_id": r.container_id,
                "timestamp": round(r.timestamp, 3),
            })
        return buf.getvalue()

    def get_stats(self) -> dict:
        pass_count = self._pass_count
        defect_rate = round(self._fail_count / self._total * 100, 1) if self._total > 0 else 0.0

        return {
            "total": self._total,
            "pass_count": pass_count,
            "unknown_count": self._unknown_count,
            "fail_count": self._fail_count,
            "defect_rate": defect_rate,
        }
