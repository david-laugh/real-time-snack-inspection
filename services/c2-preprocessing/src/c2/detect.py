"""
과자 후보 bbox 검출 (HSV 색상 임계값 + contour)

reference/c2_bbox_detect.py에서 그대로 옮긴 모듈. 임계값과 판정 로직은 실제 시뮬레이터 프레임(15/15, IoU 0.995)과
폰 촬영 영상으로 검증됐으므로 바꾸지 않는다. 서버에서 세션별 지름 평활화를 하기 위해 detect_candidates를
build_mask / estimate_diameter / filter_candidates로 나눴을 뿐, detect_candidates의 결과는 원본과 동일하다
(tests/test_detect.py에서 원본과 비교).

출력 bbox 형식은 processed-frame 스키마와 같은 [x1, y1, x2, y2] (입력 이미지 기준 pixel)
"""
import cv2
import numpy as np

# ── 검출 파라미터 (OpenCV HSV: H 0~180, S/V 0~255) ─────────────────────
# 쿠키 몸통·갈색 테두리는 H 13~17 부근, 회색 벨트는 S 약 36이라 S 하한으로 걸러진다.
HSV_LOW = (5, 80, 50)
HSV_HIGH = (30, 255, 255)
MIN_AREA_RATIO = 0.2      # 정상 쿠키 면적 대비 최소 면적 (라인 번호 글자 등 작은 주황 잡음 제거)
MAX_AREA_RATIO = 2.0      # 이보다 크면 쿠키가 아님 (페이지 테두리 등)
MAX_ASPECT = 2.5          # 가로/세로 비가 이보다 길쭉하면 제외 (화면 위아래 노란 띠 등)
EDGE_MARGIN = 2           # 화면 가장자리에 걸친 쿠키 판정 여유(px)


def find_stage_roi(bgr):
    """컨베이어 화면(어두운 무채색 영역)의 바깥 사각형 [x1, y1, x2, y2]. 폰 촬영 시 화면 밖 쿠키 잘림 판정용."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    dark = cv2.inRange(hsv, (0, 0, 0), (180, 70, 190))   # 폰 촬영 시 벨트가 V 95~150까지 밝아짐, 흰 페이지는 V 255
    dark = cv2.morphologyEx(dark, cv2.MORPH_CLOSE, np.ones((25, 25), np.uint8))
    cs, _ = cv2.findContours(dark, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cs:
        return None
    x, y, w, h = cv2.boundingRect(max(cs, key=cv2.contourArea))
    return [x, y, x + w, y + h]


def build_mask(bgr, hsv_low=HSV_LOW, hsv_high=HSV_HIGH):
    """쿠키 색 영역 이진 마스크."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, tuple(hsv_low), tuple(hsv_high))
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)    # 얇은 선·점 잡음 제거
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)   # 초코칩 구멍 메우기
    return mask


def estimate_diameter(mask, min_frac=0.0005):
    """주황 덩어리들의 면적 중앙값으로 쿠키 지름(px)을 추정. 촬영 거리·방향과 무관하게 동작.
    정상 쿠키가 다수라는 가정(시뮬레이터 기본 불량률 30%)에 기댄다. 후보가 없으면 None."""
    h, w = mask.shape
    cs, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    areas = []
    for c in cs:
        a = cv2.contourArea(c)
        if a < min_frac * h * w:
            continue
        x, y, bw, bh = cv2.boundingRect(c)
        if max(bw, bh) / max(1, min(bw, bh)) > MAX_ASPECT:
            continue
        areas.append(a)
    if not areas:
        return None
    return float(np.sqrt(4 * np.median(areas) / np.pi))


def filter_candidates(bgr, mask, expected_diameter_px, drop_edge=True, roi=None):
    """마스크의 외곽 contour 중 쿠키 크기·모양에 맞는 것만 bbox [x1, y1, x2, y2]로 반환.
    roi: 컨베이어 화면 영역 [x1, y1, x2, y2]. 'auto'면 자동 검출. 이 경계에 닿은 쿠키는 잘린 것으로 보고 제외."""
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    min_area = MIN_AREA_RATIO * np.pi * (expected_diameter_px / 2) ** 2
    h, w = mask.shape
    if isinstance(roi, str) and roi == "auto":
        roi = find_stage_roi(bgr)
    rx1, ry1, rx2, ry2 = roi if roi else (0, 0, w, h)
    m = EDGE_MARGIN if not roi else max(EDGE_MARGIN, int(expected_diameter_px * 0.06))
    boxes = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < min_area or area > MAX_AREA_RATIO * np.pi * (expected_diameter_px / 2) ** 2:
            continue
        x, y, bw, bh = cv2.boundingRect(c)
        if max(bw, bh) / max(1, min(bw, bh)) > MAX_ASPECT:
            continue
        on_edge = (x <= rx1 + m or y <= ry1 + m or
                   x + bw >= rx2 - m or y + bh >= ry2 - m)
        if drop_edge and on_edge:
            continue   # 반쯤 걸친 쿠키는 윤곽이 잘려 불량으로 오판되므로 제외
        boxes.append([float(x), float(y), float(x + bw), float(y + bh)])
    return boxes


def detect_candidates(bgr, expected_diameter_px=None, drop_edge=True, roi=None,
                      hsv_low=HSV_LOW, hsv_high=HSV_HIGH):
    """BGR 이미지에서 과자 후보 bbox 목록을 [x1, y1, x2, y2]로 반환.
    expected_diameter_px가 None이면 프레임에서 자동 추정한다."""
    mask = build_mask(bgr, hsv_low, hsv_high)
    if expected_diameter_px is None:          # None이면 프레임에서 자동 추정
        expected_diameter_px = estimate_diameter(mask)
        if expected_diameter_px is None:
            return [], mask
    return filter_candidates(bgr, mask, expected_diameter_px, drop_edge, roi), mask


def to_prompts(boxes):
    """processed-frame 스키마의 prompts[] 형태로 변환."""
    return [{"candidate_id": f"c{i}", "type": "box", "bbox": b} for i, b in enumerate(boxes)]
