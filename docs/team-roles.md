# Team Roles

| Member | 담당 | 주요 디렉터리 |
|---|---|---|
| **Member A** | C1 Gateway + Docker Compose + Integration | `services/c1-gateway/`, `docker-compose.yml`, `.env.example` |
| **Member B** | C2 Preprocessing + Simulator | `services/c2-preprocessing/`, `clients/simulator/` |
| **Member C** | C3 SAM2 Inference + Nginx Scale-out + Benchmark | `services/c3-inference/`, `infra/nginx/`, `benchmark/` |
| **Member D** | C4 Visualizer + Monitoring | `services/c4-visualizer/` |

## Member A — C1 Gateway + Docker Compose + Integration
- FastAPI WebSocket 기반 30fps 스트림 수신
- `trace_id`, `session_id`, `frame_id`, timestamp 생성
- `docker-compose.yml`, `.env.example` 관리
- 전체 서비스 통합 테스트 및 연동 이슈 조율

## Member B — C2 Preprocessing + Simulator
- OpenCV resize / ROI crop / 전처리
- 과자 후보 bbox 생성 → SAM2 prompt 변환
- 개발·벤치마크용 스트림 Simulator (`clients/simulator`)

## Member C — C3 SAM2 Inference + Nginx Scale-out + Benchmark
- SAM2 video segmentation / tracking, defect 판단
- PyTorch + CUDA 컨테이너 환경, 모델 checkpoint 관리 (git 미포함)
- Nginx inference LB 및 replica Scale-out 구성
- 병목 실험 설계·실행 (`docs/bottleneck-test-plan.md`)

## Member D — C4 Visualizer + Monitoring
- mask / bbox / 불량 마킹 실시간 overlay
- FPS, latency(P50/P95), defect count, frame drop 모니터링 대시보드
- 메트릭 CSV export (`benchmark/results/`)

## 공동 책임
- `shared/schemas/` API Contract: 변경 시 producer/consumer owner 모두 리뷰
- `docs/` 문서 최신화

## 협업 규칙 (제안)
- Branch: `feature/<c1|c2|c3|c4|infra|docs>-<topic>`
- `main` 직접 push 금지, PR + 최소 1인 리뷰
- 서비스 간 의존은 스키마로만 연결 (다른 서비스 코드 직접 import 금지)
