#!/usr/bin/env python3
"""
C1 Gateway - pocket-smart-factory

폰/Simulator 프레임을 WebSocket으로 받아 frame-envelope로 감싸 C2로 넘긴다.
계약: shared/schemas/frame-envelope.schema.json, docs/api-contract.md

  serve    게이트웨이 본체 (상태 페이지 /, 폰 카메라 페이지 /cam, WS /ws/stream)
  stub     가짜 C2. 받은 envelope를 검증하고, 원하면 가짜 detection-result를 C4로 보낸다
  loadgen  합성 프레임/영상/이미지 폴더를 지정 FPS로 /ws/stream에 송출 (벤치마크)

Client → C1 프로토콜 (WS /ws/stream?s=<session_id>)
  binary 메시지 = [8바이트 big-endian uint64 captured_at_ms][JPEG 바이트]
    JPEG만 보내도 된다(FF D8로 시작). 그때 captured_at_ms = 수신 시각.
  text 메시지  = {"type":"ping","t0":<client_ms>}
    → {"type":"pong","t0":..,"server_ms":..}  (클라이언트가 시계 오프셋 보정에 사용)

C1 → C2
  POST {C1_DOWNSTREAM}  (기본 http://c2-preprocessing:8001/preprocess)
  JSON frame-envelope, 헤더 X-Trace-Id / X-Frame-Id / X-Session-Id
  C1은 C2 응답(2xx)을 받으면 그 프레임을 완료로 본다.

환경 변수 (.env): C1_PORT, C1_DOWNSTREAM, C1_PUBLIC_URL, C1_MAX_INFLIGHT,
  C1_TIMEOUT, C1_TOKEN, C1_DASHBOARD_URL, C1_EVENT_LOG, SCHEMA_VERSION, LOG_LEVEL
"""

import argparse
import asyncio
import base64
import collections
import csv
import io
import json
import os
import random
import secrets
import socket
import struct
import sys
import time
import uuid

TS_HEADER = struct.Struct(">Q")


def now_ms() -> int:
    return int(time.time() * 1000)


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def jpeg_size(data: bytes):
    """JPEG 헤더(SOF)만 읽어 (width, height). 실패하면 (0, 0)."""
    i, n = 2, len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return 0, 0


def split_message(data: bytes, received_ms: int):
    if data[:2] == b"\xff\xd8":
        return received_ms, data
    if len(data) > TS_HEADER.size + 2:
        (captured,) = TS_HEADER.unpack_from(data)
        return int(captured), data[TS_HEADER.size:]
    return None, None


# ════════════════════════════════════════════════════════════════════
# serve
# ════════════════════════════════════════════════════════════════════

class Frame:
    __slots__ = ("envelope", "jpeg", "forwarded_at")

    def __init__(self, envelope, jpeg):
        self.envelope = envelope
        self.jpeg = jpeg
        self.forwarded_at = 0


class EventLog:
    """프레임 단위 이벤트 CSV. C4 결과와 frame_id/trace_id로 조인해 drop·latency 분석."""

    FIELDS = ["trace_id", "session_id", "frame_id", "captured_at_ms",
              "gateway_received_at_ms", "event", "event_at_ms", "c2_rtt_ms", "detail"]

    def __init__(self, path):
        self.fp = None
        if path:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            new = not os.path.exists(path)
            self.fp = open(path, "a", newline="", encoding="utf-8")
            self.w = csv.DictWriter(self.fp, fieldnames=self.FIELDS)
            if new:
                self.w.writeheader()

    def write(self, env, event, rtt=None, detail=""):
        if not self.fp:
            return
        self.w.writerow({"trace_id": env["trace_id"], "session_id": env["session_id"],
                         "frame_id": env["frame_id"], "captured_at_ms": env["captured_at_ms"],
                         "gateway_received_at_ms": env["gateway_received_at_ms"],
                         "event": event, "event_at_ms": now_ms(),
                         "c2_rtt_ms": "" if rtt is None else round(rtt, 1), "detail": detail})

    def flush(self):
        if self.fp:
            self.fp.flush()


