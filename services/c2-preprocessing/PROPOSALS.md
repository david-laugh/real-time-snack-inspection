# C2 → 공유 파일 변경 제안

C2(Member B)가 직접 수정할 수 없는 파일(`shared/schemas/`, `docker-compose.yml`, `.env.example`)에 대한 변경 제안이다.
각 항목은 해당 owner와 producer/consumer 리뷰 후 반영한다. C2 쪽 코드는 아래 제안이 반영되지 않은 현재 스키마 기준으로 동작한다.

| # | 대상 | 요약 | 관련 | 상태 |
|---|---|---|---|---|
| P1 | `processed-frame`, `detection-result` | `scale`, `crop_offset` 필드 추가 | C3(C), C4(D) | 제안 |
| P2 | `processed-frame` | `expected_diameter_px` 필드 추가 | C3(C) | 제안 |
| P3 | C1↔C2↔C3 전달 방식 | `/preprocess` 즉시 202 + 백그라운드 전달 + drop | C1(A), C3(C) | 논의 필요 |
| P4 | `docker-compose.yml` | c2 `command` 교체 / healthcheck | A | 제안 |
| P5 | `.env.example` | `C2_RESIZE_WIDTH/HEIGHT` → `C2_RESIZE_LONG_SIDE` 등 | A | 제안 |
| P6 | 스키마 공유 방식 | 서비스 이미지 안에서 `shared/schemas` 접근 | A | 제안 |
| P7 | C1 `/cam` 기본 해상도 | 640 → 960 (또는 1280) | A, C | 제안 |

## 확정된 사항

| 항목 | 결정 | 근거 |
|---|---|---|
| 프레임 이미지 전송 방식 | **frame-envelope JSON 안의 `image_b64`(base64 JPEG)** + 헤더 `X-Trace-Id` / `X-Frame-Id` / `X-Session-Id` | C1 PR #1(`feature/c1-gateway`)이 이 방식으로 구현했다. C2는 같은 방식으로 받고(`codec.decode_frame`), C3로도 같은 방식으로 보낸다(`processed-frame.image_b64`, JPEG q90) |

- 스키마의 `image_b64` 설명에 있는 "TODO: transport is not decided" 문구는 owner가 정리해 주길 요청한다 (`shared/schemas/` 소유자).
- `raw_bgr` / `raw_rgb`는 현재 보내는 쪽이 없으므로 C2는 계속 422로 거절한다. 필요해지면 `codec.py`에 추가한다.
- 비용 참고: 1881×810 JPEG 기준 base64 + 디코딩이 약 11 ms, 1024px 재인코딩이 약 6 ms다(로컬 측정). C2 처리 시간의 대부분이 여기서 나온다.

---

## P1. `scale` / `crop_offset` 필드 추가 (processed-frame, detection-result)

### 문제
C2는 긴 변 기준으로 리사이즈하고(기본 1024px), 앞으로 ROI crop도 할 수 있다. 그런데 `prompts[].bbox`와 C3의
`detections[].bbox`/`mask`는 **전처리 후 이미지** 좌표다. 현재 스키마에는 전처리 후 좌표를 원본 좌표로 되돌릴 정보가 없다.

- C4가 **원본 프레임** 위에 mask/bbox를 그리면 좌표가 어긋난다. 예를 들어 1920×1080 원본을 1024×576으로 줄이면
  배율이 0.533이라, 보정하지 않으면 overlay가 왼쪽 위로 쏠려 약 절반 크기로 그려진다.
- 해상도는 세션(기기)마다 다르고 설정(`C2_RESIZE_LONG_SIDE`)으로도 바뀌므로 C4에 하드코딩할 수 없다.
- `source_width/height`는 frame-envelope에만 있고 detection-result까지 전파되지 않는다. 배율을 다시 계산하는 것도 불가능하다.
- ROI crop을 도입하면 평행 이동(offset)이 생기는데, 이것은 width/height만으로는 복원할 수 없다.

