"""
검사 결과 인메모리 저장 + 통계 집계.
경량 컨테이너 용도 — SQLite 불필요, 메모리에서 최근 N건만 유지.

- 검출(detection): C3 결과의 프레임별 검출 1건 = 1행. 같은 쿠키가 여러 프레임에 반복된다.
- 쿠키(cookie): src/tracker.py가 검사선 통과 시 1회 확정한 쿠키 1개 = 1행. 상단 통계와 기본 CSV 기준.
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
    defect_score: float = 0.0
    bbox: list[float] | None = None
    inference_latency_ms: float = 0.0
    container_id: str = ""
    timestamp: float = 0.0


COOKIE_FIELDS = [
    "cookie_id", "session_id", "status", "defect_type", "defect_score",
    "votes_normal", "votes_defect", "votes_unknown", "crossed_frame_id",
    "first_frame_id", "last_frame_id", "frames_seen", "last_object_id", "timestamp",
]


class InspectionStore:
    def __init__(self, max_records: int = 500):
        self._records: deque[InspectionRecord] = deque(maxlen=max_records)
        self._cookies: deque[dict] = deque(maxlen=max_records)
        self._cookie_counts = {"normal": 0, "defect": 0, "unknown": 0}
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

    FIELDS = (
        "item_id", "trace_id", "frame_id", "status", "session_id", "defect_type",
        "defect_score", "bbox", "inference_latency_ms", "container_id", "timestamp",
    )

    @staticmethod
    def _row(r: InspectionRecord) -> dict:
        return {
            "item_id": r.item_id,
            "trace_id": r.trace_id,
            "frame_id": r.frame_id,
            "status": r.status,
            "session_id": r.session_id,
            "defect_type": r.defect_type,
            "defect_score": round(r.defect_score, 4),
            "bbox": r.bbox,
            "inference_latency_ms": round(r.inference_latency_ms, 1),
            "container_id": r.container_id,
            "timestamp": round(r.timestamp, 3),
        }

    def get_recent(self, limit: int = 50) -> list[dict]:
        return [self._row(r) for r in list(self._records)[:limit]]

    def add_cookie(self, cookie: dict) -> dict:
        votes = cookie["votes"]
        row = {name: cookie.get(name) for name in COOKIE_FIELDS}
        row.update(votes_normal=votes["normal"], votes_defect=votes["defect"],
                   votes_unknown=votes["unknown"], timestamp=round(time.time(), 3))
        self._cookies.appendleft(row)
        self._cookie_counts[cookie["status"]] += 1
        return row

    def get_cookies(self, limit: int = 50) -> list[dict]:
        return list(self._cookies)[:limit]

    def export_cookies_csv(self) -> str:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=COOKIE_FIELDS)
        writer.writeheader()
        writer.writerows(self._cookies)
        return buf.getvalue()

    def get_defects(self, limit: int = 50) -> list[dict]:
        return [r for r in self.get_recent(limit * 3) if r["status"] == "defect"][:limit]

    def export_detections_csv(self) -> str:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=self.FIELDS)
        writer.writeheader()
        for r in self._records:
            row = self._row(r)
            row["bbox"] = " ".join(f"{v:g}" for v in r.bbox) if r.bbox else ""
            writer.writerow(row)
        return buf.getvalue()

    def get_stats(self) -> dict:
        """Top-level counts are tracked cookies; per-frame detection counts are kept under `detections`."""
        counts = self._cookie_counts
        total = sum(counts.values())
        return {
            "total": total,
            "pass_count": counts["normal"],
            "unknown_count": counts["unknown"],
            "fail_count": counts["defect"],
            "defect_rate": round(counts["defect"] / total * 100, 1) if total > 0 else 0.0,
            "detections": {
                "total": self._total,
                "pass_count": self._pass_count,
                "unknown_count": self._unknown_count,
                "fail_count": self._fail_count,
            },
        }
