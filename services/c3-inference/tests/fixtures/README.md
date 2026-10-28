# C3 test fixtures

## `c2_snack_masks.json`

C2 산출물에 실제 SAM2 mask를 붙인 회귀 테스트용 데이터다. 불량 판정 임계값의 근거이기도 하다.

| 항목 | 내용 |
|---|---|
| 원본 이미지 | C2 fixture (`services/c2-preprocessing/tests/fixtures/`): 시뮬레이터 PNG 1장(1045×450), 폰 세로 촬영 JPEG 5장(1080×1920) |
| prompts | C2 코드(`origin/main`, 2026-10-28, 수정 없음)의 `build_processed_frame`이 만든 bbox. 리사이즈 후 좌표 |
| mask | SAM2.1 Hiera Tiny(bf16, RTX 5060 Ti), C3 `Sam2ImagePredictorBackend` 출력. bbox 주변만 잘라 RLE로 저장 (`mask_offset` = 잘라낸 원점) |
| 객체 수 | 42개: round 23, bite 7, wedge 7, cut 4, half 1 |
| 라벨 출처 | 시뮬레이터: C2 fixture의 `labels_*.json` (`label_source: c2_simulator_labels`). 폰: C2 fixture에 불량 라벨이 없어서 사람이 눈으로 판정 (`label_source: visual_inspection`) |

- 대상 과자는 **초코칩 쿠키**다. 쿠키 안의 어두운 점은 정상(칩)이다.
- 라벨 체계는 형태 불량만 있다(bite / wedge / cut / half). C3는 이 넷을 모두 `broken`으로 판정한다.

### 사용하는 테스트
- `tests/test_defects.py::test_c2_fixture_real_masks`: 저장된 mask로 `DefectAnalyzer` 판정을 확인한다. torch가 필요 없다.
- `tests/test_sam2_real.py::test_real_sam2_on_c2_fixture_frames`: C2 fixture 이미지를 C2와 같은 방식으로 리사이즈하고 JPEG q90으로 인코딩해서 실제 SAM2로 끝까지 판정한다. checkpoint나 C2 이미지가 없으면 skip한다.

### 다시 만들 때
C3 테스트가 다른 서비스 코드를 import하지 않도록 생성 스크립트는 저장소에 넣지 않았다. 만드는 절차는 다음과 같다.
1. C2 venv에서 `c2.pipeline.build_processed_frame`으로 각 fixture 이미지의 processed-frame을 만든다(frame-envelope는 원본 이미지를 JPEG q90으로 인코딩).
2. C3 venv에서 `Sam2ImagePredictorBackend`로 `set_image`와 `predict_boxes`를 실행한다.
3. 시뮬레이터 객체는 bbox IoU로 라벨에 매칭하고, 폰 객체는 눈으로 라벨을 붙여 저장한다.

C2의 bbox 로직이나 SAM2 버전이 바뀌면 다시 만들고, 임계값 근거(`src/config.py`의 주석)도 갱신한다.