### 제안
`processed-frame`과 `detection-result`에 선택(optional) 필드를 추가하고, C3는 받은 값을 그대로 전파한다.

```jsonc
"scale": {
  "type": "number", "exclusiveMinimum": 0,
  "description": "preprocessed = (source - crop_offset) * scale. Uniform scale factor applied by C2."
},
"crop_offset": {
  "type": "array", "items": { "type": "number" }, "minItems": 2, "maxItems": 2,
  "description": "[x, y] of the crop origin in source pixel coordinates. [0, 0] when not cropped."
}
```

원본 좌표 복원: `x_src = x / scale + crop_offset[0]`, `y_src = y / scale + crop_offset[1]`

- 비율을 유지하는 리사이즈라 배율은 스칼라 하나면 된다. 축마다 다른 배율이 필요해지면 `[sx, sy]`로 확장한다.
- optional로 추가하므로 1.0.0 consumer와 호환된다 → `schema_version` 1.1.0(minor).
- C2는 이미 내부에서 배율을 계산한다(`pipeline.Detection.scale`). 스키마에 반영되면 바로 넣는다.
- **C2는 이 필드가 승인될 때까지 ROI를 crop하지 않고 경계 판정에만 쓴다.** (팀 합의 사항)

---

## P2. `expected_diameter_px` 필드 추가 (processed-frame)

### 문제
촬영 방향과 거리가 정해지지 않았기 때문에 C2는 쿠키 지름을 프레임마다 자동 추정한다.
(정상 쿠키 면적의 중앙값을 쓰고, `session_id`별로 EMA α=0.2로 평활화한다.)
C3의 불량 판정(결손, 파손)은 "mask 면적 ÷ 정상 쿠키 면적" 비율에 기댈 가능성이 높은데, C3에는 이 기준 크기가 없다.
C3가 직접 다시 추정하면 로직이 중복되고, 두 서비스의 기준이 서로 어긋날 수 있다.

### 제안
```jsonc
"expected_diameter_px": {
  "type": ["number", "null"], "exclusiveMinimum": 0,
  "description": "Estimated diameter of a normal snack in preprocessed-image pixels (session EMA). null if not yet estimated."
}
```
- 기준 좌표는 **전처리 후 이미지**다. `prompts[].bbox`와 같은 좌표계를 쓴다.
- C3 예: `area_ratio = mask_area / (π · (d/2)²)`가 임계값보다 작으면 결손으로 판정.
- optional → 1.1.0(minor). C2 내부에는 이미 값이 있다(`pipeline.Detection.diameter_px`).

---

## P3. `/preprocess` 응답 방식: 동기 → 즉시 202 + 백그라운드 전달 (Member A/C와 논의 필요)

### 문제
현재 C2는 `/infer`(C3) 응답을 **기다린 뒤** C1에 응답한다. 그래서 C1이 보는 `/preprocess` 응답 시간은
C2 처리 시간(~6–25 ms)에 Nginx 대기 시간과 C3 SAM2 추론 시간을 더한 값이 된다.
C1이 프레임을 순차로 보내면 SAM2 추론이 33 ms를 넘는 순간 **C1이 30fps를 따라가지 못하고**, 지연이 C1 쪽에 쌓인다.
그러면 병목 측정에서 C3의 병목이 C1의 지연처럼 보인다.

**C1 PR #1로 확인된 근거:** C1은 C2로 동시에 나가 있는 요청을 `C1_MAX_INFLIGHT=2`(기본값)개로 제한하고,
각 요청은 C2 응답을 받을 때까지 슬롯을 차지한다. C2가 `/infer` 응답을 기다리는 현재 구조에서는
**C3 복제본을 3개로 늘려도 C3에 동시에 들어가는 요청은 최대 2개**다. 처리량 상한은 `2 / (C2 처리 + C3 추론 시간)`이 되고,
scale-out 실험(C3 x1/x2/x3)에서 x2와 x3의 차이가 나타나지 않는다. 이 실험이 프로젝트의 핵심 측정 항목이다.

