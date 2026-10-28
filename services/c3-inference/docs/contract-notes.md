# C3 Contract Notes (Plan A: frame-independent SAM2ImagePredictor)

이 문서는 C3 구현 중 **`shared/schemas/`의 스키마를 변경하지 않고** 내린 해석과 결정, 그리고 다른 서비스 owner와 **합의가 필요한 항목**을 정리한다.
스키마 파일이 우선이며, 아래 결정 중 스키마 변경이 필요한 것은 "제안"으로만 표시했다.

## 1. 요청: `POST /infer` (processed-frame)

| 항목 | 스키마 | C3 동작 |
|---|---|---|
| `image_b64` | optional, "전송 방식 TODO" | **C3는 필수로 요구한다.** 이미지는 base64로 인코딩한 **JPEG**(전처리 후 이미지)이어야 한다. 없으면 `422 missing_image`. 이미 스키마에 있는 필드를 그대로 사용하므로 C2와 충돌하지 않는다. |
| `width`/`height` | 전처리 후 크기 | decode한 이미지 크기와 다르면 `422 image_size_mismatch`. |
| `prompts[].bbox` | `[x1,y1,x2,y2]` pixel | 값이 finite가 아니거나 `x1>=x2` / `y1>=y2`이면 `422 invalid_bbox`. 이미지 밖으로 일부 나간 bbox는 이미지 경계로 **clip**한다. 완전히 밖에 있는 bbox는 `422 invalid_bbox`. |
| `prompts[].candidate_id` | "프레임 내 unique" | 중복이면 `422 duplicate_candidate_id`. |
| `prompts` | 빈 배열 허용 | `200`과 `detections: []`를 반환하고 encoder는 실행하지 않는다(`encoder_ms=0`). |
| `schema_version` | semver | major가 `SCHEMA_VERSION`과 다르면 `422 unsupported_schema_version`. |
| 정의되지 않은 필드 | `additionalProperties:false` | `422 invalid_request`. |
| prompt 수 | 제한 없음 | `C3_MAX_PROMPTS`(기본 64)를 넘으면 `422 too_many_prompts`. |

Nginx `client_max_body_size 20m`는 1024×1024 JPEG의 base64 크기(수백 KB)에 충분하다.

### 제안 (스키마 변경, C2 owner 합의 필요)
- `image_b64`를 `required`로 바꾸고, JPEG임을 명시하는 `image_format: "jpeg"`를 추가한다.
- 대역폭이 병목으로 측정되면 `multipart/form-data` 또는 binary body + JSON metadata header로 전환하는 것을 검토한다.

## 2. Trace headers

- `X-Trace-Id`, `X-Frame-Id`, `X-Session-Id`를 받는다. body를 파싱할 수 있으면 **body 값이 기준**이 된다(스키마상 공통 필드는 변경 없이 전파). header와 body 값이 다르면 경고 로그를 남기고 `/metrics.trace_header_mismatch_count`를 증가시킨다.
- 응답 header로 위 3개를 echo하고, `X-Inference-Container-Id`도 함께 보낸다.
- body 파싱에 실패하면 error 응답에 요청 header 값을 그대로 넣는다.

## 3. 응답: detection-result

| 항목 | 스키마 | C3 결정 |
|---|---|---|
| `status` | enum `normal`/`defect`/`unknown` (소문자) | 내부 enum은 `NORMAL/DEFECT/UNKNOWN`이고, wire 값은 **스키마의 소문자 그대로**다. |
| `defect_type` | 문자열, taxonomy TODO | `broken`(형태 불량: C2 라벨의 bite / wedge / cut / half를 모두 포함). `spot_defect`는 `C3_SPOT_ENABLED=1`일 때만 나온다(초코칩 쿠키는 기본 꺼짐). `status != defect`이면 `null`. |
| `defect_score` | [0,1] "defect confidence" | 규칙 기반 severity의 최댓값이다. **SAM2 mask score(predicted IoU)와 다른 값이다.** `>0.5`이면 defect이고, `unknown`이면 `0.0`이다. 정의는 `src/defects.py` docstring 참고. |
| `object_id` | "SAM2 tracking id, 세션 내 유지" | A안은 tracking을 하지 않으므로 **C2의 `candidate_id`를 그대로** 쓴다. 프레임 간 동일 id를 유지하려면 C2가 같은 객체에 같은 `candidate_id`를 부여해야 한다. **(합의 필요)** |
| `bbox` | `[x1,y1,x2,y2]` | 입력 prompt bbox(clip 후)를 그대로 반환한다. 마스크의 외접 사각형이 아니다. |
| `mask` | encoding TODO | `encoding: "rle"`을 쓴다. 형식은 아래와 같다. `C3_RETURN_MASK=0`이거나 마스크가 비어 있으면 `null`. |
| `schema_version` | — | 요청 값을 그대로 전파한다. |
| `inference_latency_ms` | — | `total_inference_ms`와 같다(perf_counter 기준). |

