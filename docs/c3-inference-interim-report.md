# C3 SAM2 Inference 중간 점검 보고서

| 항목 | 내용 |
|---|---|
| 점검일 | 2026-10-28 |
| 브랜치 | `feature/inference` (마지막 push `51ade5e`) |
| 담당 | Member C (C3 SAM2 Inference + Nginx Scale-out) |
| 범위 | 지금까지의 C3 작업 정리 + **실제 폰 촬영 연동 시험** 결과 + 남은 과제 |
| 관련 문서 | [구현 정리](c3-inference-implementation.md) · [검증 보고서](c3-inference-validation-report.md) · [C2 연동 시험](c3-inference-c2-integration-report.md) · [C4 전달 사양](c3-to-c4-handoff.md) |

> 이 문서의 수치는 모두 실제 실행 결과다. 실제 폰 촬영 영상에는 정답 라벨이 없다. 그래서 §3의 판정 품질은 **간접 지표**로만 평가했다.

---

## 1. 요약

| 항목 | 상태 | 핵심 수치 |
|---|---|---|
| C3 추론 서비스 (SAM2.1-tiny, 프레임 독립) | ✅ 동작 | 프레임당 처리 p50 43 ms (GPU, bf16) |
| C1 → C2 → C3 실제 폰 연동 | ✅ 동작 | C2 → C3 2,102건, **오류·timeout 0** |
| 불량 판정 | ✅ 동작 (라벨 데이터 기준 42/42) | 폰 시험: 정상 73.5% / 불량 26.5%, unknown 0, 프레임 간 판정 흔들림 0.5% |
| 구간별 지연 | ✅ 계산·전달 | 촬영 → 판정 완료 p50 **111 ms** / p95 140 ms (실제 폰) |
| C3 → C4 전달 | ⚠️ C3 쪽은 완료, **C4 미가동** | 폰 시험에서 C4가 떠 있지 않아 1,307건 모두 `ConnectError` (판정에는 영향 없음) |
| Scale-out 설정 | ✅ 실측으로 결정 | 30 fps 1 스트림 = **C3 ×2, `C2_MAX_INFLIGHT=3`** |
| 판정 정확도 (실제 폰) | ❓ 미측정 | 정답 라벨 없음 → §5 |

---

## 2. 지금까지 진행한 작업

