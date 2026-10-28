# C2 → 공유 파일 변경 제안

C2(Member B)가 직접 수정할 수 없는 파일(`shared/schemas/`, `docker-compose.yml`, `.env.example`)에 대한 변경 제안이다.
각 항목은 해당 owner와 producer/consumer 리뷰 후 반영한다. C2 쪽 코드는 아래 제안이 반영되지 않은 현재 스키마 기준으로 동작한다.

| # | 대상 | 요약 | 관련 | 상태 |
|---|---|---|---|---|
| P1 | `processed-frame`, `detection-result` | `scale`, `crop_offset` 필드 추가 | C3(C), C4(D) | **불필요** (C4가 C2 리사이즈 이미지 위에 그림) |
| P2 | `processed-frame` | `expected_diameter_px` 필드 추가 | C3(C) | 제안 |
| P3 | C1↔C2↔C3 전달 방식 | 동기 HTTP 때문에 C3 동시 처리가 C1_MAX_INFLIGHT로 제한됨 | C1(A), C3(C) | **해결됨** (MQTT 전환) |
| P4 | `docker-compose.yml` | c2 `command` 교체 / healthcheck | A | 제안 |
| P5 | `.env.example` | `C2_RESIZE_WIDTH/HEIGHT` → `C2_RESIZE_LONG_SIDE` 등 | A | 제안 |
| P6 | 스키마 공유 방식 | 서비스 이미지 안에서 `shared/schemas` 접근 | A | 제안 |
| P7 | C1 `/cam` 기본 해상도 | 640 → 960 (또는 1280) | A, C | **반영됨** (C1 기본 960) |
| P8 | `docs/api-contract.md` (+ 스키마) | C2 → C4 리사이즈 프레임 구간 추가 | A, D | 제안 |

## 확정된 사항

| 항목 | 결정 | 근거 |
|---|---|---|
| 프레임 이미지 전송 방식 | **frame-envelope JSON 안의 `image_b64`(base64 JPEG)** + 헤더 `X-Trace-Id` / `X-Frame-Id` / `X-Session-Id` | C1 PR #1(`feature/c1-gateway`)이 이 방식으로 구현했다. C2는 같은 방식으로 받고(`codec.decode_frame`), C3로도 같은 방식으로 보낸다(`processed-frame.image_b64`, JPEG q90) |
| C1 → C2 전송 프로토콜 | **MQTT** (`mqtt-broker:1883`, 토픽 `psf/v1/frames/+`, QoS 0). 본문은 기존과 같은 frame-envelope JSON(`image_b64` JPEG) | 팀 결정. C2 → C3는 HTTP `/infer` 유지. P3 참고 |
| C2 → C4 리사이즈 프레임 전송 | `POST http://c4-visualizer:8003/api/camera/frame`, body `{trace_id, session_id, frame_id, width, height, prompts, image_b64}` | C4(Member D)와 합의. C4는 trace_id로 이 이미지와 C3 결과를 짝지어 그린다. P1, P8 참고 |

- 스키마의 `image_b64` 설명에 있는 "TODO: transport is not decided" 문구는 owner가 정리해 주길 요청한다 (`shared/schemas/` 소유자).
- `raw_bgr` / `raw_rgb`는 현재 보내는 쪽이 없으므로 C2는 계속 422로 거절한다. 필요해지면 `codec.py`에 추가한다.
- 비용 참고: 1881×810 JPEG 기준 base64 + 디코딩이 약 11 ms, 1024px 재인코딩이 약 6 ms다(로컬 측정). C2 처리 시간의 대부분이 여기서 나온다.

---

## P1. `scale` / `crop_offset` 필드 추가 — ⛔ 불필요 (C4가 C2 리사이즈 이미지 위에 그림)

### 원래 제안 (기록)
C2가 리사이즈(긴 변 1024)와 ROI crop을 하면 C3 결과 좌표가 전처리 후 이미지 기준이 된다. C4가 **원본 프레임** 위에 그리려면
좌표를 되돌릴 정보가 필요하다. 그래서 `processed-frame`과 `detection-result`에 `scale`, `crop_offset`을 추가하자고 제안했다.

### 결론
C4(Member D)와 합의해 **C2가 리사이즈한 이미지와 prompts를 C4로 직접 보낸다** (`POST /api/camera/frame`, P8).
C4는 `trace_id`로 이 이미지와 C3 결과(bbox, mask)를 짝지어 그린다. C3 결과는 C2가 리사이즈한 이미지 기준 좌표이므로
**C4에서 좌표를 변환할 필요가 없고**, `scale` / `crop_offset` 필드도 필요 없다.

- C3로 보낸 이미지와 C4로 보낸 이미지는 같은 JPEG 바이트다. 좌표계가 정확히 일치한다.
- 나중에 C2가 ROI **crop**을 도입해도, C4로 보내는 이미지가 crop된 이미지 자체라면 계속 필요 없다.
  원본 해상도로 overlay해야 하는 요구가 새로 생기면 그때 다시 검토한다.

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

## P3. C1 → C2 전달 방식 — ✅ MQTT 전환으로 해결됨 (C2가 동시 처리 수를 직접 관리)

### 원래 문제 (기록)
C2가 `/infer`(C3) 응답을 기다린 뒤 C1에 HTTP로 응답하는 구조였다. 이 구조에서는 C1의 `C1_MAX_INFLIGHT=2`가 곧
C3에 동시에 들어가는 요청 수의 상한이 되어, C3 복제본을 늘려도 처리량이 늘지 않았다. scale-out 실험의 의미가 없어지는 문제였다.
대안으로 (a) `C1_MAX_INFLIGHT` 상향, (b) C2 즉시 202 + 백그라운드 전달을 제안했다.

