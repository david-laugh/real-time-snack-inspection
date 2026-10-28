# C2 Preprocessing

**Owner:** Member B

## 역할
- OpenCV 기반 resize(긴 변 기준) + 컨베이어 영역(ROI) 판정
- 과자 후보 bbox 생성 → SAM2 prompt(`prompts[]`)로 변환
- `processed-frame` 메시지를 Nginx Inference LB(`/infer`)로 전달

## Interface
| 방향 | Endpoint | Payload |
|---|---|---|
| C1 → C2 | `POST /preprocess` | [`frame-envelope.schema.json`](../../shared/schemas/frame-envelope.schema.json) |
| C2 → Nginx | `POST http://nginx-inference-lb:8080/infer` | [`processed-frame.schema.json`](../../shared/schemas/processed-frame.schema.json) |
| — | `GET /healthz` | `{"status": "ok"}` |

Header 전파: `X-Trace-Id`, `X-Frame-Id`, `X-Session-Id`

`POST /preprocess` 응답
| 코드 | 의미 |
|---|---|
| 200 | 전처리 후 `/infer` 전달 성공. `{trace_id, frame_id, num_prompts, preprocess_ms, infer_status}` |
| 422 | frame-envelope 스키마 위반, `image_b64` 누락·디코딩 실패, 미지원 `format`(`raw_*`) |
| 502 | `/infer` 타임아웃, 연결 실패, 4xx/5xx |

현재는 `/infer` 응답을 기다린 뒤 응답하는 동기 방식이다. 비동기(202) 전환 제안은 [`PROPOSALS.md`](PROPOSALS.md) P3 참고.

## 처리 흐름
1. 입력을 frame-envelope 스키마로 검증
2. `image_b64`(jpeg/png) 디코딩 — `src/c2/codec.py` (이미지 전송 방식이 바뀌면 이 파일만 수정)
3. 긴 변이 `C2_RESIZE_LONG_SIDE`가 되도록 축소 (비율 유지, 확대하지 않음)
4. 후보 bbox 검출 — `src/c2/detect.py` (reference/c2_bbox_detect.py와 같은 로직)
   - HSV 임계값 → morphology open/close → 외곽 contour → bbox
   - 면적 필터(기대 쿠키 면적의 0.2~2.0배), 가로세로비 ≤ 2.5
   - ROI 경계(또는 화면 가장자리)에 닿은 쿠키는 잘린 것으로 보고 제외
   - ROI auto의 close 커널은 쿠키 지름에 비례시킨다(`max(3, round(지름 × 25 / 170))`). 해상도(리사이즈, 640×480 스트림 등)와 상관없이 같은 ROI를 얻기 위해서다
   - ROI는 **crop하지 않고 판정에만** 쓴다 (좌표 복원 필드가 생기기 전까지. [`PROPOSALS.md`](PROPOSALS.md) P1)
5. 쿠키 지름(ROI 계산보다 먼저 구함): 기본은 프레임마다 자동 추정(정상 쿠키 면적 중앙값). `session_id`별로 EMA(α=0.2) 평활화하고, 추정에 실패한 프레임은 이전 값을 쓴다
6. 리사이즈된 이미지를 다시 인코딩해 `image_b64`에 넣고, 스키마에 정의된 필드만으로 processed-frame을 만든다. 출력도 스키마로 검증한 뒤 `/infer`로 전달한다

`prompts[].bbox`는 **전처리(리사이즈) 후 이미지 기준 `[x1, y1, x2, y2]` pixel**이다.

## 설정 (환경변수, `C2_` 접두어)
| 변수 | 기본값 | 설명 |
|---|---|---|
| `C2_RESIZE_LONG_SIDE` | `1024` | 긴 변 기준 리사이즈 크기(px). `0`이면 리사이즈 안 함 |
| `C2_EXPECTED_DIAMETER_PX` | (빈 값) | 쿠키 지름(px, **원본 좌표 기준**). 비우면 자동 추정. 값을 주면 고정값으로 쓴다 (리사이즈 배율은 자동 반영) |
| `C2_DIAMETER_EMA_ALPHA` | `0.2` | 자동 추정 지름의 세션별 EMA 계수 |
| `C2_DIAMETER_MAX_SESSIONS` | `256` | 지름을 기억할 최대 세션 수 (오래된 세션부터 제거) |
| `C2_HSV_LOW` / `C2_HSV_HIGH` | `5,80,50` / `30,255,255` | 쿠키 색 HSV 범위 (OpenCV: H 0~180) |
| `C2_ROI_MODE` | `auto` | `auto`: 컨베이어 영역 자동 검출 / `fixed`: `C2_ROI` 사용 / `off`: 화면 전체 |
| `C2_ROI` | (빈 값) | `fixed` 모드용 `x1,y1,x2,y2` (원본 좌표) |
| `C2_INFER_URL` | `http://nginx-inference-lb:8080/infer` | |
| `C2_INFER_TIMEOUT_S` | `5.0` | `/infer` 요청 타임아웃(초) |
| `C2_OUTPUT_IMAGE_FORMAT` | `jpeg` | C3로 보낼 이미지 포맷 (`jpeg` / `png`) |
| `C2_JPEG_QUALITY` | `90` | |
| `C2_VALIDATE_OUTPUT` | `true` | 출력 processed-frame 스키마 검증 |
| `LOG_LEVEL` | `INFO` | `DEBUG`이면 프레임별 지름·배율·ROI를 로그로 남긴다 |

