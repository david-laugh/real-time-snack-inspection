# C4 Visualizer

**Owner:** Member D
**Port:** 8003

과자 크랙 검사 결과를 실시간으로 시각화하는 대시보드 서비스.
C3 Inference에서 보내온 detection 결과를 받아 WebSocket으로 브라우저에 실시간 push합니다.

**역할:**
- Detection 결과 실시간 표시 (WebSocket push)
- 영상 위에 bbox / RLE mask / 불량 마킹 overlay (canvas 기반)
- 모니터링: Processed FPS, defect count

---

## 구현 현황

### 기존 스펙(TODO) 대비 완료 상태

| # | 기존 TODO | 상태 | 구현 내용 |
|---|-----------|------|-----------|
| 1 | Dashboard 프레임워크 선정 | **완료** | FastAPI + vanilla JS + WebSocket 실시간 통신 |
| 2 | Overlay 렌더링 | **완료** | HTML Canvas 기반 bbox + RLE mask overlay (아래 상세) |
| 3 | 메트릭 집계 및 CSV export | **완료** | `GET /api/export/csv` + 대시보드 다운로드 버튼 |
| 4 | Docker command를 실제 entrypoint로 교체 | **완료** | `uvicorn src.main:app --host 0.0.0.0 --port 8003` |

### 기존 스펙에 없었으나 추가 구현된 기능

| 기능 | 설명 | 관련 파일 |
|------|------|-----------|
| **불량 확인 뷰어** (`/viewer`) | 불량 항목 테이블 + 행 클릭 시 슬라이드인 상세 패널 (원형도 게이지, 파이프라인 정보) | `viewer.html` |
| **REST API 확장** | `/api/stats`, `/api/inspections`, `/api/defects`, `/api/export/csv` 4개 엔드포인트 추가 | `main.py` |
| **양품률 도넛 차트** | SVG 기반 실시간 양품/불량 비율 도넛 차트 | `index.html` |
| **원형도 분포 히스토그램** | 20구간 바 차트 + 0.85 임계선 표시 | `index.html` |
| **WebSocket 자동 재연결** | 연결 끊김 시 2초 후 자동 재시도 + 상태 배지 표시 | `index.html` |
| **CSV 내보내기** | 검사 이력 전체를 CSV 파일로 다운로드 | `store.py`, `main.py` |

## Open Issues (미해결)

1. **C2 전송 연동 필요** — C4의 `POST /api/camera/frame` 수신·중계는 구현 완료. C2는 C3에 보내는 것과 같은 리사이즈 이미지와 prompts를 같은 시점에 C4로 비동기 전송합니다. C3 요청 경로에서 C4 응답을 기다리지 않습니다. C2 송신 구현은 C2 담당 범위입니다.
2. **결과 지연/역순 도착** — 브라우저는 `(session_id, trace_id, frame_id)`로 이미지와 결과를 짝짓고, 최대 30개 프레임을 보관합니다. 짝이 맞는 결과를 표시한 뒤에는 다음 짝이 완성될 때까지 현재 화면을 유지합니다. 이미 선택한 이미지보다 먼저 수신된 이미지의 늦은 결과는 표시하지 않습니다. 버퍼에서 제거된 프레임은 표시하지 못할 수 있습니다.

---

## 핵심 로직 상세

### 1. 원형도(Circularity) 판정

C3에서 받은 detection의 `bbox [x1, y1, x2, y2]`로부터 양품/불량을 판정합니다.

```
width     = |x2 - x1|
height    = |y2 - y1|
area      = width × height
perimeter = 2 × (width + height)

Circularity = 4π × area / perimeter²
```

| 조건 | 판정 | 의미 |
|------|------|------|
| `circularity >= 0.85` | **pass** (양품) | 정원에 가까운 형태 |
| `circularity < 0.85` | **fail** (불량) | 찌그러지거나 깨진 형태 |

**참고값:**
- 정사각형 (100×100): `4π × 10000 / 400² ≈ 0.785` → **불량**
- 정사각형에 가까운 직사각형 (100×95): `≈ 0.784` → **불량**
- 길쭉한 직사각형 (200×20): `4π × 4000 / 440² ≈ 0.259` → **불량**

