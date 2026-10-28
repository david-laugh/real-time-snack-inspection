# C3 SAM2 Inference 구현 정리

| 항목 | 내용 |
|---|---|
| 작성일 | 2026-10-28 |
| 브랜치 | `feature/inference` |
| 담당 | [이규혁](https://github.com/david-laugh) |
| 범위 | `services/c3-inference/` 신규 구현. compose와 `.env.example`은 C3 placeholder만 최소 수정 |
| 관련 문서 | [C3 README](../services/c3-inference/README.md) · [C3 Contract Notes](../services/c3-inference/docs/contract-notes.md) · [검증 보고서](c3-inference-validation-report.md) |

---

## 1. 목표와 결정 사항

C2가 보내는 `processed-frame`(이미지 + bbox prompt)을 받아 **SAM2로 과자를 분할**하고, **mask 기반으로 불량을 판정**해서 `detection-result`를 반환하는 C3 서비스를 구현했다.

| 결정 | 선택 | 이유 |
|---|---|---|
| 추론 방식 | **A안: `SAM2ImagePredictor` 프레임 독립 추론** | 세션 state(`inference_state`, tracking memory)가 없다. 어떤 replica든 어떤 프레임이든 처리할 수 있으므로 **Nginx round-robin과 충돌하지 않는다** |
| 모델 | SAM2.1 Hiera **Tiny** (`configs/sam2.1/sam2.1_hiera_t.yaml`) | 실시간(30fps 목표) 지향. 환경변수로 다른 variant로 바꿀 수 있다 |
| 서버 | FastAPI + Uvicorn (worker 1개) | worker마다 모델이 따로 로드되므로, 확장은 replica 수로 한다 |
| 이미지 전송 | 스키마에 이미 있는 `image_b64`(base64 JPEG) 사용 | 스키마를 바꾸지 않고 C2와 충돌하지 않는 방식 |

Video Predictor(B안, 세션 tracking)는 이번 범위에서 제외했다. 나중에 전환한다면 Nginx에 `hash $http_x_session_id consistent;` 같은 session affinity가 필요하다.

---

## 2. 작업 전 확인한 계약 불일치와 처리 방안

스키마 파일(`shared/schemas/`)이 단일 진실 원천이므로 **스키마는 바꾸지 않고** 아래처럼 해석했다. 자세한 내용은 [contract-notes](../services/c3-inference/docs/contract-notes.md)에 있다.

| # | 불일치 | 처리 방안 |
|---|---|---|
| 1 | 요구사항은 `NORMAL/DEFECT/UNKNOWN`, 스키마의 `status` enum은 소문자 | 내부 enum은 대문자 의미로 두고, **wire 값은 스키마의 소문자 그대로** 보낸다. `defect_type`은 `broken` / `spot_defect` |
| 2 | `DetectionResult`가 `additionalProperties: false`라서 지표 필드를 body에 넣을 수 없음 | 지표는 **응답 header**(`X-C3-*-Ms`, `Server-Timing`)와 **`GET /metrics`**로 제공한다 |
| 3 | `image_b64`가 optional이고 전송 방식이 TODO 상태 | C3는 필수로 요구한다(없으면 422). 스키마를 required로 바꾸는 것은 **C2 owner와 합의가 필요한 제안**으로 남겼다 |
| 4 | `object_id`는 "SAM2 tracking id"로 정의되어 있지만 A안은 tracking을 하지 않음 | `object_id = candidate_id`. 프레임 간 동일성은 C2 쪽 책임 **(합의 필요)** |
| 5 | mask encoding이 TODO 상태 | `rle`(row-major, 0의 run부터 시작하는 uncompressed counts)로 정하고 형식을 문서화했다 |
| 6 | C3 → C4 `POST /results`가 TBD 상태 | 이번에는 `/infer` **응답**으로만 반환한다. 전달 주체는 미결정 |
| 7 | Nginx `proxy_next_upstream http_503` 설정 | POST는 non-idempotent라서 다른 replica로 재시도되지 않는다. 모델 로딩 중인 replica의 503이 C2까지 전달된다(Nginx는 수정하지 않음) |
| 8 | compose의 C3 `command: sleep infinity`, `.env.example`의 `SAM2_MODEL_CFG=TODO` | 둘 다 `TODO(Member C)` placeholder이므로 **이 부분만** 실제 값으로 교체했다 |

---

## 3. API

| Method | Path | 설명 |
|---|---|---|
| `POST` | `/infer` | `processed-frame`을 받아 `detection-result`를 반환 |
| `GET` | `/healthz` | 모델이 준비되면 `200`, `loading`/`error`/`not_started` 상태면 `503`. device, AMP, `model_load_ms`, `warmup_ms`, error를 포함 |
| `GET` | `/metrics` | `processed_frame_count`, `error_count`(code별), stage별 latency(mean/p50/p95/max), 모델 정보, GPU 메모리 |

**Trace header**
- `X-Trace-Id`, `X-Frame-Id`, `X-Session-Id`를 받는다. body를 파싱할 수 있으면 **body 값이 기준**이고, 응답 header로 echo한다.
- header와 body 값이 다르면 경고 로그를 남기고 `trace_header_mismatch_count`를 증가시킨다.
- body 파싱에 실패하면 error 응답에 요청 header 값을 그대로 넣는다.

**응답 header (프레임별 지표)**
`X-C3-Decode-Ms`, `X-C3-Encoder-Ms`, `X-C3-Segmentation-Ms`, `X-C3-Defect-Analysis-Ms`, `X-C3-Total-Inference-Ms`, `X-Inference-Container-Id`, `Server-Timing`

**에러 코드**

| HTTP | code |
|---|---|
| 422 | `invalid_request`, `missing_image`, `image_decode_failed`, `image_size_mismatch`, `invalid_bbox`, `duplicate_candidate_id`, `too_many_prompts`, `unsupported_schema_version` |
| 503 | `model_not_ready` (`Retry-After: 1`) |
| 500 | `internal_error` |

**bbox 처리 규칙**
- 값이 finite가 아니거나 `x1>=x2` / `y1>=y2`이면 422.
- 이미지 밖으로 일부 나간 bbox는 경계로 clip한다. 완전히 밖에 있으면 422.
- `prompts`가 비어 있으면 `200`과 `detections: []`를 반환하고 encoder는 실행하지 않는다.

---

## 4. 처리 파이프라인과 모듈 구조

```
POST /infer
  │ 1. Request validation ── pydantic(extra=forbid) + bbox/이미지 검증
  │ 2. JPEG decode ───────── decode_ms
  │ 3. SAM2 image embedding  encoder_ms        (프레임당 1회)
  │ 4. bbox segmentation ─── segmentation_ms   (전체 bbox를 batch로)
  │ 5. Defect analysis ───── defect_analysis_ms
  │ 6. RLE + 직렬화 ──────── mask_encode_ms
  ▼                          total_inference_ms (= inference_latency_ms)
DetectionResult JSON
```

```
services/c3-inference/
├── src/
│   ├── app.py              FastAPI, 백그라운드 모델 로드, /infer /healthz /metrics
│   ├── pipeline.py         1~6단계, 단계별 timing, InferenceError → HTTP 매핑
│   ├── predictor/
│   │   ├── base.py         SegmentationPredictor 인터페이스 (set_image / predict_boxes)
│   │   ├── sam2_predictor.py 실제 SAM2 backend
│   │   └── mock_predictor.py Mock backend (torch 불필요)
│   ├── defects.py          불량 판정 규칙 (SAM2와 독립)
│   ├── mask_codec.py       RLE encode/decode
│   ├── metrics.py          카운터 + rolling latency window
│   ├── schemas.py          shared/schemas의 pydantic mirror
│   └── config.py           환경변수 → Settings / DefectThresholds
├── tests/                  pytest (mock API, 불량 규칙, 실제 SAM2)
├── scripts/                download_checkpoint.py, send_frames.py
├── docs/contract-notes.md
├── Dockerfile, compose.gpu.yml, requirements*.txt, pytest.ini
```

**SAM2와 불량 판정을 분리한 방식**
- 파이프라인은 `SegmentationPredictor` 인터페이스만 사용한다. 실제 SAM2와 Mock은 `C3_PREDICTOR=sam2|mock`만 바꾸면 교체된다.
- `defects.py`는 RGB 이미지, mask, mask score만 입력으로 받으므로 SAM2 없이 단위 테스트할 수 있다.

---

## 5. 성능 최적화

| 요구사항 | 구현 |
|---|---|
| 모델 1회 로드 | FastAPI lifespan에서 **백그라운드 스레드로 1회 로드**한다. 로딩 중에도 `/healthz`가 `loading`으로 응답한다 |
| CPU/CUDA 선택 | `C3_DEVICE=auto\|cpu\|cuda\|cuda:N`. `cuda`를 지정했는데 GPU가 없으면 `/healthz`가 `error` 상태가 된다 |
| SAM2.1 Tiny 기본 | `SAM2_MODEL_CFG`, `SAM2_CHECKPOINT_PATH` 기본값 |
| Warm-up | `C3_WARMUP_ITERS`(기본 2)만큼 `C3_WARMUP_IMAGE_SIZE`(1024) 더미 이미지로 encoder와 decoder를 실행한다 |
| Embedding 1회 | `set_image()`를 프레임당 한 번만 호출한다(Mock 호출 횟수로 테스트에서 검증) |
| 다중 bbox 효율 | 모든 bbox를 `C3_BOX_BATCH_SIZE`(16) 단위로 **한 번의 decoder 호출**로 처리한다. mask는 GPU에서 threshold를 적용한 뒤 **bool로 전송**한다(float32 대비 1/4) |
| `torch.inference_mode()` | encoder와 decoder 모두 적용 |
| Mixed precision | `C3_AMP_DTYPE=auto\|none\|bfloat16\|float16`(CUDA 전용). `auto`면 bf16 지원 GPU에서 bf16. Ampere 이상이면 TF32도 활성화 |
| 정확한 timing | encoder와 segmentation 후에 `torch.cuda.synchronize()`를 호출해서 GPU 실행 시간이 포함되도록 한다 |
| CPU 후처리 | 불량 분석은 mask 외접 영역만 crop해서 계산한다. 전체 프레임 scan은 OpenCV SIMD(`countNonZero`, `boundingRect`)로 처리한다 |

스레드 안전: `SAM2ImagePredictor`가 현재 embedding을 인스턴스 상태로 들고 있다. 그래서 `set_image`부터 `predict_boxes`까지를 lock으로 직렬화하고, 대기 시간을 `Server-Timing`의 `lock_wait`로 노출한다.

---

## 6. 불량 판정 규칙 (`src/defects.py`)

| 결과 | 조건 (기본 임계값) |
|---|---|
| `unknown` | SAM2 mask score < `0.5`, mask 면적 < `100px`, 또는 외곽선 없음 |
| `defect` / `broken` | 원형도(4πA/P²) < `0.70`, solidity(A/convex hull) < `0.90`, 종횡비(minAreaRect 단변/장변) < `0.75`, 의미 있는 조각(면적 5% 이상) > 1개, (설정 시) 기대 면적 대비 편차 > `±25%` |
| `defect` / `spot_defect` | 외곽선 내부를 채운 뒤 3px 침식한 영역에서 Lab L < median(L) − `50`인 픽셀(3px opening 적용) 비율 > `3%` |
| `normal` | 위 조건에 하나도 해당하지 않음 |

**`defect_score` 정의**
- 규칙마다 측정값을 severity로 바꾼다. 이상적인 값은 0, 임계값에 정확히 걸리면 0.5, 크게 벗어나면 1이다.
- `defect_score`는 그중 최댓값이다. 따라서 `defect ⇔ defect_score > 0.5`이고, `unknown`이면 `0.0`이다.
- **SAM2 mask score(predicted IoU)는 `unknown` 판정에만 쓰며 `defect_score`와 섞지 않는다.** mask score가 달라도 defect_score가 같은지 테스트로 검증했다.

모든 임계값은 `C3_MIN_MASK_*`, `C3_BROKEN_*`, `C3_SPOT_*` 환경변수로 조정한다. 전체 목록은 [C3 README](../services/c3-inference/README.md#환경변수)에 있다.

---

## 7. Docker / 배포

| 항목 | 내용 |
|---|---|
| Base image | `pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime` (RTX 50xx / sm_120 지원) |
| SAM2 설치 | GitHub commit `2b90b9f5ceec907a1c18123530e92e794ad901a4`을 고정해서 설치(`SAM2_REF` build arg). 패키지명은 `SAM-2`. `SAM2_BUILD_CUDA=0`, `--no-build-isolation --no-deps` |
| Checkpoint | 이미지에 포함하지 않는다. `services/c3-inference/models/` → `/app/models`(ro) mount. 다운로드는 `scripts/download_checkpoint.py` |
| Healthcheck | Dockerfile `HEALTHCHECK`에서 `/healthz` 호출(start-period 120s) |
| GPU | `services/c3-inference/compose.gpu.yml` override로 모든 replica에 GPU 0번을 할당 |
| `.dockerignore` | `.venv/`, `models/`, 캐시 제외 |

**docker-compose / `.env.example` 변경 (C3 placeholder만)**
- `x-c3-inference.command`를 `["sleep","infinity"]`에서 `["python","-m","src"]`로 바꿨다. 서비스명(`c3-inference-1..3`), 포트 8002, Nginx upstream은 그대로다.
- `.env.example`의 C3 섹션은 `C3_PREDICTOR=sam2`, SAM2.1 tiny cfg/checkpoint, `C3_DEVICE=auto`, `C3_AMP_DTYPE=auto`로 채웠다.

**실행**
```bash
python services/c3-inference/scripts/download_checkpoint.py
cp .env.example .env
docker compose -f docker-compose.yml -f services/c3-inference/compose.gpu.yml up -d --build
docker compose -f docker-compose.yml -f services/c3-inference/compose.gpu.yml --profile scale-out up -d   # scale-out
python services/c3-inference/scripts/send_frames.py --url http://localhost:8080/infer -n 30 --boxes 4
```

**⚠️ 단일 GPU 다중 replica**
- replica가 GPU 연산을 time-slicing으로 나눠 쓰므로 처리량이 replica 수에 비례하지 않는다. per-frame `encoder_ms`가 늘어날 수 있다.
- replica마다 weight, CUDA context, caching allocator를 따로 가지므로 VRAM이 replica 수만큼 중복 사용된다.
- JPEG decode, 불량 분석, RLE는 CPU에서 실행되므로 호스트 CPU도 경합한다.
- `/metrics.gpu_memory`에서 `torch_*`는 해당 replica 프로세스만, `device_*`는 GPU 전체 값이다.

---

## 8. 테스트 구성

| 파일 | 개수 | 내용 |
|---|---|---|
| `tests/test_api.py` | 25 | Mock Predictor API 통합 테스트: 유효한 프레임(실제 JSON Schema 검증, trace 전파, header), spot/broken/unknown 판정, bbox 없는 요청, 잘못된 bbox 6종, 중복 id, clip, 손상된 JPEG 3종, 이미지 누락, 크기 불일치, 스키마 위반, 깨진 JSON, 모델 미준비, 백그라운드 로딩, 로드 실패, 지연 측정과 `/metrics`, header 불일치 |
| `tests/test_defects.py` | 19 | 불량 규칙 단위 테스트(정상 원/사각형, 파손 3종, 반점, 작은 반점, 가장자리 그림자, mask 구멍으로 빠진 반점, unknown 4종, 환경변수 임계값, defect/mask score 독립성), RLE round-trip |
| `tests/test_sam2_real.py` | 4 | 실제 SAM2 backend로 분할 정확도와 판정, bbox 1/4/8개 latency. torch, sam2, checkpoint가 없으면 skip |

실행: `cd services/c3-inference && python -m pytest` (실제 SAM2 출력까지 보려면 `-s`). 결과는 [검증 보고서](c3-inference-validation-report.md)에 있다.

---

## 9. 변경 파일

| 구분 | 파일 |
|---|---|
| 신규 | `services/c3-inference/src/**`, `tests/**`, `scripts/**`, `docs/contract-notes.md`, `compose.gpu.yml`, `requirements-dev.txt`, `pytest.ini`, `.dockerignore` |
| 수정 (C3) | `services/c3-inference/Dockerfile`, `requirements.txt`, `README.md` |
| 수정 (공통, C3 placeholder만) | `docker-compose.yml`(C3 anchor command), `.env.example`(C3 섹션) |
| 변경 없음 | C1, C2, C4 서비스, `infra/nginx/`, `shared/schemas/` |

---

## 10. 남은 과제

- [ ] 실제 과자 영상으로 불량 임계값 보정(현재는 합성 이미지로만 검증)
- [ ] C3 → C4 결과 전달 경로 결정(C2 relay vs C3 push)
- [ ] `object_id` 프레임 간 유지 방식 C2와 합의
- [ ] `image_b64`를 required로 바꾸는 스키마 변경 제안(C2 owner 리뷰)
- [ ] 모델 로딩 중 503 처리: 모든 replica가 healthy가 된 뒤 트래픽을 보내거나 Nginx `non_idempotent` 검토
- [ ] 동시 부하(30fps)에서 replica 1/2/3개 throughput 벤치마크([bottleneck-test-plan](bottleneck-test-plan.md))
