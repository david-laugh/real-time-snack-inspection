# C2 Preprocessing

**Owner:** Member B

## 역할
- C1이 MQTT로 발행한 프레임(frame-envelope)을 구독해 수신
- OpenCV 기반 resize(긴 변 기준) + 컨베이어 영역(ROI) 판정
- 과자 후보 bbox 생성 → SAM2 prompt(`prompts[]`)로 변환
- `processed-frame` 메시지를 Nginx Inference LB(`/infer`)로 전달
- 같은 프레임의 리사이즈 이미지 + prompts를 C4로 전달 (C4가 C3 결과를 그 위에 그림)

## Interface
| 방향 | 프로토콜 / Endpoint | Payload |
|---|---|---|
| C1 → C2 | **MQTT** 구독 `{MQTT_TOPIC_PREFIX}/frames/+` = `psf/v1/frames/+` (QoS 0, 브로커 `mqtt-broker:1883`). C1은 `{prefix}/frames/{session_id}`로 발행 | [`frame-envelope`](../../shared/schemas/frame-envelope.schema.json) JSON (`image_b64` JPEG) |
| C2 → Nginx | HTTP `POST http://nginx-inference-lb:8080/infer` | [`processed-frame`](../../shared/schemas/processed-frame.schema.json) JSON |
| C2 → C4 | HTTP `POST http://c4-visualizer:8003/api/camera/frame` (C3 전송과 동시, best-effort) | `{trace_id, session_id, frame_id, gateway_received_at_ms, preprocess_started_at_ms, preprocess_finished_at_ms, width, height, prompts, image_b64}` (10개 필드) |
| (디버깅) → C2 | HTTP `POST /preprocess` | frame-envelope JSON. MQTT와 같은 처리 함수를 쓰며 `/infer` 결과까지 기다린다 |
| — | `GET /healthz` | `{"status": "ok" \| "degraded", "mqtt": {"connected": …}}` |
| — | `GET /stats` | 이벤트 누적 수, MQTT 수신 수, 처리 중(`inflight`)·대기(`pending_sessions`) 수, `c4_sent` / `c4_skipped` / `c4_error` |

- MQTT에는 HTTP 헤더가 없으므로 `trace_id` / `frame_id` / `session_id`는 본문에서 읽는다. `/infer`로 보낼 때는
  `X-Trace-Id`, `X-Frame-Id`, `X-Session-Id` 헤더로 전파한다.
- 토픽의 마지막 레벨(`+`)은 C1과 합의 중이며, C2는 세션 구분에 토픽이 아니라 본문의 `session_id`를 쓴다.
- `/healthz`는 MQTT가 끊겨도 HTTP 200을 반환한다(`status: degraded`). 컨테이너 재시작 대신 자동 재접속(1~10초 간격)에 맡긴다.

`POST /preprocess` 응답
| 코드 | 의미 |
|---|---|
| 200 | 전처리 후 `/infer` 전달 성공. `{trace_id, frame_id, num_prompts, preprocess_ms, infer_status}` |
| 422 | frame-envelope 스키마 위반, `image_b64` 누락·디코딩 실패, 미지원 `format`(`raw_*`) |
| 502 | `/infer` 타임아웃, 연결 실패, 4xx/5xx |

### MQTT 수신과 속도 조절
```
paho 네트워크 스레드: 수신 → JSON 파싱만 → 이벤트 루프로 전달
이벤트 루프: FrameSlots (세션별 최신 1장만 보관)
            ├ 같은 세션의 새 프레임이 오면 보관 중이던 것은 버림 → dropped_stale
            └ captured_at_ms 기준 C2_MAX_FRAME_AGE_MS(500) 초과 → dropped_old (보관할 때, 꺼낼 때 모두 확인)
워커 C2_MAX_INFLIGHT(4)개: 꺼내기 → 전처리(스레드풀) → /infer 전달 → forwarded / error / timeout
```
- 들어오는 속도가 처리 속도보다 빠르면 큐가 쌓이지 않고 오래된 프레임부터 버려진다. 따라서 지연이 누적되지 않는다.
  (30fps 발행 + `/infer` 200ms 지연 테스트: 90프레임 중 약 58개 전달, 32개 dropped_stale, 도착 시점 나이 최대 72ms)
