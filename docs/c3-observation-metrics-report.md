# C3 통계 지표 개선: 프레임 · 관측 · 고유 과자 구분

| 항목 | 내용 |
|---|---|
| 작성일 | 2026-10-28 |
| 브랜치 | `feature/inference` (작업 시작 시점 `main`과 동일하게 맞춘 뒤 진행) |
| 목적 | C3의 **검출(관측) 횟수**가 **실제 고유 과자 수**로 오해되지 않도록 통계 지표와 응답 구조를 정리 |
| 범위 | C3 통계 관리, 응답 header, 테스트, 문서만 변경. SAM2 추론, 불량 판정, `detection-result` 스키마, C1·C2·C4 코드는 변경 없음 |
| 관련 문서 | [C3 README "통계 지표"](../services/c3-inference/README.md) · [C4 전달 사양 §6 고유 과자 집계](c3-to-c4-handoff.md) |

> 수치는 모두 실제 측정값이다.

---

## 1. 문제

- 체인 시험에서 **2,785개 프레임 → 15,155회 검출**이 집계되었다.
- SAM2 오검출이 아니다. **같은 과자가 여러 프레임에 반복 등장**해서 검출 횟수가 누적된 것이다.
  - C2 fixture 기준: 한 프레임 안 후보 중복 0, bbox·mask 겹침 0.
  - 실제 체인 기준: 같은 프레임 중복 처리 0, C4 중복 기록 0.
- 기존 C3 `/metrics`의 `processed_detection_count`, `status_counts`와 C4의 "총 검사"는 모두 **관측 횟수**다. 그런데 이름만 보면 검사한 과자 수처럼 읽혔다.

## 2. 원칙

1. C3는 프레임 단위 SAM2 추론을 유지한다(stateless, replica 분산).
2. 프레임 안 검출 결과는 기존 그대로 반환한다.
3. C3 지표는 **프레임 수**와 **관측 수**만 다루고, 이름과 정의로 둘을 구분한다.
4. C3 누적 검출 수를 검사 완료 과자 수로 쓰지 않는다. C3에는 고유 과자 카운터를 만들지 않는다.
5. 프레임 간 tracking과 고유 과자 집계는 C4가 한다(입력 필드는 이미 제공 중, §5).

## 3. 변경 내용

| 파일 | 변경 |
|---|---|
| `services/c3-inference/src/metrics.py` | 지표 재구성(아래 표), 지표 정의(`metric_definitions`)를 응답에 포함, 단위(frame / observation)를 모듈 docstring에 명시 |
| `services/c3-inference/src/app.py` | 성공 시 프레임 정보(trace/session/frame id, 추론 시간)를 지표에 전달. 응답 header `X-C3-Detection-Count` 추가 |
| `services/c3-inference/tests/test_observation_metrics.py` (신규) | C2 fixture 6장 + 저장된 실제 SAM2 mask로 프레임·관측·상태 집계, 반복 프레임, 오류·빈 프레임, replica 분리 검증 |
| `services/c3-inference/tests/test_sam2_real.py` | 실제 SAM2 E2E에서 `/metrics` 증가분 검증 추가 |
| `services/c3-inference/README.md` | "통계 지표" 절: 각 지표 정의, 관측 ≠ 고유 과자 |
| `docs/c3-to-c4-handoff.md` | §1 할 일 추가, **§6 고유 과자 집계(C4 구현 필요)**: 입력 필드, 권장 tracking·검사선 카운트 방식, 주의사항 |

### `/metrics` 지표 전후 비교 (replica별)

