# API Contract

JSON Schema 원본: [`shared/schemas/`](../shared/schemas/) (draft 2020-12). 이 문서는 요약이며, 불일치 시 **스키마 파일이 우선**한다.

## 메시지 흐름

| # | Producer → Consumer | Endpoint (예정) | Schema |
|---|---|---|---|
| 0 | Mobile/Simulator → C1 | `WS ws://c1-gateway:8000/ws/stream` | TBD (encoded frame) |
| 1 | C1 → C2 | `POST http://c2-preprocessing:8001/preprocess` | `frame-envelope` |
| 2 | C2 → Nginx → C3 | `POST http://nginx-inference-lb:8080/infer` | `processed-frame` |
| 3 | C3 → C4 | `POST http://c4-visualizer:8003/results` | `detection-result` |

전파 HTTP header: `X-Trace-Id`, `X-Frame-Id`, `X-Session-Id` (Nginx access log에서 추적용)

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
