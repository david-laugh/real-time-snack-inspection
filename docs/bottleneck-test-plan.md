# Bottleneck Test Plan

> ⚠️ 이 문서의 모든 수치는 **실측 전**이다. 실험 후 실제 측정값으로만 채운다. 임의 값 기입 금지.

## 목적
- 30fps 입력 스트림에서 파이프라인 병목 구간(C1/C2/C3/C4) 식별
- C3 SAM2 Inference replica 수(1/2/3)에 따른 처리량·지연 변화 측정

## 측정 Metric

| Metric | 정의 | 측정 위치 |
|---|---|---|
| Input FPS | C1이 초당 수신한 프레임 수 | C1 |
| Processed FPS | C4가 초당 표시(처리 완료)한 프레임 수 | C4 |
| Frame Drop Rate | `1 - (processed frames / input frames)` | C1 + C4 (`frame_id` 기준) |
| C1 latency | `preprocess_started_at_ms - gateway_received_at_ms` | C2 |
| C2 latency | `preprocess_finished_at_ms - preprocess_started_at_ms` | C2 |
| C3 inference latency | `inference_latency_ms` | C3 |
| End-to-End latency | `visualized_at_ms - captured_at_ms` | C4 |
| P50 | 각 latency의 50th percentile | 집계 스크립트 |
| P95 | 각 latency의 95th percentile | 집계 스크립트 |
| GPU utilization | GPU 사용률 (%) | 호스트 (nvidia-smi / pynvml) |
| GPU VRAM | GPU 메모리 사용량 (MiB) | 호스트 (nvidia-smi / pynvml) |

## 실험 조건 (TODO: 확정)
- 입력: `clients/simulator` 고정 영상, 30fps, 해상도 TODO
- 실험 시간: TODO (예: warm-up 후 N분)
- SAM2 variant: TODO
- GPU: TODO (모델명, VRAM)
- Nginx 분배 방식: round-robin (비교군 TODO: least_conn / session hash)
- 반복 횟수: TODO

## 실행 구성
| 실험 | 실행 명령 |
|---|---|
| C3 x 1 | `docker compose up` |
| C3 x 2 | `docker compose up -d` 후 `docker compose up -d c3-inference-2` (profile 서비스는 이름을 직접 지정하면 기동됨) |
| C3 x 3 | `docker compose --profile scale-out up` |

> 참고: 단일 GPU에서 replica를 늘리면 GPU를 공유하므로 처리량이 선형 증가하지 않을 수 있다. GPU utilization / VRAM을 함께 기록한다.

## 결과 비교표

### Throughput

| 구성 | Input FPS | Processed FPS | Frame Drop Rate |
|---|---|---|---|
| C3 x 1 | TODO | TODO | TODO |
| C3 x 2 | TODO | TODO | TODO |
| C3 x 3 | TODO | TODO | TODO |

### Latency (ms)

| 구성 | C1 P50 | C1 P95 | C2 P50 | C2 P95 | C3 P50 | C3 P95 | E2E P50 | E2E P95 |
|---|---|---|---|---|---|---|---|---|
| C3 x 1 | TODO | TODO | TODO | TODO | TODO | TODO | TODO | TODO |
| C3 x 2 | TODO | TODO | TODO | TODO | TODO | TODO | TODO | TODO |
| C3 x 3 | TODO | TODO | TODO | TODO | TODO | TODO | TODO | TODO |

### GPU

| 구성 | GPU utilization (%) | GPU VRAM (MiB) |
|---|---|---|
| C3 x 1 | TODO | TODO |
| C3 x 2 | TODO | TODO |
| C3 x 3 | TODO | TODO |

## 분석 (실험 후 작성)
- 병목 구간: TODO
- Scale-out 효과: TODO
- 개선 방안: TODO