| 수정 전 | 수정 후 | 비고 |
|---|---|---|
| — | `infer_request_count` | 받은 `/infer` 요청 수 = `processed_frame_count + error_count` |
| `processed_frame_count` | `processed_frame_count` | 그대로 (성공 처리 프레임 수) |
| `processed_detection_count` | **`detected_observation_count`** | 이름으로 "관측 횟수"임을 명확히 함 |
| — | `processed_detection_count` | **deprecated 별칭**, 같은 값. 기존 호환용 |
| — | `current_frame_detection_count`, `current_frame` | 마지막 처리 프레임의 검출 수, id, 상태별 수, 추론 시간 |
| `status_counts` | `status_counts` | 키는 그대로. 정의에 "관측 횟수, 고유 과자 아님" 명시 |
| — | `frames_with_detections` / `frames_without_detections` | 검출 유무별 프레임 수 |
| — | `detections_per_frame` | 최근 window의 프레임당 검출 수 평균·최대 |
| `latency_window_ms.total_inference_ms` | + `inference_latency_ms` | 프레임별 추론 시간의 최근 값과 window 통계 (기존 window도 유지) |
| — | `metric_definitions` | 각 지표의 정의 문자열 |

### C3 → C4 응답 (`detection-result`)

- **본문은 변경하지 않았다.** C4 tracking에 필요한 `frame_id`, `trace_id`, `session_id`, `bbox`, `mask`, `status`, `defect_type`, `defect_score`, `inference_container_id`, `inference_latency_ms`가 이미 모두 들어 있다(테스트와 실제 체인에서 확인).
- `current_frame_detection_count`는 본문에 넣지 않았다. 이유는 두 가지다.
  - 항상 `len(detections)`와 같다.
  - 스키마가 `additionalProperties:false`라서 추가하려면 공유 스키마를 다시 바꿔야 한다.
- 대신 `/infer` 응답 header `X-C3-Detection-Count`와 `/metrics`로 제공한다. C4는 `len(detections)`를 쓰면 된다.

## 4. 테스트 결과

### 4.1 pytest

| 항목 | 결과 |
|---|---|
| 전체 | **112 passed** (`test_api` 32, `test_defects` 71, `test_observation_metrics` 4, `test_sam2_real` 5) |
| C2 fixture 6장 (Mock + 저장된 실제 SAM2 mask) | 요청 6 = 처리 6. 프레임별 검출 수 = C2 후보 수 = 응답 `detections` 수 = header. 누적 관측 **42** = 프레임별 합. 상태 normal 23 / defect 19 (라벨과 일치, 합 = 42). `current_frame_detection_count` = 마지막 프레임 4 |
| 같은 프레임 10회 반복 (쿠키 6개) | 처리 10, 관측 **60**. 고유 과자 카운터 없음 |
| 오류·빈 프레임 혼합 | 요청 4 = 성공 3 + 오류 1. 관측 3. 검출 있음 2 / 없음 1. 오류가 `current_frame`을 덮어쓰지 않음 |
| replica 2개 (round-robin 시뮬레이션) | replica별 처리 3 / 3, 관측 26 / 16, 합 42, 상태 합 23 / 19, 각자 마지막 frame_id 4 / 5 |
| 실제 SAM2 (GPU) C2 fixture E2E | 판정 42/42. `/metrics` 증가분: 요청 6, 처리 6, 관측 42, normal 23 / defect 19 / unknown 0, 현재 프레임 4 |

### 4.2 실제 체인 정합성 (C1 loadgen 30 fps × 20 s → MQTT → C2 → Nginx → C3 ×2 → C4)

| 항목 | C2 | C3 (replica 1 + 2) | C4 | 일치 |
|---|---|---|---|---|
| 요청 / 전달 | forwarded 600, error+timeout 0 | 요청 300 + 300 = 600, 오류 0 | — | ✅ |
| 처리 프레임 | — | 300 + 300 = 600 | 수신 프레임 600 | ✅ |
| 관측(검출) | — | 1,620 + 1,620 = **3,240** | detection 3,240 | ✅ |
| normal / defect / unknown | — | 1,800 / 1,440 / 0 | 1,800 / 1,440 / 0 | ✅ |
| `processed_detection_count` 별칭 | — | 두 replica 모두 `detected_observation_count`와 같음 | — | ✅ |

- 입력은 고유 쿠키 **27개**(폰 fixture 5장)를 20초 동안 120회 반복한 것이다. 관측 3,240 = 27 × 120이다. 관측 수는 고유 과자 수와 전혀 다르다는 것이 실측으로 확인된다.
- replica별 `/metrics`는 `localhost:18021`, `18022`에서 각자 자기 몫(300 / 300)을 보여 준다.