> 임계값은 `main.py`의 `CIRCULARITY_THRESHOLD = 0.85`에서 변경 가능.

### 2. 처리량·지연·Frame Drop

`metrics.py`는 객체 수와 별개로 C3 결과 프레임을 집계합니다. detection이 0개여도 한 프레임으로 세고, 최근 5,000개 추적 키 내 중복 결과는 성능 집계에서 제외합니다. FPS는 최근 5초의 수신 프레임 수 / 5입니다. 기존 검사 건수는 객체 단위로 유지합니다.

| 대시보드 항목 | 계산 / 출처 |
|---|---|
| E2E P50/P95 | C4 결과 수신 시각 − `captured_at_ms` |
| C3 추론 P50/P95 | `inference_latency_ms` |
| C3 → C4 P50/P95 | C4 결과 수신 시각 − `inference_finished_at_ms` |
| C3 이전 전체 P50/P95 | `inference_started_at_ms` − `captured_at_ms` (전송·대기·전처리 포함) |
| C1 → C2 / C2 → C3 | **미수집**: 현행 C3 결과에 구간별 시각이 없어 분리 계산 불가 |
| C2 Frame Drop | C2 `/stats`의 `events.dropped_stale + events.dropped_old` |
| 화면용 이미지 전송 누락 | C2 `/stats`의 `c4_skipped + c4_error` (PR #8 적용 시 제공) |

지연은 최근 200개 결과 프레임의 유효 샘플로 계산합니다. 호스트 간 시각 차이를 계산하므로 시계 동기화가 필요합니다. 음수 지연 및 0인 기준 타임스탬프는 제외하고 샘플이 없으면 `미수집`을 표시합니다. C3→C4는 결과 전송·직렬화 등도 포함하므로 순수 네트워크 지연은 아닙니다.

C4는 별도 백그라운드 작업으로 C2 `/stats`를 조회합니다 (요청 timeout 1초, 조회 후 2초 대기). 프레임 수신 경로에서 기다리지 않습니다. C2 접속 실패 시 카운터를 0으로 표시하지 않고 `미수집`으로 표시합니다. C2 재시작 시 카운터가 초기화됩니다. C2 드롭과 화면용 이미지 누락은 서로 다른 지표이며 C1/C3의 전체 드롭을 뜻하지 않습니다. `frame_id` 간격은 드롭으로 세지 않습니다.

- `C4_C2_STATS_URL`: 기본 `http://c2-preprocessing:8001/stats`; 로컬 실행에서는 접근 가능한 C2 URL로 지정합니다. 빈 문자열이면 C2 조회를 끕니다.
- 기존 C2 `/stats`도 지원합니다. PR #8 이전에는 C2 드롭만 표시하고 화면용 이미지 누락은 `미수집`으로 표시합니다.
- C1→C2 및 C2→C3 개별 지연을 수집하려면 담당 서비스와 송·수신 시각 전달 계약을 추가로 합의해야 합니다. 이번 작업은 C4만 수정합니다.

### 3. BBox / RLE Mask Overlay 렌더링

- C3의 `mask`를 프레임 단위 `frame_result` WebSocket 이벤트로 전달합니다. 빈 detection 배열도 전달하여 이전 마스크를 지웁니다.
- `index.html`에서 C2 이미지와 결과를 추적 ID·세션·프레임으로 매칭한 뒤 함께 표시합니다.
- C3의 RLE는 `{"encoding":"rle","width":576,"height":1024,"data":{"order":"row-major","counts":[...]}}` 형식입니다. counts는 배경(0)부터 시작하여 배경/전경 길이가 번갈아 나오며, 합계는 width × height입니다.
- 마스크는 pass=녹색, fail=빨간색의 반투명 영역으로 그립니다. bbox와 라벨도 유지합니다.
- null, 미지원 인코딩, 잘못된 RLE, 이미지와 크기가 다른 마스크는 건너뛰고 bbox만 표시합니다.
- C2와 C3는 같은 이미지 좌표계를 사용합니다. 화면에 맞추는 확대·축소와 여백만 이미지/bbox/mask에 동일하게 적용합니다.

### 4. 인메모리 저장소

`InspectionStore` — SQLite 없이 메모리에서 최근 N건만 유지하는 경량 구조.

| 버퍼 | 최대 크기 | 용도 |
|------|-----------|------|
| `_records` | 500건 | 검사 이력 (deque, FIFO) |
| `PipelineMetrics.samples` | 200프레임 | 지연 P50/P95 집계 |

컨테이너 재시작 시 데이터는 초기화됩니다 (stateless).

---

## Interface

| 방향 | Endpoint | Payload / 설명 |
|------|----------|----------------|
| C3 → C4 | `POST /results` | [`detection-result.schema.json`](../../shared/schemas/detection-result.schema.json) |
| C2 → C4 | `POST /api/camera/frame` | 리사이즈 프레임 + prompts 전송 (`trace_id`, `session_id`, `frame_id`, `width`, `height`, `prompts`, `image_b64`) |
| Browser → C4 | `GET /` | 메인 대시보드 (port 8003) |
| Browser → C4 | `GET /viewer` | 불량 확인 뷰어 |
| Browser → C4 | `WS /ws/dashboard` | 실시간 WebSocket |
| Browser → C4 | `GET /api/stats` | 통계 JSON |
| Browser → C4 | `GET /api/inspections` | 검사 이력 |
| Browser → C4 | `GET /api/defects` | 불량 목록 |
| Browser → C4 | `GET /api/export/csv` | CSV 다운로드 |
| — | `GET /healthz` | 헬스체크 |

---

## API 명세

### `POST /results`

C3 Inference에서 detection 결과를 수신합니다.

**Request Body** (`DetectionResult`):

```json
{
  "schema_version": "1.0.0",
  "trace_id": "string (전파용 추적 ID)",
  "session_id": "string (세션 ID)",
  "frame_id": 0,
  "captured_at_ms": 1730000000000,
  "inference_container_id": "c3-inference-1",
  "inference_started_at_ms": 1730000000050,
  "inference_finished_at_ms": 1730000000100,
  "inference_latency_ms": 50.0,
  "detections": [
    {
      "object_id": "snack-001",
      "bbox": [100.0, 100.0, 200.0, 200.0],
      "status": "normal | defect | unknown",
      "defect_type": "crack | null",
      "defect_score": 0.05,
      "mask": null
    }
  ]
}
```

| 필드 | 타입 | 필수 | 설명 |
|------|------|------|------|
| `schema_version` | string | O | 스키마 버전 (기본 `"1.0.0"`) |
| `trace_id` | string | O | 파이프라인 추적 ID (C1에서 발급) |
| `session_id` | string | O | 세션 ID |
| `frame_id` | int | O | 프레임 번호 (순차 증가) |
| `captured_at_ms` | int | O | C1 캡처 시각 (Unix ms) |
| `inference_container_id` | string | O | C3 replica 식별자 (예: `c3-inference-1`) |
| `inference_started_at_ms` | int | O | C3 추론 시작 시각 (Unix ms) |
| `inference_finished_at_ms` | int | O | C3 추론 종료 시각 (Unix ms) |
| `inference_latency_ms` | float | O | C3 추론 소요 시간 (ms) |
| `detections` | array | O | detection 배열 |
| `detections[].object_id` | string | O | SAM2 추적 객체 ID |
| `detections[].bbox` | float[4] | O | `[x1, y1, x2, y2]` 픽셀 좌표 |
| `detections[].status` | string | O | `normal` / `defect` / `unknown` |
| `detections[].defect_type` | string? | - | 불량 유형 (`crack`, `broken` 등), null 가능 |
| `detections[].defect_score` | float | - | 불량 신뢰도 `[0, 1]` (기본 0.0) |
| `detections[].mask` | object? | - | 세그멘테이션 마스크 (RLE 렌더링 지원, null 가능) |

**Response** `200 OK`:

```json
{
  "status": "ok",
  "detections_processed": 1
}
```

**처리 흐름:**
1. 각 detection마다:
   - bbox에서 area, perimeter 계산
   - circularity 산출 → pass/fail 판정
   - `InspectionStore`에 기록
   - WebSocket으로 `inspection_result` 이벤트 브로드캐스트
2. 전체 통계 재계산 → WebSocket으로 `stats` 이벤트 브로드캐스트

---

### `GET /api/stats`

현재 집계 통계를 반환합니다.

**Response** `200 OK`:

```json
{
  "total": 150,
  "pass_count": 142,
  "fail_count": 8,
  "defect_rate": 5.3,
  "fps": 12.4
}
```

| 필드 | 타입 | 설명 |
|------|------|------|
| `total` | int | 총 검사 수 |
| `pass_count` | int | 양품 수 |
| `fail_count` | int | 불량 수 |
| `defect_rate` | float | 불량률 (%) |
| `fps` | float | 최근 5초 기준 C3 결과 프레임 FPS (객체 수 아님) |
| `latency` | object | `e2e`, `before_c3`, `inference`, `c3_c4` 각각 `{p50_ms, p95_ms, samples}` |
| `c2_metrics` | object? | `dropped_stale`, `dropped_old`, `c4_skipped`, `c4_error`; 미연결이면 null, 미제공 필드는 null |
| `c2_updated_at_ms` | int? | 마지막 C2 조회 성공 시각 (Unix ms) |

---

### `GET /api/inspections`

최근 검사 결과 목록을 반환합니다.

**Query Parameters:**

| 파라미터 | 타입 | 기본값 | 설명 |
|----------|------|--------|------|
| `limit` | int | 50 | 반환할 최대 건수 |

**Response** `200 OK`:

```json
[
  {
    "item_id": "snack-001",
    "trace_id": "trace-abc",
    "frame_id": 42,
    "status": "pass",
    "circularity": 0.8912,
    "area": 16900.0,
    "perimeter": 520.0,
    "defect_score": 0.02,
    "inference_latency_ms": 45.0,
    "container_id": "c3-inference-1",
    "timestamp": 1730000000.123
  }
]
```

---

### `GET /api/defects`

불량(`status: "fail"`) 항목만 필터링하여 반환합니다.

**Query Parameters:**

| 파라미터 | 타입 | 기본값 | 설명 |
|----------|------|--------|------|
| `limit` | int | 50 | 반환할 최대 건수 |

**Response**: `/api/inspections`과 동일한 형태, `status: "fail"`인 것만.

---

### `GET /api/export/csv`

검사 결과를 CSV 파일로 다운로드합니다.

**Response** `200 OK`:
- `Content-Type: text/csv`
- `Content-Disposition: attachment; filename=inspection-results.csv`

**CSV 컬럼:**

```
item_id, trace_id, frame_id, status, circularity, area, perimeter,
defect_score, inference_latency_ms, container_id, timestamp
```

---

### `GET /healthz`

컨테이너 헬스체크용 엔드포인트.

**Response** `200 OK`:

```json
{"status": "healthy"}
```

---

### `WS /ws/dashboard`

실시간 대시보드 WebSocket. 연결 즉시 현재 stats를 전송하고, 이후 detection 결과가 들어올 때마다 push합니다.

**연결:** `ws://localhost:8003/ws/dashboard`

**서버 → 클라이언트 메시지 종류:**

#### 1) `inspection_result` — detection 수신 시마다

```json
{
  "type": "inspection_result",
  "data": {
    "item_id": "snack-001",
    "status": "pass",
    "circularity": 0.8912,
    "area": 16900.0,
    "perimeter": 520.0,
    "defect_score": 0.02,
    "inference_latency_ms": 45.0,
    "container_id": "c3-inference-1",
    "frame_id": 42,
    "bbox": [100, 100, 230, 230],
    "defect_type": null
  }
}
```

#### 2) `stats` — 통계 갱신 시 (연결 직후 + 결과 수신 후 + 백그라운드 주기 갱신)

```json
{
  "type": "stats",
  "data": { /* GET /api/stats 와 동일한 구조 */ }
}
```

#### 3) `camera_frame` — 카메라 프레임 수신 시

C2 Preprocessing이 C3에 보내는 것과 동일한 리사이즈 프레임 데이터를 WebSocket으로 브라우저에 중계합니다.

**C2 → C4 전송 포맷 (`POST /api/camera/frame`):**

응답: `200 OK`, `{"status":"ok"}`. C2는 C3 요청과 같은 시점에 별도 비동기 전송하며, 응답 대기로 추론 경로를 지연시키지 않습니다.

```json
{
  "trace_id": "trace-abc-123",
  "session_id": "m-ab12cd",
  "frame_id": 297,
  "width": 576,
  "height": 1024,
  "prompts": [
    {"candidate_id": "c0", "type": "box", "bbox": [429.0, 591.0, 525.0, 681.0]},
    {"candidate_id": "c1", "type": "box", "bbox": [183.0, 584.0, 276.0, 677.0]}
  ],
  "image_b64": "/9j/4AAQ..."
}
```

| 필드 | 타입 | 필수 | 설명 |
|------|------|------|------|
| `trace_id` | string | O | 파이프라인 추적 ID (C1에서 발급) |
| `session_id` | string | O | 세션 ID (예: `m-ab12cd`) |
| `frame_id` | int | O | 프레임 번호 (순차 증가, C1이 부여) |
| `width` | int | O | C2 리사이즈 이미지 너비 (px) |
| `height` | int | O | C2 리사이즈 이미지 높이 (px) |
| `prompts` | array | O | SAM2 프롬프트 목록 — 객체 후보별 bbox |
| `prompts[].candidate_id` | string | O | 후보 객체 ID (예: `c0`, `c1`) |
| `prompts[].type` | string | O | 프롬프트 유형 (`box`) |
| `prompts[].bbox` | float[4] | O | `[x1, y1, x2, y2]` 픽셀 좌표 |
| `image_b64` | string | O | JPEG 이미지의 base64 인코딩 (`data:` 접두사 없이 raw base64) |

**WebSocket broadcast 포맷 (서버 → 브라우저):**

```json
{
  "type": "camera_frame",
  "data": {
    "trace_id": "trace-abc-123",
    "session_id": "m-ab12cd",
    "frame_id": 297,
    "width": 576,
    "height": 1024,
    "frame": "/9j/4AAQ...",
    "prompts": [
      {"candidate_id": "c0", "type": "box", "bbox": [429.0, 591.0, 525.0, 681.0]},
      {"candidate_id": "c1", "type": "box", "bbox": [183.0, 584.0, 276.0, 677.0]}
    ]
  }
}
```

| 필드 | 타입 | 설명 |
|------|------|------|
| `frame_id` | int | 프레임 번호 |
| `width` | int | 이미지 너비 (px) |
| `height` | int | 이미지 높이 (px) |
| `frame` | string | JPEG base64 문자열 — `data:image/jpeg;base64,` 접두사 포함/미포함 모두 허용 (프론트엔드에서 자동 보정) |
| `prompts` | array | 객체 후보 bbox 목록 (C2가 만든 후보 목록 그대로 전달) |

**프론트엔드 처리 흐름:**

`camera_frame`과 `frame_result`를 최대 30개 버퍼에서 매칭 → 이미지 디코딩 → 동일 프레임의 이미지/bbox/mask를 함께 갱신합니다. 결과가 먼저 도착해도 매칭할 수 있습니다. 매칭 전에는 첫 카메라 이미지만 미리 표시합니다.

#### 4) `frame_result` — 프레임의 모든 detection 처리 후

```json
{
  "type": "frame_result",
  "data": {
    "trace_id": "trace-abc-123",
    "session_id": "m-ab12cd",
    "frame_id": 297,
    "detections": [
      {
        "item_id": "c0", "bbox": [429, 591, 525, 681],
        "status": "fail", "circularity": 0.78,
        "mask": {"encoding": "rle", "width": 576, "height": 1024,
                 "data": {"order": "row-major", "counts": [589824]}}
      }
    ]
  }
}
```

위 counts는 형식을 설명하기 위한 빈 마스크 예시입니다. `inspection_result`는 기존 검사 피드용으로 유지하며 `trace_id`, `session_id`도 전달합니다. 마스크는 중복 전송하지 않고 `frame_result`에 포함합니다.

| 구성요소 | 상태 | 설명 |
|----------|------|------|
| 프론트엔드 | **구현 완료** | 이미지/결과 매칭, RLE 마스크 + bbox 렌더링 |
| 백엔드 중계 (`main.py`) | **구현 완료** | C2 프레임 수신 → WebSocket broadcast |
| C2 → C4 송신 | **C2 담당** | 동일 리사이즈 이미지와 prompts를 비동기 전송 |

---

## 아키텍처

```
C3 Inference ──POST /results──▶ C4 Visualizer ──WebSocket──▶ Browser Dashboard
                                     │
                                     ├── GET /              (메인 대시보드)
                                     ├── GET /viewer        (불량 확인 뷰어)
                                     ├── GET /api/stats     (통계 JSON)
                                     ├── GET /api/inspections (검사 이력)
                                     ├── GET /api/defects   (불량 목록)
                                     ├── GET /api/export/csv (CSV 다운로드)
                                     └── GET /healthz       (헬스체크)
```

## 폴더 구조

```
services/c4-visualizer/
├── Dockerfile              # Python 3.11-slim 기반 컨테이너
├── requirements.txt        # FastAPI + Uvicorn + Pydantic
├── README.md               # 이 문서
├── src/
│   ├── __init__.py
│   ├── main.py             # FastAPI 앱 (엔드포인트, WebSocket, 판정 로직)
│   ├── models.py           # DetectionResult / Detection Pydantic 모델
│   └── store.py            # 인메모리 저장소 (검사 이력, 통계, CSV export)
└── static/
    ├── index.html           # 메인 대시보드 UI (실시간 차트, bbox overlay, 검사 피드)
    └── viewer.html          # 불량 상세 뷰어 UI (테이블, 상세 패널)
```

---

## Docker로 실행하기

### 사전 준비

- **Docker Desktop** 설치 (Windows/Mac) 또는 **Docker Engine** (Linux)
- **Docker Compose** v2 이상 (`docker compose version`으로 확인)

### 방법 1: C4만 단독 실행 (가장 간단)

프로젝트 루트에서:

```bash
docker compose up c4-visualizer --build
```

또는 `services/c4-visualizer/` 폴더에서 직접:

```bash
docker build -t c4-visualizer .
docker run -p 8003:8003 c4-visualizer
```

실행 후 브라우저에서 **http://localhost:8003** 접속.

### 방법 2: 전체 파이프라인 실행

```bash
docker compose up --build
```

> C1~C3는 아직 placeholder(`sleep infinity`)이므로 C4만 실제로 동작합니다.

### 방법 3: Scale-out 프로필 (C3 3대)

```bash
docker compose --profile scale-out up --build
```

---

## 대시보드 페이지

| URL | 설명 |
|-----|------|
| `http://localhost:8003/` | 메인 대시보드 — 실시간 검사 통계, 카메라 피드 + bbox overlay, 원형도 분포 차트 |
| `http://localhost:8003/viewer` | 불량 확인 뷰어 — 불량 항목 테이블, 클릭 시 상세 패널 (원형도 게이지, 파이프라인 정보) |

### 메인 대시보드 구성요소

| 영역 | 표시 항목 |
|------|-----------|
| **상단 통계 카드 (4개)** | 총 검사, 양품, 불량, 불량률 |
| **성능 카드** | 프레임 FPS, 지연 P50/P95, C2 드롭, 화면용 이미지 전송 누락 |
| **카메라 피드** | 원본 이미지 + canvas bbox overlay (pass=녹색, fail=빨간색) |
| **검사 결과 피드** | 최근 60건 실시간 스크롤 목록 (아이템 ID, 원형도) |
| **양품률 도넛 차트** | SVG 기반 pass/fail 비율 시각화 |
| **원형도 분포 차트** | 20구간 히스토그램 + 0.85 임계선 |

---

## 테스트 데이터 보내보기

C4가 실행 중일 때 터미널에서 아래 curl로 테스트 데이터를 보내볼 수 있습니다:

### 정상 과자 (원형도 높음)

```bash
curl -X POST http://localhost:8003/results \
  -H "Content-Type: application/json" \
  -d '{
    "schema_version": "1.0.0",
    "trace_id": "test-trace-001",
    "session_id": "test-session",
    "frame_id": 1,
    "captured_at_ms": 1730000000000,
    "inference_container_id": "c3-inference-1",
    "inference_started_at_ms": 1730000000050,
    "inference_finished_at_ms": 1730000000100,
    "inference_latency_ms": 50.0,
    "detections": [
      {
        "object_id": "snack-001",
        "bbox": [100, 100, 200, 200],
        "status": "normal",
        "defect_type": null,
        "defect_score": 0.05,
        "mask": null
      }
    ]
  }'
```

### 불량 과자 (원형도 낮음 — 길쭉한 bbox)

```bash
curl -X POST http://localhost:8003/results \
  -H "Content-Type: application/json" \
  -d '{
    "schema_version": "1.0.0",
    "trace_id": "test-trace-002",
    "session_id": "test-session",
    "frame_id": 2,
    "captured_at_ms": 1730000001000,
    "inference_container_id": "c3-inference-1",
    "inference_started_at_ms": 1730000001050,
    "inference_finished_at_ms": 1730000001120,
    "inference_latency_ms": 70.0,
    "detections": [
      {
        "object_id": "snack-002",
        "bbox": [100, 100, 300, 120],
        "status": "defect",
        "defect_type": "crack",
        "defect_score": 0.92,
        "mask": null
      }
    ]
  }'
```

> **Windows PowerShell**에서는 `curl` 대신 아래처럼 사용:
> ```powershell
> Invoke-RestMethod -Uri http://localhost:8003/results -Method Post -ContentType "application/json" -Body '{ ... }'
> ```
> 또는 Git Bash에서 위의 curl 명령을 그대로 사용할 수 있습니다.

---

## 환경 변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `C4_PORT` | `8003` | 호스트에 노출할 포트 (docker-compose) |
| `SERVICE_NAME` | `c4-visualizer` | 서비스 식별 이름 |

## 컨테이너 상세

| 항목 | 값 |
|------|-----|
| Base image | `python:3.11-slim` |
| Working dir | `/app` |
| Exposed port | `8003` |
| Entrypoint | `uvicorn src.main:app --host 0.0.0.0 --port 8003` |
| Healthcheck | `GET /healthz` (10초 간격, 3초 timeout, 3회 retry) |
| Max records | 500건 (인메모리, 컨테이너 재시작 시 초기화) |

---

## 개발 모드 (Docker 없이)

Docker 없이 로컬에서 바로 실행할 수도 있습니다:

```bash
cd services/c4-visualizer
pip install -r requirements.txt
uvicorn src.main:app --host 0.0.0.0 --port 8003 --reload
```

`--reload` 옵션으로 코드 수정 시 자동 재시작됩니다.

---

## 트러블슈팅

| 증상 | 원인 | 해결 |
|------|------|------|
| 브라우저에서 "연결 끊김" 표시 | WebSocket 미연결 | C4 서버가 실행 중인지 확인, 포트 8003 확인 |
| curl 후 대시보드에 안 뜸 | 브라우저 탭이 WS 연결 전 | 브라우저를 먼저 열고 WS 연결(연결됨) 확인 후 curl |
| `docker compose up` 에러 | Docker Desktop 미실행 | Docker Desktop 실행 후 재시도 |
| port 8003 already in use | 다른 프로세스가 포트 사용 중 | `.env`에서 `C4_PORT=8004` 등으로 변경 |
| CSV 다운로드가 빈 파일 | 아직 detection 결과가 없음 | curl로 테스트 데이터를 먼저 전송 |
| bbox overlay가 안 보임 | 카메라 이미지 없이 좌표만 수신 | Open Issue #1 — 프레임 수신 경로 구현 후 표시 |


## C4 회귀 테스트

프로젝트 루트에서 프론트엔드 RLE/프레임 매칭 테스트:

```bash
node --test services/c4-visualizer/tests/test_dashboard.cjs
```

C4 Python 의존성이 설치된 환경에서 수신 핸들러/이벤트 테스트:

```bash
cd services/c4-visualizer
python -m unittest discover -s tests -p 'test_*.py' -v
```
