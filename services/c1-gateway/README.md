# C1 Gateway

**Owner:** Member A

스마트폰(브라우저) 또는 Simulator의 프레임을 WebSocket으로 받아 `frame-envelope`로 감싸 C2 Preprocessing으로 전달한다. 코드는 `src/c1_gateway.py` 파일 하나이며 서브커맨드 세 개로 나뉜다. `serve`는 게이트웨이 본체, `stub`은 C2가 없을 때 쓰는 가짜 C2, `loadgen`은 재현 가능한 부하를 만드는 송출기다.

## 실행

```bash
pip install -r requirements.txt
python src/c1_gateway.py serve                         # C1_DOWNSTREAM이 비어 있으면 에코 모드(수신만)
python src/c1_gateway.py stub --port 8001              # 다른 터미널: 계약 검증용 가짜 C2
python src/c1_gateway.py serve --downstream http://localhost:8001/preprocess
python src/c1_gateway.py loadgen --fps 30 --duration 20 --reset
```

Compose에서는 `docker compose up -d c1-gateway`로 뜨고, 기본 downstream은 `http://c2-preprocessing:8001/preprocess`다. 상태 페이지는 `http://<host>:8000/`, 폰 카메라 페이지는 `/cam`이다.

폰 브라우저의 카메라는 HTTPS에서만 열린다. `cloudflared tunnel --url http://localhost:8000`처럼 터널을 띄우고 그 주소를 `.env`의 `C1_PUBLIC_URL`에 넣으면 상태 페이지의 QR이 그 주소를 가리킨다. 컨테이너 안에서는 LAN IP를 알 수 없으므로 Docker로 띄울 때는 이 값이 사실상 필수다.

## 인터페이스

**Client → C1** `WS /ws/stream?s=<session_id>&t=<token>`. 바이너리 메시지 하나가 프레임 하나이며, 앞 8바이트는 big-endian uint64 `captured_at_ms`, 나머지는 JPEG이다. 8바이트 없이 JPEG만 보내도 되고 그때 `captured_at_ms`는 C1 수신 시각으로 채운다. 텍스트 메시지 `{"type":"ping","t0":<client_ms>}`를 보내면 `{"type":"pong","t0":..,"server_ms":..}`로 답하므로, 클라이언트는 이걸로 시계 오프셋을 보정해 `captured_at_ms`를 서버 시계 기준으로 보낼 수 있다(`/cam` 페이지는 5초마다 보정한다). 접속 직후 C1은 `{"type":"hello","session_id":..}`를 보낸다.

**C1 → C2** `POST http://c2-preprocessing:8001/preprocess`, 본문은 [`frame-envelope`](../../shared/schemas/frame-envelope.schema.json) JSON이고 이미지는 `image_b64`(JPEG)로 싣는다. 헤더 `X-Trace-Id`, `X-Frame-Id`, `X-Session-Id`를 함께 보낸다. C2가 2xx로 응답하면 C1은 그 프레임을 전달 완료로 본다.

**ID 규칙** `trace_id`는 프레임마다 UUID v4(hex), `session_id`는 WebSocket 연결 단위, `frame_id`는 세션 안에서 0부터 단조 증가하는 정수다.

**기타** `GET /healthz`, `GET /api/metrics`(입력/전달 FPS, C1 drop, C1 대기·C2 응답 p50/p95), `POST /api/metrics/reset`, 상태 페이지 `GET /`, 카메라 페이지 `GET /cam`.

## Backpressure 정책

C1은 세션마다 아직 보내지 않은 최신 프레임 1장만 보관한다. 새 프레임이 오면 보관 중이던 프레임은 버리고(`dropped`) 새 것으로 바꾼다. C2로 동시에 나가 있는 요청은 `C1_MAX_INFLIGHT`개로 제한하고, `C1_TIMEOUT` 안에 응답이 없으면 실패로 기록하고 다음 프레임으로 넘어간다. 뒤쪽이 느려져도 C1에서 오래된 프레임이 쌓이지 않으므로 지연이 누적되지 않는다.

## 측정

`C1_EVENT_LOG`(compose 기본 `/app/results/c1_events.csv` → host `benchmark/results/`)에 프레임마다 `forwarded / dropped / error / timeout` 이벤트가 `trace_id`, `frame_id`와 함께 기록된다. C4 결과와 `trace_id`로 조인하면 Frame Drop Rate와 구간별 latency를 계산할 수 있다. C1 latency는 계약대로 C2의 `preprocess_started_at_ms - gateway_received_at_ms`로 계산한다.

## 남은 일

- Overlay용 원본 프레임을 C4로 미러링할지 결정 (C4 open issue)
- C2 실제 엔드포인트와 응답 형식 확정 후 통합 테스트
