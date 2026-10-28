# C4 Visualizer

**Owner:** Member D
**Port:** 8003

과자 크랙 검사 결과를 실시간으로 시각화하는 대시보드 서비스.

**역할:**
- Detection 결과 실시간 표시 (WebSocket push)
- 영상 위에 mask / bbox / 불량 마킹 overlay (TODO)
- 모니터링: Processed FPS, latency (E2E P50/P95), defect count
- End-to-End latency 계산 (`now - captured_at_ms`)

---

## 구현 현황

- [x] Dashboard 프레임워크 선정 → FastAPI + vanilla JS (WebSocket 실시간)
- [x] Docker `command`를 실제 entrypoint로 교체 → `uvicorn src.main:app`
- [x] 실시간 통계 대시보드 (총 검사/양품/불량/불량률/FPS/Latency P50·P95)
- [x] 불량 확인 뷰어 (`/viewer`) — 테이블 + 상세 패널
- [x] 원형도(Circularity) 기반 양품/불량 판정
- [x] WebSocket 브로드캐스트 (`/ws/dashboard`)
- [x] Overlay 렌더링 — canvas 기반 bbox overlay (pass/fail 색상 구분, 라벨 표시, fade-out)
- [x] 구간별 Latency 모니터링 — E2E / Inference(C3) / Network 각각 P50·P95 표시
- [x] frame drop 감지 및 모니터링 — `frame_id` 갭 감지, 대시보드 카드 + 서버 로그
- [x] 메트릭 집계 및 CSV export — `GET /api/export/csv` 엔드포인트 + 대시보드 다운로드 버튼

## Open Issues

1. **원본 프레임 수신 경로 미정** — Overlay를 그리려면 원본 이미지가 필요.
   현재 후보: (a) C1이 C4로 프레임 미러링, (b) detection-result에 frame 참조 URL 포함
2. **C3 replica 결과 순서 문제** — C3가 여러 대일 때 결과가 `frame_id` 순서와 다르게 도착할 수 있음.
   → `frame_id` 기준 재정렬 또는 late frame drop 정책 필요

---

## Interface

| 방향 | Endpoint | Payload / 설명 |
|------|----------|----------------|
| C3 → C4 | `POST /results` | [`detection-result.schema.json`](../../shared/schemas/detection-result.schema.json) |
| Browser → C4 | `GET /` | 메인 대시보드 (port 8003) |
| Browser → C4 | `GET /viewer` | 불량 확인 뷰어 |
| Browser → C4 | `WS /ws/dashboard` | 실시간 WebSocket (inspection_result, stats, camera_frame) |
| Browser → C4 | `GET /api/stats` | 통계 JSON |
| Browser → C4 | `GET /api/inspections` | 검사 이력 |
| Browser → C4 | `GET /api/defects` | 불량 목록 |
| Browser → C4 | `GET /api/export/csv` | 검사 결과 CSV 다운로드 |
| — | `GET /healthz` | 헬스체크 |

## 아키텍처

```
C3 Inference ──POST /results──▶ C4 Visualizer ──WebSocket──▶ Browser Dashboard
                                     │
                                     ├── GET /           (메인 대시보드)
                                     ├── GET /viewer     (불량 확인 뷰어)
                                     ├── GET /api/stats  (통계 JSON)
                                     ├── GET /api/inspections (검사 이력)
                                     ├── GET /api/defects    (불량 목록)
                                     └── GET /healthz        (헬스체크)
```

## 폴더 구조

```
services/c4-visualizer/
├── Dockerfile              # Python 3.11-slim 기반 컨테이너
├── requirements.txt        # FastAPI + Uvicorn + Pydantic
├── README.md               # 이 문서
├── src/
│   ├── __init__.py
│   ├── main.py             # FastAPI 앱, 엔드포인트, WebSocket 브로드캐스트
│   ├── models.py           # DetectionResult Pydantic 모델
│   └── store.py            # 인메모리 검사 결과 저장소 + 통계
└── static/
    ├── index.html           # 메인 대시보드 UI
    └── viewer.html          # 불량 상세 뷰어 UI
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
| `http://localhost:8003/` | 메인 대시보드 — 실시간 검사 통계, 카메라 피드, 원형도 분포 차트 |
| `http://localhost:8003/viewer` | 불량 확인 뷰어 — 불량 항목 테이블, 클릭 시 상세 패널 |

## API 엔드포인트

| Method | Path | 설명 |
|--------|------|------|
| `POST` | `/results` | C3에서 detection 결과 수신 (DetectionResult JSON) |
| `GET` | `/api/stats` | 현재 통계 (총 검사, 양품/불량, 불량률, FPS, latency) |
| `GET` | `/api/inspections?limit=50` | 최근 검사 결과 목록 |
| `GET` | `/api/defects?limit=50` | 불량 항목만 필터링 |
| `GET` | `/api/export/csv` | 검사 결과 CSV 다운로드 |
| `GET` | `/healthz` | 헬스체크 (`{"status": "healthy"}`) |
| `WS` | `/ws/dashboard` | 실시간 WebSocket (inspection_result, stats, camera_frame 이벤트) |

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

## 핵심 로직: 원형도(Circularity) 판정

과자의 양품/불량은 **원형도** 기준으로 판정합니다:

```
Circularity = 4π × Area / Perimeter²
```

- **Circularity >= 0.85** → 양품 (pass)
- **Circularity < 0.85** → 불량 (fail)

> 정사각형 bbox: Area=10000, Perimeter=400 → Circularity ≈ 0.785 (불량)
> 정원에 가까운 형태일수록 1.0에 가까워짐

임계값은 `main.py`의 `CIRCULARITY_THRESHOLD = 0.85`에서 변경 가능.

---

## 환경 변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `C4_PORT` | `8003` | 호스트에 노출할 포트 (docker-compose) |
| `SERVICE_NAME` | `c4-visualizer` | 서비스 식별 이름 |

---

## 컨테이너 상세

| 항목 | 값 |
|------|-----|
| Base image | `python:3.11-slim` |
| Working dir | `/app` |
| Exposed port | `8003` |
| Entrypoint | `uvicorn src.main:app --host 0.0.0.0 --port 8003` |
| Healthcheck | `GET /healthz` (10초 간격) |

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
