"""프레임 단위 이벤트 CSV와 누적 카운터.

이벤트
  forwarded      전처리 후 /infer 전달 성공
  skipped_empty  prompts가 0개라 /infer로 보내지 않음 (C2_SKIP_EMPTY=true). C4 전송은 그대로 한다
  dropped_stale  처리 전에 같은 세션의 새 프레임이 와서 버림 (세션별 최신 1장만 보관)
  dropped_old    captured_at_ms 기준 C2_MAX_FRAME_AGE_MS보다 오래돼서 버림
  error          입력 오류(JSON/스키마/디코딩) 또는 /infer 실패(연결 실패, 4xx/5xx)
  timeout        /infer 타임아웃
  c4_error       C4로 프레임 전송 실패 (CSV에만 기록. 누적 수는 /stats의 c4_error. C4 성공·skip은 기록하지 않음)

컬럼은 C1 이벤트 로그(c1_events.csv)와 맞췄다. trace_id / frame_id로 C1·C4 로그와 조인한다.
이벤트 루프 스레드에서만 호출한다.
"""
import csv
import os
from collections import Counter
from typing import Optional

from .pipeline import now_ms

EVENTS = ("forwarded", "skipped_empty", "dropped_stale", "dropped_old", "error", "timeout")


class EventLog:
    FIELDS = ["trace_id", "session_id", "frame_id", "captured_at_ms", "gateway_received_at_ms",
              "source", "event", "event_at_ms", "preprocess_ms", "infer_rtt_ms", "detail"]

    def __init__(self, path: Optional[str] = None):
        self.counts = Counter({e: 0 for e in EVENTS})
        self.fp = None
        if path:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            new = not os.path.exists(path) or os.path.getsize(path) == 0
            self.fp = open(path, "a", newline="", encoding="utf-8")
            self.w = csv.DictWriter(self.fp, fieldnames=self.FIELDS)
            if new:
                self.w.writeheader()

    def write(self, envelope, event: str, source: str, preprocess_ms=None, infer_rtt_ms=None, detail="",
              count: bool = True):
        """count=False면 CSV에만 남기고 프레임 이벤트 누적 수(counts)에는 넣지 않는다 (c4_error)."""
        if count:
            self.counts[event] += 1
        if not self.fp:
            return
        env = envelope if isinstance(envelope, dict) else {}
        self.w.writerow({
            "trace_id": env.get("trace_id", ""), "session_id": env.get("session_id", ""),
            "frame_id": env.get("frame_id", ""), "captured_at_ms": env.get("captured_at_ms", ""),
            "gateway_received_at_ms": env.get("gateway_received_at_ms", ""),
            "source": source, "event": event, "event_at_ms": now_ms(),
            "preprocess_ms": "" if preprocess_ms is None else preprocess_ms,
            "infer_rtt_ms": "" if infer_rtt_ms is None else round(infer_rtt_ms, 1),
            "detail": str(detail)[:300],
        })
        self.fp.flush()

    def close(self):
        if self.fp:
            self.fp.close()
            self.fp = None
