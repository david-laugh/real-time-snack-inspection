# C4 Visualizer

C2의 리사이즈 이미지와 C3의 검사 결과를 매칭해 실시간 표시하는 FastAPI 대시보드입니다. 기본 포트는 8003입니다.

**C4는 표시만 합니다.** 판정(정상/불량/판정 보류)은 C3 결과를, 구간 지연은 C3 `latency_breakdown_ms`와 C2 이미지 시각을, 손실은 C1 `/api/metrics`·C2 `/stats` 카운터를 그대로 사용합니다. C4가 bbox 등으로 다시 판정하거나 값을 추정해 채우지 않습니다.

## 통신 구조

```text
C1 --MQTT--> C2 --HTTP /infer--> Nginx --> C3
             |                           |
             +--POST /api/camera/frame   +--POST /results
                         |                    |
                         +-------- C4 --------+
                                    |
                              WebSocket → Browser
C4 --GET /api/metrics--> C1
C4 --GET /stats-------> C2
```

C4는 MQTT를 사용하지 않습니다. C3 결과 전송은 전달받은 2026-10-28 사양(`feature/inference`, schema 1.1.0)에 대응합니다. 해당 송신 구현이 적용된 C3를 실행해야 합니다. C3는 `/infer` 응답 후 백그라운드로 결과를 한 번 보내며 재시도하지 않습니다. C4는 200 응답을 반환합니다.

## 실행과 환경변수

```bash
docker compose up -d --no-deps --build c4-visualizer
```

브라우저: `http://localhost:8003/`, 상세 뷰어: `/viewer`.
소스 변경 후에는 위 명령으로 이미지를 재빌드하고 브라우저를 새로고침합니다.

**전체 체인 (C1 → MQTT → C2 → Nginx → C3 ×2 → C4)** — repo root, GPU 필요. C2 실행·C3 포트 공개는 C3 테스트 override(`services/c3-inference/dev/chain.override.yml`)가 맡고, C3 결과 수신처만 환경변수로 C4 컨테이너를 가리킵니다:

```bash
export C3_RESULTS_URL=http://c4-visualizer:8003/results MQTT_HOST_PORT=11884
F="-f docker-compose.yml -f services/c3-inference/compose.gpu.yml -f services/c3-inference/dev/chain.override.yml"
docker compose $F up -d --build c4-visualizer
C2_MAX_INFLIGHT=3 docker compose $F up -d --build mqtt-broker c3-inference-1 c3-inference-2 nginx-inference-lb c2-preprocessing c1-gateway
# 부하: 폰 /cam 접속, 또는 C1 loadgen
docker compose $F exec c1-gateway python src/c1_gateway.py loadgen --source /frames --fps 30 --duration 30 --width 960
```

이 폴더의 `.env.example`에서 필요한 값을 **프로젝트 루트 `.env`**에 추가합니다. 루트 Compose가 값을 주입하므로 C4 별도 Compose 수정은 필요 없습니다. 기존 `.env`를 덮어쓰지 마세요. 로컬 Python 실행 시에는 셸 환경변수를 설정해야 하며 예시 파일은 자동 로드하지 않습니다.

| 설정 | 기본값 | 의미 |
|---|---|---|
| `C4_PORT` | `8003` | Compose 호스트 공개 포트 (컨테이너 내부는 8003) |
| `C4_C1_METRICS_URL` | `http://c1-gateway:8000/api/metrics` | C1 누적 카운터 조회. 빈 값이면 끔 |
| `C4_C2_STATS_URL` | `http://c2-preprocessing:8001/stats` | C2 누적 카운터 조회. 빈 값이면 끔 |
| `C4_C2_STATS_TIMEOUT_S` | `1` | C1/C2 각각의 조회 timeout, 양의 초 |
| `C4_C2_STATS_INTERVAL_S` | `1` | 두 조회가 끝난 뒤 대기 시간, 양의 초 |
| `C4_SCAN_LINE_RATIO` | `0.34` | 검사선 x (이미지 폭 대비 비율, 0~1). 대시보드 클릭으로 실행 중 변경 가능(재시작 시 이 값으로 복귀) |
| `C4_TRAVEL_DIRECTION` | `left` | 쿠키 진행 방향 `left` / `right` |
| `C4_TRACK_VOTE_FRAMES` | `2` | 판정 다수결에 쓰는 검사선 전·후 프레임 수 (전 N + 통과 + 후 N) |
| `C4_TRACK_REORDER_S` | `0.5` | 결과를 frame_id 순서로 처리하기 위해 기다리는 시간 |

