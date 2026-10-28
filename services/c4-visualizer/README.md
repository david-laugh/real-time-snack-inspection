# C4 Visualizer

C2의 리사이즈 이미지와 C3의 검사 결과를 매칭해 실시간 표시하는 FastAPI 대시보드입니다. 기본 포트는 8003입니다.

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

이 폴더의 `.env.example`에서 필요한 값을 **프로젝트 루트 `.env`**에 추가합니다. 루트 Compose가 값을 주입하므로 C4 별도 Compose 수정은 필요 없습니다. 기존 `.env`를 덮어쓰지 마세요. 로컬 Python 실행 시에는 셸 환경변수를 설정해야 하며 예시 파일은 자동 로드하지 않습니다.

| 설정 | 기본값 | 의미 |
|---|---|---|
| `C4_PORT` | `8003` | Compose 호스트 공개 포트 (컨테이너 내부는 8003) |
| `C4_C1_METRICS_URL` | `http://c1-gateway:8000/api/metrics` | C1 누적 카운터 조회. 빈 값이면 끔 |
| `C4_C2_STATS_URL` | `http://c2-preprocessing:8001/stats` | C2 누적 카운터 조회. 빈 값이면 끔 |
| `C4_C2_STATS_TIMEOUT_S` | `1` | C1/C2 각각의 조회 timeout, 양의 초 |
| `C4_C2_STATS_INTERVAL_S` | `2` | 두 조회가 끝난 뒤 대기 시간, 양의 초 |

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

필수 7필드: `trace_id`, `session_id`, `frame_id`, `width`, `height`, `prompts`, `image_b64`.
추가 optional 필드: `gateway_received_at_ms`, `preprocess_started_at_ms`, `preprocess_finished_at_ms` (Unix ms).

C2는 C3와 같은 이미지/좌표계로 전송합니다. C4는 `image_b64`를 WebSocket의 `frame`으로 바꾸고 나머지 값을 보존합니다. 구형 7필드와 최신 10필드 모두 지원합니다.

### POST /results — C3

기본 필드: `schema_version`, `trace_id`, `session_id`, `frame_id`, `captured_at_ms`, `inference_container_id`, `inference_started_at_ms`, `inference_finished_at_ms`, `inference_latency_ms`, `detections`.

1.1.0 optional 필드: `preprocess_started_at_ms`, `preprocess_finished_at_ms`, `inference_received_at_ms`, `latency_breakdown_ms`, `inference_timings_ms`. `schema_version` 문자열로 새 필드 사용 여부를 제한하지 않습니다. C3가 C1의 버전 값을 그대로 전파할 수 있기 때문입니다.

**판정은 C3 값을 그대로 보존합니다.** bbox 원형도로 재판정하지 않습니다.

| C3 status | 표시 / 집계 |
|---|---|
| `normal` | 정상 / `pass_count` |
| `defect` | 불량 / `fail_count` |
| `unknown` | 판정 보류 / `unknown_count` (정상·불량에 포함하지 않음) |

`defect_type`(예: `broken`)과 `defect_score`도 검사 피드, 상세 뷰어, CSV에 반영됩니다. `object_id`는 프레임 안에서만 유일하므로 세션·trace·프레임과 함께 해석해야 합니다. 기존 API/CSV 호환용 `circularity`, `area`, `perimeter`는 null이며, 화면은 불량 점수를 표시합니다.

## 화면과 프레임 매칭

성능 카드는 **Frame Drop / Latency 두 개**만 표시합니다. 세부 지표는 API 및 카드 툴팁으로 확인합니다.

- 이미지와 결과는 `(session_id, trace_id, frame_id)`로 매칭합니다. 어느 쪽이 먼저 와도 처리합니다.
- 브라우저 버퍼는 최대 30개입니다. 이미지가 없는 결과는 검사 피드·통계에는 반영하고, 카메라는 이전 매칭 화면을 유지합니다.
- 새 매칭 화면보다 먼저 수신된 이미지의 늦은 결과로 화면을 되돌리지 않습니다. 완전한 프레임 순서 재정렬 기능은 아닙니다.
- 빈 detections는 이전 마스크를 지웁니다.
- RLE: `{"encoding":"rle","width":576,"height":1024,"data":{"order":"row-major","counts":[...]}}`. 배경(0) run부터 시작하고 합계는 width × height입니다. COCO column-major 형식은 사용하지 않습니다.
- 마스크/박스 색상: 정상 녹색, 불량 빨간색, 판정 보류 회색. 잘못된 마스크는 bbox로 대체합니다.

