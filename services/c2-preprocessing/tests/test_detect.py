import sys
from pathlib import Path

import pytest

from c2 import detect
from c2.config import Settings
from c2.pipeline import DiameterTracker, detect_frame
from helpers import evaluate, load_fixture, rot90_case, scale_case

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "reference"))
import c2_bbox_detect as reference  # noqa: E402


def _case(name):
    img, gt = load_fixture()
    if name == "original":
        return img, gt
    if name == "rot90":
        return rot90_case(img, gt)
    return scale_case(img, gt, float(name.removeprefix("scale")))


CASES = ["original", "scale0.5", "scale1.8", "rot90"]


@pytest.mark.parametrize("name", CASES)
@pytest.mark.parametrize("roi", [None, "auto"])
@pytest.mark.parametrize("diameter", ["auto", "fixed"])
def test_matches_reference(name, roi, diameter):
    """모듈로 옮긴 검출 함수가 reference와 같은 bbox를 낸다."""
    img, _ = _case(name)
    s = float(name.removeprefix("scale")) if name.startswith("scale") else 1.0
    d = None if diameter == "auto" else 78 * s
    ours, _ = detect.detect_candidates(img, expected_diameter_px=d, roi=roi)
    ref, _ = reference.detect_candidates(img, expected_diameter_px=d, roi=roi)
    assert ours == ref


def test_estimate_diameter_matches_reference():
    img, _ = load_fixture()
    mask = detect.build_mask(img)
    assert detect.estimate_diameter(mask) == reference.estimate_diameter(mask)


@pytest.mark.parametrize("roi", [None, "auto"])
def test_fixture_fixed_diameter(roi):
    """reference가 검증한 설정 그대로: 원본 해상도, 지름 78."""
    img, gt = load_fixture()
    boxes, _ = detect.detect_candidates(img, expected_diameter_px=78, roi=roi)
    res = evaluate(boxes, gt)
    assert res["recall"] == 1.0 and res["mean_iou"] >= 0.95, res


@pytest.mark.parametrize("name", CASES)
@pytest.mark.parametrize("roi", [None, "auto"])
def test_auto_diameter_invariance(name, roi):
    """촬영 거리(배율)·방향이 달라도 지름 자동 추정으로 검출된다. 라벨도 같은 변환을 적용."""
    img, gt = _case(name)
    boxes, _ = detect.detect_candidates(img, roi=roi)
    res = evaluate(boxes, gt)
    print(f"{name:9s} roi={roi!s:5s} {img.shape[1]}x{img.shape[0]} -> {res}")
    assert res["recall"] == 1.0 and res["mean_iou"] >= 0.95, res


@pytest.mark.parametrize("name", CASES)
@pytest.mark.parametrize("roi_mode", ["auto", "off"])
def test_service_pipeline(name, roi_mode):
    """서비스 기본 설정(긴 변 1024 리사이즈 + 자동 지름)에서도 정확도 유지. 라벨은 리사이즈 배율만큼 축소."""
    img, gt = _case(name)
    settings = Settings(roi_mode=roi_mode)
    resized, det = detect_frame(img, settings, "s", DiameterTracker())
    gt_resized = [[v * det.scale for v in b] for b in gt]
    res = evaluate(det.boxes, gt_resized)
    assert max(resized.shape[:2]) <= settings.resize_long_side
    assert res["recall"] == 1.0 and res["mean_iou"] >= 0.95, res


def test_fixed_diameter_is_scaled_to_resized_image():
    img, gt = load_fixture()
    settings = Settings(expected_diameter_px=78, resize_long_side=522)
    _, det = detect_frame(img, settings, "s", None)
    assert det.diameter_px == pytest.approx(78 * det.scale)
    res = evaluate(det.boxes, [[v * det.scale for v in b] for b in gt])
    assert res["recall"] == 1.0 and res["mean_iou"] >= 0.95, res


def test_fixed_roi_is_scaled():
    img, _ = load_fixture()
    settings = Settings(roi_mode="fixed", roi="0,0,1045,450", resize_long_side=522)
    _, det = detect_frame(img, settings, "s", None)
    assert det.roi == [0, 0, 522, 225]


def test_diameter_tracker_ema():
    t = DiameterTracker(alpha=0.2, max_sessions=2)
    assert t.update("a", None) is None
    assert t.update("a", 100.0) == 100.0
    assert t.update("a", 50.0) == pytest.approx(90.0)
    assert t.update("a", None) == pytest.approx(90.0)     # 추정 실패 -> 이전 값
    t.update("b", 10.0)
    t.update("c", 20.0)                                   # 가장 오래된 세션 a 제거
    assert t.update("a", None) is None


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("C2_HSV_LOW", "1,2,3")
    monkeypatch.setenv("C2_ROI", "")
    monkeypatch.setenv("C2_EXPECTED_DIAMETER_PX", "")
    monkeypatch.setenv("C2_ROI_MODE", "OFF")
    s = Settings()
    assert s.hsv_low == (1, 2, 3) and s.roi is None and s.expected_diameter_px is None
    assert s.roi_mode == "off" and s.resize_long_side == 1024
    monkeypatch.setenv("C2_ROI", "10,20,30,40")
    monkeypatch.setenv("C2_EXPECTED_DIAMETER_PX", "170")
    s = Settings()
    assert s.roi == (10, 20, 30, 40) and s.expected_diameter_px == 170.0