### 결정과 구현 (`feature/c2-mqtt`)
팀 결정으로 C1 → C2를 **MQTT**(브로커 `mqtt-broker:1883`, 토픽 `psf/v1/frames/+`, QoS 0, 본문은 기존 frame-envelope JSON)로 바꿨다.
C2 → C3는 기존 HTTP(`nginx-inference-lb:8080/infer`)를 유지한다.

- C1은 발행만 하고 C2 응답을 기다리지 않는다. C1과 C3의 처리 속도가 분리된다.
- C2가 동시 처리 수를 직접 관리한다. 워커 `C2_MAX_INFLIGHT`(기본 4, C3 복제본 수 이상 권장)개가 C3로 동시에 요청을 보낸다.
- 처리보다 빨리 들어오면 세션별 최신 1장만 보관하고 나머지는 `dropped_stale`로 버린다. `captured_at_ms` 기준
  `C2_MAX_FRAME_AGE_MS`(500)를 넘은 프레임은 `dropped_old`로 버린다. 그래서 지연이 쌓이지 않는다.
- 프레임별 이벤트(`forwarded` / `dropped_stale` / `dropped_old` / `error` / `timeout`)는 `C2_EVENT_LOG` CSV와 `GET /stats`로 확인한다.
  Frame Drop Rate는 C1 이벤트와 C2 이벤트를 `trace_id`로 조인해 단계별로 나눠 계산할 수 있다.
- HTTP `POST /preprocess`는 테스트·디버깅용으로 남겼다. MQTT와 같은 처리 함수(`handle_frame`)를 쓴다.

### 남은 합의 사항 (Member A)
- 토픽 마지막 레벨(`+`)의 의미(세션 id 권장). C2는 본문의 `session_id`로 세션을 구분하므로 어느 쪽이든 동작한다.
- 운영 브로커의 인증·ACL 여부. 현재 dev 설정은 `allow_anonymous true`다.
- 벤치마크 구성별 `C2_MAX_INFLIGHT` 값 (C3 x1/x2/x3). Member C와 함께 정한다.

---

## P4. `docker-compose.yml`의 c2 entrypoint 교체 (Member A)

C1 PR #1은 머지됐지만 c2의 `command`는 아직 `sleep infinity`다. MQTT 전환에 따라 compose는 C1 담당(Member A)이
수정하기로 했다. 필요한 설정(브로커 서비스, C2 환경변수, 이벤트 로그 볼륨)은 `README.md`의 "compose 설정 예시"와
`dev/docker-compose.override.yml`에 정리했다.

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

## P7. C1 `/cam` 기본 해상도 640 → 960 또는 1280 (Member A, C) — ✅ 반영됨 (C1 `/cam` 기본 960)

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

---

## P8. `docs/api-contract.md`에 C2 → C4 구간 추가 (Member A, D)

C2 → C4 리사이즈 프레임 전송이 C4와 합의되어 구현됐다(`src/c2/c4_frames.py`). 하지만 API 계약 문서의 메시지 흐름에는
C3 → C4(detection-result)만 있다. 문서와 스키마 owner가 다음을 추가해 주길 제안한다. C2는 문서를 직접 수정하지 않는다.

**메시지 흐름 표에 행 추가**

| # | Producer → Consumer | Endpoint | Schema |
|---|---|---|---|
| 2b | C2 → C4 | `POST http://c4-visualizer:8003/api/camera/frame` | `camera-frame` (신규) |

**camera-frame (C2 → C4)**: C3로 보내는 processed-frame의 일부. 필드를 추가하거나 빼지 않는다.

| Field | Type | 설명 |
|---|---|---|
| `trace_id` | string | processed-frame과 같음. C4는 이 값으로 C3 결과와 짝을 짓는다 |
| `session_id` | string | 〃 |
| `frame_id` | integer | 〃 |
| `width` / `height` | integer | 리사이즈된 이미지 크기 (= C3 결과 좌표계) |
| `prompts` | prompt[] | processed-frame의 prompts 그대로. 0개여도 보낸다 |
| `image_b64` | string | processed-frame과 **같은 JPEG** (재인코딩 없음) |

동작 규약 (C2 구현 기준)
- C3로 보내는 프레임만 보낸다. C2에서 drop된 프레임(`dropped_stale` / `dropped_old`)은 보내지 않는다.
- C3 전송과 동시에 보내고 응답을 기다리지 않는다. 대개 이미지가 C3 결과보다 먼저 도착하지만 **순서는 보장하지 않는다.**
  C4는 이미지와 결과 중 어느 쪽이 먼저 와도 `trace_id`로 짝지을 수 있도록 짧게 버퍼링하는 것을 권장한다.
- best-effort다. C4가 느리거나 꺼져 있으면 C2는 보내지 않거나(`c4_skipped`, 동시 전송 한도 `C2_C4_MAX_INFLIGHT` 초과)
  실패로 기록(`c4_error`)할 뿐 재시도하지 않는다. 따라서 C4에는 C3 결과는 있는데 이미지가 없는 프레임이 생길 수 있다.
- 헤더 `X-Trace-Id` / `X-Frame-Id` / `X-Session-Id`를 함께 보낸다(본문 필드와 같은 값).

`shared/schemas/camera-frame.schema.json`도 만들기를 제안한다(`additionalProperties: false`, prompt는 processed-frame의 `$defs/prompt` 재사용).
그러면 C4가 입력을 검증할 수 있고, C2 테스트도 가짜 C4(`dev/fake_c4.py`) 대신 스키마로 검증할 수 있다.
