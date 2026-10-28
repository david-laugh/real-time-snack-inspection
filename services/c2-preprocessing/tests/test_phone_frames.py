"""폰 촬영 프레임 회귀 테스트.
expected_count는 사람이 눈으로 확인한 '완전히 보이는 쿠키 수'다.
reference_boxes_xyxy는 검증 당시 검출 결과(눈으로 확인 완료)로, 위치가 크게 어긋나지 않는지만 본다.
기대값은 수정하지 않는다. 실패하면 검출 쪽 원인을 찾는다.
"""
import json
from pathlib import Path

import cv2
import pytest

from c2.config import Settings
from c2.pipeline import detect_frame
from helpers import iou

FIX = Path(__file__).parent / "fixtures" / "phone"
SPEC = json.loads((FIX / "phone_frames_expected.json").read_text(encoding="utf-8"))


def _load(case, width=None):
    img = cv2.imread(str(FIX / case["file"]))
    assert img is not None
    if width is not None:
        h, w = img.shape[:2]
        img = cv2.resize(img, (width, round(h * width / w)), interpolation=cv2.INTER_AREA)
    return img


def _detect(img, resize_long_side):
    """서비스 검출 경로(pipeline.detect_frame): 지름 자동 추정, ROI auto. 세션 EMA 없이 프레임 단독."""
    settings = Settings(resize_long_side=resize_long_side, roi_mode="auto", expected_diameter_px=None)
    _, det = detect_frame(img, settings, "phone", None)
    return det


def _check(case, det, ref_scale=1.0):
    """ref_scale: 기준 bbox(폰 원본 1080x1920 좌표)에서 검출 입력 이미지까지의 배율."""
    assert len(det.boxes) == case["expected_count"], (case["note"], det.boxes)
    for ref in case["reference_boxes_xyxy"]:
        ref = [v * ref_scale * det.scale for v in ref]
        assert max(iou(ref, b) for b in det.boxes) >= 0.8, f"{case['file']}: {ref} 위치 불일치"


@pytest.mark.parametrize("case", SPEC["frames"], ids=lambda c: c["file"])
def test_phone_frame(case):
    """원본 해상도."""
    det = _detect(_load(case), resize_long_side=0)
    assert det.scale == 1.0
    _check(case, det)


@pytest.mark.parametrize("case", SPEC["frames"], ids=lambda c: c["file"])
def test_phone_frame_resized_1024(case):
    """서비스 기본값(긴 변 1024 리사이즈)에서도 같은 개수. 기준 bbox는 리사이즈 배율만큼 축소해 비교."""
    det = _detect(_load(case), resize_long_side=1024)
    assert det.scale < 1.0
    _check(case, det)


@pytest.mark.parametrize("width", [576, 720])
@pytest.mark.parametrize("case", SPEC["frames"], ids=lambda c: c["file"])
def test_phone_frame_low_res_source(case, width):
    """폰이 처음부터 저해상도로 보내는 경우(getUserMedia 기본 640x480 등). 줄인 이미지를 리사이즈 없이 원본으로 넣는다.
    ROI close 커널이 고정 px이면 0297에서 경계에 걸친 쿠키가 포함돼 7개가 나온다."""
    img = _load(case, width)
    det = _detect(img, resize_long_side=0)
    assert det.scale == 1.0
    _check(case, det, ref_scale=width / 1080)
