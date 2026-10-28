# C2 Preprocessing

**Owner:** Member B

## 역할
- OpenCV 기반 resize, ROI crop, 정규화 등 전처리
- 과자 후보 bbox 생성 → SAM2 prompt(`prompts[]`)로 변환
- `processed-frame` 메시지를 Nginx Inference LB(`/infer`)로 전달

## Interface (예정)
| 방향 | Endpoint | Payload |
|---|---|---|
| C1 → C2 | `POST /preprocess` (TBD) | [`frame-envelope.schema.json`](../../shared/schemas/frame-envelope.schema.json) |
| C2 → Nginx | `POST http://nginx-inference-lb:8080/infer` | [`processed-frame.schema.json`](../../shared/schemas/processed-frame.schema.json) |
| — | `GET /healthz` | — |

Header 전파: `X-Trace-Id`, `X-Frame-Id`, `X-Session-Id`

## TODO
- [ ] Resize / ROI crop 파라미터 확정 (`.env`의 `C2_*`)
- [ ] 후보 bbox 생성 방식 결정 (배경 차분 / contour / 고정 그리드 등)
- [ ] bbox 좌표계 확정: **전처리 후 이미지 기준 `[x1, y1, x2, y2]` pixel**
- [ ] `preprocess_started_at_ms` / `preprocess_finished_at_ms` 기록
- [ ] Docker `command`를 실제 entrypoint로 교체
