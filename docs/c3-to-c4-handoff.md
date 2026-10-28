# C3 → C4 전달 사양 (C4 작업자용)

| 항목 | 내용 |
|---|---|
| 작성 | Member C (C3), 2026-10-28 |
| 대상 | Member D (C4 Visualizer) |
| 기준 | `shared/schemas/detection-result.schema.json` **1.1.0** (1.0.0에 optional 필드만 추가, 하위 호환) |
| 확인한 C4 버전 | `origin/C4-Visualizer` `23022c2`. 이 사양의 메시지를 그대로 받는 것을 실제 체인에서 확인함(오류 0) |

## 1. 요약: C4가 해야 할 일

| # | 할 일 | 우선순위 |
|---|---|---|
| 1 | `status` / `defect_type`을 **C3 값 그대로** 쓰기. 현재는 bbox 사각형으로 원형도를 다시 계산해서 정상 쿠키도 `fail`이 된다 | **높음** |
| 2 | 구간별 지연은 결과의 `latency_breakdown_ms`를 그대로 표시 (§3) | 중간 |
| 3 | Frame drop은 C1·C2 카운터와 C4 수신 수로 계산 (§4). C2 `/stats`의 `error` + `timeout`(C2→C3 실패)도 읽기 | 중간 |
| 4 | overlay용 mask는 row-major RLE (§2.3) | 필요 시 |

## 2. 전달 방식

### 2.1 Endpoint와 시점
```
POST {C3_RESULTS_URL}            기본 http://c4-visualizer:8003/results
Content-Type: application/json
X-Trace-Id / X-Frame-Id / X-Session-Id   (body 값과 같음)
body: DetectionResult (detection-result.schema.json)
```
- C3가 `/infer`에 성공한 프레임마다 **정확히 1번** 보낸다. C2에 응답한 뒤 백그라운드로 보낸다.
- 오류 프레임(422/503/500)은 보내지 않는다.
- 재시도하지 않는다. C4가 실패하면(4xx/5xx, 연결 실패, 2 s timeout) 그 결과는 버려지고 C3 `/metrics`의 `results_push_error_count`에 기록된다.
- C3 replica가 여러 개면 결과가 `frame_id` 순서대로 도착하지 않을 수 있다. 정렬이 필요하면 `session_id` + `frame_id`로 맞춘다.
- C4의 응답 내용은 C3가 보지 않는다. 2xx만 확인한다.

### 2.2 본문 필드

| 필드 | 필수 | 설명 |
|---|---|---|
| `schema_version`, `trace_id`, `session_id`, `frame_id`, `captured_at_ms` | O | C1에서 생성되어 변경 없이 전파됨 |
| `inference_container_id` | O | 처리한 replica (`c3-inference-1..3`) |
| `inference_started_at_ms`, `inference_finished_at_ms` | O | epoch ms |
| `inference_latency_ms` | O | C3 처리 시간 (replica 안의 대기 포함) |
| `detections[]` | O | 아래 표 |
| `preprocess_started_at_ms`, `preprocess_finished_at_ms` | **1.1.0** | C2 시각 (processed-frame에서 전파) |
| `inference_received_at_ms` | **1.1.0** | C3가 요청을 받은 시각 |
| `latency_breakdown_ms` | **1.1.0** | C3가 계산한 구간별 지연 (§3) |
| `inference_timings_ms` | **1.1.0** | C3 내부 단계별 지연: `lock_wait`, `decode`, `encoder`, `segmentation`, `defect_analysis`, `mask_encode`, `total` |

`detections[]`

| 필드 | 설명 |
|---|---|
| `object_id` | C2의 `candidate_id` (`c0`, `c1`, …). 프레임 안에서만 유일하다. 프레임 간 같은 쿠키라는 보장은 없다 |
| `bbox` | `[x1, y1, x2, y2]`, **C2가 리사이즈한 이미지 기준 픽셀**. C2가 C4로 보내는 `camera_frame` 이미지와 같은 좌표계라 변환이 필요 없다 |
| `status` | `normal` / `defect` / `unknown` ← **표시에 이 값을 쓴다** |
| `defect_type` | `broken`(형태 불량: 파임, 조각 결손, 절단, 반쪽) 또는 `null` |
| `defect_score` | 0~1, 불량 정도. 0.5를 넘으면 defect. SAM2 mask 품질 점수와는 다른 값이다 |
| `mask` | 아래 RLE, 또는 `null` |

### 2.3 mask 디코딩 (overlay용)
```json
{"encoding": "rle", "width": 576, "height": 1024,
 "data": {"order": "row-major", "counts": [0의 개수, 1의 개수, 0의 개수, ...]}}
```
항상 0의 run부터 시작하고(길이 0일 수 있음), `sum(counts) == width*height`다. COCO RLE(column-major)와는 순서가 다르다.

```js
// JS: ImageData 알파 마스크 만들기
function decodeRle(m) {
  const out = new Uint8Array(m.width * m.height);
  let pos = 0, val = 0;
  for (const n of m.data.counts) { if (val) out.fill(1, pos, pos + n); pos += n; val ^= 1; }
  return out; // index = y * width + x
}
```
Python 예시는 `services/c3-inference/src/mask_codec.py`의 `rle_decode`다. mask를 쓰지 않으면 C3에서 `C3_RETURN_MASK=0`으로 끌 수 있다. 끄면 C3 처리가 프레임당 약 4 ms 줄어든다.

