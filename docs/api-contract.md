# API Contract

JSON Schema 원본: [`shared/schemas/`](../shared/schemas/) (draft 2020-12). 이 문서는 요약이며, 불일치 시 **스키마 파일이 우선**한다.

## 메시지 흐름

| # | Producer → Consumer | Endpoint (예정) | Schema |
|---|---|---|---|
| 0 | Mobile/Simulator → C1 | `WS ws://c1-gateway:8000/ws/stream` | TBD (encoded frame) |
| 1 | C1 → C2 | MQTT publish `psf/v1/frames/{session_id}` (QoS 0) | `frame-envelope` |
| 2 | C2 → Nginx → C3 | `POST http://nginx-inference-lb:8080/infer` | `processed-frame` |
| 2b | C2 → C4 | `POST http://c4-visualizer:8003/api/camera/frame` | `camera-frame` (신규) |
| 3 | C3 → C4 | `POST http://c4-visualizer:8003/results` | `detection-result` |

전파 HTTP header: `X-Trace-Id`, `X-Frame-Id`, `X-Session-Id` (HTTP 구간, Nginx access log에서 추적용). MQTT 구간(C1 → C2)에는 header가 없으며 같은 값이 payload에 들어 있다.

## 공통 필드 (모든 메시지)

| Field | Type | 설명 |
|---|---|---|
| `schema_version` | string | 계약 버전 (semver) |
| `trace_id` | string | 프레임 단위 end-to-end trace id (C1 생성) |
| `session_id` | string | 스트리밍 세션 id |
| `frame_id` | integer | 세션 내 프레임 번호 (C1 생성, 단조 증가) |
| `captured_at_ms` | integer | 클라이언트 촬영 시각 (epoch ms) |

공통 필드는 C1에서 생성된 값을 **수정 없이** 다음 단계로 전파한다.

## 1. frame-envelope (C1 → C2)

| Field | Type | 설명 |
|---|---|---|
| `source_width` | integer | 원본 width |
| `source_height` | integer | 원본 height |
| `format` | enum | `jpeg` / `png` / `raw_bgr` / `raw_rgb` |
| `gateway_received_at_ms` | integer | C1 수신 시각 |
| `image_b64` | string (optional) | TODO: 이미지 전송 방식 미정 |

## 1a. C1 → C2 MQTT 전송

C1 → C2 구간만 MQTT를 쓴다. C2 → Nginx → C3, C3 → C4는 HTTP를 유지한다(Nginx 로드밸런싱 기반 Scale-out).

브로커는 compose 서비스 `mqtt-broker`(eclipse-mosquitto:2, 내부 포트 1883)이다. C1은 프레임마다 `psf/v1/frames/{session_id}`에 frame-envelope JSON을 발행하고, C2는 `psf/v1/frames/+`를 구독한다. 페이로드는 HTTP 시절 본문과 같은 frame-envelope JSON(`image_b64` JPEG 포함)이며 스키마 검증도 그대로 쓴다. QoS 0, retain 없음. MQTT에는 HTTP 헤더가 없으므로 `trace_id` 등은 JSON 본문에서만 읽는다.

속도 조절은 C2가 맡는다. C1은 받은 프레임을 즉시 발행한다(C1 이벤트 로그 `published`). C2는 세션별 최신 프레임 1장만 보관·처리하며 프레임별 이벤트를 `C2_EVENT_LOG`에 남긴다. C2 이벤트는 `forwarded`(C3로 전달 완료), `dropped_stale`(처리 전에 같은 세션의 새 프레임이 와서 버림), `dropped_old`(`captured_at_ms` 기준 `C2_MAX_FRAME_AGE_MS`(기본 500ms)보다 오래돼서 버림), `error`, `timeout`이다. Frame Drop Rate는 C1 `published`와 C2 이벤트를 `trace_id`로 조인해 계산한다.

브로커는 호스트의 `127.0.0.1:${MQTT_HOST_PORT}`(기본 11883)에만 노출된다. 컨테이너 안의 서비스는 `mqtt-broker:1883`, 컨테이너 밖에서 실행하는 C1·stub·도구는 `localhost:11883`을 쓴다. 다른 프로젝트 브로커로 잘못 발행하지 않도록 C1/C2 도구는 호스트의 `localhost:1883` 접속을 거부한다.

C1 latency(`preprocess_started_at_ms - gateway_received_at_ms`)에는 브로커 경유 시간과 C2 대기 시간이 포함된다. frame-envelope 필드는 변경 없음.

## 2. processed-frame (C2 → C3)

| Field | Type | 설명 |
|---|---|---|
| `preprocess_started_at_ms` | integer | C2 시작 시각 |
| `preprocess_finished_at_ms` | integer | C2 종료 시각 |
| `width` | integer | 전처리 후 width |
| `height` | integer | 전처리 후 height |
| `prompts` | prompt[] | SAM2 prompt 목록 |
| `image_b64` | string (optional) | TODO |

**prompt**

| Field | Type | 설명 |
|---|---|---|
| `candidate_id` | string | 프레임 내 후보 id |
| `type` | enum | `box` (TODO: `point` 추가 여부) |
| `bbox` | number[4] | `[x1, y1, x2, y2]`, 전처리 후 이미지 pixel 좌표 |

