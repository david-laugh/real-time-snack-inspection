# benchmark

**Owner:** Member C (with Member D for metric collection)

병목 측정 실험의 스크립트, 설정, 결과를 관리한다. 측정 항목과 실험 설계는 [`docs/bottleneck-test-plan.md`](../docs/bottleneck-test-plan.md) 참고.

## 구조
```
benchmark/
  README.md
  results/        # 실험 결과 (raw *.csv 는 git에서 제외)
```

## 결과 파일 규칙 (예정)
- 파일명: `results/<YYYYMMDD>_c3x<N>_<label>.csv`
- 프레임 단위 row: `trace_id, session_id, frame_id, inference_container_id, captured_at_ms, gateway_received_at_ms, preprocess_started_at_ms, preprocess_finished_at_ms, inference_started_at_ms, inference_finished_at_ms, visualized_at_ms`
- 요약 수치는 `docs/bottleneck-test-plan.md`의 비교표에 기록

## TODO
- [ ] 결과 수집/집계 스크립트
- [ ] GPU 메트릭 수집 방법 (nvidia-smi / pynvml / DCGM)