**쿠키 지름은 촬영 세팅마다 다르다.** 예를 들어 시뮬레이터 PNG(1045×450)에서는 약 **78px**,
폰 세로 촬영에서는 약 **170px**이다. 그래서 기본값은 자동 추정이다. 촬영 세팅이 고정되어 있고 쿠키 대부분이 불량인
상황처럼 중앙값 추정이 흔들리는 경우에만 `C2_EXPECTED_DIAMETER_PX`를 지정한다.

## 실행

### Docker
```bash
cd services/c2-preprocessing
docker build -t pocket-smart-factory/c2-preprocessing:dev .
docker run --rm -p 8001:8001 pocket-smart-factory/c2-preprocessing:dev
curl http://localhost:8001/healthz
```
compose에서는 `command: ["sleep", "infinity"]`가 이미지 CMD를 덮어쓴다. 교체 제안은 [`PROPOSALS.md`](PROPOSALS.md) P4 참고.

### 로컬
```bash
cd services/c2-preprocessing
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
uvicorn c2.app:app --app-dir src --port 8001
```

### 테스트
```bash
cd services/c2-preprocessing
pytest            # 처리 시간 출력까지 보려면 pytest -s

# Python 3.11 컨테이너 안에서 (repo root에서 실행, shared/ 동기화 검사 포함)
docker run --rm -v "$PWD:/repo:ro" -w /repo/services/c2-preprocessing \
  pocket-smart-factory/c2-preprocessing:dev \
  sh -c "pip install -q pytest==8.3.4 && python -m pytest -p no:cacheprovider -s"
```
| 테스트 | 내용 |
|---|---|
| `test_detect.py` | reference와 동일 결과, fixture 정확도(recall 1.0, 평균 IoU ≥ 0.95), 0.5배/1.8배/90도 회전 불변성, 1024 리사이즈 파이프라인, EMA |
| `test_preprocess_api.py` | `/infer` mock. 전송 메시지의 스키마 통과, 공통 필드·헤더 전파, 오류 코드 |
| `test_latency.py` | 프레임당 처리 시간 (목표 < 33 ms) |
| `test_phone_frames.py` | 폰 촬영 프레임 5장의 기대 개수와 위치 (원본, 1024 리사이즈, 576/720px 저해상도 입력) |
| `test_schema_sync.py` | `schemas/` 사본이 `shared/schemas` 원본과 같은지 |

## 디렉터리
```
src/c2/
  app.py       FastAPI 앱, /infer 전달(forward_to_infer)
  config.py    환경변수 설정
  pipeline.py  resize → 검출 → processed-frame 생성, 세션별 지름 EMA
  detect.py    검출 로직 (reference에서 이식)
  codec.py     이미지 디코딩/인코딩 (전송 방식 변경 지점)
  schemas.py   jsonschema 검증
schemas/       shared/schemas 사본 (컨테이너용)
reference/     검증용 원본 스크립트 (이미지에 포함 안 함)
tests/         pytest + fixtures
PROPOSALS.md   공유 파일(스키마/compose/.env) 변경 제안
```

## TODO
- [x] Resize / ROI 파라미터 확정 (`C2_RESIZE_LONG_SIDE=1024`, `C2_ROI_MODE=auto`) — `.env.example` 반영은 PROPOSALS P5
- [x] 후보 bbox 생성 방식 결정: HSV 임계값 + contour, 지름 자동 추정
- [x] bbox 좌표계 확정: **전처리 후 이미지 기준 `[x1, y1, x2, y2]` pixel**
- [x] `preprocess_started_at_ms` / `preprocess_finished_at_ms` 기록
- [x] Dockerfile CMD를 uvicorn으로 교체 (compose `command` 교체는 PROPOSALS P4, Member A)
- [ ] 이미지 전송 방식 확정 후 `codec.py` 반영 (`raw_bgr` / `raw_rgb` 지원 포함)
- [ ] `scale` / `crop_offset` / `expected_diameter_px` 스키마 반영 후 출력에 추가, ROI crop 도입 (PROPOSALS P1, P2)
- [ ] `/preprocess` 비동기(202) + drop 카운트 여부 결정 (PROPOSALS P3)
- [x] 폰 촬영 프레임 fixture 추가 (`tests/fixtures/phone/`, 5장)