### RLE 형식
```json
{"encoding": "rle", "width": 1024, "height": 1024,
 "data": {"order": "row-major", "counts": [n0, n1, n2, ...]}}
```
`counts`는 row-major로 펼친 마스크의 run length를 번갈아 담는다. **항상 0의 run부터 시작**하고(길이 0일 수 있음), `sum(counts) == width*height`이다.
COCO RLE(column-major)와는 순서가 다르다. decoder 예시는 `src/mask_codec.py:rle_decode`이며 numpy 3줄이다.

## 4. 성능 지표 전달 방식

`DetectionResult`가 `additionalProperties:false`라서 body에 지표 필드를 추가할 수 없다. 그래서 다음 두 경로로 제공한다.
- **응답 header**: `X-C3-Decode-Ms`, `X-C3-Encoder-Ms`, `X-C3-Segmentation-Ms`, `X-C3-Defect-Analysis-Ms`, `X-C3-Total-Inference-Ms`, 그리고 표준 `Server-Timing`
- **`GET /metrics`**: `processed_frame_count`, `error_count`, stage별 p50/p95, model load/warm-up 시간, GPU 메모리

제안: C4가 프레임 단위 stage timing을 표시하려면 스키마에 optional `timings` 객체를 추가하는 것을 검토한다(C4 owner 합의 필요).

## 5. 에러 응답

```json
{"error": {"code": "invalid_bbox", "message": "...", "inference_container_id": "c3-inference-1",
           "trace_id": "...", "frame_id": "7", "session_id": "..."}}
```

| HTTP | code |
|---|---|
| 422 | `invalid_request`, `missing_image`, `image_decode_failed`, `image_size_mismatch`, `invalid_bbox`, `duplicate_candidate_id`, `too_many_prompts`, `unsupported_schema_version` |
| 503 | `model_not_ready` (`Retry-After: 1`) |
| 500 | `internal_error` |

⚠️ Nginx 설정에 `proxy_next_upstream error timeout http_502 http_503`가 있다. 그러나 nginx는 **POST 같은 non-idempotent 요청을 기본적으로 다른 upstream에 재시도하지 않는다**(`non_idempotent` 플래그 필요). 따라서 모델 로딩 중인 replica가 낸 503은 C2에 그대로 전달된다. 대응 방법은 둘 중 하나다. (a) 모든 replica의 `/healthz`가 200이 된 뒤 트래픽을 보낸다. (b) Nginx에 `non_idempotent`를 추가한다. 이번 작업에서는 Nginx를 수정하지 않았다.

## 6. 미해결 / 범위 밖

- ~~**C3 → C4 전달**~~ → **C3 push로 구현함 (2026-10-28)**.
  - 근거: C2는 `/infer` 응답 body를 읽지 않고 상태 코드만 본다. C4(`C4-Visualizer` 브랜치)와 C1 stub은 모두 C3가 `POST /results`를 보내는 것을 전제로 한다.
  - 동작: `/infer` 응답을 보낸 뒤 백그라운드로 `C3_RESULTS_URL`에 같은 JSON과 trace header를 POST한다. 실패는 `/metrics`에서 카운트하고 `/infer` 결과에는 영향을 주지 않는다.
  - 연동 확인: C2(`origin/main`) → C3 → C4(`C4-Visualizer` 브랜치)를 로컬에서 연결해서 6프레임 42개 detection이 모두 C4에 도착했다.
- **C4가 C3 판정을 쓰지 않음 (C4 owner 확인 필요)**: C4 `receive_result`는 `status`/`defect_type`을 무시하고, bbox 사각형의 w·h로 원형도(4π·wh/(2(w+h))²)를 계산해 pass/fail을 정한다. bbox가 정사각형이면 이 값은 항상 π/4 ≈ 0.785다. 그래서 연동 시험에서 정상 23개를 포함한 42개 모두 `fail`로 표시됐다. C3가 보내는 `status`를 그대로 쓰도록 C4를 바꾸는 것을 제안한다.
- **Session affinity**: A안은 stateless라서 Nginx round-robin과 충돌하지 않는다. 나중에 Video Predictor(B안)로 전환하면 `hash $http_x_session_id consistent;`가 필요하다.