- `C2_MAX_INFLIGHT`는 C3 복제본 수 이상으로 둔다. 이 값이 C3로 동시에 들어가는 요청 수의 상한이다.
- `captured_at_ms`는 C1이 서버 시계 기준으로 보정해 보낸다(`/cam`의 시계 차이 측정). 보정되지 않은 클라이언트 시계를 쓰면
  나이 판정이 틀어지므로, 그런 경우에는 `C2_MAX_FRAME_AGE_MS=0`으로 끈다.

### C4 프레임 전송 (`C2_C4_FRAME_URL`)
C4와 합의한 인터페이스다. C4는 `trace_id`로 이 이미지와 C3 결과(bbox, mask)를 짝지어 그린다. C3 결과는 C2가 리사이즈한
이미지 기준 좌표이므로 C4에서 좌표를 변환할 필요가 없다.
```
POST http://c4-visualizer:8003/api/camera/frame
Content-Type: application/json
X-Trace-Id / X-Frame-Id / X-Session-Id
{"trace_id": "…", "session_id": "…", "frame_id": 297,
 "gateway_received_at_ms": 1793157016122, "preprocess_started_at_ms": 1793157016131,
 "preprocess_finished_at_ms": 1793157016145, "width": 576, "height": 1024,
 "prompts": [{"candidate_id": "c0", "type": "box", "bbox": [429.0, 591.0, 525.0, 681.0]}, …],
 "image_b64": "/9j/4AAQ…"}
```
- 본문은 정확히 이 **10개 필드**다.
  - `gateway_received_at_ms`: 입력 frame-envelope의 값(C1 수신 시각, C1 시계)을 그대로 전달
  - `preprocess_started_at_ms` / `preprocess_finished_at_ms`, `width` / `height` / `prompts` / `image_b64`: C3로 보내는
    processed-frame과 같은 값. 이미지는 **같은 JPEG 문자열을 재사용**한다(다시 인코딩하지 않는다)
  - C4는 이 시각들로 C1·C2 구간 지연을 C3 결과 없이도 계산할 수 있다. `gateway_received_at_ms`는 C1 시계,
    `preprocess_*`는 C2 시계라서 서로 다른 호스트에서 돌면 시계 차이만큼 오차가 생긴다
- C3로 보내는 프레임만 보낸다. `dropped_stale` / `dropped_old`, 입력 오류 프레임은 보내지 않는다. `prompts`가 0개여도 보낸다(영상이 멈춰 보이지 않게).
- C3 전송과 **동시에** 보내고 응답을 기다리지 않는다. 그래서 대개 이미지가 C3 결과보다 먼저 C4에 도착한다(순서 보장은 아님).
- 메인 경로(C3 전송)와 분리돼 있다. 별도 HTTP 클라이언트를 쓰고, 동시 전송 한도 `C2_C4_MAX_INFLIGHT`(2)가 차 있으면 그 프레임은
  보내지 않고 `c4_skipped`로 센다. 타임아웃은 `C2_C4_TIMEOUT_S`(1.0초)이고 재시도하지 않는다.
  C4가 2초씩 지연되거나 꺼져 있어도 C3 전달 처리량·지연이 C4를 끈 경우와 같은 것을 테스트로 확인했다.
- `/stats`의 `c4_sent` / `c4_skipped` / `c4_error`로 확인한다. 이벤트 CSV에는 실패(`c4_error`)만 남긴다.
- `C2_C4_FRAME_URL`을 빈 값으로 두면 기능이 꺼진다.

### 이벤트 로그 (`C2_EVENT_LOG`)
프레임마다 한 줄씩 CSV로 남긴다. 컬럼은 C1의 `c1_events.csv`와 맞췄다:
`trace_id, session_id, frame_id, captured_at_ms, gateway_received_at_ms, source(mqtt|http), event, event_at_ms, preprocess_ms, infer_rtt_ms, detail`

