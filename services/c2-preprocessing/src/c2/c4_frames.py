"""C2 -> C4 리사이즈 프레임 전송.

C4는 trace_id로 이 이미지와 C3 결과(bbox, mask)를 짝지어 그린다. C3 결과는 C2가 리사이즈한 이미지 기준 좌표라서
C4에서 좌표를 변환할 필요가 없다.

  POST {C2_C4_FRAME_URL}   (기본 http://c4-visualizer:8003/api/camera/frame)
  body: {trace_id, session_id, frame_id, gateway_received_at_ms, preprocess_started_at_ms,
         preprocess_finished_at_ms, width, height, prompts, image_b64}       (C4와 합의된 10개 필드)

- gateway_received_at_ms는 입력 frame-envelope 값(C1 수신 시각)을 그대로 전달한다.
- preprocess_*_at_ms, width/height/prompts/image_b64는 C3로 보낸 processed-frame 값을 그대로 쓴다.
  JPEG를 다시 인코딩하지 않는다.
- C3 전송과 동시에 보내고 응답을 기다리지 않는다. 그래야 이미지가 C3 결과보다 먼저 C4에 도착한다.
- prompts가 0개라 C3로 보내지 않은 프레임(skipped_empty)도 보낸다. 이 프레임에는 C3 결과가 오지 않는다.
- 메인 경로(C3 전송)와 분리된다. 별도 httpx 클라이언트를 쓰고, 동시 전송 한도(C2_C4_MAX_INFLIGHT)가 차 있으면
  그 프레임은 보내지 않고 c4_skipped로 센다. 타임아웃은 짧게(C2_C4_TIMEOUT_S).
"""
import asyncio
import json
import logging
from typing import Optional

import httpx

from .config import Settings
from .events import EventLog

log = logging.getLogger("c2.c4")

C4_FIELDS = ("trace_id", "session_id", "frame_id", "gateway_received_at_ms", "preprocess_started_at_ms",
             "preprocess_finished_at_ms", "width", "height", "prompts", "image_b64")


def c4_frame_body(msg: dict, envelope: dict) -> dict:
    """processed-frame(msg)과 입력 frame-envelope에서 C4 프레임 본문을 만든다.
    gateway_received_at_ms만 envelope에서, 나머지는 msg에서 가져온다. image_b64는 같은 문자열을 재사용한다."""
    body = {k: msg[k] for k in C4_FIELDS if k != "gateway_received_at_ms"}
    body["gateway_received_at_ms"] = envelope["gateway_received_at_ms"]
    return {k: body[k] for k in C4_FIELDS}


class C4FrameSender:
    def __init__(self, settings: Settings, events: EventLog,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self.url = settings.c4_frame_url
        self.limit = settings.c4_max_inflight
        self.timeout_s = settings.c4_timeout_s
        self.events = events
        self.client = httpx.AsyncClient(timeout=settings.c4_timeout_s, transport=transport,
                                        limits=httpx.Limits(max_connections=max(1, self.limit)))
        self.inflight = 0
        self.sent = 0
        self.skipped = 0
        self.error = 0
        self._tasks: set[asyncio.Task] = set()

    def submit(self, msg: dict, envelope: dict, source: str):
        """전송을 예약하고 바로 돌아온다. 한도가 차 있으면 보내지 않는다."""
        if self.inflight >= self.limit:
            self.skipped += 1
            return
        self.inflight += 1
        task = asyncio.create_task(self._send(msg, envelope, source))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, msg: dict, envelope: dict, source: str):
        try:
            body = json.dumps(c4_frame_body(msg, envelope), separators=(",", ":")).encode()
            resp = await self.client.post(self.url, content=body, headers={
                "Content-Type": "application/json",
                "X-Trace-Id": msg["trace_id"],
                "X-Frame-Id": str(msg["frame_id"]),
                "X-Session-Id": msg["session_id"],
            })
            if resp.status_code >= 400:
                self._fail(envelope, source, f"C4 returned {resp.status_code}")
            else:
                self.sent += 1
        except httpx.TimeoutException as e:
            self._fail(envelope, source, f"C4 timeout: {e!r}")
        except httpx.HTTPError as e:
            self._fail(envelope, source, f"C4 request failed: {e!r}")
        except Exception as e:          # noqa: BLE001 - 메인 경로로 예외가 새지 않게
            log.exception("unexpected C4 send error")
            self._fail(envelope, source, f"internal error: {e!r}")
        finally:
            self.inflight -= 1

    def _fail(self, envelope, source, detail):
        self.error += 1
        log.debug("trace_id=%s %s", envelope.get("trace_id"), detail)
        self.events.write(envelope, "c4_error", source, detail=detail, count=False)

    def stats(self) -> dict:
        return {"c4_sent": self.sent, "c4_skipped": self.skipped, "c4_error": self.error,
                "c4_inflight": self.inflight}

    async def aclose(self):
        """남은 전송은 타임아웃 안에 끝나므로 그만큼만 기다린 뒤 닫는다."""
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=self.timeout_s + 0.5)
        for t in set(self._tasks):
            t.cancel()
        await self.client.aclose()