### 4.3 성능 (변경 전후, 같은 조건)

**단일 요청** (실제 SAM2, 1024×1024, 20프레임 중앙값, ms)

| bbox | 변경 전 | 변경 후 |
|---|---|---|
| 1 | 40.13 | 39.74 |
| 4 | 45.93 | 45.20 |
| 8 | 55.32 | 54.15 |

**실제 체인** (C3 ×2, `C2_MAX_INFLIGHT=3`, 30 fps, 30 s)

| 실행 | C2 전달 | Nginx p50 / p95 (ms) | C3 처리 p50 / p95 (ms) | encoder p50 (ms) | GPU 평균 | timeout · 오류 |
|---|---|---|---|---|---|---|
| 변경 전 #1 | 30.0 fps | 42 / 55 | 39.6 / 53.3 | 24.96 | 62.6% | 0 · 0 |
| 변경 전 #2 | 30.0 fps | 42 / 55 | 40.0 / 52.2 | 24.91 | 61.7% | 0 · 0 |
| 변경 후 #1 | 30.0 fps | 43 / **72** | 40.6 / **69.5** | 25.28 | 65.5% | 0 · 0 |
| 변경 후 #2 | 30.0 fps | 42 / 55 | 40.3 / 53.0 | 25.26 | 64.8% | 0 · 0 |
| 변경 후 #3 | 30.0 fps | 42 / 57 | 39.8 / 54.8 | 24.93 | 61.7% | 0 · 0 |

- 처리량(30.0 fps)과 중앙값 지연은 변경 전후가 같다.
- 변경 후 #1의 p95만 높았다(72 ms). #2·#3은 변경 전 수준(55~57 ms)이라 실행 간 편차로 판단했다. 지표 갱신은 프레임당 lock 1회와 정수 덧셈뿐이다.

## 5. C4 담당자 구현 사항 (요약, 상세는 [c3-to-c4-handoff.md §6](c3-to-c4-handoff.md))

1. 세션별 `frame_id` 정렬 버퍼 (replica 때문에 도착 순서가 바뀔 수 있음)
2. 프레임 간 bbox IoU / 중심 거리 매칭으로 트랙 ID 부여 (`object_id`는 프레임 안에서만 유효하므로 키로 쓰지 않음)
3. 검사선 통과 시 트랙당 1회 카운트 (시뮬레이터 `total_passed` / `defect_passed`와 비교 가능)
4. 트랙별 최종 판정(프레임별 판정 다수결 등), 과자 기준 불량률
5. 화면에서 "관측(검출) 수"와 "검사 완료 과자 수"를 분리 표시. 현재 C4의 "총 검사"는 관측 수다

## 6. 남은 문제와 개선 방향

| 항목 | 내용 |
|---|---|
| 고유 과자 수 미산출 | 설계상 C3에서는 불가(stateless, replica 분산). C4 tracking 전까지 시스템에 "검사 완료 과자 수"가 없다 |
| 빈 프레임 미전송 | C2 `C2_SKIP_EMPTY`로 후보 0개 프레임은 C3에 오지 않는다. C3 `frames_without_detections`는 C3까지 온 빈 프레임만 센다. C4 tracking은 `frame_id` 빈 번호를 고려해야 한다 |
| 프레임 내 중복 후보 | fixture·실측에서는 0건. 반사광 등으로 한 과자의 마스크가 쪼개지는 경우는 미확인. 이번 범위에서 C3 dedup은 구현하지 않음(요청사항). C4 tracking에서 같은 프레임 고IoU 병합을 권장 |
| replica 재시작 시 초기화 | `/metrics`는 프로세스 메모리 누적값이라 재시작하면 0이 된다. 장기 집계는 C3 프레임별 JSON 로그나 C4 기록을 사용한다 |
| 실제 폰 장시간 검증 | 이번 정합성 시험은 loadgen(fixture 반복) 기준이다. 폰 스트림 장시간 시험에서 같은 확인이 필요하다 |