상대 서비스 설정 (C4가 대신 변경하지 않습니다):

| 담당 | 설정 | 용도 |
|---|---|---|
| C2 | `C2_C4_FRAME_URL=http://c4-visualizer:8003/api/camera/frame` | 리사이즈 이미지와 prompts 전달 |
| C3 | `C3_RESULTS_URL=http://c4-visualizer:8003/results` | 전달받은 사양의 결과 push URL |
| C3 | `C3_RETURN_MASK=1` | row-major RLE 마스크 포함 |
| C1/C2 | `MQTT_HOST=mqtt-broker`, `MQTT_PORT=1883`, `MQTT_TOPIC_PREFIX=psf/v1` | C1→C2 통신용 |

컨테이너 내부에서 상대 서비스를 `localhost`로 지정하지 않습니다. 호스트 MQTT 도구는 `localhost:11883`을 사용합니다. C4를 로컬 Python으로 실행할 때는 C1/C2의 접근 가능한 주소를 지정해야 합니다. 루트 Compose의 C2 8001은 호스트에 공개되지 않습니다. 다른 PC에서는 C4 호스트 IP와 공개 포트를 사용합니다.

## 수신 계약

### POST /api/camera/frame — C2

`shared/schemas/camera-frame.schema.json`의 **10개 필드가 모두 필수**입니다: `trace_id`, `session_id`, `frame_id`, `gateway_received_at_ms`, `preprocess_started_at_ms`, `preprocess_finished_at_ms`, `width`, `height`, `prompts`, `image_b64`.

- 스키마대로 검증합니다(정수 필드에 문자열 불가, 추가 필드 불가, prompt는 `{candidate_id, type: "box", bbox[4]}`). 위반하면 **422**입니다. 구형 7필드 요청도 422입니다.
- 통과하면 **202**를 바로 반환하고, WebSocket 중계는 응답 뒤에 합니다. 브라우저가 느려도 C2를 붙잡지 않습니다.
- C2는 C3와 같은 이미지/좌표계로 전송합니다(좌표 변환 없음). C4는 `image_b64`를 WebSocket의 `frame`으로 바꾸고 나머지 값을 보존합니다.

### POST /results — C3

기본 필드: `schema_version`, `trace_id`, `session_id`, `frame_id`, `captured_at_ms`, `inference_container_id`, `inference_started_at_ms`, `inference_finished_at_ms`, `inference_latency_ms`, `detections`.

1.1.0 optional 필드: `preprocess_started_at_ms`, `preprocess_finished_at_ms`, `inference_received_at_ms`, `latency_breakdown_ms`, `inference_timings_ms`. `schema_version` 문자열로 새 필드 사용 여부를 제한하지 않습니다. C3가 C1의 버전 값을 그대로 전파할 수 있기 때문입니다.

**판정은 C3 값을 그대로 보존합니다.** bbox 원형도로 재판정하지 않습니다.

| C3 status | 표시 / 집계 |
|---|---|
| `normal` | 정상 / `pass_count` |
| `defect` | 불량 / `fail_count` |
| `unknown` | 판정 보류 / `unknown_count` (정상·불량에 포함하지 않음) |

`defect_type`(예: `broken`)과 `defect_score`도 검사 피드, 상세 뷰어, CSV에 반영됩니다. `object_id`는 프레임 안에서만 유일하므로 세션·trace·프레임과 함께 해석해야 합니다. 과거 재판정용 필드(`circularity`, `area`, `perimeter`)는 API·CSV에서 제거했고, 대신 C3가 보낸 `bbox`를 그대로 기록합니다. 불량 점수 분포 차트의 점선은 C3 기준(`defect_score > 0.5` ⇔ 불량)입니다.

## 화면과 프레임 매칭

### 파이프라인 패널

상단 통계 카드(총 검사·양품·불량·판정 보류·불량률) 아래에 **컨테이너를 박스로, 구간을 화살표로** 표시합니다.

```text
[📱 카메라] →업로드→ [C1 Gateway] →MQTT·C2 대기→ [C2 Preprocess] →HTTP·Nginx→ [C3 Inference ×N] →HTTP push→ [C4]
```

