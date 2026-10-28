"""개발용 가짜 /infer 서버 (C3 + Nginx 대역).

C3 없이 C1 -> C2 통합 테스트를 할 때 C2_INFER_URL을 이 서버로 돌린다.
받은 processed-frame을 스키마로 검증하고 로그만 남긴다. 통과하면 200, 위반이면 422로 응답한다.
추론은 하지 않으며 detection-result도 C4로 보내지 않는다.

  python dev/fake_infer.py --port 8080 [--delay-ms 0]
  GET /stats  : 받은 프레임 수, 스키마 위반 수, 세션별 마지막 frame_id,
               도착 시점 프레임 나이(now - captured_at_ms) 최근 값·최대값
"""
import argparse
import asyncio
import logging
import os
import sys
import time
from collections import Counter, deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from c2 import schemas  # noqa: E402

log = logging.getLogger("fake-infer")


def create_app(delay_ms: float = 0.0) -> FastAPI:
    app = FastAPI(title="fake /infer")
    stats = Counter()
    last_frame = {}
    ages = deque(maxlen=200)          # (frame_id, 도착 시점 나이 ms)
    app.state.trace_ids = deque(maxlen=1000)   # 받은 trace_id (테스트에서 C4로 간 프레임과 비교)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/stats")
    async def get_stats():
        recent = [a for _, a in ages]
        return {**stats, "last_frame_id": last_frame,
                "age_ms": {"max": max(recent, default=None), "recent": list(ages)[-20:]}}

    @app.post("/infer")
    async def infer(request: Request):
        received = int(time.time() * 1000)
        try:
            msg = await request.json()
        except ValueError:
            stats["invalid"] += 1
            return JSONResponse({"error": "body is not valid JSON"}, status_code=422)
        errs = schemas.errors("processed-frame", msg) if isinstance(msg, dict) else ["<root>: not an object"]
        if errs:
            stats["invalid"] += 1
            log.error("schema violation trace_id=%s: %s",
                      msg.get("trace_id") if isinstance(msg, dict) else None, errs)
            return JSONResponse({"error": "processed-frame schema violation", "details": errs}, status_code=422)

        stats["received"] += 1
        last_frame[msg["session_id"]] = msg["frame_id"]
        ages.append((msg["frame_id"], received - msg["captured_at_ms"]))
        app.state.trace_ids.append(msg["trace_id"])
        hdr = request.headers
        header_ok = (hdr.get("x-trace-id") == msg["trace_id"] and hdr.get("x-frame-id") == str(msg["frame_id"])
                     and hdr.get("x-session-id") == msg["session_id"])
        if not header_ok:
            stats["header_mismatch"] += 1
        log.info("session=%s frame_id=%s trace_id=%s %dx%d prompts=%d preprocess=%dms "
                 "captured->infer=%dms image=%s headers=%s",
                 msg["session_id"], msg["frame_id"], msg["trace_id"], msg["width"], msg["height"],
                 len(msg["prompts"]), msg["preprocess_finished_at_ms"] - msg["preprocess_started_at_ms"],
                 received - msg["captured_at_ms"], "yes" if msg.get("image_b64") else "no",
                 "ok" if header_ok else "MISMATCH")
        if delay_ms > 0:                       # C3 추론 시간 흉내
            await asyncio.sleep(delay_ms / 1000)
        return {"ok": True, "trace_id": msg["trace_id"], "frame_id": msg["frame_id"],
                "num_prompts": len(msg["prompts"])}

    return app


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--delay-ms", type=float, default=0.0, help="응답 전에 기다릴 시간 (C3 추론 시간 흉내)")
    args = ap.parse_args()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    import uvicorn
    uvicorn.run(create_app(args.delay_ms), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