해결안
- **(a) `C1_MAX_INFLIGHT`를 C3 복제본 수보다 크게** 설정한다(예: 복제본 수 × 2). 코드 변경 없이 바로 적용할 수 있다.
  다만 C1의 inflight가 사실상 C3 큐 길이가 되므로, scale-out 구성마다 값을 맞춰야 한다.
- **(b) C2 즉시 202 응답 + 백그라운드 전달** (아래 제안). C1과 C3가 분리되어 C1 설정이 C3 구성에 의존하지 않는다.

당장의 벤치마크는 (a)로 진행하고, 구조 개선은 (b)로 하는 것을 권장한다.

### 제안 (b)
1. C2는 검증과 전처리를 마치면 C1에 **즉시 `202 Accepted`**를 반환한다.
2. `/infer` 전달은 백그라운드 task로 처리한다.
3. 동시 전달 수 한도(예: `C2_MAX_INFLIGHT`, C3 replica 수 × 2)를 초과하면 새 프레임은 **drop**하고 `202` 대신 `429`(또는 `202` + `dropped:true`)로 응답한다.
4. drop 수와 `/infer` 실패 수를 카운트해서 `GET /metrics`(또는 로그)로 노출한다. Frame Drop Rate 측정(`docs/bottleneck-test-plan.md`)에 쓴다.

장단점
- C1 응답 시간이 C3와 분리되어, C1/C2/C3 각 단계의 병목을 따로 볼 수 있다.
- 대신 C2가 C3 실패를 C1에 동기적으로 알려줄 수 없다. 실패는 카운터와 로그로만 확인한다.
- drop 정책을 어디에 둘지(C1 backpressure vs C2 drop) A와 합의가 필요하다. 한도는 C3 처리량에 맞춰 C와 정해야 한다.

### 현재 구현
동기 방식으로 구현했다. `/infer` 전달은 `src/c2/app.py`의 `forward_to_infer()` 하나로 분리해 두었으므로,
비동기로 바꿀 때는 이 함수와 응답 코드만 바꾸면 된다.

---

## P4. `docker-compose.yml`의 c2 entrypoint 교체 (Member A)

C1 PR #1은 c1의 `command`만 제거하고 c2는 건드리지 않는다. 이 항목은 C1 PR이 머지된 뒤
`feature/c2-preprocessing`을 rebase해서 처리할 예정이다. 다른 부분과 충돌하지 않도록 c2 블록만 수정한다.

이미지 CMD는 이미 uvicorn이고(`Dockerfile`), HEALTHCHECK도 들어 있다.
compose의 `command: ["sleep", "infinity"]`가 CMD를 덮어쓰므로 제거하거나 아래처럼 교체해야 한다.

```yaml
  c2-preprocessing:
    # ...
    command: ["uvicorn", "c2.app:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8001"]
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/healthz', timeout=2)"]
      interval: 10s
      timeout: 3s
      retries: 3
```
`command` 줄을 지우기만 해도 같은 결과가 난다. C1의 `depends_on`은 `condition: service_healthy`로 바꾸는 것을 권장한다.

---

## P5. `.env.example` C2 변수 정리 (Member A)

변수 이름 통일: `C2_RESIZE_WIDTH` / `C2_RESIZE_HEIGHT` → `C2_RESIZE_LONG_SIDE` (값 1024 유지)

```dotenv
# ---- C2 Preprocessing ----
C2_PORT=8001
# Resize so that the longer side is this many px (aspect ratio kept, never upscaled). 0 = no resize.
C2_RESIZE_LONG_SIDE=1024
# ROI format: x1,y1,x2,y2 (source pixel coords). Empty = full frame.
C2_ROI=
```