| 위치 | 표시 값 | 출처 |
|---|---|---|
| 카메라 | 세션 수 | C1 `/api/metrics` `sessions` |
| C1 | 입력 fps, 수신, C1 드롭 | C1 `input_fps`, `counters.received/dropped` |
| 화살표 업로드 | P50 (P95) | `gateway_received_at_ms − captured_at_ms` (C2 이미지 + C3 결과 trace 매칭) |
| 화살표 C1→C2 | P50 (P95), **밀려서 버림** 누적·최근 비율 | `preprocess_started − gateway_received`; C2 `dropped_stale+old`, `drop_window.c2_drop_pct`. 이미지 시각이 없으면 `capture_to_preprocess`(촬영→C2 합계)로 표시 |
| C2 | 전처리 P50, C3 전달 성공, 처리 중/최대 | C3 `latency_breakdown_ms.preprocess`; C2 `forwarded`, `inflight/max_inflight` |
| 화살표 C2→C3 | P50 (P95), **실패(오류+timeout)** 누적·최근 비율 | C3 `c2_to_c3`; C2 `error+timeout`, `drop_window.c2_error_pct` |
| C3 | 대기·추론 P50/P95, replica별 처리 프레임·`c3_total` P50 | C3 `c3_queue`, `c3_service`, 결과의 `inference_container_id` |
| 화살표 C3→C4 | P50 (P95), **결과 미도착(추정)** | C4 수신 − `inference_finished_at_ms`; `drop_window.result_gap_estimate` |
| C4 | 결과 프레임 수, 수신 fps, C2→C4 이미지 전송 실패 | C4 집계; C2 `c4_error+c4_skipped` |

- 하단 막대: 구간별 P50을 순서대로 쌓아 **어느 구간이 지연을 차지하는지** 보여 줍니다. 범례 끝에 구간 합과 실측 E2E(촬영→C4 수신) P50/P95를 함께 표시합니다.
- 박스의 점: 녹색 = 해당 출처에서 데이터를 받는 중, 회색·점선 = 미수신(카운터 조회 실패 또는 결과 없음).
- 값이 없으면 `—`로 표시합니다. 0으로 채우지 않습니다. 손실이 0보다 크면 빨간색입니다.

### 성능 카드와 카메라

성능 카드는 **Frame Drop / Latency 두 개**입니다. 세부 구간은 위 파이프라인 패널에서 확인합니다.

- 이미지와 결과는 `(session_id, trace_id, frame_id)`로 매칭합니다. 어느 쪽이 먼저 와도 처리합니다.
- 브라우저 버퍼는 **최근 2초**입니다(안전 상한 300개). 2초 안에 짝이 오지 않으면 버립니다. 이미지가 없는 결과는 검사 피드·통계에는 반영하고, 카메라는 이전 매칭 화면을 유지합니다.
- 화면은 세션 안에서 **frame_id가 증가하는 순서로만** 바뀝니다. 더 큰 frame_id가 이미 표시됐으면 늦게 짝이 맞은 이전 프레임은 그리지 않습니다.
- **prompts가 빈 배열인 이미지는 C3 결과를 기다리지 않고 바로 표시**합니다(C2 `C2_SKIP_EMPTY=true`면 C3로 가지 않아 결과가 오지 않음). 이전 마스크는 지워지고 상태에 `객체 없음`이 표시됩니다.
- 빈 detections는 이전 마스크를 지웁니다.
- RLE: `{"encoding":"rle","width":576,"height":1024,"data":{"order":"row-major","counts":[...]}}`. 배경(0) run부터 시작하고 합계는 width × height입니다. COCO column-major 형식은 사용하지 않습니다.
- 마스크/박스 색상: 정상 녹색, 불량 빨간색(235,59,59), 판정 보류 회색, 반투명(alpha 96/255). 잘못된 마스크는 bbox로 대체합니다.

## 쿠키 추적과 1회 집계 (`src/tracker.py`)

C3 `object_id`는 프레임 안에서만 유일하므로 같은 쿠키가 프레임마다 다시 집계되지 않도록 C4가 추적합니다.