class Gateway:
    """
    ingest()     세션별 '최신 1장' 슬롯에 넣는다. 아직 안 보낸 프레임을 덮어쓰면 dropped.
    dispatcher() C2로 나가 있는 요청이 max_inflight 미만일 때만 슬롯에서 꺼내 보낸다.
    뒤쪽이 느리면 C1에서 오래된 프레임을 버리므로 지연이 쌓이지 않는다.
    """

    def __init__(self, a):
        self.a = a
        self.echo = not a.downstream
        self.pending = collections.OrderedDict()   # session_id -> Frame
        self.sessions = {}                         # session_id -> next frame_id
        self.viewers = set()
        self.preview = None
        self.last_error = ""
        self.log = EventLog(a.event_log)
        self.reset()

    def reset(self):
        self.t_reset = now_ms()
        self.rx_t = collections.deque(maxlen=4000)
        self.fw_t = collections.deque(maxlen=4000)
        self.rtt = collections.deque(maxlen=1000)
        self.wait = collections.deque(maxlen=1000)
        self.skew = collections.deque(maxlen=200)
        self.c = collections.Counter(received=0, forwarded=0, dropped=0, error=0, timeout=0, bad=0)

    async def start(self):
        import httpx
        self.window = asyncio.Semaphore(self.a.max_inflight)
        self.inflight = 0
        self.has_pending = asyncio.Event()
        self.client = httpx.AsyncClient(timeout=self.a.timeout,
                                        limits=httpx.Limits(max_connections=self.a.max_inflight + 4))
        self.tasks = [asyncio.create_task(self.stats_loop())]
        if not self.echo:
            self.tasks.append(asyncio.create_task(self.dispatcher()))

    async def stop(self):
        for t in self.tasks:
            t.cancel()
        await self.client.aclose()
        self.log.flush()

    # ── 입력 ─────────────────────────────────────────
    def ingest(self, session, data):
        received = now_ms()
        captured, jpeg = split_message(data, received)
        if jpeg is None or jpeg[:2] != b"\xff\xd8":
            self.c["bad"] += 1
            return
        w, h = jpeg_size(jpeg)
        if not w:
            self.c["bad"] += 1
            return
        fid = self.sessions.get(session, 0)
        self.sessions[session] = fid + 1
        env = {"schema_version": self.a.schema_version, "trace_id": uuid.uuid4().hex,
               "session_id": session, "frame_id": fid, "captured_at_ms": captured,
               "source_width": w, "source_height": h, "format": "jpeg",
               "gateway_received_at_ms": received}
        f = Frame(env, jpeg)
        self.c["received"] += 1
        self.rx_t.append(received)
        self.skew.append(received - captured)
        self.preview = jpeg
        if self.echo:
            self.c["forwarded"] += 1
            self.fw_t.append(received)
            self.log.write(env, "echo")
            return
        old = self.pending.get(session)
        if old is not None:
            self.c["dropped"] += 1
            self.log.write(old.envelope, "dropped", detail="replaced_by_newer")
        self.pending[session] = f
        self.pending.move_to_end(session)
        self.has_pending.set()

    # ── C2 전달 ──────────────────────────────────────
    async def dispatcher(self):
        while True:
            await self.window.acquire()
            while not self.pending:
                self.has_pending.clear()
                await self.has_pending.wait()
            _, f = self.pending.popitem(last=False)     # 세션 간 라운드로빈
            self.inflight += 1
            asyncio.create_task(self.send(f))

    async def send(self, f):
        env = f.envelope
        body = dict(env, image_b64=base64.b64encode(f.jpeg).decode("ascii"))
        t0 = time.perf_counter()
        f.forwarded_at = now_ms()
        self.wait.append(f.forwarded_at - env["gateway_received_at_ms"])
        try:
            r = await self.client.post(self.a.downstream, json=body, headers={
                "X-Trace-Id": env["trace_id"], "X-Frame-Id": str(env["frame_id"]),
                "X-Session-Id": env["session_id"]})
            rtt = (time.perf_counter() - t0) * 1000
            if r.status_code >= 300:
                raise RuntimeError(f"C2 HTTP {r.status_code}: {r.text[:120]}")
            self.c["forwarded"] += 1
            self.fw_t.append(now_ms())
            self.rtt.append(rtt)
            self.log.write(env, "forwarded", rtt)
        except Exception as e:
            import httpx
            kind = "timeout" if isinstance(e, httpx.TimeoutException) else "error"
            self.c[kind] += 1
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            self.log.write(env, kind, detail=self.last_error)
        finally:
            self.inflight -= 1
            self.window.release()

    # ── 상태 ─────────────────────────────────────────
    async def stats_loop(self):
        tick = 0
        while True:
            await asyncio.sleep(0.25)
            tick += 1
            if tick % 8 == 0:
                self.log.flush()
            if not self.viewers:
                continue
            msgs = []
            if tick % 4 == 0:
                msgs.append(json.dumps({"type": "stats", **self.stats()}))
            for v in list(self.viewers):
                try:
                    for m in msgs:
                        await asyncio.wait_for(v.send_text(m), 1.0)
                    if self.preview is not None:
                        await asyncio.wait_for(v.send_bytes(self.preview), 1.0)
                except Exception:
                    self.viewers.discard(v)
            self.preview = None

    def stats(self):
        t = now_ms()
        win = min(3000.0, max(1.0, t - self.t_reset))

        def fps(dq):
            return round(sum(1 for x in dq if t - x <= win) * 1000.0 / win, 1)

        def pct(dq):
            s = sorted(dq)
            if not s:
                return {"p50": None, "p95": None}
            return {"p50": round(s[len(s) // 2], 1), "p95": round(s[min(len(s) - 1, int(0.95 * len(s)))], 1)}

        rx = self.c["received"]
        return {
            "mode": "echo" if self.echo else "forward",
            "downstream": self.a.downstream or None,
            "sessions": len(self.sessions), "viewers": len(self.viewers),
            "input_fps": fps(self.rx_t), "forwarded_fps": fps(self.fw_t),
            "drop_rate_c1": round(self.c["dropped"] / rx, 4) if rx else 0.0,
            "inflight": self.inflight if not self.echo else 0, "max_inflight": self.a.max_inflight,
            "c1_queue_wait_ms": pct(self.wait), "c2_rtt_ms": pct(self.rtt),
            "client_clock_gap_ms": pct(self.skew),
            "counters": dict(self.c), "last_error": self.last_error,
        }


def build_app(a):
    from contextlib import asynccontextmanager
    from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse, Response

    gw = Gateway(a)

    @asynccontextmanager
    async def lifespan(_):
        await gw.start()
        yield
        await gw.stop()

    app = FastAPI(title="C1 Gateway", lifespan=lifespan)

    def base_url(request: Request) -> str:
        if a.public_url:
            return a.public_url.rstrip("/")
        host = request.url.hostname or "localhost"
        if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
            host = lan_ip()
        port = request.url.port
        return f"{request.url.scheme}://{host}" + (f":{port}" if port else "")

    def cam_url(request: Request) -> str:
        return base_url(request) + "/cam" + (f"?t={a.token}" if a.token else "")

    @app.get("/", response_class=HTMLResponse)
    async def status_page():
        return STATUS_HTML

    @app.get("/cam", response_class=HTMLResponse)
    async def cam_page():
        return CAM_HTML

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "service": "c1-gateway", "mode": "echo" if gw.echo else "forward"}

    @app.get("/api/info")
    async def info(request: Request):
        return {"cam_url": cam_url(request), "mode": "echo" if gw.echo else "forward",
                "downstream": a.downstream or None, "dashboard_url": a.dashboard_url or None}

    @app.get("/qr.png")
    async def qr(request: Request):
        import qrcode
        buf = io.BytesIO()
        qrcode.make(cam_url(request), border=2, box_size=8).save(buf, format="PNG")
        return Response(buf.getvalue(), media_type="image/png")

    @app.get("/api/metrics")
    async def metrics():
        return gw.stats()

    @app.post("/api/metrics/reset")
    async def metrics_reset():
        gw.reset()
        return {"ok": True}

    @app.websocket("/ws/stream")
    async def ws_stream(ws: WebSocket):
        if a.token and ws.query_params.get("t") != a.token:
            await ws.close(code=4401)
            return
        await ws.accept()
        session = (ws.query_params.get("s") or "s-" + secrets.token_hex(4))[:64]
        if session in gw.sessions:                      # 같은 id 재접속이면 새 세션으로
            session = f"{session}-{secrets.token_hex(2)}"
        gw.sessions[session] = 0
        await ws.send_text(json.dumps({"type": "hello", "session_id": session, "server_ms": now_ms()}))
        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if msg.get("bytes"):
                    gw.ingest(session, msg["bytes"])
                elif msg.get("text"):
                    try:
                        m = json.loads(msg["text"])
                    except ValueError:
                        continue
                    if m.get("type") == "ping":
                        await ws.send_text(json.dumps({"type": "pong", "t0": m.get("t0"), "server_ms": now_ms()}))
        except WebSocketDisconnect:
            pass
        finally:
            gw.pending.pop(session, None)
            gw.sessions.pop(session, None)

    @app.websocket("/ws/status")
    async def ws_status(ws: WebSocket):
        await ws.accept()
        gw.viewers.add(ws)
        try:
            while True:
                await ws.receive_text()
        except Exception:
            pass
        finally:
            gw.viewers.discard(ws)

    return app


def env(name, default):
    return os.getenv(name, default)


def cmd_serve(a):
    import uvicorn
    if a.token == "auto":
        a.token = secrets.token_urlsafe(8)
    scheme = "https" if a.ssl_certfile else "http"
    print(f"[C1] mode: {'forward → ' + a.downstream if a.downstream else 'echo (C1_DOWNSTREAM 비어 있음)'}")
    print(f"[C1] status: {scheme}://{lan_ip()}:{a.port}/   stream: /ws/stream")
    if a.public_url:
        print(f"[C1] public url: {a.public_url}")
    elif scheme == "http":
        print("[C1] 주의: HTTP에서는 폰 카메라가 열리지 않습니다. C1_PUBLIC_URL(HTTPS 터널) 또는 --ssl-* 사용")
    uvicorn.run(build_app(a), host=a.host, port=a.port, log_level=a.log_level.lower(),
                ssl_certfile=a.ssl_certfile, ssl_keyfile=a.ssl_keyfile, ws_max_size=16 * 1024 * 1024)


# ════════════════════════════════════════════════════════════════════
# stub: 가짜 C2 (+ 선택적으로 가짜 C3 결과를 C4로)
# ════════════════════════════════════════════════════════════════════

ENVELOPE_REQUIRED = ["schema_version", "trace_id", "session_id", "frame_id", "captured_at_ms",
                     "source_width", "source_height", "format", "gateway_received_at_ms"]


def cmd_stub(a):
    import httpx
    import uvicorn
    from contextlib import asynccontextmanager
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    st = {"bad": 0, "ok": 0, "image_ok": 0, "image_bad": 0, "saved": 0, "last_image": None}
    if a.save_dir:
        os.makedirs(a.save_dir, exist_ok=True)

    def check_image(env):
        """image_b64가 실제 JPEG인지, 크기가 envelope의 source_width/height와 같은지 확인."""
        from PIL import Image
        b64 = env.get("image_b64")
        if not b64:
            return None, "image_b64 없음"
        try:
            raw = base64.b64decode(b64, validate=True)
        except Exception:
            return None, "base64 디코딩 실패"
        if raw[:2] != b"\xff\xd8":
            return None, "JPEG 시작 바이트(FF D8) 아님"
        try:
            im = Image.open(io.BytesIO(raw))
            im.load()
        except Exception as e:
            return None, f"이미지 열기 실패: {e}"
        if im.format != "JPEG":
            return None, f"형식이 JPEG 아님: {im.format}"
        if (im.width, im.height) != (env["source_width"], env["source_height"]):
            return None, (f"크기 불일치: 실제 {im.width}x{im.height}, "
                          f"envelope {env['source_width']}x{env['source_height']}")
        return raw, None

    @asynccontextmanager
    async def lifespan(_):
        st["sem"] = asyncio.Semaphore(a.workers)
        st["client"] = httpx.AsyncClient(timeout=5.0)
        yield
        await st["client"].aclose()

    app = FastAPI(title="C2 stub", lifespan=lifespan)

    async def to_c4(env, started, finished):
        res = {k: env[k] for k in ("schema_version", "trace_id", "session_id", "frame_id", "captured_at_ms")}
        res.update(inference_container_id="stub", inference_started_at_ms=started,
                   inference_finished_at_ms=finished, inference_latency_ms=float(finished - started),
                   detections=[])
        try:
            await st["client"].post(a.c4.rstrip("/") + "/results", json=res)
        except Exception as e:
            print(f"[stub] C4 post failed: {e}", file=sys.stderr)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "service": "c2-stub",
                **{k: st[k] for k in ("ok", "bad", "image_ok", "image_bad", "saved", "last_image")}}

    @app.post("/preprocess")
    async def preprocess(request: Request):
        env = await request.json()
        missing = [k for k in ENVELOPE_REQUIRED if k not in env]
        extra = [k for k in env if k not in ENVELOPE_REQUIRED + ["image_b64"]]
        hdr_ok = request.headers.get("x-trace-id") == env.get("trace_id")
        if missing or extra or not hdr_ok:
            st["bad"] += 1
            return JSONResponse({"error": "contract violation", "missing": missing,
                                 "extra": extra, "header_trace_match": hdr_ok}, status_code=422)
        raw, why = await asyncio.to_thread(check_image, env)
        if why:
            st["image_bad"] += 1
            print(f"[stub C2] 이미지 오류 frame {env['frame_id']}: {why}", file=sys.stderr)
            return JSONResponse({"error": "image check failed", "detail": why}, status_code=422)
        st["image_ok"] += 1
        st["last_image"] = {"session_id": env["session_id"], "frame_id": env["frame_id"],
                            "size": f"{env['source_width']}x{env['source_height']}", "bytes": len(raw)}
        if a.save_dir and env["frame_id"] % a.save_every == 0:
            name = f"{env['session_id']}_{env['frame_id']:06d}.jpg"
            with open(os.path.join(a.save_dir, name), "wb") as fp:
                fp.write(raw)
            st["saved"] += 1
        async with st["sem"]:
            started = now_ms()
            await asyncio.sleep(max(0.0, random.gauss(a.delay_ms, a.jitter_ms)) / 1000.0)
            finished = now_ms()
        st["ok"] += 1
        if a.c4:
            asyncio.create_task(to_c4(env, started, finished))
        return {"ok": True, "trace_id": env["trace_id"], "frame_id": env["frame_id"]}

    print(f"[stub C2] :{a.port}/preprocess  delay={a.delay_ms}±{a.jitter_ms}ms  workers={a.workers}"
          + (f"  → C4 {a.c4}" if a.c4 else "")
          + (f"  이미지 저장: {a.save_dir} ({a.save_every}장마다)" if a.save_dir else ""))
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


# ════════════════════════════════════════════════════════════════════
# loadgen
# ════════════════════════════════════════════════════════════════════

def load_frames(a):
    from PIL import Image, ImageDraw

    def enc(im):
        if im.width > a.width:
            im = im.resize((a.width, round(im.height * a.width / im.width)))
        b = io.BytesIO()
        im.convert("RGB").save(b, format="JPEG", quality=a.quality)
        return b.getvalue()

    if a.source == "synthetic":
        frames, W = [], a.width
        H = round(W * 9 / 16)
        for i in range(60):
            im = Image.new("RGB", (W, H), (34, 38, 41))
            d = ImageDraw.Draw(im)
            for lane in range(3):
                y = H * (lane + 0.5) / 3
                for k in range(6):
                    x = (W + 60) - ((i * 6 + k * 130 + lane * 40) % (W + 120))
                    d.ellipse([x - 28, y - 28, x + 28, y + 28], fill=(201, 146, 78))
            frames.append(enc(im))
        return frames
    if os.path.isdir(a.source):
        names = sorted(n for n in os.listdir(a.source) if n.lower().endswith((".jpg", ".jpeg", ".png")))
        return [enc(Image.open(os.path.join(a.source, n))) for n in names[: a.max_frames]]
    try:
        import cv2
    except ImportError:
        sys.exit("영상 파일을 쓰려면 opencv-python-headless 를 설치하세요.")
    cap, frames = cv2.VideoCapture(a.source), []
    while len(frames) < a.max_frames:
        ok, bgr = cap.read()
        if not ok:
            break
        frames.append(enc(Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))))
    if not frames:
        sys.exit(f"프레임을 읽지 못했습니다: {a.source}")
    return frames