C2가 읽는 나머지 변수(`C2_ROI_MODE`, `C2_EXPECTED_DIAMETER_PX`, `C2_INFER_URL` 등)는 모두 기본값이 있다. 그래서 `.env.example`에 추가할 필요는 없다.
목록은 `services/c2-preprocessing/README.md`에 있다. 다만 `C2_ROI`는 `C2_ROI_MODE=fixed`일 때만 쓰인다
(기본값 `auto`에서는 무시된다). 이 점을 주석에 적어 두면 좋겠다.

---

## P6. 컨테이너 안에서 `shared/schemas` 접근 (Member A)

compose의 build context가 `./services/c2-preprocessing`라서 이미지에 `shared/schemas`를 넣을 수 없다.
현재는 `services/c2-preprocessing/schemas/`에 **사본**을 두고, `tests/test_schema_sync.py`로 원본과 같은지 검사한다.
대안은 다음 두 가지다.

- (a) 볼륨 마운트: `./shared/schemas:/app/schemas:ro`. 코드는 수정할 필요가 없다(`C2_SCHEMA_DIR` 기본값이 `/app/schemas`).
- (b) build context를 repo root로 바꾸고 `dockerfile: services/c2-preprocessing/Dockerfile`을 지정한다.
  이 경우 Dockerfile의 COPY 경로도 수정해야 한다.

(a)가 가장 단순하다. 다른 서비스도 같은 방식을 쓰도록 통일하는 것을 제안한다.

---

## P7. C1 `/cam` 기본 해상도 640 → 960 또는 1280 (Member A, C)

### 문제
C1의 `/cam` 페이지는 해상도 기본값이 640px이다. 폰을 **가로**로 촬영하면 긴 변 1920이 640으로 줄어(1/3)
쿠키 지름이 약 **50 px**까지 작아진다. 세로 1080×1920 원본에서는 약 170 px이다.

- C2 검출 자체는 지름 자동 추정과 지름 비례 ROI 커널 덕분에 저해상도에서도 동작한다.
  (`tests/test_phone_frames.py`: 576px 폭 입력도 기대 개수와 일치)
- 하지만 C3의 원형도·결손 판정은 윤곽 픽셀 수에 민감하다. 지름 50 px이면 둘레가 약 157 px밖에 안 된다.
  JPEG 품질 0.7 압축이나 SAM2 마스크 경계 오차 1~2 px만으로도 원형도가 흔들릴 수 있다.
  그러면 작은 결손(쐐기 각도가 작은 경우)이나 미세 균열을 정상과 구분하기 어려워진다.
- C2는 확대하지 않는다(`C2_RESIZE_LONG_SIDE=1024`는 축소만 한다). 그래서 640 입력은 640 그대로 C3에 전달된다.

### 제안
가로 촬영 기준으로 C3에 도착하는 쿠키 크기는 다음과 같다. C2가 긴 변을 1024로 줄인다는 점을 반영했다.

| `/cam` 해상도 | C2 출력 긴 변 | C3가 보는 쿠키 지름(가로 촬영) |
|---|---|---|
| 640 (현재) | 640 | 약 50 px |
| 960 | 960 | 약 75 px |
| 1280 | 1024 (C2가 축소) | 약 80 px |

- 기본값은 **960**을 제안한다. 1280은 업링크 JPEG 크기가 약 1.8배로 늘지만, 지금 설정에서 C3가 받는 크기는 약 80 px로 거의 같다.
  1280은 `C2_RESIZE_LONG_SIDE`를 함께 올릴 때만 의미가 있다(SAM2 입력 1024와의 관계는 C와 확인 필요).
- 세로 촬영이면 해상도 값이 폭이라 긴 변이 더 길어진다. 960 폭이면 긴 변 약 1707이 되고, C2가 1024로 줄여 폭 576(쿠키 약 90 px)이 된다.
- 대역·지연 부담은 업링크 JPEG 크기에 비례한다. 960 기준으로 측정하고, 30fps가 유지되지 않으면 화질(q)을 먼저 낮추는 것을 권장한다.
- 최종 값은 C3의 판정 정확도 실험(지름별 원형도 분포)을 보고 C와 함께 정한다.