| event | 의미 |
|---|---|
| `forwarded` | `/infer` 전달 성공 |
| `dropped_stale` | 처리 전에 같은 세션의 새 프레임이 와서 버림 |
| `dropped_old` | `captured_at_ms` 기준 `C2_MAX_FRAME_AGE_MS` 초과 |
| `error` | 잘못된 메시지(JSON·스키마·디코딩) 또는 `/infer` 실패(연결 실패, 4xx/5xx) |
| `timeout` | `/infer` 타임아웃(`C2_INFER_TIMEOUT_S`) |
| `c4_error` | C4 프레임 전송 실패. CSV에만 기록하고, 누적 수는 `/stats`의 `c4_error`에 있다(프레임 이벤트 집계와 분리) |

## 처리 흐름
1. 입력을 frame-envelope 스키마로 검증
2. `image_b64`(base64 JPEG, png도 허용) 디코딩. 전송 방식은 C1 PR #1에서 확정됐다. 관련 코드는 `src/c2/codec.py`에만 있다
3. 긴 변이 `C2_RESIZE_LONG_SIDE`가 되도록 축소 (비율 유지, 확대하지 않음)
4. 후보 bbox 검출 — `src/c2/detect.py` (reference/c2_bbox_detect.py와 같은 로직)
   - HSV 임계값 → morphology open/close → 외곽 contour → bbox
   - 면적 필터(기대 쿠키 면적의 0.2~2.0배), 가로세로비 ≤ 2.5
   - ROI 경계(또는 화면 가장자리)에 닿은 쿠키는 잘린 것으로 보고 제외
   - ROI auto의 close 커널은 쿠키 지름에 비례시킨다(`max(3, round(지름 × 25 / 170))`). 해상도(리사이즈, 640×480 스트림 등)와 상관없이 같은 ROI를 얻기 위해서다
   - ROI는 **crop하지 않고 판정에만** 쓴다 (좌표 복원 필드가 생기기 전까지. [`PROPOSALS.md`](PROPOSALS.md) P1)
5. 쿠키 지름(ROI 계산보다 먼저 구함): 기본은 프레임마다 자동 추정(정상 쿠키 면적 중앙값). `session_id`별로 EMA(α=0.2) 평활화하고, 추정에 실패한 프레임은 이전 값을 쓴다
6. 리사이즈된 이미지를 다시 인코딩해 `image_b64`에 넣고, 스키마에 정의된 필드만으로 processed-frame을 만든다. 출력도 스키마로 검증한 뒤 `/infer`로 전달한다

`prompts[].bbox`는 **전처리(리사이즈) 후 이미지 기준 `[x1, y1, x2, y2]` pixel**이다.

**동시 처리:** 요청 본문 수신과 `/infer` 전달(I/O)만 이벤트 루프에서 한다. JSON 파싱부터 직렬화까지의 CPU 작업은
`process_request()` 하나로 묶어 스레드풀에서 실행한다. OpenCV는 GIL을 풀고 실행되므로 동시 요청이 병렬로 처리된다.
세션별 지름 EMA는 lock으로 보호한다. 이 상태는 프로세스 메모리에 있으므로 uvicorn worker를 늘리지 않는다(worker마다 상태가 갈라진다).
서버가 시작될 때(lifespan) 1080×1920 더미 프레임으로 파이프라인을 한 번 돌려 워밍업한다.

