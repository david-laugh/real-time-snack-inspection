# Stream Simulator

**Owner:** Member B

스마트폰 없이 개발/벤치마크를 하기 위해, 녹화 영상 또는 이미지 시퀀스를 지정 FPS로 C1 Gateway에 송출하는 도구.

## 요구사항 (예정)
- 입력: 영상 파일 / 이미지 폴더
- 송출 FPS 지정 (기본 30fps), 반복 재생
- 해상도 / JPEG 품질 지정
- 송출 timestamp 기록 → Input FPS / Frame Drop Rate 계산에 사용
- 병목 실험 시 재현 가능한 부하 생성기로 사용 (`docs/bottleneck-test-plan.md`)

## TODO
- [ ] `src/`에 simulator 구현
- [ ] 테스트용 샘플 영상 확보 방법 (용량 큰 파일은 repository에 포함하지 않음)
