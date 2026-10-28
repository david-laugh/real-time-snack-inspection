# C3 × C2(MQTT) 연동 시험 보고서

| 항목 | 내용 |
|---|---|
| 작성일 | 2026-10-28 |
| 브랜치 | `feature/inference` (`origin/main` 5c5c014 = C1 MQTT PR #5, C2 MQTT PR #6 머지본 포함) |
| 목적 | ① C1 → C2 프로토콜이 MQTT로 바뀐 뒤 C2 → C3 연동 확인, ② C2(Member B) 요청 3건 회신, ③ 실제 체인 부하 시험으로 replica 수, `C2_MAX_INFLIGHT`, Nginx timeout 결정 |
| 수정 범위 | **C3만 수정**: `services/c3-inference/`, `infra/nginx/nginx.conf`(C3 inference LB, Member C 담당), C3 문서. C1, C2, C4, `shared/schemas`, `docker-compose.yml`, `.env.example`은 변경하지 않음 |
| 관련 문서 | [구현 정리](c3-inference-implementation.md) · [1·2차 검증 보고서](c3-inference-validation-report.md) · [C3 dev 도구](../services/c3-inference/dev/README.md) |

> 이 문서의 수치는 모두 실제 실행 결과다. 입력 영상은 C2 fixture(시뮬레이터 1장, 폰 5장)뿐이다.

---

## 1. 요약

| 항목 | 결과 |
|---|---|
| C2(MQTT 버전) → C3 계약 | ✅ 변경 없음. `forward_to_infer`는 JSON body, trace header 3개, timeout 3 s 그대로다. 실제 체인 9개 구성에서 Nginx 응답이 모두 200, C2 timeout 0 |
| C2 요청 ① 불량 판정 기본값 | ✅ 실제 SAM2 mask로 이미 재보정함(커밋 `9f9f57b`). 42/42. C2가 제안한 값(원형도 0.87, solidity 0.975, 면적 ±7%)은 실제 mask에서 39/42 |
| C2 요청 ② `expected_diameter_px` | 지금은 **C3에 필요 없음**. 실제 mask의 면적 비율이 정상과 불량 사이에서 겹친다(§3.2). 스키마에 optional로 추가하는 것 자체에는 반대하지 않는다 |
| C2 요청 ③ replica, inflight, timeout | **30 fps 1 스트림: C3 ×2, `C2_MAX_INFLIGHT=3`**. 일반 규칙은 `C2_MAX_INFLIGHT = replica 수 + 1`이다. Nginx `proxy_read_timeout` 2500ms로 변경 |
| 실제 체인 시험 (C1 → MQTT → C2 → Nginx → C3 → C4) | 9개 구성 모두 성공. C3 오류 0, C4 전송 실패 0. 30 fps에서 C3 ×2면 C2 전달 30.0 fps, Nginx p50/p95 42/47 ms |
| pytest | 105 passed (실제 SAM2 포함). C2 fixture E2E 42/42 |
| 시험 중 발견해 고친 결함 | Nginx `2.5s` 표기 오류. nginx는 소수 초를 받지 않아 재시작 루프에 빠졌다 → `2500ms`로 수정하고 `nginx -t`로 검증 |

---

## 2. C2 → C3 계약 재확인 (MQTT 전환 후)

C2 `origin/main`(PR #6)의 `src/c2/app.py`를 분석하고 실제로 실행했다.

| 항목 | MQTT 전환 전 | MQTT 전환 후 | C3 영향 |
|---|---|---|---|
| C1 → C2 | HTTP `POST /preprocess` (동기) | MQTT `psf/v1/frames/{session}`, QoS 0. C2는 세션별 최신 1장만 보관 | 없음 |
| C2 → C3 | HTTP `POST nginx-inference-lb:8080/infer` | **동일** (`forward_to_infer`, JSON, `X-Trace-Id`/`X-Frame-Id`/`X-Session-Id`) | 없음 |
| C3에 동시에 들어오는 요청 | C1 `C1_MAX_INFLIGHT`(2)가 상한 | **C2 워커 `C2_MAX_INFLIGHT`(기본 4)** | replica 수에 맞춰 조정 필요 → §4 |
| C2 `/infer` timeout | 3 s | 3 s | Nginx를 그보다 짧게 → §4.3 |
| C2가 보내는 이미지 | 긴 변 1024, JPEG q90 | 동일 (C1 960폭 폰 영상은 576×1024로 C3에 도착) | 없음 |
| 잘린 쿠키 | C2가 제외 | 동일 | 없음 |

---

## 3. C2 요청 회신

### 3.1 [요청 ①] 불량 판정 기본값을 실제 SAM2 mask로 재보정

**이미 반영되어 있다** (커밋 `9f9f57b`). C2가 지적한 문제(초코칩 때문에 spot 규칙이 오판하고, 파임과 절단을 놓침)를 같은 fixture에서 실제 SAM2.1-tiny mask로 재현해서 고쳤다.

| 규칙 세트 (실제 SAM2 mask 42개, 상태와 유형 모두 일치한 수) | 결과 |
|---|---|
| C3 1차 기본값 (spot 켬, 원형도 0.70, 종횡비 0.75) | 20 / 42 |
| C2 제안 (spot 끔, 원형도 0.87, solidity 0.975, 면적 ±7%, 면적 기준은 C2 지름 추정치) | 39 / 42. 폰 정상 쿠키 3개가 면적 규칙에 걸림(비율 0.896, 1.134, 1.171) |
| **C3 현재 기본값** (spot 끔, 오목 깊이 > 0.03, 종횡비 < 0.87, 원형도 < 0.80 보조, solidity < 0.90 보조) | **42 / 42** |

C2 제안에서 **원형도 0.87을 주 기준으로 쓰지 않은 이유**: 완벽한 마스크에서도 원형도는 픽셀 격자 때문에 크기에 따라 달라진다. 완벽한 원이 지름 30px에서 0.855, 50px에서 0.874, 100px에서 0.891이다. 그래서 0.87 기준이면 작은 쿠키(C1 640px 가로 촬영 등)를 정상인데도 불량으로 판정한다. 대신 크기와 무관한 비율인 **오목 깊이 ÷ 지름**(bite, wedge)과 **종횡비**(cut, half)로 나눴다.

근거 데이터(실제 SAM2 mask)는 [검증 보고서 §9.2](c3-inference-validation-report.md#92-불량-판정-1차-규칙-vs-2차-규칙)에 있다. 회귀 테스트는 `services/c3-inference/tests/fixtures/c2_snack_masks.json`(42개)이다.

### 3.2 [요청 ②] `expected_diameter_px`(C2 PROPOSALS P2)에 대한 의견

**결론: 현재 C3 판정에는 필요 없다. 스키마에 optional로 추가하는 것은 반대하지 않는다(우선순위 낮음).**

C2의 지름 추정치(세션 EMA)로 기대 면적 π(d/2)²를 만들고, 실제 SAM2 mask 면적과 비교했다.

| 라벨 | 개수 | 실제 면적 ÷ 기대 면적 |
|---|---|---|
| 정상 (round) | 23 | **0.896 ~ 1.171** |
| bite | 7 | 0.972 ~ 1.181 |
| wedge | 7 | 0.886 ~ 1.064 |
| cut | 4 | 0.841 ~ 0.974 |
| half | 1 | 0.591 |

- 정상 쿠키의 면적 비율 폭(±15%)이 bite, wedge, cut 범위를 모두 덮는다. 원인은 SAM2 mask와 C2 HSV 마스크의 경계 차이, 폰 원근, 그림자다. 그래서 면적 규칙은 **반쪽(half) 수준의 큰 결손**에서만 구분력이 있다. 그런데 half는 종횡비 규칙(0.558)이 이미 잡는다.
- C3의 현재 규칙은 모두 크기와 무관한 비율이라서 **촬영 거리가 바뀌어도 기대 크기가 필요 없다.** C2가 지적한 "고정 px 면적 규칙(`C3_BROKEN_EXPECTED_AREA_PX`)은 거리가 바뀌면 쓸 수 없다"는 문제는, 이 규칙을 기본으로 끈 상태로 두는 것으로 해결된다.
- 필드가 추가되면 쓸 곳: 오목 깊이의 픽셀 하한(`C3_BROKEN_CONCAVITY_MIN_PX=2`)을 지름에 비례시키기, 결과 로그와 C4 표시용 메타데이터.
- 추가할 경우 주의할 점: C3는 스키마(`additionalProperties: false`)를 엄격히 따른다. 그래서 **스키마 변경(1.1.0)이 머지되고 C3가 같이 업데이트되기 전에 C2가 이 필드를 보내면 C3는 422를 반환한다.** 스키마 PR에 C3를 리뷰어로 넣어 주길 요청한다.

### 3.3 [요청 ③] replica 수, `C2_MAX_INFLIGHT`, Nginx timeout

§4의 실제 체인 시험으로 정했다.

| 항목 | 권장값 | 근거 |
|---|---|---|
| C3 replica (단일 GPU, 30 fps 1 스트림) | **2** | 1개는 상한이 약 29 fps라서 대기열이 생기거나(inflight 4: Nginx p50 127 ms) 프레임이 버려진다(inflight 2: 15% drop). 2개면 대기열 0, p50 42 ms. 3개는 p50이 같고 p95만 나빠진다(47 → 74 ms) |
| `C2_MAX_INFLIGHT` | **replica 수 + 1** (×1: 2, ×2: **3**, ×3: 4) | replica 수보다 크게 잡으면 C3 안에서 대기열만 생기고, 단일 GPU에서는 경합이 커진다. ×3에서 inflight 4 → 6으로 올리면 처리량 35.6 → 31.1 fps, p50 86 → 179 ms로 오히려 나빠졌다. C1 `.env.example`의 "C3 복제 수 + 1" 가이드와 같다 |
| 60 fps(2 스트림) 이상 | C3 ×3, inflight 4. 단일 GPU에서는 **약 30~36 fps가 상한** | GPU 하나를 time-slicing으로 나눠 쓰기 때문에 replica를 늘려도 처리량이 비례하지 않는다(×1 29.0 → ×2 30.5 → ×3 35.6 fps). 더 늘리려면 GPU를 추가하거나 프레임당 연산을 줄여야 한다(§6) |
| Nginx `proxy_read_timeout` / `proxy_send_timeout` | **2500ms** (기존 10s) | C2 3 s보다 짧다. C3가 멈추면 C2가 포기하기 전에 Nginx 504와 `upstream=`이 access log에 남는다. timeout 체인: C1 5 s > C2 3 s > Nginx 2.5 s. 실측 최대 응답 203 ms |
| Nginx `proxy_connect_timeout` | **1s** (기존 2s) | 같은 Docker 네트워크 안의 연결이다 |

---

## 4. 실제 체인 부하 시험

### 4.1 구성

```
C1 loadgen (WS, 폭 960, 30 fps × client 수)
  → C1 gateway ─MQTT(mosquitto)→ C2 (C2_MAX_INFLIGHT 워커)
  → Nginx (round-robin) → C3 ×N (RTX 5060 Ti 1장 공유, bf16)
  → C4 (C4-Visualizer 브랜치, 호스트 :8003)
```

| 항목 | 값 |
|---|---|
| 실행 | `services/c3-inference/dev/bench_chain.py` + `dev/chain.override.yml` (재현 방법: [dev/README.md](../services/c3-inference/dev/README.md)) |
| C1, C2, C4 코드 | 각각 `origin/main`(C1, C2)과 `origin/C4-Visualizer` 그대로. override는 C2를 uvicorn으로 띄우고 C1에 프레임 폴더를 mount하는 것만 한다 |
| 입력 | C2 폰 fixture 5장을 C1이 폭 960으로 줄여 반복 송출. C2는 576×1024로 C3에 보낸다. 프레임당 bbox 4~6개 |
| 측정 | 구성마다 warm-up 3 s 후 **30 s**. 30 fps면 900프레임, 60 fps면 1,800프레임 |
| 환경 | Windows 11, Docker 29.1.2, RTX 5060 Ti 16 GB, C3 이미지 torch 2.7.1+cu128, SAM2.1-tiny, bf16 |

### 4.2 결과

| # | C3 replica | `C2_MAX_INFLIGHT` | 입력 | C2 → C3 전달 | C2 drop (stale) | C2 timeout | Nginx 분배 | Nginx p50 / p95 / max (ms) | C3 대기 p50 / p95 (ms) | C3 encoder p50 (ms) | GPU 평균 / 최대 (%) | GPU 메모리 최대 (MiB) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1 | 2 | 30 fps | 764 (25.5 fps) | 136 | 0 | 764 | 70 / 76 / 79 | 21.3 / 24.7 | 28.7 | 63 / 82 | 3,004 |
| 2 | 1 | 4 | 30 fps | 886 (29.5 fps) | 14 | 0 | 886 | 127 / 133 / 141 | 83.3 / 89.0 | 25.0 | 60 / 76 | 2,940 |
| 3 | **2** | **3** | 30 fps | **899 (30.0 fps)** | 1 | 0 | 449 / 450 | **43 / 47 / 125** | **0 / 0** | 24.1 | 61 / 87 | 3,827 |
| 4 | 2 | 4 | 30 fps | 900 (30.0 fps) | 0 | 0 | 450 / 450 | 42 / 47 / 108 | 0 / 0 | 23.9 | 60 / 86 | 3,739 |
| 5 | 3 | 4 | 30 fps | 899 (30.0 fps) | 1 | 0 | 299 / 300 / 300 | 43 / 74 / 157 | 0 / 0 | 24.1 | 62 / 93 | 4,469 |
| 6 | 1 | 4 | 60 fps | 870 (29.0 fps) | 930 | 0 | 870 | 127 / 153 / 168 | 83.8 / 103.8 | 24.9 | 61 / 81 | 2,972 |
| 7 | 2 | 4 | 60 fps | 916 (30.5 fps) | 884 | 0 | 458 / 458 | 121 / 128 / 138 | 43.5 / 48.5 | **49.4** | 66 / 88 | 3,774 |
| 8 | **3** | **4** | 60 fps | **1,069 (35.6 fps)** | 731 | 0 | 356 / 356 / 357 | 86 / 152 / 201 | 0 / 54.7 | 43.6 | 74 / 97 | 4,593 |
| 9 | 3 | 6 | 60 fps | 932 (31.1 fps) | 868 | 0 | 310 / 311 / 311 | 179 / 189 / 203 | 70.2 / 77.7 | **73.3** | 67 / 85 | 4,639 |

모든 구성에서 공통으로 확인한 사항:
- C1 수신 = 송출 프레임 수(900 / 1,800)
- Nginx 응답 **100% 200**, C3 `error_count` **0**
- C3 → C4 전송 **성공 = 처리 프레임 수**, 실패 0
- C4가 받은 detection 수 4,140 ~ 5,816 (프레임당 4~6개)
- replica별 C3 프로세스 GPU 메모리(`torch_reserved`)는 **640 MiB**로 동일

### 4.3 해석

1. **replica 1개는 30 fps의 경계에 있다.** 프레임당 연산은 약 40 ms다(decode 1.2, encoder 24~29, segmentation 9, defect 2.5, mask RLE 4).
   - SAM2 lock이 잡는 구간은 encoder + segmentation(약 34 ms)뿐이다. decode, 불량 분석, RLE는 lock 밖에서 다른 요청과 겹쳐 실행된다. 그래서 replica 1개의 상한은 약 1000 / 34 ≈ **29 fps**다.
   - inflight 2(#1)로는 겹쳐 실행되는 정도가 부족해서 25.5 fps에 그치고 C2가 15%를 버린다.
   - inflight 4(#2)면 29.5 fps까지 나온다. 대신 C3 안에서 요청이 대기해서 지연이 약 80 ms 늘어난다(Nginx p50 127 ms).
2. **replica 2개 + inflight 3(#3)이 30 fps의 최적 구성**이다. 대기열 0, Nginx p50 43 ms, drop 0.1%.
   - inflight를 4로 올려도(#4) 차이가 없다. 들어오는 프레임은 33 ms 간격이고 처리는 40 ms라서, 동시에 2개 이상 처리 중일 일이 드물기 때문이다.
3. **단일 GPU에서는 replica 수에 비례해 빨라지지 않는다.** 60 fps 입력에서 replica 2개(#7)는 encoder가 25 → 49 ms로 느려져 처리량이 거의 그대로다(29.0 → 30.5 fps). GPU 하나를 time-slicing으로 나눠 쓰기 때문이다. replica 3개(#8)에서 35.6 fps로 약간 올라간다.
4. **inflight를 replica 수보다 크게 잡으면 손해다.** #9(×3, inflight 6)는 #8보다 처리량이 줄고(35.6 → 31.1 fps) 지연이 두 배가 됐다(encoder 73 ms). C3 안에 쌓인 요청이 GPU 경합만 키운다.
5. **Nginx timeout 2.5 s는 충분하다.** 전체 시험에서 최대 응답이 203 ms였다.

---

## 5. 이번에 수정한 내용 (C3 범위)

| 파일 | 변경 | 이유 |
|---|---|---|
| `infra/nginx/nginx.conf` | `proxy_connect_timeout 2s → 1s`, `proxy_send_timeout`(신규) / `proxy_read_timeout 10s → 2500ms` | C2 요청 ③: C2 timeout 3 s보다 짧게. upstream 목록과 분배 방식은 그대로 |
| `services/c3-inference/src/metrics.py` | `/metrics`의 `latency_window_ms`에 `lock_wait_ms`(replica 안의 대기 시간) 추가 | 부하 시험에서 `total_inference_ms`의 절반 이상이 대기 시간이었다(#2: 125 ms 중 83 ms). 처리가 느린 것과 대기가 긴 것을 구분하려고 추가 |
| `services/c3-inference/tests/test_api.py` | 위 지표 검증 추가 | |
| `services/c3-inference/dev/` (신규) | `chain.override.yml`, `bench_chain.py`, `README.md` | 이 보고서의 시험을 명령 한 줄로 재현 |
| 문서 | 이 보고서, C3 README | |

시험 중 발견해서 바로 고친 결함:
- **Nginx 시간 단위 오류.** 처음에 `proxy_read_timeout 2.5s`로 썼는데, nginx는 소수 초를 받지 않는다(`invalid value`). 컨테이너가 재시작 루프에 빠져 C2가 `ConnectTimeout`을 냈다. `2500ms`로 고치고 `nginx -t`와 실제 체인으로 확인했다.
- **`dev/bench_chain.py` 변수 가림 버그.** `args`를 함수 인자 이름이 가렸다. `opts`로 바꾸고 C4 없이 `--plan 2:3:1`로 실행해서 확인했다(29.7 fps, timeout 0).

---

## 6. 테스트 결과

| 항목 | 결과 |
|---|---|
| pytest (`services/c3-inference`) | **105 passed** (`test_api` 29, `test_defects` 71, `test_sam2_real` 5) |
| C2 fixture 회귀 (저장된 실제 SAM2 mask) | 42 / 42 |
| C2 fixture E2E (실제 SAM2, GPU) | 42 / 42 |
| 단독 추론 시간 (1024×1024, 20프레임 중앙값, bbox 1 / 4 / 8) | 39.1 / 49.2 / 66.1 ms |
| Nginx 설정 | `nginx -t` 통과, 체인 시험 9구성에서 200 100% |
| 실제 체인 9구성 | §4.2 |
| `dev/bench_chain.py` (저장소 버전) | `--plan 2:3:1 --duration 10`, C4 없이 실행: 29.7 fps, timeout 0 |

---

## 7. 남은 과제와 제안

| 대상 | 내용 |
|---|---|
| Member A (compose) | `docker-compose.yml`의 c2가 아직 `sleep infinity`다. `dev/chain.override.yml`의 c2 `command`를 base로 옮기면 compose만으로 전체 체인이 뜬다. 벤치마크 구성별 `C2_MAX_INFLIGHT`는 §3.3 표대로 |
| Member B (C2) | `C2_MAX_INFLIGHT` 기본값 4 → 단일 replica 기준으로는 2, 권장 구성(×2) 기준으로는 3. `expected_diameter_px` 스키마 PR에 C3 리뷰어 추가 |
| Member D (C4) | C4가 C3의 `status` 대신 bbox 원형도로 다시 판정하는 문제([검증 보고서 §9.3](c3-inference-validation-report.md#93-전체-체인-연동-c2--c3--c4)) |
| C3 (후속) | 처리량을 높일 방법: ① C4가 mask를 쓰지 않으면 `C3_RETURN_MASK=0`(프레임당 RLE 약 4 ms 절감), ② encoder 입력 해상도와 batch 최적화, ③ GPU 추가 |
| 시험 한계 | 입력은 fixture 5장 반복이고 세션은 1~2개뿐이다. 실제 폰 스트림, 장시간 시험, 세션 수 증가는 측정하지 않았다. 단일 GPU 결과이며 다중 GPU에서는 결론이 달라질 수 있다 |

---

## 8. 추가: 구간별 지연과 frame drop을 C4로 전달 (C2 제안 회신)

### 8.1 C2 제안과 C3 회신

| C2 제안 | 회신 |
|---|---|
| 1. C2가 `camera_frame`에 `gateway_received_at_ms`, `preprocess_started_at_ms`, `preprocess_finished_at_ms`를 넣고, C4가 trace_id로 C3 결과와 짝짓기 | **동의하되 범위 축소 제안.** `preprocess_*`는 C3가 이미 받고 있어서 C3 결과(1.1.0)에 넣어 보낸다. C3가 C2→C3 구간까지 직접 계산해 `latency_breakdown_ms`로 보내므로 C4는 짝짓기 없이 바로 표시할 수 있다. C2가 `camera_frame`에 추가할 필드는 **`gateway_received_at_ms` 하나면 충분하다**(C1 업로드와 C1→C2 구간 분리용) |
| 2. Frame drop은 C4가 C1 `/api/metrics`, C2 `/stats`를 주기적으로 조회 | **동의.** C3는 버려진 프레임을 볼 수 없고, replica마다 일부 프레임만 보므로 drop을 계산할 수 없다. C4가 C2 `/stats`에서 `forwarded`, `error`, `timeout`도 읽으면 C2→C3 실패율까지 나온다. 계산식은 [`c3-to-c4-handoff.md` §4](c3-to-c4-handoff.md#4-frame-drop-계산) |
| 3. 발표용 최종 숫자는 이벤트 CSV + trace_id 조인 | **동의.** C3도 프레임마다 JSON 로그 한 줄(trace_id, `timings_ms`, `latency_breakdown_ms`, 판정 근거)을 남긴다. `dev/bench_chain.py`는 이 로그로 구간별 p50/p95를 집계한다 |

### 8.2 C3 변경

| 파일 | 변경 |
|---|---|
| `shared/schemas/detection-result.schema.json` | **1.1.0, optional 필드만 추가**: `preprocess_started_at_ms`, `preprocess_finished_at_ms`, `inference_received_at_ms`, `latency_breakdown_ms`, `inference_timings_ms`. C3가 producer인 스키마이고, 기존 필드는 그대로다. C2의 스키마 사본 검사(`frame-envelope`, `processed-frame`)에는 영향이 없다. **C4(Member D) 리뷰 필요** |
| `services/c3-inference/src/{schemas,pipeline,app}.py` | 요청 도착 시각 기록, 구간별 지연 계산(시계가 역전되면 null), 결과와 프레임별 로그에 포함 |
| `docs/c3-to-c4-handoff.md` (신규) | C4 작업자용 전달 사양: endpoint, 필드, mask RLE, 지연 표시, drop 계산식, 해야 할 일 |
| `docs/api-contract.md` | detection-result 1.1.0 필드와 변경 이력 |

### 8.3 검증

| 항목 | 결과 |
|---|---|
| pytest | 108 passed (신규: 구간 계산, 시계 역전 → null, C4로 전달되는 본문에 포함) |
| 실제 체인 (C1 → MQTT → C2 → Nginx → C3 ×2 → **C4 최신 브랜치 `23022c2`**), 30 fps, 30 s, inflight 3 | C2 forwarded 900, error 0, timeout 0. C3 → C4 전송 900/900. C4 오류 로그 0건(새 필드를 무시하고 정상 수신) |
| 실측 구간 (900프레임, p50 / p95 ms) | capture_to_preprocess **3 / 4**, preprocess **6 / 7**, c2_to_c3 **2 / 3**, c3_queue 0 / 0, c3_service 43 / 70, capture_to_c3_done **55 / 83** |
| 같은 시험에서 C4가 계산한 값 | e2e p50 65.4 ms, before_c3 p50 12 ms(≈ 3 + 6 + 2), inference p50 51 ms, c3_c4 p50 2.5 ms |

지연의 대부분은 C3 처리 시간이다(촬영부터 판정까지 55 ms 중 43 ms). C1 → C2 → C3 전달은 합쳐서 약 11 ms다. 단, 이 시험의 입력은 C1 컨테이너 안의 loadgen이라 폰 업로드 구간이 사실상 0이다. 실제 폰이면 `capture_to_preprocess`가 늘어난다.