## 설정 (환경변수, `C2_` 접두어)
| 변수 | 기본값 | 설명 |
|---|---|---|
| `C2_RESIZE_LONG_SIDE` | `1024` | 긴 변 기준 리사이즈 크기(px). `0`이면 리사이즈 안 함 |
| `C2_EXPECTED_DIAMETER_PX` | (빈 값) | 쿠키 지름(px, **원본 좌표 기준**). 비우면 자동 추정. 값을 주면 고정값으로 쓴다 (리사이즈 배율은 자동 반영) |
| `C2_DIAMETER_EMA_ALPHA` | `0.2` | 자동 추정 지름의 세션별 EMA 계수 |
| `C2_DIAMETER_MAX_SESSIONS` | `256` | 지름을 기억할 최대 세션 수 (오래된 세션부터 제거) |
| `C2_HSV_LOW` / `C2_HSV_HIGH` | `5,80,50` / `30,255,255` | 쿠키 색 HSV 범위 (OpenCV: H 0~180) |
| `C2_ROI_MODE` | `auto` | `auto`: 컨베이어 영역 자동 검출 / `fixed`: `C2_ROI` 사용 / `off`: 화면 전체 |
| `C2_ROI` | (빈 값) | `fixed` 모드용 `x1,y1,x2,y2` (원본 좌표) |
| `C2_INFER_URL` | `http://nginx-inference-lb:8080/infer` | |
| `C2_INFER_TIMEOUT_S` | `3.0` | `/infer` 요청 타임아웃(초). C1의 `C1_TIMEOUT`(5s)보다 짧게 유지 |
| `C2_OUTPUT_IMAGE_FORMAT` | `jpeg` | C3로 보낼 이미지 포맷 (`jpeg` / `png`) |
| `C2_JPEG_QUALITY` | `90` | |
| `C2_VALIDATE_OUTPUT` | `true` | 출력 processed-frame 스키마 검증 |
| `C2_MQTT_ENABLED` | `true` | MQTT 수신 사용 여부 (`false`면 HTTP `/preprocess`만) |
| `C2_MQTT_HOST` / `C2_MQTT_PORT` | 공용 `MQTT_HOST` / `MQTT_PORT` → `mqtt-broker` / `1883` | 브로커 주소 |
| `C2_MQTT_TOPIC` | `{MQTT_TOPIC_PREFIX}/frames/+` → `psf/v1/frames/+` | 구독 토픽 |
| `C2_MQTT_QOS` | `0` | |
| `C2_MQTT_CLIENT_ID` | (빈 값) | 비우면 `c2-preprocessing-<hostname>` |
| `C2_MQTT_KEEPALIVE_S` | `30` | |
| `C2_MQTT_RECONNECT_MIN_S` / `_MAX_S` | `1` / `10` | 재접속 대기 시간 범위 |
| `C2_MAX_INFLIGHT` | `4` | 워커 수 = 동시 처리·전달 수. C3 복제본 수 이상 권장 |
| `C2_MAX_FRAME_AGE_MS` | `500` | 이보다 오래된 프레임은 버림(`captured_at_ms` 기준). `0`이면 끔 |
| `C2_EVENT_LOG` | (빈 값) | 프레임 이벤트 CSV 경로. 비우면 기록 안 함 |
| `C2_C4_FRAME_URL` | `http://c4-visualizer:8003/api/camera/frame` | C4 프레임 전송 주소. **빈 값이면 끔** |
| `C2_C4_MAX_INFLIGHT` | `2` | C4 동시 전송 한도. 차 있으면 그 프레임은 보내지 않음(`c4_skipped`) |
| `C2_C4_TIMEOUT_S` | `1.0` | C4 전송 타임아웃(초) |
| `LOG_LEVEL` | `INFO` | `DEBUG`이면 프레임별 지름·배율·ROI를 로그로 남긴다 |

**MQTT 설정 우선순위:** `C2_MQTT_*` 값 → 공용 `MQTT_HOST` / `MQTT_PORT` / `MQTT_TOPIC_PREFIX`(C1과 공유, `.env`가 env_file로
모든 컨테이너에 주입) → 기본값 순으로 적용한다. 빈 값(`C2_MQTT_HOST=`)은 지정하지 않은 것으로 본다. 보통은 공용 변수만 두면 된다.

