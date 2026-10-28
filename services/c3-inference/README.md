# C3 SAM2 Inference

**Owner:** [이규혁](https://github.com/david-laugh)

C2에서 매 프레임 받는 `image + prompts[]`로 **SAM2.1 `SAM2ImagePredictor` 프레임 독립 추론(A안)**을 수행하고, mask 기반 불량 판정 결과(`DetectionResult`)를 JSON으로 반환한다.
Video Predictor의 `inference_state`나 세션 memory는 두지 않는다. 그래서 어떤 replica든 어떤 프레임이든 처리할 수 있고, Nginx round-robin과 충돌하지 않는다.

계약 해석과 스키마 관련 결정: [`docs/contract-notes.md`](docs/contract-notes.md)

## API

| Method | Path | 설명 |
|---|---|---|
| `POST` | `/infer` | body: [`processed-frame`](../../shared/schemas/processed-frame.schema.json) (+ `image_b64` = base64 JPEG) → [`detection-result`](../../shared/schemas/detection-result.schema.json) |
| `GET` | `/healthz` | 모델이 준비되면 `200`, `loading`/`error`/`not_started` 상태면 `503`. body에 status, device, amp, `model_load_ms`, `warmup_ms`, error 포함 |
| `GET` | `/metrics` | `processed_frame_count`, `error_count`(+code별), stage별 latency window(mean/p50/p95/max), 모델 정보, GPU 메모리 |

- Trace header `X-Trace-Id`, `X-Frame-Id`, `X-Session-Id`를 받아 응답 header로 echo한다(body 값이 기준).
- `DetectionResult`가 `additionalProperties:false`라서 per-frame timing은 **응답 header**로 보낸다. `X-C3-Decode-Ms`, `X-C3-Encoder-Ms`, `X-C3-Segmentation-Ms`, `X-C3-Defect-Analysis-Ms`, `X-C3-Total-Inference-Ms`, `Server-Timing`, `X-Inference-Container-Id`.
- 에러는 `{"error": {"code", "message", trace...}}` 형식이다. 422는 입력 오류, 503은 `model_not_ready`, 500은 내부 오류.

## 처리 단계와 측정

| 단계 | 모듈 | 지표 |
|---|---|---|
| 1. Request validation | `src/pipeline.py` | — (total에 포함) |
| 2. JPEG decode | `src/pipeline.py` | `decode_ms` |
| 3. SAM2 image embedding (**프레임당 1회**) | `src/predictor/sam2_predictor.py` | `encoder_ms` |
| 4. bbox prompt segmentation (모든 bbox를 한 번에, `C3_BOX_BATCH_SIZE` 단위) | 〃 | `segmentation_ms` |
| 5. 파손/색상 이상 분석 | `src/defects.py` | `defect_analysis_ms` |
| 6. Mask RLE + 결과 생성 | `src/mask_codec.py`, `src/pipeline.py` | `mask_encode_ms` |
| 전체 | | `total_inference_ms` (= `inference_latency_ms`) |

**모델 로딩은 inference latency와 따로 측정한다.** 로딩과 warm-up은 startup 때 한 번만 백그라운드 스레드에서 실행되고, 결과는 `/healthz`의 `model_load_ms`와 `warmup_ms`로 확인한다.
encoder/segmentation 단계는 CUDA를 `synchronize`한 뒤 시간을 잰다. 따라서 GPU 실행 시간이 실제로 포함된다.

## 모듈 구조

```
src/
  app.py              FastAPI (lifespan에서 모델 백그라운드 로드, /infer /healthz /metrics)
  pipeline.py         validation → decode → encode → segment → defect → serialize
  predictor/
    base.py           SegmentationPredictor 인터페이스 (set_image / predict_boxes)
    sam2_predictor.py 실제 SAM2 (inference_mode, autocast, warm-up, bbox batch, GPU memory)
    mock_predictor.py Mock (torch 불필요, 지연/로드 실패 주입 가능)
  defects.py          불량 판정 규칙 (SAM2와 독립)
  mask_codec.py       RLE encode/decode
  metrics.py          카운터 + rolling latency window
  config.py           환경변수
```

Mock과 SAM2는 같은 `SegmentationPredictor` 인터페이스를 쓰므로 `C3_PREDICTOR=mock|sam2`만 바꾸면 교체된다.

## 불량 판정 (`src/defects.py`)

| 결과 | 조건 |
|---|---|
| `unknown` | SAM2 mask score < `C3_MIN_MASK_SCORE`, 또는 mask 면적 < `C3_MIN_MASK_AREA_PX` |
| `defect` / `broken` | 가장 큰 component의 원형도(4πA/P²) < min, solidity(A/convex hull) < min, 종횡비(minAreaRect 단변/장변) < min, 의미 있는 조각 수 > 1, (설정한 경우) 기대 면적 대비 편차 > tolerance |
| `defect` / `spot_defect` | 침식(erode)한 mask 내부에서 Lab L < median(L) − delta인 픽셀(opening 적용) 비율 > `C3_SPOT_RATIO_MAX` |
| `normal` | 위 조건에 하나도 해당하지 않음 |

`defect_score`는 각 규칙의 severity 중 최댓값이다(이상적인 값 = 0, 임계값에 정확히 걸리면 = 0.5, 크게 벗어나면 = 1). 그래서 `defect ⇔ defect_score > 0.5`이고, `unknown`이면 `0.0`이다.
**SAM2 mask score(predicted IoU)는 `unknown` 판정에만 쓰며 `defect_score`와 섞지 않는다.**
기본 임계값은 둥근 과자를 기준으로 잡았다. 사각형 과자는 원형도가 약 0.78이므로 기본값 0.70으로 통과한다. 실제 과자와 카메라 조건에 맞춰 조정해야 한다.

## 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `C3_PORT` | `8002` | 서비스 포트 (Nginx upstream이 8002를 가정) |
| `INFERENCE_CONTAINER_ID` | `c3-inference-local` | compose가 replica별로 주입 |
| `SCHEMA_VERSION` | `1.0.0` | 요청 `schema_version`의 major 호환성 검사에 사용 |
| `LOG_LEVEL` | `INFO` | 프레임마다 JSON 로그 1줄(trace, timings, 판정 근거) |
| `C3_PREDICTOR` | `sam2` | `sam2` \| `mock` |
| `SAM2_MODEL_CFG` | `configs/sam2.1/sam2.1_hiera_t.yaml` | SAM2.1 Tiny |
| `SAM2_CHECKPOINT_PATH` | `/app/models/sam2.1_hiera_tiny.pt` | read-only mount |
| `C3_DEVICE` | `auto` | `auto` \| `cpu` \| `cuda` \| `cuda:N`. `cuda`를 지정했는데 GPU가 없으면 `/healthz`가 error 상태가 된다 |
| `C3_AMP_DTYPE` | `auto` | CUDA에서만 적용. `auto`는 bf16을 지원하면 bf16, 아니면 끔. `none`, `bfloat16`, `float16` 선택 가능 |
| `C3_WARMUP_ITERS` | `2` | startup warm-up 반복 횟수 (0이면 끔) |
| `C3_WARMUP_IMAGE_SIZE` | `1024` | warm-up 이미지 크기 |
| `C3_BOX_BATCH_SIZE` | `16` | mask decoder 1회 호출에 넣는 bbox 수 |
| `C3_MAX_PROMPTS` | `64` | 초과하면 422 |
| `C3_RETURN_MASK` | `1` | `0`이면 `mask: null` (응답 크기/latency 절감) |
| `C3_METRICS_WINDOW` | `1000` | `/metrics` percentile 계산에 쓰는 최근 프레임 수 |
| `C3_MIN_MASK_SCORE` | `0.5` | 이 값 미만이면 unknown |
| `C3_MIN_MASK_AREA_PX` | `100` | 이 값 미만이면 unknown |
| `C3_BROKEN_CIRCULARITY_MIN` | `0.70` | |
| `C3_BROKEN_SOLIDITY_MIN` | `0.90` | |
| `C3_BROKEN_ASPECT_RATIO_MIN` | `0.75` | |
| `C3_BROKEN_FRAGMENT_MIN_RATIO` | `0.05` | mask 면적에서 이 비율 이상인 component만 조각으로 셈 |
| `C3_BROKEN_EXPECTED_AREA_PX` | `0` | 0이면 면적 규칙을 쓰지 않음 |
| `C3_BROKEN_AREA_TOLERANCE` | `0.25` | 기대 면적 대비 ±25% |
| `C3_SPOT_DARK_DELTA` | `50` | median L보다 이만큼 어두우면 dark 픽셀 |
| `C3_SPOT_RATIO_MAX` | `0.03` | dark 픽셀 비율 상한 |
| `C3_SPOT_ERODE_PX` | `3` | 가장자리 그림자를 제외하기 위한 침식 폭 |
| `C3_SPOT_OPEN_KERNEL` | `3` | dark map opening kernel (노이즈 제거) |
| `C3_MOCK_ENCODER_DELAY_MS` / `C3_MOCK_SEGMENT_DELAY_MS` | `0` | mock 사용 시 인위적 지연 |

## 실행 방법

### 1) Checkpoint 준비 (git 제외)
```bash
python services/c3-inference/scripts/download_checkpoint.py   # -> models/sam2.1_hiera_tiny.pt
```

### 2) Docker Compose
```bash
cp .env.example .env
# CPU (또는 mock)
docker compose up -d --build
# GPU (NVIDIA Container Toolkit 필요)
docker compose -f docker-compose.yml -f services/c3-inference/compose.gpu.yml up -d --build
# Scale-out
docker compose -f docker-compose.yml -f services/c3-inference/compose.gpu.yml --profile scale-out up -d

curl http://localhost:8080/healthz        # nginx 자체 health
docker compose exec c3-inference-1 python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8002/healthz').read().decode())"
python services/c3-inference/scripts/send_frames.py --url http://localhost:8080/infer -n 30 --boxes 4
```
- 이미지 base: `pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime`. CUDA 12.8 wheel에 RTX 50xx(sm_120) 커널이 포함되어 있다. 호스트 드라이버는 570 이상이어야 한다.
- SAM2는 고정된 GitHub commit(`SAM2_REF` build arg)으로 설치한다. optional CUDA extension은 빌드하지 않는다(`SAM2_BUILD_CUDA=0`).
- Uvicorn worker는 1개다. worker 수만큼 모델이 따로 로드되므로, 확장은 worker가 아니라 replica 수로 한다.
- 컨테이너 `HEALTHCHECK`는 `/healthz`를 쓴다(`start-period` 120s). 모델이 준비되기 전에는 `unhealthy/starting` 상태다.

### 3) 로컬 (Docker 없이)
```bash
cd services/c3-inference
python -m venv .venv && .venv/Scripts/activate      # Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
# 실제 SAM2를 쓸 때만:
pip install torch==2.7.1 torchvision==0.22.1 --index-url https://download.pytorch.org/whl/cu128
SAM2_BUILD_CUDA=0 pip install --no-build-isolation --no-deps "SAM-2 @ https://github.com/facebookresearch/sam2/archive/2b90b9f5ceec907a1c18123530e92e794ad901a4.zip"

C3_PREDICTOR=mock python -m src                                                    # mock
SAM2_CHECKPOINT_PATH=models/sam2.1_hiera_tiny.pt C3_DEVICE=cuda python -m src     # real
```

### 4) 테스트
```bash
cd services/c3-inference
python -m pytest                 # mock API 통합 + 불량 규칙 + RLE (torch 불필요)
python -m pytest -s tests/test_sam2_real.py   # torch/sam2/checkpoint가 있으면 실제 SAM2로 실행, 없으면 skip
```

## ⚠️ 단일 GPU Scale-out (자원 경합)

`compose.gpu.yml`은 모든 replica에 **같은 GPU 0번**을 할당한다. 단일 GPU에서 replica를 늘리면 다음 문제가 생길 수 있다.
- **연산 경합**: 여러 프로세스의 CUDA kernel이 time-slicing으로 GPU를 나눠 쓴다(MPS 미사용). 그래서 처리량이 replica 수에 비례해 늘지 않는다. 오히려 context switch 때문에 **per-frame latency(`encoder_ms`)가 늘어날 수 있다**. GPU가 이미 포화 상태라면 replica를 늘려도 throughput 이득이 거의 없다.
- **VRAM 중복**: replica마다 모델 weight, CUDA context(수백 MiB), PyTorch caching allocator를 따로 가진다. replica 수 × (`torch_reserved_mib` + context)만큼 VRAM이 필요하다.
- **CPU 경합**: JPEG decode, 불량 분석, RLE는 CPU에서 실행된다. replica가 늘면 호스트 CPU도 나눠 쓴다.
- **측정 방법**: `/metrics.gpu_memory`에서 `torch_*`는 해당 replica 프로세스만, `device_used_mib`와 `device_utilization_pct`는 GPU 전체(모든 replica 합산) 값이다. replica 구성별로 `encoder_ms` p50/p95와 GPU utilization을 함께 기록한다([`docs/bottleneck-test-plan.md`](../../docs/bottleneck-test-plan.md)).
- 한 replica 안에서는 SAM2 predictor가 프레임 단위 lock으로 직렬화된다. 동시에 들어온 요청은 대기하며, 대기 시간은 `Server-Timing`의 `lock_wait`로 확인할 수 있다.

## Open issues
- C3 → C4 결과 전달 경로(현재는 `/infer` 응답으로만 반환)와 프레임 간 `object_id` 유지 방식. 자세한 내용은 [`docs/contract-notes.md`](docs/contract-notes.md).
- Nginx `proxy_next_upstream`는 POST를 재시도하지 않으므로, 모델 로딩 중인 replica의 503이 C2까지 전달된다.
- 불량 임계값은 합성 이미지로만 검증했다. 실제 과자 영상으로 보정해야 한다.
