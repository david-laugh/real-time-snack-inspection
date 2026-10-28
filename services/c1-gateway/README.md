# C1 Gateway

**Owner:** Member A

스마트폰(브라우저) 또는 Simulator의 프레임을 WebSocket으로 받아 `frame-envelope`로 감싸 C2 Preprocessing으로 전달한다. C1 → C2 전송은 **MQTT**가 기본이고(HTTP는 예비), 나머지 구간은 HTTP다. 코드는 `src/c1_gateway.py` 파일 하나이며 서브커맨드 세 개로 나뉜다. `serve`는 게이트웨이 본체, `stub`은 C2가 없을 때 쓰는 가짜 C2(MQTT/HTTP), `loadgen`은 재현 가능한 부하를 만드는 송출기다.

## 실행

로컬에서 MQTT로 돌리려면 브로커가 먼저 떠 있어야 한다. 레포 루트에서 `docker compose up -d mqtt-broker`로 브로커만 띄울 수 있다. 브로커는 호스트의 `127.0.0.1:11883`에만 열리고(`MQTT_HOST_PORT`), 컨테이너 밖에서 실행하는 C1과 stub은 기본값 `localhost:11883`으로 접속한다. 호스트의 `localhost:1883`은 다른 프로젝트 브로커일 수 있어 C1과 stub이 접속을 거부한다.

```bash
pip install -r requirements.txt
python src/c1_gateway.py stub --save-dir check_frames        # 가짜 C2: MQTT 구독, 세션별 최신 1장 처리
python src/c1_gateway.py serve --transport mqtt               # C1: localhost:1883으로 발행
python src/c1_gateway.py loadgen --fps 30 --duration 20 --reset
```

`serve`만 실행하면(전송 방식 지정 없음) 에코 모드로 수신만 한다. HTTP로 돌리려면 `stub --transport http --port 8001`과 `serve --downstream http://localhost:8001/preprocess`를 쓴다.

Compose에서는 `C1_TRANSPORT=mqtt`, `MQTT_HOST=mqtt-broker`로 뜬다. 상태 페이지는 `http://<host>:8000/`, 폰 카메라 페이지는 `/cam`이다. 상태 페이지 상단에 MQTT 연결 여부가 표시된다.

폰 브라우저의 카메라는 HTTPS에서만 열린다. `cloudflared tunnel --url http://localhost:8000`처럼 터널을 띄우고 그 주소를 `.env`의 `C1_PUBLIC_URL`에 넣으면 상태 페이지의 QR이 그 주소를 가리킨다. 컨테이너 안에서는 LAN IP를 알 수 없으므로 Docker로 띄울 때는 이 값이 사실상 필수다.

## 인터페이스

**Client → C1** `WS /ws/stream?s=<session_id>&t=<token>`. 바이너리 메시지 하나가 프레임 하나이며, 앞 8바이트는 big-endian uint64 `captured_at_ms`, 나머지는 JPEG이다. 8바이트 없이 JPEG만 보내도 되고 그때 `captured_at_ms`는 C1 수신 시각으로 채운다. 텍스트 메시지 `{"type":"ping","t0":<client_ms>}`를 보내면 `{"type":"pong","t0":..,"server_ms":..}`로 답하므로, 클라이언트는 이걸로 시계 오프셋을 보정해 `captured_at_ms`를 서버 시계 기준으로 보낼 수 있다(`/cam` 페이지는 5초마다 보정한다). 접속 직후 C1은 `{"type":"hello","session_id":..}`를 보낸다.

**C1 → C2 (MQTT, 기본)** C1은 `{MQTT_TOPIC_PREFIX}/frames/{session_id}`(기본 `psf/v1/frames/...`)에 [`frame-envelope`](../../shared/schemas/frame-envelope.schema.json) JSON을 QoS 0, retain 없이 발행한다. 이미지는 `image_b64`(JPEG)이고 `source_width`/`source_height`는 그 JPEG의 실제 크기다. C2는 `psf/v1/frames/+`를 구독한다. C1은 받은 프레임을 즉시 발행하고 밀린 프레임 드롭은 C2가 담당한다. 자세한 규칙은 [`docs/api-contract.md`](../../docs/api-contract.md)의 1a절에 있다.

실험용으로 `C1_MQTT_ACK=1`(`--mqtt-ack`)을 켜면, `{prefix}/acks`에 `{trace_id, session_id, frame_id, status}`가 올 때 C1이 아래 Backpressure 정책으로 흐름을 제어한다. 계약에 없는 실험 기능이며 현재 C2는 ack를 보내지 않으므로 기본값 0으로 둔다(`stub --ack`로만 흉내 낼 수 있다).

**C1 → C2 (HTTP, 예비)** `C1_TRANSPORT=http`이면 `POST {C1_DOWNSTREAM}`으로 같은 JSON을 보내고 헤더 `X-Trace-Id`, `X-Frame-Id`, `X-Session-Id`를 붙인다. 2xx 응답이 ack 역할을 한다.

**ID 규칙** `trace_id`는 프레임마다 UUID v4(hex), `session_id`는 WebSocket 연결 단위, `frame_id`는 세션 안에서 0부터 단조 증가하는 정수다.

**기타** `GET /healthz`(전송 방식, MQTT 연결 여부), `GET /api/metrics`(입력/전달 FPS, C1 drop, C1 대기·C2 ack p50/p95, MQTT 상태), `POST /api/metrics/reset`, 상태 페이지 `GET /`, 카메라 페이지 `GET /cam`.

## Backpressure 정책

MQTT 기본 모드에서는 C1이 드롭하지 않고 C2가 세션별 최신 1장 정책으로 드롭한다. 아래는 HTTP 모드와 MQTT ack 실험 모드에서의 C1 정책이다.

C1은 세션마다 아직 보내지 않은 최신 프레임 1장만 보관한다. 새 프레임이 오면 보관 중이던 프레임은 버리고(`dropped`) 새 것으로 바꾼다. ack를 기다리는 프레임은 `C1_MAX_INFLIGHT`개로 제한하고, `C1_TIMEOUT` 안에 ack가 없으면 timeout으로 기록하고 다음 프레임으로 넘어간다. C2가 C3 결과까지 받은 뒤 ack하므로 이 값이 파이프라인 전체의 동시 처리 상한이며, Scale-out 실험에서는 C3 복제 수 + 1로 맞춘다. 뒤쪽이 느려져도 C1에서 오래된 프레임이 쌓이지 않으므로 지연이 누적되지 않는다.

## 측정

`C1_EVENT_LOG`(compose 기본 `/app/results/c1_events.csv` → host `benchmark/results/`)에 프레임마다 이벤트(MQTT 기본 모드는 `published / error`, 그 외 `forwarded / dropped / error / timeout`)가 `trace_id`, `frame_id`와 함께 기록된다. C4 결과와 `trace_id`로 조인하면 Frame Drop Rate와 구간별 latency를 계산할 수 있다. C1 latency는 계약대로 C2의 `preprocess_started_at_ms - gateway_received_at_ms`로 계산한다.

## 남은 일

- C2의 MQTT 구독 구현 후 통합 테스트 (C1 `published` ↔ C2 `forwarded/dropped`를 `trace_id`로 조인)
- Overlay용 원본 프레임: C4가 `psf/v1/frames/+`를 구독하면 C1 수정 없이 받을 수 있음 (C4와 논의)