1. 세션별로 결과를 `C4_TRACK_REORDER_S`(0.5초)만큼 모아 **frame_id 순서로** 처리합니다. 이미 처리한 frame_id보다 늦게 온 결과와 중복 결과는 추적에서 제외합니다(검사 피드에는 반영).
2. 이전 프레임 쿠키와 현재 쿠키를 **같은 라인**(bbox 중심 y 차이 ≤ 지름/2)이면서 **진행 방향으로 앞선** 것끼리, 가까운 쌍부터 매칭합니다. 매칭이 안 되면 새 쿠키, 3번 연속 안 보이면 추적 종료입니다.
3. 쿠키 중심이 검사선 x(`C4_SCAN_LINE_RATIO` × 이미지 폭)를 **넘는 순간 한 번만** 집계합니다. 판정은 검사선 전·후 `C4_TRACK_VOTE_FRAMES`(2)프레임 + 통과 프레임의 C3 status **다수결**입니다(unknown은 기권, 정상·불량 동수면 불량). 그래서 집계는 통과 후 2프레임 뒤에 확정됩니다.
4. 이미지 폭은 마스크 `width`, 없으면 같은 세션 카메라 프레임의 `width`를 씁니다.

상단 카드(총 검사/양품/불량/불량률)와 양품률 도넛은 **추적 쿠키 기준**입니다. 검사 결과 피드와 불량 뷰어는 기존처럼 프레임별 검출 기준입니다. `/api/stats`의 `detections`에 검출 단위 누적 수가 있습니다.

대시보드에서 카메라 화면을 **클릭하면 그 위치로 검사선이 이동**합니다(노란 점선, 모든 접속 화면에 반영). 시뮬레이터 라벨 기반 테스트(`tests/test_tracker.py`)로 같은 쿠키가 여러 프레임에 나와도 1번만 집계되는지 확인합니다.

## 지연과 드롭

### 지연

`latency_breakdown_ms`의 `capture_to_preprocess`, `preprocess`, `c2_to_c3`, `c3_queue`, `c3_service`, `c3_total`, `capture_to_c3_done`은 C3 제공값을 우선 사용합니다. null/음수/비유한 값은 집계에서 제외합니다. 제공된 null을 카메라 시각으로 덮어쓰지 않습니다. `inference_timings_ms`는 프레임 WebSocket 이벤트에 그대로 포함합니다.

- 카메라 아래 줄에 **표시 중인 프레임**의 구간 지연을 보여줍니다: C1 = `preprocess_started − gateway_received`(C1 처리 + MQTT + C2 대기), C2 = `preprocess_finished − preprocess_started`, C3 = `inference_latency_ms`, E2E = 화면 표시 − 촬영. **C3 `inference_latency_ms`에는 replica 대기 시간이 섞여 있어** `(대기 포함)`으로 표시하고, `inference_timings_ms.lock_wait`가 있으면 그 값을 함께 보여줍니다. prompts가 없어 C3로 가지 않은 프레임은 `C3 미전송`입니다.
- `/api/stats.latency`: 최근 200개 결과 프레임별 P50/P95와 유효 샘플 수.
- `e2e`: C4 **서버 수신** − 촬영 시각. `c3_c4`: C4 수신 − C3 종료.
- Latency 카드: 매칭 이미지/마스크를 canvas에 그린 시각 − 촬영 시각을 브라우저에서 측정하고 최근 200회 표시의 P50/P95를 보여줍니다 (`표시 · P50`). 첫 매칭 전에는 서버 수신 지연을 보여줍니다 (`수신 · P50`). 브라우저 paint 완료 시각까지 측정하는 것은 아닙니다.
- 카메라 시각이 있으면 `upload`, `c1_c2`도 계산합니다. C3 breakdown이 없을 때만 기존 `preprocess`, `c2_c3` 추정을 사용합니다. 이는 대기 시간까지 포함할 수 있습니다.
- 서버는 이미지 메타데이터 500개, 매칭 대상 결과 200개를 보관합니다. 이미지 누락은 C3 제공 지연 집계에 영향을 주지 않습니다.
- 호스트/폰/브라우저의 시계 동기화가 필요합니다. 지연을 0으로 대체하지 않습니다.

### 드롭

Frame Drop 카드의 큰 값은 **C1 dropped + C2 dropped_stale + dropped_old 누적 수**(각 서비스 기동 이후)이며, 카드 아래에 C1·C2 각각의 값을 표시합니다.

C1 `/api/metrics`와 C2 `/stats`를 1초마다 읽습니다. C1의 `counters.received/dropped`, C2의 `events.forwarded/skipped_empty/error/timeout`을 씁니다. 카드 아래 줄에 구간 drop률(C1·C2·전체 유실 추정)과 **C2 처리량 = forwarded + skipped_empty**(fps)를 표시합니다. C1 조회가 꺼져 있으면 구간 지표는 `수집 중`으로 남습니다. `received_frames`는 detection 개수가 아닌 결과 프레임 수이며 최근 5,000개 `(session_id, trace_id, frame_id)` 키 내 중복을 제외합니다.

