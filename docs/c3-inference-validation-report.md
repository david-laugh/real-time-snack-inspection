# C3 SAM2 Inference 검증 보고서

| 항목 | 내용 |
|---|---|
| 작성일 | 2026-10-28 |
| 브랜치 | `feature/inference` |
| 담당 | [이규혁](https://github.com/david-laugh) |
| 대상 | C3 SAM2 Inference (A안: `SAM2ImagePredictor` 프레임 독립 추론) |
| 관련 문서 | [구현 정리](c3-inference-implementation.md) · [C3 README](../services/c3-inference/README.md) |

> 이 문서에는 **실제로 실행한 테스트와 측정값만** 기록했다. 입력은 모두 **합성 이미지**(단색 배경 위 원판 과자, 어두운 원형 반점)이고, 실제 과자 영상으로는 아직 검증하지 않았다.
> 요청은 **순차 전송**으로만 보냈다. 동시 부하에서의 throughput과 scale-out 효과는 측정하지 않았다.

---

## 1. 요약

| 검증 항목 | 결과 |
|---|---|
| pytest 전체 (Mock API 통합 + 불량 규칙 + 실제 SAM2) | ✅ **48 passed** |
| 실제 SAM2 분할 정확도 (합성 원판) | ✅ IoU **0.995** |
| 실제 SAM2 불량 판정 (정상 / 반점) | ✅ `normal` / `spot_defect` 정확히 판정 |
| 실제 SAM2 추론 시간 (GPU, bbox 1개) | 전체 중앙값 **47.0 ms** (encoder 32.7 ms) |
| Docker 이미지 빌드 | ✅ 성공 (torch `2.7.1+cu128`) |
| 컨테이너 GPU 기동과 `/healthz` | ✅ `ready`, device `cuda`, AMP `bfloat16` |
| Nginx 경유 `/infer` (replica 1개, 30프레임) | ✅ 30/30 성공, 에러 0, 전체 중앙값 50.46 ms |
| Nginx round-robin (replica 2개, 40프레임) | ✅ 40/40 성공, **20/20 분배** |
| 검증 중 발견한 결함 | 2건 발견, 2건 수정 후 재검증 완료 (§6) |

---

## 2. 테스트 환경

| 항목 | 값 |
|---|---|
| OS | Windows 11 Home 10.0.26200 |
| GPU | NVIDIA GeForce RTX 5060 Ti, 16,311 MiB |
| Docker | 29.1.2 (nvidia runtime 사용 가능) |
| 로컬 Python | 3.12.10 (venv `services/c3-inference/.venv`) |
| PyTorch | 2.7.1+cu128 (로컬 venv와 컨테이너 모두) |
| SAM2 | `facebookresearch/sam2` commit `2b90b9f5ceec907a1c18123530e92e794ad901a4` |
| 모델 | SAM2.1 Hiera Tiny, `sam2.1_hiera_tiny.pt` (156,008,466 bytes), cfg `configs/sam2.1/sam2.1_hiera_t.yaml` |
| 컨테이너 base | `pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime` |
| 주요 설정 | `C3_DEVICE=auto`(→ cuda), `C3_AMP_DTYPE=auto`(→ bfloat16), `C3_WARMUP_ITERS=2`, `C3_BOX_BATCH_SIZE=16`, 불량 임계값은 기본값 |

---

## 3. 자동화 테스트 결과 (pytest)

실행: `cd services/c3-inference && python -m pytest -s`. 최종 결과는 **48 passed**이다.

| 파일 | 개수 | 결과 | 검증 내용 |
|---|---|---|---|
| `tests/test_api.py` | 25 | ✅ 전체 통과 | Mock Predictor 기반 API 통합 테스트 |
| `tests/test_defects.py` | 19 | ✅ 전체 통과 | 불량 규칙과 RLE 단위 테스트 |
| `tests/test_sam2_real.py` | 4 | ✅ 전체 통과 | 실제 SAM2(GPU) 분할, 판정, latency |

### 3.1 요구된 시나리오별 결과 (Mock Predictor)

| 시나리오 | 테스트 | 기대 동작 | 결과 |
|---|---|---|---|
| 유효한 프레임 요청 | `test_valid_frame_returns_schema_valid_detection_result` | 200. 응답이 `detection-result.schema.json`(실제 파일)을 통과. 공통 필드 전파, trace header echo, timing header 존재, 프레임당 `set_image` 1회와 `predict_boxes` 1회 | ✅ |
| bbox 없는 요청 | `test_frame_without_prompts_skips_encoder` | 200, `detections: []`, encoder 미호출(`encoder_ms=0`), decode는 수행 | ✅ |
| 잘못된 bbox | `test_invalid_bbox_rejected` ×6, `test_duplicate_candidate_id_rejected`, `test_bbox_partially_outside_is_clipped` | x2<x1, 폭 0, 이미지 밖 2종은 `422 invalid_bbox`. 원소 3개와 문자열 원소는 `422 invalid_request`. 중복 id는 `422 duplicate_candidate_id`. 일부만 벗어난 bbox는 clip `[540,180,640,300]` | ✅ |
| 손상된 JPEG | `test_corrupt_jpeg_rejected` | 깨진 JPEG 바이트, 200바이트로 자른 JPEG, base64가 아닌 문자열 모두 `422 image_decode_failed`. error와 header에 trace_id 포함 | ✅ |
| 모델 미준비 | `test_model_not_ready`, `test_background_loading_then_ready`, `test_model_load_failure_reported` | 미로드 상태면 `/healthz` 503, `/infer` 503 + `Retry-After: 1`, `error_count=1`. 로딩 중에는 `status=loading`(503)이었다가 이후 `ready`(200)가 되고 `model_load_ms ≥ 300`. 로드 실패 시 `status=error`와 원인 메시지 | ✅ |
| 처리 지연 측정 | `test_latency_measurement_and_metrics` | Mock에 encoder 30 ms, segment 20 ms 지연을 주입. 각 stage 측정값이 주입값 이상이고, total ≥ stage 합, `inference_latency_ms`가 total과 일치. `/metrics`에 5프레임 집계와 `error_count=1`(invalid_bbox) | ✅ |

그 밖의 API 테스트(모두 통과): 이미지 누락(`missing_image`), 이미지 크기 불일치(`image_size_mismatch`), 스키마에 없는 필드와 major가 다른 `schema_version`, 깨진 JSON(header의 trace 값으로 응답), trace header/body 불일치 카운트, Mock mask를 이용한 `spot_defect` / `broken` / `unknown` 판정.

Mock 지연 측정에서 프레임당 `total_inference_ms`는 52.45 ~ 53.67 ms였다(주입 지연 50 ms 포함, 5프레임).

### 3.2 불량 판정 규칙 단위 테스트

| 입력 mask / 이미지 | 기대 판정 | 결과 |
|---|---|---|
| 원판(r=80) | `normal`, score < 0.5, 원형도 > 0.85, solidity > 0.95 | ✅ |
| 정사각형 | `normal` (기본 임계값) | ✅ |
| 80° 베어낸 원판 / 반원 / 두 조각 | `defect` / `broken`, 0.5 < score ≤ 1 | ✅ |
| 원판 + 반점(r=20, 약 6%) | `defect` / `spot_defect`, dark_ratio > 0.03 | ✅ |
| 원판 + 작은 반점(r=6, 약 0.6%) | `normal`, 0 < dark_ratio < 0.03 | ✅ |
| 가장자리만 어두운 원판 | `normal` (침식으로 제외) | ✅ |
| 반점이 mask 구멍으로 빠진 경우 | `defect` / `spot_defect`, `hole_px > 0` | ✅ |
| mask score 0.3 / NaN, 면적 과소, 빈 mask | `unknown`, `defect_type=null`, score 0.0 | ✅ |
| `C3_SPOT_RATIO_MAX=0.2`로 완화 | `defect` → `normal`로 바뀜 | ✅ |
| `C3_BROKEN_EXPECTED_AREA_PX=40000` | 면적 편차로 `broken` | ✅ |
| mask score 0.6 vs 0.99 | 같은 `defect_score` (두 점수가 독립적) | ✅ |
| RLE round-trip (무작위 5개, 전부 0, 전부 1) | 원본 복원, `sum(counts)=W×H` | ✅ |

---

## 4. 실제 SAM2 모델 검증 (로컬 GPU)

### 4.1 분할 정확도와 판정

입력: 1024×1024 합성 이미지에 원판 2개.
- 원판 A: 중심 (300,300), r=120, 정상
- 원판 B: 중심 (700,650), r=110, 반점 r=35

bbox는 각 원판을 감싸도록 줬다.

| 항목 | 결과 |
|---|---|
| 응답 스키마 검증 | ✅ `detection-result.schema.json` 통과 |
| 원판 A mask와 정답 원의 IoU | **0.995** |
| 원판 A 판정 | `normal`, `defect_score=0.1714` |
| 원판 B 판정 | `defect` / `spot_defect`, `defect_score=1.0` |

### 4.2 모델 로딩 시간 (추론 latency와 분리 측정)

| 실행 | `model_load_ms` | `warmup_ms` (2회) | 비고 |
|---|---|---|---|
| 로컬 1회차 | 18,494.6 | 7,046.1 | 첫 실행 |
| 로컬 2회차 | 2,635.3 | 14,680.3 | |
| 로컬 3회차 | 2,509.2 | 5,286.9 | |
| 로컬 4회차 | 4,969.8 | 25,684.8 | 실행 중인 컨테이너와 GPU를 공유하던 상태 |
| 컨테이너 (`c3-inference-1`) | 8,527.9 | 10,000.1 | |

로딩 시간과 warm-up 시간은 실행마다 편차가 컸다. 첫 실행은 파일·CUDA 초기화 캐시의 영향, 4회차는 다른 GPU 프로세스와 동시 실행된 영향으로 보이지만 원인을 따로 검증하지는 않았다. 어느 경우든 `/healthz`가 `ready`가 되기 전에는 트래픽을 받지 않는다.

### 4.3 추론 latency (최종 코드 기준)

- 조건: 1024×1024 JPEG, 반지름 90px 원판 N개, bbox 200×200
- 방법: 같은 프레임을 **20회 순차 요청**해서 **중앙값**을 집계(ms)

| bbox 수 | decode | encoder | segmentation | defect_analysis | total_inference |
|---|---|---|---|---|---|
| 1 | 3.26 | 32.66 | 7.02 | 1.21 | **47.01** |
| 4 | 3.26 | 31.62 | 9.85 | 4.17 | **58.68** |
| 8 | 3.55 | 31.67 | 18.60 | 8.23 | **80.33** |

관찰:
- **encoder는 bbox 수와 관계없이 약 32 ms로 일정하다.** 프레임당 embedding을 한 번만 계산한다는 것이 실측으로 확인된다.
- segmentation은 bbox 수에 따라 완만하게 증가한다(batch decoder 1회 호출).
- total과 각 stage 합계의 차이(bbox 8개 기준 약 13 ms)는 표에 없는 mask RLE 인코딩(`mask_encode_ms`)과 validation 시간이다.
- 이 측정은 단일 요청 기준이다. 30fps(33 ms/프레임)를 replica 1개로 처리할 수 있는지는 동시 부하 벤치마크로 확인해야 한다.

### 4.4 GPU 메모리 (로컬, `/metrics.gpu_memory`)

| 항목 | 값 |
|---|---|
| `torch_allocated_mib` (해당 프로세스) | 315.0 |
| `torch_reserved_mib` (해당 프로세스) | 640.0 |
| `torch_max_allocated_mib` (해당 프로세스) | 564.3 |
| `device_used_mib` (GPU 전체, 다른 프로세스 포함) | 3,285.6 ~ 3,286.5 |

bbox 1, 4, 8개 실행에서 프로세스 메모리 값은 동일했다.

---

## 5. Docker / Nginx 연동 검증

명령: `docker compose -f docker-compose.yml -f services/c3-inference/compose.gpu.yml up -d nginx-inference-lb c3-inference-1 [c3-inference-2]`

부하 클라이언트: `services/c3-inference/scripts/send_frames.py --url http://localhost:8080/infer --boxes 4`
- 1024×1024 합성 프레임
- bbox 4개 중 `c2`에 반점을 그림
- 순차 전송, trace header 포함

### 5.1 이미지 빌드와 기동

| 항목 | 결과 |
|---|---|
| `docker compose build c3-inference-1` | ✅ 성공. 빌드 중 확인 출력: `torch 2.7.1+cu128` |
| Container healthcheck | ✅ `healthy` (replica 2개 구성에서 기동 후 약 27초 안에 둘 다 healthy) |
| 컨테이너 안 `GET /healthz` | `status=ready`, `model_ready=true`, `device=cuda`, `amp_dtype=bfloat16`, `gpu_name=NVIDIA GeForce RTX 5060 Ti` |

### 5.2 Replica 1개, Nginx 경유 30프레임

| 항목 | 결과 |
|---|---|
| 응답 | 30/30 HTTP 200, `inference_container_id=c3-inference-1` |
| C3 stage 중앙값 (ms) | decode 1.39 / encoder 31.31 / segmentation 8.36 / defect 3.06 / **total 50.46** |
| 클라이언트 RTT 중앙값 | 74.08 ms (첫 프레임 168.8 ms) |
| Nginx access log | `POST /infer 200 upstream=172.19.0.2:8002 request_time=0.051 upstream_response_time=0.050 trace_id=… frame_id=29` → trace header가 Nginx까지 전달됨 |
| `/metrics` | `processed_frame_count=30`, `error_count=0`, `status_counts={normal: 120}`, `torch_reserved_mib=640.0`, `device_used_mib=3322.0` |

⚠️ 이 실행에서 반점을 그린 `c2`가 `normal`로 판정되었다. 이는 결함 #2로 수정했다(§6). 위 결과는 수정 전 코드로 측정한 값이다.

### 5.3 Replica 2개, Round-robin (수정 후 코드)

| 항목 | 결과 |
|---|---|
| 응답 | 40/40 HTTP 200 |
| 분배 | `c3-inference-1`: 20, `c3-inference-2`: 20 |
| 판정 (frame 0) | `c0` normal, `c1` normal, **`c2` defect / spot_defect (1.0)**, `c3` normal |
| C3 stage 중앙값 (ms) | decode 1.33 / encoder 32.80 / segmentation 8.52 / defect 3.61 / **total 51.86** |
| 클라이언트 RTT 중앙값 | 73.19 ms |
| `nvidia-smi` memory.used (GPU 전체, 실행 후) | 3,839 MiB |

요청을 순차로 보냈기 때문에 두 replica가 동시에 GPU를 쓰는 상황은 만들어지지 않았다. 이 결과는 **분배 동작만** 확인한 것이며, scale-out에 따른 처리량 변화는 측정하지 않았다.

---

## 6. 검증 중 발견한 결함과 조치

| # | 발견 경로 | 증상 | 원인 | 조치 | 재검증 |
|---|---|---|---|---|---|
| 1 | 실제 SAM2 latency 측정 | `defect_analysis_ms`가 bbox 1개당 약 2.8 ms씩 선형 증가(bbox 8개에 22.72 ms) | bbox마다 1024×1024 전체 mask에 `np.nonzero` / `sum` 실행 | OpenCV `countNonZero`와 `boundingRect`로 교체하고, 이후 처리는 crop 영역에서만 수행 | bbox 8개 기준 **22.72 → 7.48 ms**, total 94.39 → 81.04 ms (같은 테스트, 수정 전후) |
| 2 | 컨테이너 E2E (§5.2) | 반점이 있는 과자 `c2`가 `normal`로 판정 | **SAM2가 대비가 강한 반점을 mask에서 구멍으로 제외**함. mask 면적 30,719 px(원판 이론 면적 약 32,685 px), `dark_ratio=0.0059` | 반점 분석을 외곽선 내부를 채운 영역에서 수행하고, `hole_px`를 feature로 기록. 회귀 테스트 `test_spot_carved_out_as_mask_hole_is_still_detected` 추가 | 단위 테스트 통과, 실제 SAM2 테스트 통과, 컨테이너 2-replica 실행에서 `c2` → `spot_defect (1.0)` |
| 3 | 빌드 (설정 오류) | 로컬 venv에서 SAM2 설치 실패: `No matching distribution found for sam2` | 해당 GitHub 소스의 패키지 이름이 `SAM-2` | Dockerfile과 README의 requirement 이름을 `SAM-2 @ …`로 수정 | venv 설치와 이미지 빌드 성공 |

결함 #1의 latency 변화 (bbox 수별, 같은 테스트):

| bbox 수 | 수정 전 defect / total (ms) | 수정 후 defect / total (ms) |
|---|---|---|
| 1 | 3.00 / 48.82 | 1.15 / 48.71 |
| 4 | 11.50 / 66.27 | 3.85 / 59.35 |
| 8 | 22.72 / 94.39 | 7.48 / 81.04 |

---

## 7. 완료 조건 대비

| 완료 조건 | 상태 | 근거 |
|---|---|---|
| C2의 `/infer` 요청을 정상 수신 | ✅ | `processed-frame.schema.json`을 통과한 요청으로 테스트(§3.1). Nginx 경유 70건 성공(§5) |
| DetectionResult를 JSON으로 반환 | ✅ | 응답을 `detection-result.schema.json`으로 검증(Mock과 실제 SAM2 모두) |
| `GET /healthz`가 모델 준비 상태 표시 | ✅ | `not_started` / `loading` / `ready` / `error` 상태 테스트, 컨테이너 healthcheck |
| 모델 로딩과 inference latency 분리 측정 | ✅ | `model_load_ms`·`warmup_ms`(§4.2)와 stage별 latency(§4.3)를 따로 측정 |
| C1/C2/C4 인터페이스 무변경 | ✅ | 해당 서비스, `shared/schemas/`, `infra/nginx/` 변경 없음 |
| 환경변수와 실행 방법 README 작성 | ✅ | `services/c3-inference/README.md` |

---

## 8. 한계와 후속 검증 항목

- **합성 데이터만 사용했다.** 실제 과자 영상(조명, 질감, 과자 겹침, 배경)에서의 분할 품질과 불량 임계값(원형도 0.70, solidity 0.90, 종횡비 0.75, 반점 3%)은 검증하지 않았다.
- **동시 부하 미측정.** 30fps 입력에서의 throughput, frame drop, lock 대기, replica 1/2/3개 비교는 [bottleneck-test-plan](bottleneck-test-plan.md)에 따라 별도로 수행해야 한다.
- **CPU 모드 미측정.** `C3_DEVICE=cpu` 추론 시간은 측정하지 않았다.
- **GPU 경합 미측정.** 여러 replica가 단일 GPU에 동시 부하를 줄 때의 `encoder_ms` 증가폭과 VRAM 합계는 측정하지 않았다.
- **모델 로딩 중 503.** Nginx가 POST를 재시도하지 않아서 503이 C2까지 전달되는 동작은 코드와 설정 분석 결과이며, 실제 로딩 중에 트래픽을 보내 재현하지는 않았다.

---

## 9. 2차 검증: C1/C2 머지 후 연동

| 항목 | 내용 |
|---|---|
| 일시 | 2026-10-28 |
| 대상 코드 | C1·C2 = `origin/main`(PR #1, #2 머지본, **수정 없음**), C4 = `origin/C4-Visualizer` 브랜치(수정 없음, 임시 worktree에서 실행), C3 = 이번 수정본 |
| 데이터 | C2 fixture: 시뮬레이터 PNG 1장(1045×450, 라벨 15개), 폰 세로 촬영 JPEG 5장(1080×1920, 27개, 라벨은 사람이 눈으로 판정) |
| 대상 과자 | 원형 초코칩 쿠키. 불량 유형 bite / wedge / cut / half |

### 9.1 C2 → C3 계약 확인 (C2 코드로 만든 요청)

| 항목 | 결과 |
|---|---|
| C2 `build_processed_frame` 출력을 C3 `/infer`로 전송 | 6/6 HTTP 200, 응답 6/6 `detection-result.schema.json` 통과 |
| C3가 받은 이미지 크기 | 1024×441(시뮬레이터), 576×1024(폰): 정사각형이 아닌 이미지도 정상 처리 |
| C2 bbox와 시뮬레이터 라벨 IoU | 0.95 ~ 0.99 |
| C3 처리 시간 (폰 프레임, 4~6 bbox) | 52.9 ~ 69.1 ms. C2의 `/infer` timeout 3 s보다 충분히 짧다 |

### 9.2 불량 판정: 1차 규칙 vs 2차 규칙

같은 C2 prompt와 실제 SAM2.1-tiny mask 42개에 대해 상태와 유형이 모두 맞은 개수다.

| 구분 | 객체 수 | 1차 규칙 | 2차 규칙 |
|---|---|---|---|
| 시뮬레이터 정상 (round) | 8 | 3 | **8** |
| 시뮬레이터 파손 (bite 3, wedge 2, cut 1, half 1) | 7 | 0 (7개 모두 `spot_defect`로 분류) | **7** |
| 폰 정상 | 15 | 14 | **15** |
| 폰 파손 (bite 4, wedge 5, cut 3) | 12 | 3 (놓침 4, 유형 오류 5) | **12** |
| **합계** | **42** | **20** | **42** |

1차 규칙이 틀린 원인:
- **반점 규칙**: 초코칩 때문에 정상 쿠키의 어두운 영역이 0.021~0.047이었다. 파손 쿠키(0.041~0.063)와 겹친다.
- **bite**: 얕은 오목부를 1차 형태 규칙(원형도 0.70, solidity 0.90)으로는 잡지 못했다. 원형도 0.83~0.87, solidity 0.958~0.978이었다.
- **cut**: 종횡비가 0.817~0.839로 1차 임계값 0.75를 넘었다.

2차 규칙을 정할 때 측정한 특징값 분포 (실제 SAM2 mask):

| 특징 | 정상 (23) | bite (7) | wedge (7) | cut (4) | half (1) |
|---|---|---|---|---|---|
| 오목 깊이 ÷ 볼록 껍질 지름 | 0.009 ~ 0.013 | 0.049 ~ 0.093 | 0.306 ~ 0.346 | 0.012 ~ 0.016 | 0.020 |
| 종횡비 | 0.899 ~ 1.000 | 0.903 ~ 0.956 | 0.905 ~ 0.956 | 0.817 ~ 0.839 | 0.558 |
| 원형도 | 0.888 ~ 0.902 | 0.831 ~ 0.869 | 0.628 ~ 0.658 | 0.848 ~ 0.859 | 0.720 |

- 2차 규칙: 오목 깊이 > 0.03, 종횡비 < 0.87, 원형도 < 0.80(보조), solidity < 0.90(보조), 반점 규칙 꺼짐.
- 픽셀 격자 원형도(완벽한 원 마스크): 지름 30px에서 0.855, 40px 0.867, 50px 0.874, 60px 0.879, 80px 0.887, 100px 0.891. 그래서 원형도는 주 판정 기준으로 쓰지 않는다.
- ⚠️ 임계값을 정한 데이터와 검증 데이터가 같다. 폰 라벨 27개는 사람이 눈으로 붙였다. 정상과 불량 사이 여유는 종횡비 0.06(0.839 → 0.899), 오목 깊이 0.036(0.013 → 0.049)이다.

### 9.3 전체 체인 연동: C2 → C3 → C4

구성(로컬 프로세스):
- C2: `uvicorn c2.app:app`, `C2_INFER_URL=http://127.0.0.1:8002/infer`
- C3: 실제 SAM2, `C3_RESULTS_URL=http://127.0.0.1:8003/results`
- C4: `uvicorn src.main:app --port 8003`

C1 대신 frame-envelope 6개를 C2 `/preprocess`로 직접 보냈다.

| 단계 | 결과 |
|---|---|
| C2 `/preprocess` | 6/6 HTTP 200, `infer_status: 200`, C2 전처리 18~24 ms |
| C3 `/metrics` | `processed_frame_count=6`, `error_count=0`, `status_counts={normal: 23, defect: 19}` (라벨과 일치), `results_push_ok_count=6`, `results_push_error_count=0` |
| C4 `/api/stats` | `total: 42` → **detection 42개 모두 C4에 도착** |
| C4 표시 결과 | `pass_count: 0, fail_count: 42` ⚠️ C4가 C3의 `status`를 쓰지 않고 bbox 사각형 원형도(정사각형이면 π/4 ≈ 0.785)로 다시 판정하기 때문이다. **C4 쪽 이슈**이고 C3에서는 수정하지 않았다 |

### 9.4 C4 장애 시 동작

| 시험 | 결과 |
|---|---|
| Mock C4 (pytest): 정상 응답 | C4가 받은 body가 `/infer` 응답 JSON과 같고, trace header 포함, `results_push_ok_count=1` |
| Mock C4 (pytest): 500 / 연결 실패 / timeout | `/infer`는 200, 판정 정상, `results_push_errors`가 각각 `{http_500: 1}` / `{ConnectError: 1}` / `{timeout: 1}`, `error_count=0` |
| Mock C4 (pytest): 422 오류 요청 | C4로 전송하지 않음 |
| Docker (GPU, Nginx 경유, C4 컨테이너 없음) 10프레임 | `/infer` 10/10 성공(전체 중앙값 55.58 ms). 2 s 뒤 `results_push_error_count=10`, `{timeout: 10}`. Docker DNS가 없는 호스트를 조회하는 동안 대기하다 `C3_RESULTS_TIMEOUT_S`(2 s)에 걸린다 |

C4 장애 중에는 전송 task가 요청마다 최대 2 s씩 남아 있다. 30fps면 동시에 약 60개다(httpx 기본 connection pool 100개 이내). `/infer` 응답 latency에는 영향이 없다(응답 이후 실행).

### 9.5 최종 테스트 결과

| 항목 | 결과 |
|---|---|
| pytest | **105 passed** (`test_api` 29, `test_defects` 71, `test_sam2_real` 5) |
| C2 fixture 회귀 (저장된 실제 SAM2 mask) | 42/42 |
| C2 fixture E2E (실제 SAM2, C2 이미지 → C3) | 42/42 |
| 합성 이미지 실제 SAM2 | 원판 IoU 0.996, 정상 → `normal`, bite → `broken` |
| 추론 시간 (20프레임 중앙값, bbox 1 / 4 / 8) | 전체 44.8 / 57.0 / 74.53 ms. 1차보다 불량 분석이 짧다(반점 규칙 꺼짐): 0.68 / 3.13 / 4.72 ms |
| Docker 이미지 재빌드 | 성공(`httpx-0.28.1` 설치 확인), 컨테이너 `healthy` |
| C1/C2/C4/shared/infra/compose/`.env.example` 변경 | 없음 (`git status` 0건) |

### 9.6 남은 확인 사항

- **새 촬영 데이터로 검증**: 이번 임계값은 6장, 42개로 정했다. 특히 C1 `/cam` 640px 가로 촬영(쿠키 지름 약 50px)에서는 bite 깊이가 2~3px라서 `C3_BROKEN_CONCAVITY_MIN_PX=2`의 경계에 가깝다. 합성 데이터로는 지름 50px bite를 잡는 것을 확인했지만 실제 영상으로는 확인하지 않았다.
- **C4 판정 로직 수정**(Member D)이 끝나야 대시보드에 C3 판정이 제대로 표시된다.
- compose에서 C2가 아직 `sleep infinity`라서 compose만으로 C1 → C4 전체 체인을 띄우는 시험은 하지 못했다. 위 9.3은 로컬 프로세스로 한 시험이다.
