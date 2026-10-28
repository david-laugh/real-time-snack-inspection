"""개발용 가짜 C4 프레임 수신 서버 (POST /api/camera/frame).

C2가 C4로 보내는 리사이즈 프레임을 C4와 합의한 형식대로 검증하고 기록만 한다.
  body: {trace_id, session_id, frame_id, width, height, prompts, image_b64}  -- 정확히 이 7개 필드
  - image_b64가 JPEG로 디코딩되고, 디코딩한 크기가 width x height와 같아야 한다
통과하면 200, 위반이면 422. --delay-ms로 느린 C4를, 서버를 띄우지 않는 것으로 꺼진 C4를 흉내 낸다.

  python dev/fake_c4.py --port 8003 [--delay-ms 0]
  GET /stats : 받은 수, 위반 수, 최근 trace_id
"""
import argparse
import asyncio
import base64
import binascii
import logging
import os
from collections import Counter, OrderedDict

import cv2
import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

log = logging.getLogger("fake-c4")

C4_FIELDS = {"trace_id", "session_id", "frame_id", "width", "height", "prompts", "image_b64"}


def check_frame(body) -> list[str]:
    """합의 형식 위반 목록. 비어 있으면 통과."""
    if not isinstance(body, dict):
        return ["body is not an object"]
    errs = []
    keys = set(body)
    if keys != C4_FIELDS:
        errs.append(f"fields mismatch: missing={sorted(C4_FIELDS - keys)} extra={sorted(keys - C4_FIELDS)}")
        return errs
    if not isinstance(body["prompts"], list):
        errs.append("prompts is not a list")
    try:
        raw = base64.b64decode(body["image_b64"], validate=True)
    except (binascii.Error, ValueError, TypeError):
        return errs + ["image_b64 is not base64"]
    if not raw.startswith(b"\xff\xd8"):
        errs.append("image_b64 is not JPEG")
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        errs.append("image_b64 does not decode")
    elif (img.shape[1], img.shape[0]) != (body["width"], body["height"]):
        errs.append(f"image {img.shape[1]}x{img.shape[0]} != width/height {body['width']}x{body['height']}")
    return errs


def create_app(delay_ms: float = 0.0, keep: int = 200) -> FastAPI:
    app = FastAPI(title="fake C4 /api/camera/frame")
    stats = Counter()
    app.state.frames = OrderedDict()      # trace_id -> body (최근 keep개). 테스트에서 C3 요청과 비교할 때 쓴다

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/stats")
    async def get_stats():
        return {**stats, "recent_trace_ids": list(app.state.frames)[-20:]}

    @app.post("/api/camera/frame")
    async def frame(request: Request):
        if delay_ms > 0:                   # 느린 C4 흉내
            await asyncio.sleep(delay_ms / 1000)
        try:
            body = await request.json()
        except ValueError:
            stats["invalid"] += 1
            return JSONResponse({"error": "body is not valid JSON"}, status_code=422)
        errs = check_frame(body)
        if errs:
            stats["invalid"] += 1
            log.error("invalid frame trace_id=%s: %s", body.get("trace_id") if isinstance(body, dict) else None, errs)
            return JSONResponse({"error": "invalid camera frame", "details": errs}, status_code=422)
        stats["received"] += 1
        app.state.frames[body["trace_id"]] = body
        while len(app.state.frames) > keep:
            app.state.frames.popitem(last=False)
        log.info("session=%s frame_id=%s trace_id=%s %dx%d prompts=%d", body["session_id"], body["frame_id"],
                 body["trace_id"], body["width"], body["height"], len(body["prompts"]))
        return {"ok": True}

    return app


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8003)
    ap.add_argument("--delay-ms", type=float, default=0.0, help="응답 전에 기다릴 시간 (느린 C4 흉내)")
    args = ap.parse_args()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    import uvicorn
    uvicorn.run(create_app(args.delay_ms), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