**쿠키 지름은 촬영 세팅마다 다르다.** 예를 들어 시뮬레이터 PNG(1045×450)에서는 약 **78px**,
폰 세로 촬영에서는 약 **170px**이다. 그래서 기본값은 자동 추정이다. 촬영 세팅이 고정되어 있고 쿠키 대부분이 불량인
상황처럼 중앙값 추정이 흔들리는 경우에만 `C2_EXPECTED_DIAMETER_PX`를 지정한다.

## 실행

### Docker
```bash
cd services/c2-preprocessing
docker build -t pocket-smart-factory/c2-preprocessing:dev .
docker run --rm -p 8001:8001 -e C2_MQTT_HOST=<broker> pocket-smart-factory/c2-preprocessing:dev
curl http://localhost:8001/healthz
```

### compose 설정 예시 (C1 MQTT PR #5 기준, compose 파일은 Member A가 수정)
브로커 서비스 `mqtt-broker`와 공용 변수 `MQTT_HOST` / `MQTT_PORT` / `MQTT_TOPIC_PREFIX`(`.env` → env_file)는 C1 PR #5에 있다.
C2는 공용 변수를 그대로 읽으므로 c2 블록에서는 **`command` 제거와 이벤트 로그 볼륨만** 필요하다.
```yaml
  c2-preprocessing:
    <<: *common-service
    build:
      context: ./services/c2-preprocessing
    image: pocket-smart-factory/c2-preprocessing:dev
    environment:
      SERVICE_NAME: c2-preprocessing
      C2_EVENT_LOG: /app/results/c2_events.csv          # 추가
    expose:
      - "${C2_PORT:-8001}"
    volumes:                                              # 추가
      - ./benchmark/results:/app/results
    depends_on:
      - mqtt-broker                                       # 선택 (C2는 브로커가 늦게 떠도 재접속한다)
      - nginx-inference-lb
    # command: ["sleep", "infinity"]                      # 삭제 → 이미지 CMD(uvicorn :8001)
```
`C2_MAX_INFLIGHT`(기본 4)는 C3 복제본 수에 맞춰 `.env`에서 조정한다.

> **호스트 1883 충돌 주의:** C1 PR의 `mqtt-broker`는 호스트 포트 `${MQTT_HOST_PORT:-1883}`로 노출된다. 같은 PC에서 다른 MQTT 브로커
> (예: 다른 프로젝트의 mosquitto)가 1883을 쓰고 있으면 기동에 실패한다. 그리고 컨테이너 밖에서 실행한 도구가 `localhost:1883`에
> 붙으면 **그 다른 브로커에 발행**하게 된다. 이런 PC에서는 `.env`에 `MQTT_HOST_PORT=11883`을 두고, 로컬 도구도 11883을 쓴다.

### 로컬 통합 테스트: 발행 → MQTT → C2 → 가짜 `/infer`
- `dev/fake_infer.py`: Nginx/C3 대역. processed-frame을 스키마로 검증하고 로그만 남긴다. 통과하면 200, 위반이면 422로 응답한다.
  `--delay-ms`로 C3 추론 시간을 흉내 낼 수 있고, `GET /stats`는 받은 수와 도착 시점 프레임 나이(`now - captured_at_ms`)를 보여준다.
- `dev/fake_c4.py`: C4 대역(`POST /api/camera/frame`, 기본 포트 8003). 합의된 10개 필드인지, 시각 필드가 정수이고 `preprocess_started ≤ preprocess_finished`인지, JPEG 크기가 width/height와 같은지 검증한다(`--delay-ms` 지원).
  C2에 `C2_C4_FRAME_URL=http://localhost:8003/api/camera/frame`을 주면 C4 전송까지 확인할 수 있다.
- `dev/publish_frames.py`: C1 대역 발행기. 기본 `localhost:11883`이며, **호스트의 `localhost:1883`에는 발행을 거부한다.**

