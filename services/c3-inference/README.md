# C3 SAM2 Inference

**Owner:** Member C

## 역할
- SAM2 video segmentation / tracking (GPU 사용 예정)
- segmentation mask 기반 defect 판단 (`status`, `defect_type`, `defect_score`)
- Nginx 뒤에서 여러 replica(`c3-inference-1..3`)로 Scale-out
- `detection-result` 메시지를 C4 Visualizer로 전달

## Interface (예정)
| 방향 | Endpoint | Payload |
|---|---|---|
| Nginx → C3 | `POST /infer` (port 8002) | [`processed-frame.schema.json`](../../shared/schemas/processed-frame.schema.json) |
| C3 → C4 | `POST http://c4-visualizer:8003/results` (TBD) | [`detection-result.schema.json`](../../shared/schemas/detection-result.schema.json) |
| — | `GET /healthz` | — |

`inference_container_id`는 환경변수 `INFERENCE_CONTAINER_ID`(compose에서 주입)를 사용한다.

## Model weights
- Checkpoint는 **repository에 포함하지 않는다** (`.gitignore`: `models/*.pt`, `models/*.pth`, `checkpoints/`).
- 로컬에서 `services/c3-inference/models/`에 직접 배치 → 컨테이너 `/app/models`에 read-only mount.
- 사용할 SAM2 variant와 checkpoint 파일명은 `.env`의 `SAM2_MODEL_CFG`, `SAM2_CHECKPOINT_PATH`로 지정.

## Open issues
- **Session state vs. round-robin:** SAM2 video tracking은 세션별 memory state를 가진다.
  round-robin 시 같은 세션의 프레임이 다른 replica로 갈 수 있음 →
  (a) `session_id` 기반 sticky routing, (b) stateless per-frame 추론, (c) 외부 state 공유 중 결정 필요.

## TODO
- [ ] PyTorch + CUDA base image 확정, SAM2 설치
- [ ] SAM2 predictor 로딩 / prompt(bbox) 기반 추론
- [ ] Defect 판단 로직 및 `defect_type` 목록 확정
- [ ] Mask 인코딩 방식 확정 (RLE / polygon / PNG base64)
- [ ] GPU utilization / VRAM 메트릭 수집
- [ ] docker-compose GPU reservation 활성화