def cmd_loadgen(a):
    import httpx
    import websockets

    frames = load_frames(a)
    http_base = a.url.replace("wss://", "https://").replace("ws://", "http://").split("/ws/")[0]
    print(f"[loadgen] {len(frames)} frames, {a.clients} client(s) × {a.fps} fps × {a.duration}s → {a.url}")
    if a.reset:
        httpx.post(http_base + "/api/metrics/reset", timeout=5)

    async def client(i):
        q = f"?s=load-{i}" + (f"&t={a.token}" if a.token else "")
        sent = 0
        async with websockets.connect(a.url + q, max_size=None) as ws:
            t0, period = time.perf_counter(), 1.0 / a.fps
            while time.perf_counter() - t0 < a.duration:
                await ws.send(TS_HEADER.pack(now_ms()) + frames[sent % len(frames)])
                sent += 1
                await asyncio.sleep(max(0.0, t0 + sent * period - time.perf_counter()))
        return sent

    async def main():
        return await asyncio.gather(*(client(i) for i in range(a.clients)))

    sent = sum(asyncio.run(main()))
    time.sleep(a.settle)
    m = httpx.get(http_base + "/api/metrics", timeout=5).json()
    c = m["counters"]
    summary = {"label": a.label, "fps": a.fps, "clients": a.clients, "duration_s": a.duration,
               "sent": sent, "received": c["received"], "forwarded": c["forwarded"],
               "dropped_c1": c["dropped"], "errors": c["error"], "timeouts": c["timeout"],
               "forwarded_ratio": round(c["forwarded"] / sent, 3) if sent else 0,
               "forwarded_fps": round(c["forwarded"] / a.duration, 2),
               "c1_queue_wait_ms": m["c1_queue_wait_ms"], "c2_rtt_ms": m["c2_rtt_ms"]}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if a.out:
        with open(a.out, "a", encoding="utf-8") as fp:
            fp.write(json.dumps({"ts": time.time(), **summary}, ensure_ascii=False) + "\n")