`drop_window`는 최초 비교 기준 수집 후 약 5초 이상 간격의 증가분을 사용합니다:

| 필드 | 계산 |
|---|---|
| `c1_drop_pct` | ΔC1 dropped / ΔC1 received |
| `c2_drop_pct` | ΔC2 stale+old / ΔC1 received |
| `c2_error_pct` | ΔC2 error+timeout / ΔC2 forwarded+error+timeout |
| `result_gap_estimate` | max(0, ΔC2 forwarded − ΔC4 received_frames) |
| `overall_loss_pct_estimate` | 1 − (ΔC4 received_frames + ΔC2 skipped_empty) / ΔC1 received (0~100%로 제한). skipped_empty는 결과가 원래 오지 않는 프레임이라 유실이 아님 |
| `c2_processed_per_s` | (ΔC2 forwarded + ΔC2 skipped_empty) / 구간 초 |

분모가 0이면 비율은 null입니다. 조회 실패·미제공 카운터·카운터 감소(재시작/reset) 시 비교 기준과 구간 지표를 초기화합니다. 폴링 시점과 비동기 전송 시점이 달라 경계의 처리 중 프레임도 차이에 포함될 수 있어 **유실은 추정값이며 확정 드롭이 아닙니다**. C2 error에는 입력 오류도 포함될 수 있습니다. frame_id 갭을 즉시 드롭으로 세지 않습니다. C4만 재시작해도 새 기준을 수집하므로 누적 카운터의 시작 시점 차이를 직접 빼지 않습니다.

## API와 연결 진단

| 경로 | 내용 |
|---|---|
| `GET /healthz` | C4 자체 생존 확인 (upstream 연결 보장 아님) |
| `GET /api/connections` | C1/C2 조회 활성·성공 여부, 마지막 이미지/결과 수신 시각, WS 접속 수 |
| `GET /api/stats` | 객체 판정 수, 결과 프레임 FPS, 지연, C1/C2 카운터, 구간 유실 추정 |
| `GET /api/inspections?limit=50` | 최근 검출 목록 (프레임별) |
| `GET /api/defects?limit=50` | C3 status=defect 검출 목록 |
| `GET /api/cookies?limit=50` | 검사선 통과로 집계된 쿠키 목록 (투표 수, 통과 frame_id 포함) |
| `GET /api/export/csv` | **쿠키 CSV**: 쿠키 1개당 1행 (`inspection-cookies.csv`) |
| `GET /api/export/detections.csv` | 검출 CSV: 프레임별 검출 1건당 1행 (이전 `/api/export/csv` 형식) |
| `GET/POST /api/scan-line` | 검사선 조회 / 변경 `{"ratio": 0.34}` (0<ratio<1, 위반 시 422) |
| `WS /ws/dashboard` | `stats`, `camera_frame`, `inspection_result`, `frame_result`, `cookie_counted`, `scan_line` |

결과만 도착하지 않으면 C3 송신 구현/`C3_RESULTS_URL` 및 C3 `/metrics`의 `results_push_error_count`를 확인합니다. replica별 결과 push 실패 카운터는 현재 C4가 자동 조회하지 않습니다. 프레임에 detection이 없어도 frame_result와 성능 집계는 갱신됩니다. 검사 이력은 최대 500건이며 재시작 시 초기화됩니다.

```bash
curl http://localhost:8003/api/connections
curl http://localhost:8003/api/stats
```

## 검증

프로젝트 루트:

```bash
node --test services/c4-visualizer/tests/test_dashboard.cjs
```

C4 Python 의존성 설치 환경:

```bash
cd services/c4-visualizer
pip install -r requirements-dev.txt   # TestClient용 httpx
python -m unittest discover -s tests -p 'test_*.py' -v
```

통합 확인(2026-10-28): 루트 compose의 C4 + C2 dev 브로커/C2 + 가짜 C3(Otsu 마스크 + RLE로 `/results` 전송)에 C2 시뮬레이터 fixture를 왼쪽으로 이동시키며 60프레임 발행 → 라벨로 계산한 검사선 통과 21건과 C4 집계 21건이 프레임·라인까지 일치, 마스크 오버레이와 빈 prompts 프레임 즉시 표시 확인.