## 3. 구간별 지연 (`latency_breakdown_ms`)

C3는 C2 시각(processed-frame에 들어 있음)과 자기 시각을 모두 알고 있어서, 프레임마다 직접 계산해서 보낸다. **C4는 trace_id로 다른 메시지와 짝지을 필요 없이 이 값을 그대로 표시하면 된다.**

| 필드 | 계산 | 의미 |
|---|---|---|
| `capture_to_preprocess` | `preprocess_started_at_ms − captured_at_ms` | 폰 업로드 + C1 + MQTT + C2 대기 |
| `preprocess` | `preprocess_finished − preprocess_started` | C2 전처리 |
| `c2_to_c3` | `inference_received_at_ms − preprocess_finished_at_ms` | **C2 → Nginx → C3 전달** |
| `c3_queue` | C3 내부 측정 | replica 안에서 SAM2 차례를 기다린 시간 |
| `c3_service` | C3 내부 측정 | C3 실제 처리 시간 (대기 제외) |
| `c3_total` | `inference_finished_at_ms − inference_received_at_ms` | C3 전체 |
| `capture_to_c3_done` | `inference_finished_at_ms − captured_at_ms` | 촬영부터 판정 완료까지 |

- 단위는 ms다. 끝 시각이 시작 시각보다 이르면(기기 간 시계 차이) 그 값은 `null`이다. 평균, p50, p95를 낼 때 `null`은 제외한다.
- **C4 쪽에서 계산할 구간**:
  - `c3_to_c4 = C4 수신 시각 − inference_finished_at_ms`
  - `e2e = C4 표시 시각 − captured_at_ms`
- 현재 C4의 `before_c3`(= `inference_started − captured`)는 `capture_to_preprocess + preprocess + c2_to_c3`와 같다. 앞으로는 이 세 값으로 나눠 보여 주면 된다.
- C1 처리와 MQTT 구간을 더 나누려면 `gateway_received_at_ms`가 필요하다. C2가 `camera_frame`에 넣어 보내면(C2 제안) C4가 `trace_id`로 짝지어 `gateway_received_at_ms − captured_at_ms`(업로드)와 `preprocess_started_at_ms − gateway_received_at_ms`(C1 → C2)로 나눈다.

실측 예 (2026-10-28, 폰 fixture 30 fps, C3 ×2, 900프레임, p50 / p95 ms):
capture_to_preprocess 3 / 4, preprocess 6 / 7, c2_to_c3 2 / 3, c3_queue 0 / 0, c3_service 43 / 70, capture_to_c3_done 55 / 83.

## 4. Frame drop 계산

**C3는 frame drop을 계산할 수 없다.**
- 버려진 프레임은 C3에 오지 않는다.
- replica가 여러 개면 각 replica는 일부 프레임만 본다.

전체 결과를 받는 C4가 아래 카운터로 계산한다. 모두 **누적값**이므로, 일정 구간(예: 5 s)의 증가분으로 비율을 낸다.

| 구간 | 출처 | 계산 |
|---|---|---|
| C1 drop | C1 `GET :8000/api/metrics` → `counters.received`, `counters.dropped` | `dropped / received` (MQTT 모드에서는 보통 0) |
| C2 drop (밀린 프레임) | C2 `GET :8001/stats` → `events.dropped_stale`, `events.dropped_old` | `(dropped_stale + dropped_old) / C1 received` |
| **C2 → C3 실패** | C2 `GET :8001/stats` → `events.forwarded`, `events.error`, `events.timeout` | `(error + timeout) / (forwarded + error + timeout)`. `forwarded`는 C3가 200으로 응답한 수다. 422/5xx/timeout은 `error`/`timeout`에 들어간다 |
| C3 → C4 유실 | C2 `forwarded`와 C4가 받은 **프레임** 수(`trace_id` 기준, detection 수가 아님) | `forwarded − C4 수신 프레임`. 성공한 C3 요청 1건마다 결과 1건을 보내기 때문 |
| 전체 | C1 `received`, C4 수신 프레임 | `1 − C4 수신 프레임 / C1 received` |

- C4는 이미 C2 `/stats`를 조회하고 있다(`C4_C2_STATS_URL`). 지금 읽는 `dropped_stale`, `dropped_old`에 **`forwarded`, `error`, `timeout`을 추가**하면 C2→C3 구간까지 나온다.
- 세션 단위로도 볼 수 있다. C1이 `frame_id`를 세션마다 1씩 증가시키므로, C4가 받은 `frame_id` 사이의 빈 번호가 그 세션에서 C4까지 오지 못한 프레임이다.
- `/api/stats`의 `total`은 detection 수다. 프레임 수와 섞이지 않게 주의한다.

## 5. 변경 이력과 호환성

| 버전 | 변경 |
|---|---|
| 1.0.0 | 초기 |
| 1.1.0 (C3 `feature/inference`) | optional 필드 추가: `preprocess_started_at_ms`, `preprocess_finished_at_ms`, `inference_received_at_ms`, `latency_breakdown_ms`, `inference_timings_ms`. 기존 필드 변경 없음. 메시지의 `schema_version` 값은 계속 C1 값을 그대로 전파한다 |

C4 `models.py`의 `DetectionResult`(pydantic 기본 설정)는 모르는 필드를 무시하므로 **수정하지 않아도 계속 동작한다**. 새 필드를 쓰려면 model에 optional로 추가하면 된다.