| 단계 | 내용 | 커밋 / 문서 |
|---|---|---|
| 1. C3 구현 | FastAPI `/infer`·`/healthz`·`/metrics`, SAM2ImagePredictor(프레임당 embedding 1회, bbox batch, inference_mode, bf16), Mock predictor, Dockerfile(CUDA 12.8), pytest | `7f87b5a` (PR #4 머지) · [구현 정리](c3-inference-implementation.md) |
| 2. C2 연동 분석 후 수정 | C4로 결과 push. 불량 판정을 실제 과자(초코칩 쿠키) 기준으로 재보정해 **20/42 → 42/42** (반점 규칙 끔, 오목 깊이 규칙 추가, 종횡비 0.87) | `9f9f57b` · [검증 보고서 §9](c3-inference-validation-report.md) |
| 3. MQTT 전환 후 연동 | 실제 체인 9구성 부하 시험, replica와 inflight 권장값 결정, Nginx timeout 2500ms, detection-result 1.1.0(구간별 지연), C4 전달 사양 | `51ade5e` · [C2 연동 시험](c3-inference-c2-integration-report.md) · [C4 전달 사양](c3-to-c4-handoff.md) |
| 4. 개발 편의 | 테스트 override에서 C3 replica별 호스트 포트 공개(`localhost:18021~18023/metrics`) | 미커밋 (`services/c3-inference/dev/`) |

---

## 3. 실제 폰 촬영 연동 시험

### 3.1 구성

```
휴대폰 /cam (HTTPS 터널) → C1 → MQTT → C2 (C2_MAX_INFLIGHT=3)
  → Nginx → C3 ×2 (RTX 5060 Ti 1장 공유, bf16) → C4 (미가동)
```

| 항목 | 값 |
|---|---|
| 실행 방식 | `docker-compose.yml` + `compose.gpu.yml` + `services/c3-inference/dev/chain.override.yml` |
| 세션 | 폰 1대 (`m-ngf5e9`) |
| 집계 범위 | C1·C2는 C1·C2 기동 후 누적. C3는 **재기동(포트 공개 적용) 이후 1,307프레임**. 재기동 전에 처리된 약 795프레임은 C3 기록이 남지 않았다(C2 기준으로는 모두 성공) |
| 정답 라벨 | 없음 |

### 3.2 처리 안정성 (오류율)

| 구간 | 결과 | 오류율 |
|---|---|---|
| C1 수신 | 2,109프레임, C1 drop 0 | 0% |
| C2 → C3 | forwarded 2,102, error 0, timeout 0. 다음 프레임이 먼저 와서 버린 것(`dropped_stale`) 7 | **0%** (버린 것 0.3%) |
| C3 처리 | 1,307프레임(replica별 654 / 653, Nginx 분배 균등), `error_count` 0 | **0%** |
| C3 → C4 | 1,307건 모두 `ConnectError` | C4 미가동이 원인. C3 판정에는 영향 없음 |

### 3.3 검출 결과

| 항목 | 값 |
|---|---|
| 총 검출 | 5,111개 (프레임당 0~6개; 3개 718프레임, 6개 422프레임) |
| normal | 3,757 (73.5%) |
| defect (`broken`) | 1,354 (26.5%): 오목 깊이 규칙 675, 종횡비 규칙 679 |
| unknown | 0 (SAM2 mask 품질 점수 0.973 ~ 0.992) |
| 검출이 없는 프레임 | 54 (4.1%) |

프레임별 판정 패턴 상위 (정상, 불량, unknown):
- (3, 0, 0) 24.9%
- (2, 1, 0) 19.6%
- (5, 1, 0) 12.5%
- (4, 2, 0) 8.3%
- (1, 2, 0) 6.4%
- (6, 0, 0) 5.7%
- (0, 3, 0) 4.1%

### 3.4 판정 품질 (간접 지표)

정답이 없으므로 아래 세 가지로 판정이 믿을 만한지 봤다.

**① 판정이 임계값에서 충분히 떨어져 있는가**

| 구분 | 측정값 범위 | 임계값 | 경계 근처 |
|---|---|---|---|
| 정상: 오목 깊이 | 0.005 ~ 0.025 | 0.03 | ≥ 0.02인 것 6개 |
| 정상: 종횡비 | 0.875 ~ 1.000 | 0.87 | < 0.89인 것 2개 |
| 정상: 원형도 | 0.843 ~ 0.908 | 0.80 | — |
| 불량(종횡비): 종횡비 | 0.542 ~ 0.87 | 0.87 | 0.84 ~ 0.87인 것 12개 |
| 불량(오목 깊이): 오목 깊이 | 대부분 ≥ 0.06 | 0.03 | < 0.06인 것 2개 |

- 경계 근처에 있는 판정은 정상 8 / 3,757(0.2%), 불량 14 / 1,354(1.0%)뿐이다.

**② 불량 형태가 물리적으로 맞는가**
- 종횡비 < 0.6 (388개): 면적이 정상의 **0.59배**, 조각 1개 → **반쪽(half)**. 두 쿠키가 한 박스로 합쳐진 오검출이 아니다.
- 종횡비 0.6 ~ 0.87 (291개): 면적 0.83배 → **절단(cut)**.
- 오목 깊이 규칙 (675개): 대부분 0.06 이상 → **파임(bite)·조각 결손(wedge)**.

**③ 프레임 사이에 판정이 흔들리는가**
- 연속 프레임 중 검출 개수가 같은 1,097쌍에서 불량 개수가 바뀐 경우는 **6쌍(0.5%)**이다.

참고: 정상 쿠키 중 면적이 중앙값의 2배를 넘는 것이 17개 있었다. 모두 원형(원형도 ≈ 0.895, 종횡비 ≈ 0.99)이라 카메라와 가까운 쿠키로 보인다.

### 3.5 구간별 지연 (실제 폰, `latency_breakdown_ms`, p50 / p95 ms)

| 구간 | 실제 폰 | 참고: loadgen 시험 (폰 업로드 없음) |
|---|---|---|
| 촬영 → C2 시작 (폰 업로드 + 터널 + C1 + MQTT) | **59 / 72** | 3 / 4 |
| C2 처리 | 8 / 10 | 6 / 7 |
| C2 → C3 전달 | 2 / 3 | 2 / 3 |
| C3 대기 | 0 / 0.5 | 0 / 0 |
| C3 처리 | 42.7 / 72.4 | 43 / 70 |
| **촬영 → 판정 완료** | **111 / 140** | 55 / 83 |

- C3 단계별(replica 1 기준, p50 / p95 ms): encoder 26.9 / 42.1, segmentation 9.3 / 28.0, 불량 분석 1.7 / 3.4, mask RLE 2.4 / 4.7.
- 실제 폰에서 늘어난 지연은 대부분 **폰 → C1 업로드 구간**(+56 ms)이다. C2 이후 구간은 loadgen 시험과 같다.
- 폰과 서버의 시계 차이는 C1이 보정하고 있다(`client_clock_gap_ms` p50 56). 시계가 역전되어 null이 된 구간은 0건이다.

---

## 4. 현재 이슈와 위험

| # | 이슈 | 영향 | 담당 / 조치 |
|---|---|---|---|
| 1 | **C4 미가동 / 미완성** | 화면 표시 불가. C3 → C4 전송이 전부 실패로 기록됨 | Member D. C4를 호스트 8003에 띄우면 바로 연결된다(override 기준) |
| 2 | C4가 C3 `status`를 쓰지 않고 bbox로 다시 판정 | C4를 띄워도 정상 쿠키가 `fail`로 표시됨 | Member D. [C4 전달 사양 §1](c3-to-c4-handoff.md) |
| 3 | 실제 폰 판정 정확도 미측정 | 오검출률·미검출률을 숫자로 말할 수 없음 | §5 방법으로 측정 예정 |
| 4 | 불량 기준을 6장(42개)으로만 보정 | 조명, 거리, 각도가 바뀌면 오판 가능 | 폰 시험에서 경계 근처 판정 0.2~1.0%로 아직 여유는 있음. 라벨 데이터로 계속 확인 |
| 5 | compose 기본 구성에서 C2·C4가 `sleep infinity` | 테스트 override 없이 전체 체인이 뜨지 않음 | Member A |
| 6 | C3 재기동 시 `/metrics`가 초기화됨 | 장시간 시험 집계가 끊김 | 시험 전에 재기동하거나 C2 `/stats`와 함께 본다 |

---

## 5. 다음 단계

| 우선순위 | 할 일 | 방법 |
|---|---|---|
| 1 | **실제 폰 판정 정확도 측정** | (a) 정상 n개 + 불량 m개로 개수를 아는 장면을 고정하고 촬영한다. 프레임별 패턴 `(n, m, 0)`의 비율이 정확도다. (b) 시뮬레이터 화면을 촬영하면서 "라벨 저장"으로 정답을 확보하고 C3 로그와 비교한다 |
| 2 | C4와 함께 E2E 표시 확인 | C4 가동 후 같은 시험을 반복한다. C4 수신 프레임 수가 C2 `forwarded`와 같은지 확인 |
| 3 | 장시간·다중 세션 시험 | 폰 2대 이상, 10분 이상. 60 fps 이상이면 C3 ×3 + inflight 4 |
| 4 | 처리량 여유 확보 (필요할 때) | C4가 mask를 쓰지 않으면 `C3_RETURN_MASK=0`(프레임당 약 2~4 ms 절감) |

---

## 부록: 이 수치를 다시 얻는 방법

repo root, Git Bash 기준. 테스트 override로 체인이 떠 있는 상태여야 한다.

```bash
# C3 replica별 누적 지표 (브라우저로도 열린다)
curl -s localhost:18021/metrics; curl -s localhost:18022/metrics
# C1 / C2
curl -s localhost:8000/api/metrics; curl -s localhost:8001/stats
# C3 프레임별 판정 집계 (상태 비율, 규칙별 원인, 프레임별 패턴)
F="-f docker-compose.yml -f services/c3-inference/compose.gpu.yml -f services/c3-inference/dev/chain.override.yml"
docker compose $F logs --no-log-prefix c3-inference-1 c3-inference-2 | python -c "
import sys,json,collections as C
fr=0;st=C.Counter();rs=C.Counter();pat=C.Counter()
for l in sys.stdin:
    if '\"event\": \"infer\"' not in l: continue
    d=json.loads(l[l.index('{'):]);fr+=1
    c=C.Counter(x['status'] for x in d['decisions']);st.update(c);pat[(c['normal'],c['defect'],c['unknown'])]+=1
    rs.update(x['reason'] for x in d['decisions'] if x['status']!='normal')
n=sum(st.values());print('frames',fr,'detections',n,{k:f'{v} ({v/n:.1%})' for k,v in st.items()})
print('reasons',dict(rs));print('patterns',[(k,v,f'{v/fr:.1%}') for k,v in pat.most_common(8)])"
```
