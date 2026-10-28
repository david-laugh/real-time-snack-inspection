# C4 Visualizer

**Owner:** Member D

## 역할
- Detection 결과 실시간 표시
- 영상 위에 mask / bbox / 불량 마킹 overlay
- 모니터링: Processed FPS, latency(C1/C2/C3/E2E, P50/P95), defect count, frame drop
- End-to-End latency 계산 지점 (`now - captured_at_ms`)

## Interface (예정)
| 방향 | Endpoint | Payload |
|---|---|---|
| C3 → C4 | `POST /results` (TBD) | [`detection-result.schema.json`](../../shared/schemas/detection-result.schema.json) |
| Browser → C4 | `GET /` (dashboard, port 8003) | — |
| — | `GET /healthz` | — |

## Open issues
- Overlay를 그릴 원본 프레임 수신 경로 미정: (a) C1이 C4로 프레임 미러링, (b) detection-result에 frame 참조 포함 등
- C3 replica가 여러 개일 때 결과 순서가 뒤바뀔 수 있음 → `frame_id` 기준 재정렬 / late frame drop 정책 필요

## TODO
- [ ] Dashboard 프레임워크 선정
- [ ] Overlay 렌더링
- [ ] 메트릭 집계 및 `benchmark/results/`로 CSV export
- [ ] Docker `command`를 실제 entrypoint로 교체