# ════════════════════════════════════════════════════════════════════
# 페이지
# ════════════════════════════════════════════════════════════════════

STATUS_HTML = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>C1 게이트웨이</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+KR:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{--steel:#2b3439;--belt:#1d2326;--panel:#eceae4;--rule:#c9c5bb;--ink:#22282b;--muted:#6b6f6c;--dough:#c98a3c;--ng:#c0412b}
*{box-sizing:border-box}
body{margin:0;background:var(--panel);color:var(--ink);font:15px/1.5 "IBM Plex Sans KR",system-ui,sans-serif;font-variant-numeric:tabular-nums}
.wrap{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:20px;padding:20px;max-width:1400px;margin:0 auto}
@media(max-width:900px){.wrap{grid-template-columns:1fr}}
header{grid-column:1/-1;display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
h1{margin:0;font-size:22px;font-weight:600}
.mode{font-size:13px;color:var(--muted)} .mode b{color:var(--ink);font-weight:500}
.mode a{color:var(--steel)}
.stage{position:relative;background:var(--belt);border-radius:6px;overflow:hidden;aspect-ratio:16/9;
background-image:repeating-linear-gradient(90deg,#252c30 0 2px,transparent 2px 46px)}
.stage img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;display:none}
.empty{position:absolute;inset:0;display:grid;place-content:center;text-align:center;color:#aab3b6;padding:20px}
.empty strong{display:block;color:#e8e4da;font-size:18px;font-weight:500;margin-bottom:4px}
.note{font-size:13px;color:var(--muted);margin:8px 0 0}
aside{display:flex;flex-direction:column;gap:18px}
.qr{background:#fff;border:1px solid var(--rule);border-radius:6px;padding:14px;text-align:center}
.qr img{width:190px;height:190px;image-rendering:pixelated}
.qr p{margin:6px 0 0;font-size:13px;color:var(--muted);word-break:break-all}
.warn{background:#f6e3dc;color:#7c2b1c;border-left:3px solid var(--ng);padding:8px 10px;font-size:13px;text-align:left;margin-top:10px;display:none}
h2{font-size:15px;font-weight:600;margin:0 0 6px}
dl{margin:0;display:grid;grid-template-columns:1fr auto;row-gap:5px;font-size:14px}
dt{color:var(--muted)} dd{margin:0;text-align:right;font-weight:500}
.err{font-size:12px;color:var(--ng);word-break:break-all}
</style></head><body>
<div class="wrap">
<header><h1>C1 게이트웨이</h1><span class="mode" id="mode">연결 중</span></header>
<main>
  <div class="stage"><img id="live" alt="최근 수신 프레임">
    <div class="empty" id="empty"><strong>아직 들어온 영상이 없습니다</strong>오른쪽 QR을 폰으로 찍고 촬영 시작을 누르세요.</div></div>
  <p class="note">C1이 받은 원본 프레임 미리보기입니다. 검출 결과는 C4 대시보드에서 확인하세요.</p>
</main>
<aside>
  <div class="qr"><img src="/qr.png" alt="카메라 페이지 QR 코드"><p id="url"></p>
    <div class="warn" id="warn">이 주소는 HTTP라서 폰에서 카메라가 열리지 않습니다. HTTPS 터널 주소를 C1_PUBLIC_URL로 지정하세요.</div></div>
  <section><h2>수신과 전달</h2><dl>
    <dt>스트림 세션</dt><dd id="s-ses">0</dd>
    <dt>입력</dt><dd id="s-in">0 fps</dd>
    <dt>C2로 전달</dt><dd id="s-fw">0 fps</dd>
    <dt>C1에서 버린 프레임</dt><dd id="s-drop">0%</dd>
    <dt>C1 대기 p50 / p95</dt><dd id="s-wait">–</dd>
    <dt>C2 응답 p50 / p95</dt><dd id="s-rtt">–</dd>
    <dt>전달 중</dt><dd id="s-inf">0</dd>
    <dt>클라이언트 시계 차이 p50</dt><dd id="s-skew">–</dd>
  </dl></section>
  <div class="err" id="err"></div>
</aside>
</div>
<script>
const $=id=>document.getElementById(id), ms=p=>p&&p.p50!=null?p.p50+' / '+p.p95+' ms':'–';
fetch('/api/info').then(r=>r.json()).then(i=>{
  $('url').textContent=i.cam_url;
  let m=i.mode==='echo'?'<b>에코 모드</b> C2로 보내지 않고 수신만 합니다':'<b>전달 중</b> '+i.downstream;
  if(i.dashboard_url) m+=' <a href="'+i.dashboard_url+'">C4 대시보드 열기</a>';
  $('mode').innerHTML=m;
  const u=new URL(i.cam_url);
  if(u.protocol==='http:'&&!['localhost','127.0.0.1'].includes(u.hostname))$('warn').style.display='block';
});
let prev=null;
function connect(){
  const ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws/status');
  ws.binaryType='blob';
  ws.onmessage=ev=>{
    if(typeof ev.data==='string'){ const s=JSON.parse(ev.data); if(s.type==='stats') stats(s); return; }
    const url=URL.createObjectURL(ev.data), img=$('live');
    img.onload=()=>{ if(prev)URL.revokeObjectURL(prev); prev=url; };
    img.src=url; img.style.display='block'; $('empty').style.display='none';
  };
  ws.onclose=()=>setTimeout(connect,1000);
}
function stats(s){
  $('s-ses').textContent=s.sessions; $('s-in').textContent=s.input_fps+' fps'; $('s-fw').textContent=s.forwarded_fps+' fps';
  $('s-drop').textContent=(s.drop_rate_c1*100).toFixed(1)+'%'; $('s-wait').textContent=ms(s.c1_queue_wait_ms);
  $('s-rtt').textContent=ms(s.c2_rtt_ms); $('s-inf').textContent=s.inflight+' / '+s.max_inflight;
  $('s-skew').textContent=s.client_clock_gap_ms.p50==null?'–':s.client_clock_gap_ms.p50+' ms';
  $('err').textContent=s.last_error?'최근 오류: '+s.last_error:'';
}
connect();
</script></body></html>"""

CAM_HTML = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>검사 카메라</title>
<style>
:root{--belt:#1d2326;--paper:#e8e4da;--muted:#9aa3a6;--dough:#c98a3c;--ng:#e0644d;--ok:#5fae82}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--belt);color:var(--paper);font:16px/1.45 system-ui,"Apple SD Gothic Neo",sans-serif;font-variant-numeric:tabular-nums}
body{display:flex;flex-direction:column;padding:env(safe-area-inset-top) 0 env(safe-area-inset-bottom)}
video{flex:1;min-height:0;width:100%;object-fit:contain;background:#111}
.bar{padding:14px 16px 18px;display:flex;flex-direction:column;gap:12px}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
button{flex:1;font:inherit;font-weight:600;padding:14px;border:0;border-radius:6px;background:var(--dough);color:#1d2326}
button.stop{background:#3a4448;color:var(--paper)}
button:focus-visible,select:focus-visible{outline:2px solid var(--paper);outline-offset:2px}
select{font:inherit;font-size:14px;padding:6px 8px;border-radius:4px;background:#2b3439;color:var(--paper);border:1px solid #3a4448}
label{font-size:14px;color:var(--muted);display:flex;gap:6px;align-items:center}
.st{font-size:14px;color:var(--muted)} .st b{color:var(--paper);font-weight:500}
.on{color:var(--ok)} .off{color:var(--ng)} .msg{font-size:14px;color:var(--ng)}
</style></head><body>
<video id="v" playsinline muted></video>
<div class="bar">
  <div class="row">
    <label>FPS <select id="fps"><option>5</option><option>10</option><option>15</option><option selected>30</option></select></label>
    <label>해상도 <select id="w"><option>480</option><option selected>640</option><option>960</option><option>1280</option></select></label>
    <label>화질 <select id="q"><option value="0.5">낮음</option><option value="0.7" selected>보통</option><option value="0.85">높음</option></select></label>
  </div>
  <div class="row"><button id="go">촬영 시작</button></div>
  <div class="st"><span id="conn" class="off">연결 안 됨</span>, 보낸 프레임 <b id="sent">0</b>, 건너뛴 프레임 <b id="skip">0</b></div>
  <div class="msg" id="msg"></div>
</div>
<script>
const $=id=>document.getElementById(id), P=new URLSearchParams(location.search);
['fps','w','q'].forEach(k=>{ if(P.get(k)) $(k).value=P.get(k); });
const token=P.get('t'), sid=P.get('s')||('m-'+Math.random().toString(36).slice(2,8));
const v=$('v'), cv=document.createElement('canvas'), ctx=cv.getContext('2d');
let ws=null, stream=null, timer=null, busy=false, sent=0, skip=0, running=false, lock=null, offset=0, bestRtt=1e9, pinger=null;

if(!window.isSecureContext||!navigator.mediaDevices)
  $('msg').textContent='카메라는 HTTPS 주소에서만 열립니다. 게이트웨이를 HTTPS 터널로 띄운 뒤 QR을 다시 찍으세요.';

function connect(){
  const q='?s='+encodeURIComponent(sid)+(token?'&t='+encodeURIComponent(token):'');
  ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws/stream'+q);
  ws.binaryType='arraybuffer';
  ws.onopen=()=>{ $('conn').textContent='게이트웨이 연결됨'; $('conn').className='on'; bestRtt=1e9;
    clearInterval(pinger); ping(); pinger=setInterval(ping,5000); };
  ws.onmessage=e=>{ const m=JSON.parse(e.data); if(m.type!=='pong') return;
    const t1=Date.now(), rtt=t1-m.t0; if(rtt<bestRtt){ bestRtt=rtt; offset=m.server_ms-(m.t0+t1)/2; } };
  ws.onclose=e=>{ clearInterval(pinger); $('conn').className='off';
    $('conn').textContent=e.code===4401?'접속 토큰이 맞지 않습니다':'연결 끊김, 다시 연결하는 중';
    if(running&&e.code!==4401) setTimeout(connect,1000); };
}
function ping(){ if(ws&&ws.readyState===1) ws.send(JSON.stringify({type:'ping',t0:Date.now()})); }
function tick(){
  if(!ws||ws.readyState!==1||busy||ws.bufferedAmount>0||!v.videoWidth){ skip++; $('skip').textContent=skip; return; }
  busy=true;
  const captured=Math.round(Date.now()+offset);          // 서버 시계 기준 촬영 시각
  const W=+$('w').value, H=Math.round(v.videoHeight*W/v.videoWidth);
  if(cv.width!==W||cv.height!==H){ cv.width=W; cv.height=H; }
  ctx.drawImage(v,0,0,W,H);
  cv.toBlob(async b=>{
    if(b&&ws.readyState===1){
      const head=new ArrayBuffer(8); new DataView(head).setBigUint64(0,BigInt(captured));
      ws.send(new Blob([head,b])); sent++; $('sent').textContent=sent;
    }
    busy=false;
  },'image/jpeg',+$('q').value);
}
function schedule(){ clearInterval(timer); if(running) timer=setInterval(tick,1000/+$('fps').value); }
$('fps').onchange=schedule;
$('go').onclick=async()=>{
  if(running){
    running=false; clearInterval(timer); clearInterval(pinger); ws&&ws.close();
    stream&&stream.getTracks().forEach(t=>t.stop()); lock&&lock.release();
    $('go').textContent='촬영 시작'; $('go').className=''; return;
  }
  try{ stream=await navigator.mediaDevices.getUserMedia({audio:false,video:{facingMode:{ideal:'environment'},width:{ideal:1280},height:{ideal:720}}}); }
  catch(e){ $('msg').textContent='카메라를 열지 못했습니다: '+e.message; return; }
  $('msg').textContent=''; v.srcObject=stream; await v.play();
  try{ lock=await navigator.wakeLock.request('screen'); }catch(e){}
  running=true; connect(); schedule(); $('go').textContent='촬영 멈추기'; $('go').className='stop';
};
</script></body></html>"""


# ════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="C1 Gateway (serve / stub / loadgen)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="게이트웨이 실행")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=int(env("C1_PORT", 8000)))
    s.add_argument("--downstream", default=env("C1_DOWNSTREAM", ""),
                   help="C2 엔드포인트 (compose: http://c2-preprocessing:8001/preprocess). 비우면 에코 모드")
    s.add_argument("--public-url", default=env("C1_PUBLIC_URL", ""), help="QR에 넣을 외부 HTTPS 주소")
    s.add_argument("--max-inflight", type=int, default=int(env("C1_MAX_INFLIGHT", 2)),
                   help="C2로 동시에 나가 있을 요청 수")
    s.add_argument("--timeout", type=float, default=float(env("C1_TIMEOUT", 5.0)), help="C2 응답 대기(초)")
    s.add_argument("--token", default=env("C1_TOKEN", ""), help="스트림 접속 토큰, 'auto'면 자동 생성")
    s.add_argument("--dashboard-url", default=env("C1_DASHBOARD_URL", ""), help="상태 페이지에 걸 C4 주소")
    s.add_argument("--event-log", default=env("C1_EVENT_LOG", ""), help="프레임 이벤트 CSV 경로")
    s.add_argument("--schema-version", default=env("SCHEMA_VERSION", "1.0.0"))
    s.add_argument("--ssl-certfile", default=None)
    s.add_argument("--ssl-keyfile", default=None)
    s.add_argument("--log-level", default=env("LOG_LEVEL", "warning"))
    s.set_defaults(fn=cmd_serve)

    t = sub.add_parser("stub", help="가짜 C2 (/preprocess)")
    t.add_argument("--host", default="0.0.0.0")
    t.add_argument("--port", type=int, default=8001)
    t.add_argument("--delay-ms", type=float, default=60)
    t.add_argument("--jitter-ms", type=float, default=10)
    t.add_argument("--workers", type=int, default=1, help="동시 처리 수")
    t.add_argument("--c4", default="", help="가짜 detection-result를 보낼 C4 주소 (예: http://localhost:8003)")
    t.add_argument("--save-dir", default="", help="받은 이미지를 JPEG로 저장할 폴더 (눈으로 확인용)")
    t.add_argument("--save-every", type=int, default=10, help="N번째 프레임마다 한 장 저장")
    t.set_defaults(fn=cmd_stub)

    g = sub.add_parser("loadgen", help="부하 생성 / 벤치마크")
    g.add_argument("--url", default="ws://localhost:8000/ws/stream")
    g.add_argument("--source", default="synthetic", help="'synthetic', 이미지 폴더, 영상 파일")
    g.add_argument("--fps", type=float, default=30)
    g.add_argument("--clients", type=int, default=1)
    g.add_argument("--duration", type=float, default=20)
    g.add_argument("--width", type=int, default=640)
    g.add_argument("--quality", type=int, default=70)
    g.add_argument("--max-frames", type=int, default=300)
    g.add_argument("--token", default="")
    g.add_argument("--reset", action="store_true", help="시작 전 C1 지표 초기화")
    g.add_argument("--settle", type=float, default=2.0)
    g.add_argument("--out", default="", help="요약을 JSON Lines로 덧붙일 파일")
    g.add_argument("--label", default="")
    g.set_defaults(fn=cmd_loadgen)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