## 2b. camera-frame (C2 → C4)

C2는 전처리한 이미지와 prompts를 C4로 직접 전달한다.

- Endpoint: `POST http://c4-visualizer:8003/api/camera/frame`
- 전송 형식: JSON (10개 필드)

필드 구성:
- `trace_id`: 프레임 고유 식별자
- `session_id`: 스트리밍 세션 식별자
- `frame_id`: 세션 내 프레임 번호
- `gateway_received_at_ms`: C1 프레임 수신 시각
- `preprocess_started_at_ms`: C2 전처리 시작 시각
- `preprocess_finished_at_ms`: C2 전처리 완료 시각
- `width`: 전처리 이미지 너비
- `height`: 전처리 이미지 높이
- `prompts`: C3에 전달하는 후보 객체 정보
- `image_b64`: 전처리 이미지 (Base64 JPEG)

### C2 → C4 전송 규칙

- C2는 C3로 전달하는 프레임만 C4로 전송한다.
- C2에서 드롭된 프레임(dropped_stale, dropped_old)은 C4로 보내지 않는다.
- C3와 C4로 동시에 전송하며, 어느 쪽이 먼저 도착할지는 보장하지 않는다.
- C4는 trace_id를 기준으로 이미지와 C3의 검출 결과를 짝지어야 한다.
- C4 전송은 best-effort 방식으로, 실패해도 재시도하지 않는다.
- C4 전송 실패가 C3의 추론 처리를 지연시키지 않도록 별도로 처리한다.
- C4에 이미지가 도착하지 않고 C3 결과만 도착할 수 있다.
- HTTP 헤더에 X-Trace-Id, X-Frame-Id, X-Session-Id를 포함한다.
- C1과 C2가 서로 다른 컴퓨터에서 실행되는 경우 시계 차이로 latency 계산에 오차가 발생할 수 있다.

## 3. detection-result (C3 → C4)

| Field | Type | 설명 |
|---|---|---|
| `inference_container_id` | string | 처리한 C3 replica (`c3-inference-N`) |
| `inference_started_at_ms` | integer | 추론 시작 시각 |
| `inference_finished_at_ms` | integer | 추론 종료 시각 |
| `inference_latency_ms` | number | 추론 소요 시간 |
| `detections` | detection[] | 검출 결과 |
| `preprocess_started_at_ms` / `preprocess_finished_at_ms` | integer (optional, 1.1.0) | processed-frame에서 전파한 C2 시각 |
| `inference_received_at_ms` | integer (optional, 1.1.0) | C3가 `/infer` 요청을 받은 시각 |
| `latency_breakdown_ms` | object (optional, 1.1.0) | C3가 계산한 구간별 지연: `capture_to_preprocess`, `preprocess`, `c2_to_c3`, `c3_queue`, `c3_service`, `c3_total`, `capture_to_c3_done` (ms, 시계 역전 시 null) |
| `inference_timings_ms` | object (optional, 1.1.0) | C3 내부 단계: `lock_wait`, `decode`, `encoder`, `segmentation`, `defect_analysis`, `mask_encode`, `total` |

C4 전달 방식, mask RLE 디코딩, frame drop 계산식은 [`c3-to-c4-handoff.md`](c3-to-c4-handoff.md)에 있다.

**detection**

| Field | Type | 설명 |
|---|---|---|
| `object_id` | string | SAM2 tracking object id (세션 내 유지) |
| `bbox` | number[4] | `[x1, y1, x2, y2]` |
| `status` | enum | `normal` / `defect` / `unknown` |
| `defect_type` | string \| null | `status != defect`이면 null. TODO: 불량 분류 체계 |
| `defect_score` | number | 0 ~ 1 |
| `mask` | object \| null | `{encoding: rle\|polygon\|png_b64, width, height, data}` TODO: 인코딩 확정 |

## Latency 계산식

| Metric | 계산 |
|---|---|
| C1 latency | `preprocess_started_at_ms - gateway_received_at_ms` (C1 처리 + C1→C2 전송) |
| C2 latency | `preprocess_finished_at_ms - preprocess_started_at_ms` |
| C3 inference latency | `inference_latency_ms` |
| C2 → C3 전달 | `latency_breakdown_ms.c2_to_c3` (= `inference_received_at_ms - preprocess_finished_at_ms`, 1.1.0) |
| End-to-End latency | `visualized_at_ms(C4) - captured_at_ms` |

TODO: 정의는 구현 시 팀 합의로 확정한다.

## 변경 이력

| Version | 날짜 | 변경 |
|---|---|---|
| 1.0.0 | 2026-10-28 | 초기 skeleton |
| 1.1.0 (제안) | 2026-10-28 | C1 → C2 전송을 MQTT로 변경 (frame-envelope 필드 변경 없음, 드롭은 C2 담당) |
| detection-result 1.1.0 | 2026-10-28 | optional 필드 추가: `preprocess_started/finished_at_ms`, `inference_received_at_ms`, `latency_breakdown_ms`, `inference_timings_ms` (C3 → C4 구간별 지연, 하위 호환) |
