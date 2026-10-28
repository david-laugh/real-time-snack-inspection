# C3 dev tools: 실제 체인 부하 시험

`docker-compose.yml`을 고치지 않고 **C1 → MQTT → C2 → Nginx → C3 ×N (GPU) → C4** 전체를 띄워 C3 scale-out을 측정한다.
C1, C2, C4 코드는 실행만 하고 수정하지 않는다. 결과 해석은 [`docs/c3-inference-c2-integration-report.md`](../../../docs/c3-inference-c2-integration-report.md)에 있다.

| 파일 | 내용 |
|---|---|
| `chain.override.yml` | 테스트 전용 compose override. C2를 uvicorn으로 실행(base는 아직 `sleep infinity`), C1에 C2 폰 fixture(`/frames`)를 mount, C3 → 호스트 C4(`host.docker.internal:8003`) |
| `bench_chain.py` | 구성(replica 수 : `C2_MAX_INFLIGHT` : client 수)별로 컨테이너를 다시 띄우고 부하를 줘서 결과를 JSON으로 저장. 표준 라이브러리만 사용 |

## 준비
- NVIDIA GPU, Docker(NVIDIA Container Toolkit), `services/c3-inference/models/sam2.1_hiera_tiny.pt`(`scripts/download_checkpoint.py`)
- 이미지 빌드 (repo root):
  ```bash
  MQTT_HOST_PORT=11884 docker compose -f docker-compose.yml -f services/c3-inference/compose.gpu.yml \
    -f services/c3-inference/dev/chain.override.yml build c1-gateway c2-preprocessing nginx-inference-lb c3-inference-1
  ```
- (선택) C4: `C4-Visualizer` 브랜치의 `services/c4-visualizer`에서 `uvicorn src.main:app --host 0.0.0.0 --port 8003`.
  C4가 없어도 시험은 돌아간다. 이 경우 C3의 `results_push_error_count`가 늘고, 결과의 `c4_received_detections`는 `null`이 된다.
- 호스트 포트 8000(C1), 8001(C2), 8003(C4), 11884(MQTT)를 비워 둔다.

## 실행
```bash
# 권장 구성 확인 (30 fps 1 스트림, C3 x2, C2_MAX_INFLIGHT=3)
python services/c3-inference/dev/bench_chain.py --plan 2:3:1 --duration 30

# 보고서와 같은 9개 구성
python services/c3-inference/dev/bench_chain.py \
  --plan 1:2:1,1:4:1,2:3:1,2:4:1,3:4:1,1:4:2,2:4:2,3:4:2,3:6:2 --duration 30 --out bench.json

# 정리
MQTT_HOST_PORT=11884 docker compose -f docker-compose.yml -f services/c3-inference/compose.gpu.yml \
  -f services/c3-inference/dev/chain.override.yml --profile scale-out down
```

- 구성마다 3초 동안 warm-up 트래픽을 보낸 뒤 `--duration`초를 측정한다. 1구성은 약 1분이 걸린다.
- 입력 영상: C2 폰 fixture 5장을 C1 `loadgen`이 폭 960으로 줄여 30 fps로 반복 송출한다. C2는 이를 576×1024로 처리한다.
- 출력 항목:
  - C2: `forwarded` / `dropped_stale` / `timeout`
  - Nginx: 상태 코드, replica별 분배, `request_time` p50/p95
  - C3: 단계별 p50/p95 (`lock_wait_ms` = replica 안의 대기 시간)
  - C4가 받은 detection 수, GPU 사용률과 메모리