compose override 예시 [`dev/docker-compose.override.yml`](dev/docker-compose.override.yml) (repo root에서).
브로커는 base의 `mqtt-broker`와 겹치지 않는 별도 서비스 `c2-dev-broker`이고, 호스트 `127.0.0.1:11883`에만 노출된다.
```bash
docker build -t pocket-smart-factory/c2-preprocessing:dev services/c2-preprocessing
docker compose -f docker-compose.yml -f services/c2-preprocessing/dev/docker-compose.override.yml \
  up -d --no-deps c2-dev-broker fake-infer fake-c4 c2-preprocessing
curl localhost:8001/healthz                                            # mqtt.connected: true
python services/c2-preprocessing/dev/publish_frames.py --fps 30 --seconds 5
curl localhost:8001/stats
docker compose -f docker-compose.yml -f services/c2-preprocessing/dev/docker-compose.override.yml logs -f fake-infer
```

로컬 프로세스로 띄울 때 (services/c2-preprocessing에서)
```bash
docker run -d --rm --name c2-dev-mosquitto -p 127.0.0.1:11883:1883 \
  -v "$PWD/dev/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro" eclipse-mosquitto:2.0.20
python dev/fake_infer.py --port 8080 --delay-ms 0
C2_MQTT_HOST=localhost C2_MQTT_PORT=11883 C2_INFER_URL=http://localhost:8080/infer \
  uvicorn c2.app:app --app-dir src --port 8001
python dev/publish_frames.py --fps 30 --seconds 5
```

### 테스트
MQTT 통합 테스트(`test_mqtt_integration.py`)는 **테스트가 직접 mosquitto 컨테이너를 띄운다**(`127.0.0.1`의 무작위 포트, 끝나면 삭제).
`C2_TEST_MQTT_HOST` / `C2_TEST_MQTT_PORT`로 브로커를 지정할 수도 있지만, 호스트의 `localhost:1883`은 거부한다.
docker가 없고 브로커도 지정하지 않으면 skip된다.
```bash
cd services/c2-preprocessing
pytest -rs        # 처리 시간·30fps 결과까지 보려면 pytest -s

# Python 3.11 컨테이너 안에서 (repo root에서 실행, 사이드카 브로커를 지정)
docker network create c2-test-net
docker run -d --rm --name mqtt-test-broker --network c2-test-net \
  -v "$PWD/services/c2-preprocessing/dev/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro" eclipse-mosquitto:2.0.20
docker run --rm --network c2-test-net -e C2_TEST_MQTT_HOST=mqtt-test-broker -e C2_TEST_MQTT_PORT=1883 \
  -v "$PWD:/repo:ro" -w /repo/services/c2-preprocessing pocket-smart-factory/c2-preprocessing:dev \
  sh -c "pip install -q pytest==8.3.4 && python -m pytest -p no:cacheprovider -rs"
docker rm -f mqtt-test-broker && docker network rm c2-test-net
```
| 테스트 | 내용 |
|---|---|
| `test_detect.py` | reference와 동일 결과, fixture 정확도(recall 1.0, 평균 IoU ≥ 0.95), 0.5배/1.8배/90도 회전 불변성, 1024 리사이즈 파이프라인, EMA |
| `test_preprocess_api.py` | `/infer` mock. 전송 메시지의 스키마 통과, 공통 필드·헤더 전파, 오류 코드 |
| `test_latency.py` | 프레임당 처리 시간 (목표 < 33 ms) |
| `test_phone_frames.py` | 폰 촬영 프레임 5장의 기대 개수와 위치 (원본, 1024 리사이즈, 576/720px 저해상도 입력) |
| `test_concurrency.py` | 처리 중에도 이벤트 루프 응답, 동시 요청 병렬 처리, EMA 스레드 안전성, 워밍업 |
| `test_frame_slots.py` | 세션별 최신 1장 보관, `dropped_stale` / `dropped_old`, 이벤트 CSV |
| `test_mqtt_integration.py` | 실제 mosquitto: 발행 → 수신 → 가짜 `/infer` 도착, 30fps + `/infer` 200ms에서 지연 누적 없음·drop 기록, 오래된/잘못된 메시지, 연결 끊김 후 재접속, 호스트 `localhost:1883` 거부, drop된 프레임은 C4로 가지 않음 |
| `test_settings_mqtt.py` | MQTT 설정 우선순위 (`C2_MQTT_*` → 공용 `MQTT_*` → 기본값, 빈 값 처리) |
| `test_c4_frames.py` | C4 본문이 합의된 10개 필드인지, JPEG 크기, C3 요청과 trace_id·preprocess 시각·prompts·이미지 일치, `gateway_received_at_ms`가 입력 envelope 값과 같은지, 가짜 C4 검증 동작, prompts 0개 전송, 입력 오류 프레임 미전송, C4 지연(2초)·다운에도 C3 처리량·지연 유지, C4 연결 실패 중 이벤트 루프 막힘 없음(sniffio 회귀), URL 빈 값이면 끔 |
| `test_fake_infer.py` | 가짜 `/infer` 검증 동작, C2 → 가짜 `/infer` 연결 |
| `test_schema_sync.py` | `schemas/` 사본이 `shared/schemas` 원본과 같은지 |