## 지연과 드롭

### 지연

`latency_breakdown_ms`의 `capture_to_preprocess`, `preprocess`, `c2_to_c3`, `c3_queue`, `c3_service`, `c3_total`, `capture_to_c3_done`은 C3 제공값을 우선 사용합니다. null/음수/비유한 값은 집계에서 제외합니다. 제공된 null을 카메라 시각으로 덮어쓰지 않습니다. `inference_timings_ms`는 프레임 WebSocket 이벤트에 그대로 포함합니다.

- `/api/stats.latency`: 최근 200개 결과 프레임별 P50/P95와 유효 샘플 수.
- `e2e`: C4 **서버 수신** − 촬영 시각. `c3_c4`: C4 수신 − C3 종료.
- Latency 카드: 매칭 이미지/마스크를 canvas에 그린 시각 − 촬영 시각을 브라우저에서 측정하고 최근 200회 표시의 P50/P95를 보여줍니다 (`표시 · P50`). 첫 매칭 전에는 서버 수신 지연을 보여줍니다 (`수신 · P50`). 브라우저 paint 완료 시각까지 측정하는 것은 아닙니다.
- 카메라 시각이 있으면 `upload`, `c1_c2`도 계산합니다. C3 breakdown이 없을 때만 기존 `preprocess`, `c2_c3` 추정을 사용합니다. 이는 대기 시간까지 포함할 수 있습니다.
- 서버는 이미지 메타데이터 500개, 매칭 대상 결과 200개를 보관합니다. 이미지 누락은 C3 제공 지연 집계에 영향을 주지 않습니다.
- 호스트/폰/브라우저의 시계 동기화가 필요합니다. 지연을 0으로 대체하지 않습니다.

### 드롭

Frame Drop 카드의 큰 값은 **C2 기동 이후 dropped_stale + dropped_old 누적 수**입니다.

C1의 `counters.received/dropped`, C2의 `events.forwarded/error/timeout`도 읽습니다. `received_frames`는 detection 개수가 아닌 결과 프레임 수이며 최근 5,000개 `(session_id, trace_id, frame_id)` 키 내 중복을 제외합니다.

`drop_window`는 최초 비교 기준 수집 후 약 5초 이상 간격의 증가분을 사용합니다:

| 필드 | 계산 |
|---|---|
| `c1_drop_pct` | ΔC1 dropped / ΔC1 received |
| `c2_drop_pct` | ΔC2 stale+old / ΔC1 received |
| `c2_error_pct` | ΔC2 error+timeout / ΔC2 forwarded+error+timeout |
| `result_gap_estimate` | max(0, ΔC2 forwarded − ΔC4 received_frames) |
| `overall_loss_pct_estimate` | 1 − ΔC4 received_frames / ΔC1 received (0~100%로 제한) |

분모가 0이면 비율은 null입니다. 조회 실패·미제공 카운터·카운터 감소(재시작/reset) 시 비교 기준과 구간 지표를 초기화합니다. 폴링 시점과 비동기 전송 시점이 달라 경계의 처리 중 프레임도 차이에 포함될 수 있어 **유실은 추정값이며 확정 드롭이 아닙니다**. C2 error에는 입력 오류도 포함될 수 있습니다. frame_id 갭을 즉시 드롭으로 세지 않습니다. C4만 재시작해도 새 기준을 수집하므로 누적 카운터의 시작 시점 차이를 직접 빼지 않습니다.

## API와 연결 진단

| 경로 | 내용 |
|---|---|
| `GET /healthz` | C4 자체 생존 확인 (upstream 연결 보장 아님) |
| `GET /api/connections` | C1/C2 조회 활성·성공 여부, 마지막 이미지/결과 수신 시각, WS 접속 수 |
| `GET /api/stats` | 객체 판정 수, 결과 프레임 FPS, 지연, C1/C2 카운터, 구간 유실 추정 |
| `GET /api/inspections?limit=50` | 최근 검사 목록 |
| `GET /api/defects?limit=50` | C3 status=defect 목록 |
| `GET /api/export/csv` | 검사 이력 CSV |
| `WS /ws/dashboard` | `stats`, `camera_frame`, `inspection_result`, `frame_result` |

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
python -m unittest discover -s tests -p 'test_*.py' -v
```
