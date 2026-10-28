# 첫 전체 통합 테스트 결과 (2026-10-28)

- 수행: Member B (C2), 이 PC 단일 호스트
- 기준 커밋: `main` `eba11a3` (PR #10 feature/inference 머지 직후)
- Raw 데이터: `benchmark/results/c1_events.csv`, `benchmark/results/c2_events.csv` (git 제외), C3 컨테이너 로그(프레임별 JSON)

## 1. 구성

| 항목 | 값 |
|---|---|
| 경로 | 폰 `/cam` 또는 C1 loadgen → C1(WebSocket) → `mqtt-broker`(MQTT) → C2 → Nginx `/infer` → C3(SAM 2.1 tiny) → (C4) |
| GPU | RTX 3060 12GB, 드라이버 580.97, WDDM. 테스트 전 데스크톱 앱이 VRAM 약 4.7GB 사용 |
| C3 | `C3_PREDICTOR=sam2`, cuda, AMP bf16, box batch 16. 모델 로딩 11.7s, warm-up 11.8s |
| 브로커 | `127.0.0.1:11883` → 컨테이너 1883. 호스트 1883의 다른 프로젝트 브로커(`ocr-mqtt`)는 사용하지 않음 |
| C4 | main에서 아직 `sleep infinity` → C2 `c4_error`, C3 `results_push_error`는 예상된 실패 |
| 실행 | `docker compose -f docker-compose.yml -f services/c3-inference/compose.gpu.yml up -d --build` |
| `.env` 변경 | `MQTT_HOST_PORT=11883`(기본), `C2_MAX_INFLIGHT=4`(scale-out까지) → `2`(폰 테스트), `C1_PUBLIC_URL=<cloudflared 주소>` |

테스트 입력

| 이름 | 입력 | 경로 |
|---|---|---|
| loadgen | `services/c2-preprocessing/tests/fixtures/phone/*.jpg` 5장 반복, 30fps × 20s. loadgen이 폭 960(960×1707), JPEG q70으로 재인코딩 | C1 → C2 → C3 (전 구간) |
| sim | 시뮬레이터 PNG 1045×450 1장 반복, 30fps × 10s (`dev/publish_frames.py`, compose 브로커에 직접 발행) | C2 → C3 |
| probe | C2 코드(`process_envelope`)로 만든 processed-frame을 Nginx `/infer`에 순차 전송 (동시 요청 없음) | Nginx → C3 |
| 폰 1·2차 | 폰으로 모니터의 시뮬레이터 화면 촬영 (cloudflared 터널) | 전 구간 |

## 2. Health

| 서비스 | 결과 |
|---|---|
| C1 `/healthz` | ok, transport mqtt, `mqtt_connected: true` |
| C2 `/healthz`, `/stats` | ok, `mqtt-broker:1883` 연결, 구독 `psf/v1/frames/+` |
| C3 `/healthz`, `/metrics` | ready, device cuda, VRAM torch reserved 640MiB |
| Nginx `/healthz` | 200 |
| C4 | 응답 없음 (placeholder) |

## 3. 단계별 결과 (C3 ×1, `C2_MAX_INFLIGHT=4`)

### 3.1 처리량

| 지표 | loadgen (폰 프레임 5장) | sim (시뮬레이터 PNG) |
|---|---|---|
| C1 수신·발행 | 600 (30.0fps), C1 drop 0 | — (C2로 직접 발행 300) |
| C2 forwarded | 218 (**10.9fps**) | 108 (**10.8fps**) |
| C2 dropped_stale / dropped_old / error / timeout | 382 / 0 / 0 / 0 | 192 / 0 / 0 / 0 |
| C2 c4_error | 218 (C4 미구현) | 108 |
| C3 processed_frame_count / error | 218 / 0 | 108 / 0 |
| C2 preprocess_ms p50 / p95 | 11 / 19 | 15 / 17 |
| C3 total_inference_ms p50 / p95 | 304 / 475 | 345 / 396 |
| C2 `/infer` RTT p50 / p95 | 313 / 483 | 351 / 403 |
| 촬영 → C3 응답(C2 기준) p50 / p95 | 352 / 542 | 394 / 447 |
| GPU 사용률 p50 / max, VRAM(전체) | 64% / 84%, 5352MiB | — |

### 3.2 prompts 수 (C2 검출)

| 이미지 | 기대 개수 | C2 prompts (모든 프레임) |
|---|---|---|
| phone_frame_0000 | 6 | 6 (38/38) |
| phone_frame_0150 | 6 | 6 (40/40) |
| phone_frame_0297 | 6 | 6 (54/54) |
| phone_frame_0450 | 5 | 5 (41/41) |
| phone_frame_0497 | 4 | 4 (45/45) |
| 시뮬레이터 PNG | 15 (라벨) | 15 (108/108) |

loadgen이 960 폭으로 재인코딩한 입력에서도 기대 개수와 모두 일치했다.

### 3.3 판정 분포

| 입력 | normal | defect | unknown | defect 사유 |
|---|---|---|---|---|
| loadgen (1177 객체) | 638 (54.2%) | 539 (45.8%) | 0 | broken:concavity 413, broken:aspect_ratio 126 |
| sim (1620 객체) | 864 (53.3%) | 756 (46.7%) | 0 | broken:concavity 540, broken:aspect_ratio 216 |

spot_defect(초코칩 오검출)는 이번 입력에서 0건이었다.

### 3.4 객체 단위 판정 (시뮬레이터 PNG, 라벨과 IoU 매칭)

C2가 1045×450 → 1024×441로 리사이즈(배율 0.9799)했다. 라벨 bbox에 배율을 곱해 C3 detections와 IoU로 짝지었다. 15/15가 짝지어졌고(IoU 0.973~0.987), **15/15 판정이 라벨과 일치**했다.

| 라벨 id | 라벨 | 정답 | IoU | C3 판정 | defect_score | 원형도 | 오목도 | 종횡비 |
|---|---|---|---|---|---|---|---|---|
| 16 | wedge | defect | 0.986 | defect/broken | 1.00 | 0.633 | 0.332 | 0.942 |
| 18 | bite | defect | 0.973 | defect/broken | 1.00 | 0.827 | 0.090 | 0.947 |
| 19 | bite | defect | 0.985 | defect/broken | 1.00 | 0.839 | 0.083 | 0.943 |
| 21 | half | defect | 0.980 | defect/broken | 0.68 | 0.718 | 0.020 | 0.561 |
| 25 | wedge | defect | 0.981 | defect/broken | 1.00 | 0.615 | 0.351 | 0.946 |
| 26 | bite | defect | 0.981 | defect/broken | 1.00 | 0.840 | 0.100 | 0.943 |
| 27 | cut | defect | 0.978 | defect/broken | **0.52** | 0.857 | 0.016 | 0.839 |
| 17 | round | normal | 0.982 | normal | 0.25 | 0.900 | 0.012 | 1.000 |
| 20 | round | normal | 0.987 | normal | 0.26 | 0.896 | 0.012 | 1.000 |
| 22 | round | normal | 0.985 | normal | 0.28 | 0.889 | 0.012 | 0.987 |
| 23 | round | normal | 0.985 | normal | 0.24 | 0.902 | 0.012 | 0.987 |
| 24 | round | normal | 0.985 | normal | 0.27 | 0.894 | 0.011 | 1.000 |
| 28 | round | normal | 0.983 | normal | 0.25 | 0.901 | 0.012 | 0.987 |
| 29 | round | normal | 0.985 | normal | 0.27 | 0.894 | 0.012 | 1.000 |
| 30 | round | normal | 0.973 | normal | 0.26 | 0.896 | 0.012 | 0.987 |

`cut`(27)은 defect_score 0.52로 임계값(0.5) 바로 위다.

### 3.5 C3 지연 분해

| 단계 (ms, p50) | probe 15 prompts | probe 6 prompts | sim 부하 15 prompts | loadgen 부하 4~6 prompts |
|---|---|---|---|---|
| decode | 1.5 | 2.0 | 1.6 | 2.1 |
| **lock_wait** | 0.0 | 0.0 | **223.9** | **202.0** |
| encoder | 55.8 | 56.7 | 58.0 | 62.8 |
| segmentation | 34.5 | 17.0 | 34.0 | 16.7 |
| defect_analysis | 4.9 | 2.6 | 11.4 | 6.1 |
| mask_encode | 11.7 | 6.0 | 15.1 | 9.3 |
| **total_inference** | **108.1** | **85.4** | **345.4** | **304.2** |
| Nginx 경유 RTT | 118.2 | 94.4 | 350.5 | 312.6 |

probe는 응답 헤더 `X-C3-*`, `Server-Timing` 30회의 p50이고, 부하 열은 C3 프레임별 로그다.

- 부하 시 C3 지연의 약 2/3가 `lock_wait`다. C3 ×1에 `C2_MAX_INFLIGHT=4`로 동시 요청 4개가 들어가 predictor lock에서 대기한다. 처리량은 서비스 시간(약 92~108ms)으로 정해지므로 대기는 지연만 늘린다.
- encoder는 프레임당 1회라 prompts 수와 무관하다(약 56ms). prompts 15개와 6개의 차이(약 23ms)는 segmentation, mask_encode, defect_analysis에서 나온다.
- `total_inference_ms`(= detection-result `inference_latency_ms`)에 `lock_wait`가 포함된다.

### 3.6 CSV 조인

| 실행 | C1 published | C2 trace | 조인 | trace당 C2 이벤트 |
|---|---|---|---|---|
| loadgen | 600 | 600 | 600/600 | 정확히 1개 (`c4_error` 제외) |
| scale-out ×1 / ×3 | 600 / 600 | 600 / 600 | 600/600, 600/600 | 1개 |

Frame Drop Rate(C2) = dropped_stale / published로 바로 계산할 수 있다.

## 4. Scale-out 비교 (loadgen 동일 부하, `C2_MAX_INFLIGHT=4`)

VRAM을 쓰던 프로그램 하나를 종료한 뒤 ×1을 다시 측정하고 ×3을 측정했다. ×3 기동 후 VRAM 여유는 5343MiB로 부족하지 않았다.

| 지표 | C3 ×1 | C3 ×3 |
|---|---|---|
| C1 발행 | 600 (30fps) | 600 (30fps) |
| C2 forwarded / dropped_stale | 220 / 380 | 286 / 314 |
| **처리량** | **11.0fps** | **14.3fps (+30%)** |
| replica별 처리 | 220 | 96 / 95 / 95 (round-robin 균등) |
| lock_wait p50 / p95 | 218 / 295 | 0.2 / 101 |
| encoder p50 / p95 | 67 / 103 | **119 / 261** |
| segmentation p50 | 17.5 | 51.0 |
| total_inference p50 / p95 | 325 / 442 | 216 / 374 |
| 촬영 → C3 응답 p50 / p95 | 375 / 495 | 279 / 468 |
| GPU 사용률 p50 / max | 63% / 82% | **89% / 100%** |
| VRAM 사용(전체, max) | 5351MiB | 6851MiB (replica당 약 +0.75GB) |
| replica별 torch reserved | 640MiB | 640MiB × 3 |

- 단일 GPU에서 replica 3배는 처리량 1.3배다. GPU가 포화되고 time-slicing으로 encoder/segmentation 시간이 약 2~3배로 늘었다.
- ×3의 지연 감소는 주로 lock 대기가 사라진 효과다. 대기를 뺀 서비스 시간은 ×1이 가장 짧다.

## 5. 폰 테스트 (C3 ×1, `C2_MAX_INFLIGHT=2`)

두 촬영 모두 세션 id `m-4qf015`다(폰이 같은 id로 재접속, 2차에서 frame_id가 0부터 다시 시작). frame_id가 다시 시작된 지점으로 1차와 2차를 나눴다.

| 지표 | 1차 | 2차 (재촬영) |
|---|---|---|
| 길이 | 32.6s | 17.9s |
| C1 수신 | 907 (27.8fps), C1 drop 0 | 498 (27.8fps), C1 drop 0 |
| C2 forwarded / dropped_stale / error | 771 / 136 / 0 | 227 / 271 / 0 |
| **C2 → C3 처리량** | 23.6fps | **12.7fps** |
| **촬영 → C3 응답 p50 / p95** | 96 / 270ms | **268 / 313ms** |
| prompts > 0인 프레임 | **152/771 (20%)** | **206/227 (91%)** |
| 프레임당 prompts | 0개 619, 1~10개 152 | 0:21, 1:9, 2:6, 3:124, 4:59, 5:8 |
| 판정 (객체 수) | 535 | 669 |
| normal / defect / unknown | 48.4% / 50.8% / 0.7% | **64.3% / 35.7% / 0%** |
| defect 사유 | concavity 149, aspect_ratio 121, circularity 2 | **aspect_ratio 160**, concavity 79 |
| 시계 보정 차이(`client_clock_gap_ms`) | p50 68 / p95 77 | — |

- 1차는 프레임 80%에서 prompts가 0개였다. C3가 빈 요청을 빨리 처리해서 처리량과 지연이 좋아 보일 뿐이고, 성능 수치로 쓰면 안 된다.
- 2차가 이 구성의 실제 성능에 가깝다. loadgen ×1(`C2_MAX_INFLIGHT=4`)의 촬영→C3 p50 375ms보다 낮지만, 입력이 달라 `C2_MAX_INFLIGHT` 효과만 따로 분리한 비교는 아니다.
- 폰 프레임을 저장하지 않아서 prompts 수와 화면 속 쿠키 수가 맞는지, defect 비율이 정답과 맞는지는 확인하지 못했다.

## 6. 정상 동작한 것

- 전 구간 C1 → MQTT(11883) → C2 → Nginx → C3가 오류 없이 연결됐다(C2 error/timeout 0, C3 error 0).
- C2 세션별 최신 1장 정책: 입력 30fps, 처리 약 11fps에서 dropped_stale로 버려 지연이 누적되지 않았다. dropped_old 0.
- C2 prompts 수: fixture 6종 모두 기대값과 100% 일치. 시뮬레이터 객체 단위 판정 15/15 일치.
- C1/C2 이벤트 CSV `trace_id` 조인 100%.
- Nginx round-robin: ×3에서 96/95/95로 균등 분배. replica 추가와 제거 시 Nginx 재시작이 필요 없었다.
- 호스트 1883(`ocr-mqtt`)에는 접속하거나 발행하지 않았다.

## 7. 실패, 예상된 실패와 원인

| 항목 | 원인 | 분류 |
|---|---|---|
| C2 `c4_error` 100%, C3 `results_push_error` 100% (`ConnectError`) | C4가 main에서 `sleep infinity` | 예상됨 |
| 폰 1차 prompts 0개 80% | 미조사. 후보: 모니터 재촬영 시 색(HSV 범위), ROI auto 검출 실패, 테두리 접촉 제외. 2차 촬영에서는 91%로 개선 | 확인 필요 (C2) |
| 첫 loadgen 실행이 "opencv 설치" 오류로 종료 | Git Bash가 컨테이너 인자 `/tmp/phone5`를 Windows 경로로 바꿈. `MSYS_NO_PATHCONV=1`로 해결 | 테스트 환경 |
| 호스트 python에 `websockets`, `paho-mqtt` 없음 | loadgen은 c1-gateway, `publish_frames.py`는 c2-preprocessing 컨테이너 안에서 실행 | 테스트 환경 |

## 8. 담당자별 전달 사항

### Member A (C1, compose)
- 폰이 재접속할 때 같은 `session_id`(`m-4qf015`)를 쓰고 `frame_id`가 0부터 다시 시작한다. trace_id 조인에는 문제가 없지만, 계약의 "세션 내 frame_id 단조 증가"와 어긋나고 C2의 세션별 최신 1장/EMA 상태가 이전 연결과 섞인다. 재접속마다 새 session_id를 주거나 frame_id를 이어 가는 것을 제안한다.
- loadgen은 입력을 폭 960, q70으로 재인코딩한다(문서화 권장). 실측 처리량 비교 시 폰 `/cam` 설정과 맞춰야 한다.
- `.env.example`에 `C2_MAX_INFLIGHT`를 추가하고 "C3 replica 수 이상, ×1이면 1~2" 주석을 다는 것을 제안한다(PROPOSALS P5와 함께).

### Member B (C2, 본인)
- 폰 1차 촬영의 prompts 0개 80% 원인을 조사한다. 폰 프레임을 저장(`c1_gateway.py stub --save-dir`)하고 같은 C2 코드로 HSV, ROI, 테두리 제외를 단계별로 확인한다.
- `C2_MAX_INFLIGHT` 권장값을 문서화한다. ×1에서 4는 처리량 이득 없이 C3 대기 약 200ms를 만든다.
- PROPOSALS P8(api-contract에 C2 → C4 구간)은 테스트 이후 PR #11(`camera-frame` 스키마, api-contract 2b절)로 반영됐다. `PROPOSALS.md`의 P8 상태를 갱신하고, C2 출력이 새 스키마를 통과하는지 확인한다.

### Member C (C3, Nginx, benchmark)
- `inference_latency_ms`(`total_inference_ms`)에 predictor lock 대기(`lock_wait`)가 포함된다. 부하 시 값의 약 2/3가 대기다. 계약의 "C3 inference latency"와 의미가 달라지므로 대기를 빼거나 별도 필드로 나누는 것을 제안한다.
- 시뮬레이터 `cut` 결손이 defect_score 0.52로 임계값에 가깝다. 실제 폰 입력에서는 놓칠 수 있다.
- 폰 2차 촬영에서 불량 사유 1위가 `broken:aspect_ratio`(160/239)다. 비스듬한 촬영으로 원이 타원이 된 것일 수 있으니 확인이 필요하다(정답 라벨 없음).
- 이번 입력에서 spot_defect는 0건이었다.
- 단일 GPU scale-out: ×3에서 처리량 +30%, GPU 100% 포화, encoder p50이 67ms에서 119ms로 늘었다. VRAM은 replica당 약 0.75GB.

### Member D (C4)
- C2는 `POST http://c4-visualizer:8003/api/camera/frame`(10개 필드), C3는 `POST http://c4-visualizer:8003/results`로 이미 전송을 시도하고 있다. C4가 뜨면 바로 연결된다.
- C3 ×1, `C2_MAX_INFLIGHT=2` 기준 C4에 도착할 프레임은 약 12~13fps다.