## 디렉터리
```
src/c2/
  app.py       FastAPI 앱, 공통 처리 handle_frame(), /infer 전달(forward_to_infer), MQTT 워커
  mqtt_ingest.py  MQTT 구독·재접속(paho-mqtt), 세션별 최신 1장 보관(FrameSlots)
  events.py    프레임 이벤트 CSV, 누적 카운터
  c4_frames.py C4로 리사이즈 프레임 전송 (동시 전송 한도, 메인 경로와 분리)
  config.py    환경변수 설정
  pipeline.py  resize → 검출 → processed-frame 생성, 세션별 지름 EMA
  detect.py    검출 로직 (reference에서 이식)
  codec.py     이미지 디코딩/인코딩 (전송 방식 변경 지점)
  schemas.py   jsonschema 검증
schemas/       shared/schemas 사본 (컨테이너용)
reference/     검증용 원본 스크립트 (이미지에 포함 안 함)
dev/           개발 도구: fake_infer.py (가짜 /infer), fake_c4.py (가짜 C4), publish_frames.py (dev 발행기), mosquitto.conf, docker-compose.override.yml
tests/         pytest + fixtures
PROPOSALS.md   공유 파일(스키마/compose/.env) 변경 제안
```

## TODO
- [x] Resize / ROI 파라미터 확정 (`C2_RESIZE_LONG_SIDE=1024`, `C2_ROI_MODE=auto`) — `.env.example` 반영은 PROPOSALS P5
- [x] 후보 bbox 생성 방식 결정: HSV 임계값 + contour, 지름 자동 추정
- [x] bbox 좌표계 확정: **전처리 후 이미지 기준 `[x1, y1, x2, y2]` pixel**
- [x] `preprocess_started_at_ms` / `preprocess_finished_at_ms` 기록
- [x] Dockerfile CMD를 uvicorn으로 교체 (compose `command` 교체는 PROPOSALS P4, Member A)
- [x] 이미지 전송 방식 확정: frame-envelope `image_b64`(base64 JPEG) (C1 PR #1)
- [ ] `raw_bgr` / `raw_rgb` 지원 (보내는 쪽이 생기면)
- [ ] `/cam` 기본 해상도 상향 (PROPOSALS P7)
- [x] C4로 리사이즈 프레임 + prompts 전송 (`scale` / `crop_offset` 불필요, PROPOSALS P1)
- [ ] `expected_diameter_px` 스키마 반영 후 출력에 추가 (PROPOSALS P2)
- [ ] api-contract에 C2 → C4 구간 반영 (PROPOSALS P8, Member A/D)
- [x] C1 → C2 MQTT 전환, 세션별 최신 1장 + 나이 기준 drop, 이벤트 CSV (PROPOSALS P3 해결)
- [ ] MQTT 토픽 구조·인증 C1과 확정 (`psf/v1/frames/+`의 `+` 의미, 운영 브로커 인증)
- [x] 폰 촬영 프레임 fixture 추가 (`tests/fixtures/phone/`, 5장)
