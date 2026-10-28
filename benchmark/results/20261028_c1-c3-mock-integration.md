# C1→C3(mock) 통합 테스트 결과

## 1. 테스트 개요

- 날짜: 2026-10-28
- 목적: 최신 main 기준 C1→MQTT→C2→Nginx→C3 통신 검증
- 환경: Windows, Docker Desktop, Docker Compose
- C3 설정: `C3_PREDICTOR=mock`
- C3 인스턴스: 1개
- C4: 미가동

## 2. 테스트 조건

- 입력 방식: C1 loadgen (synthetic)
- 입력 FPS: 5
- 테스트 시간: 5초
- 입력 이미지 너비: 640px
- 목표 전송 프레임: 25개

실행 명령:

```powershell
docker compose up -d --build c1-gateway c2-preprocessing nginx-inference-lb c3-inference-1
```

```powershell
docker compose exec -T c1-gateway python src/c1_gateway.py loadgen --fps 5 --duration 5 --width 640 --reset
```

C2 통계 확인:

```powershell
docker compose exec -T c2-preprocessing python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8001/stats').read().decode())"
```

## 3. 측정 결과

C2 `/stats` 기준:

- MQTT 연결: 정상 (`connected: true`)
- MQTT 수신: 25프레임
- C3 전달 성공 (`forwarded`): 25프레임
- 오래된 프레임 드롭 (`dropped_stale`): 0건
- 이전 프레임 드롭 (`dropped_old`): 0건
- 처리 오류 (`error`): 0건
- 타임아웃 (`timeout`): 0건

C4 관련 통계:

- 전송 성공 (`c4_sent`): 0건
- 전송 실패 (`c4_error`): 10건
- 전송 생략 (`c4_skipped`): 15건

## 4. 결과 해석

C1에서 MQTT를 통해 C2로 전달한 테스트 프레임 25개가 C2에서 수신되었으며, 25개 모두 C3(mock)까지 정상 전달되었다.

C2 기준 프레임 드롭, 처리 오류, 타임아웃은 발생하지 않았다.

C4는 미가동 상태였으므로 전송 성공이 없었으며, 전송 실패와 동시 전송 한도에 따른 생략이 기록되었다.

## 5. 한계 및 후속 검증

이번 테스트는 C3 mock 기반의 통신 검증이다. 실제 SAM2 모델의 판정 정확도나 C4 대시보드 표시까지 검증한 결과는 아니다.

후속 작업:

- 실제 SAM2 기반 추론 확인
- C4 가동 후 카메라 프레임 및 판정 결과 수신 확인
- C1→C4 전체 통합 테스트
- 실제 휴대폰 촬영 기반 테스트
