# C1 Gateway

**Owner:** Member A

## 역할
- Smartphone / Simulator로부터 실시간 30fps 영상 스트림 수신 (FastAPI WebSocket 예정)
- 프레임마다 `trace_id`, `session_id`, `frame_id`, `captured_at_ms`, `gateway_received_at_ms` 부여
- `frame-envelope` 메시지를 C2 Preprocessing으로 전달
- 입력 FPS / frame drop 측정 지점

## Interface (예정)
| 방향 | Endpoint | Payload |
|---|---|---|
| Client → C1 | `WS /ws/stream` | 인코딩된 프레임 (JPEG 등, TBD) |
| C1 → C2 | `POST http://c2-preprocessing:8001/preprocess` (TBD) | [`frame-envelope.schema.json`](../../shared/schemas/frame-envelope.schema.json) |
| — | `GET /healthz` | — |

## TODO
- [ ] FastAPI WebSocket 수신 엔드포인트
- [ ] `trace_id` / `frame_id` 생성 규칙 확정 (UUID v4 / 세션 내 단조 증가 정수)
- [ ] Backpressure 정책 (C2가 느릴 때 drop 정책 / queue 크기)
- [ ] 입력 FPS, drop count 메트릭 노출
- [ ] Docker `command`를 실제 entrypoint로 교체
