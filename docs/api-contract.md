# API Contract

JSON Schema 원본: [`shared/schemas/`](../shared/schemas/) (draft 2020-12). 이 문서는 요약이며, 불일치 시 **스키마 파일이 우선**한다.

## 메시지 흐름

| # | Producer → Consumer | Endpoint (예정) | Schema |
|---|---|---|---|
| 0 | Mobile/Simulator → C1 | `WS ws://c1-gateway:8000/ws/stream` | TBD (encoded frame) |
| 1 | C1 → C2 | MQTT publish `psf/v1/frames/{session_id}` (QoS 0) | `frame-envelope` |
| 1a | C2 → C1 | MQTT publish `psf/v1/acks` (QoS 0) | `frame-ack` |
| 2 | C2 → Nginx → C3 | `POST http://nginx-inference-lb:8080/infer` | `processed-frame` |
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

## 1a. C1 ↔ C2 MQTT 전송

C1 → C2 구간만 MQTT를 쓴다. C2 → Nginx → C3, C3 → C4는 HTTP를 유지한다(Nginx 로드밸런싱 기반 Scale-out).

브로커는 compose 서비스 `mqtt-broker`(Mosquitto, 1883)이고 토픽 prefix는 `MQTT_TOPIC_PREFIX`(기본 `psf/v1`)다. C1은 프레임마다 `psf/v1/frames/{session_id}`에 frame-envelope JSON(`image_b64` 포함)을 QoS 0으로 발행한다. C2는 `psf/v1/frames/+`를 구독한다.

C2는 프레임 하나를 처리하고 나면(보통 `/infer` 응답 이후) `psf/v1/acks`에 [`frame-ack`](../shared/schemas/frame-ack.schema.json)을 발행한다. 필수 필드는 `trace_id`, `session_id`, `frame_id`, `status`(`ok` / `error`)이고, 실패 시 `detail`에 이유를 넣는다. C1은 ack를 받아야 다음 프레임을 보내며(동시 대기 수 `C1_MAX_INFLIGHT`), `C1_TIMEOUT` 안에 ack가 없으면 timeout으로 기록하고 넘어간다. 따라서 ack 시점이 파이프라인의 backpressure를 결정한다. 프레임을 받자마자 ack하면 C1의 drop 정책이 무력화되므로, C3 결과를 받은 뒤 ack하는 것을 기본으로 한다.

QoS 0을 쓰는 이유는 실시간 검사에서 유실된 프레임을 재전송하는 것보다 최신 프레임을 처리하는 것이 중요하기 때문이다. C2를 여러 개 띄우는 경우에는 공유 구독(`$share/c2/psf/v1/frames/+`)으로 프레임을 나눠 받을 수 있다.

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

## 3. detection-result (C3 → C4)

| Field | Type | 설명 |
|---|---|---|
| `inference_container_id` | string | 처리한 C3 replica (`c3-inference-N`) |
| `inference_started_at_ms` | integer | 추론 시작 시각 |
| `inference_finished_at_ms` | integer | 추론 종료 시각 |
| `inference_latency_ms` | number | 추론 소요 시간 |
| `detections` | detection[] | 검출 결과 |

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
| End-to-End latency | `visualized_at_ms(C4) - captured_at_ms` |

TODO: 정의는 구현 시 팀 합의로 확정한다.

## 변경 이력

| Version | 날짜 | 변경 |
|---|---|---|
| 1.0.0 | 2026-10-28 | 초기 skeleton |
| 1.1.0 (제안) | 2026-10-28 | C1 → C2 전송을 MQTT로 변경, `frame-ack` 추가 (frame-envelope 필드 변경 없음) |
