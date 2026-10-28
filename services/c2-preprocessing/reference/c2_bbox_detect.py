"""
C2 Preprocessing - 과자 후보 bbox 검출 (HSV 색상 임계값 + contour)

사용법
  # 1) 실제 시뮬레이터 프레임으로 검증 (Space로 멈춘 뒤 S, L로 저장한 한 쌍)
  python c2_bbox_detect.py --image frame.png --labels labels.json --save-vis vis.png

  # 2) PNG가 아직 없을 때: 라벨 JSON으로 비슷한 프레임을 그려서 코드 동작만 확인
  python c2_bbox_detect.py --labels labels.json --synthetic --save-vis vis.png

출력 bbox 형식은 processed-frame 스키마와 같은 [x1, y1, x2, y2] (전처리 후 이미지 기준 pixel)
"""
import argparse
import json
import time

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


def detect_candidates(bgr, expected_diameter_px=None, drop_edge=True, roi=None):
    """BGR 이미지에서 과자 후보 bbox 목록을 [x1, y1, x2, y2]로 반환.
    roi: 컨베이어 화면 영역 [x1, y1, x2, y2]. 'auto'면 자동 검출. 이 경계에 닿은 쿠키는 잘린 것으로 보고 제외."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, HSV_LOW, HSV_HIGH)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)    # 얇은 선·점 잡음 제거
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)   # 초코칩 구멍 메우기

    if expected_diameter_px is None:          # None이면 프레임에서 자동 추정
        expected_diameter_px = estimate_diameter(mask)
        if expected_diameter_px is None:
            return [], mask
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
    return boxes, mask


def to_prompts(boxes):
    """processed-frame 스키마의 prompts[] 형태로 변환."""
    return [{"candidate_id": f"c{i}", "type": "box", "bbox": b} for i, b in enumerate(boxes)]


# ── 평가 ───────────────────────────────────────────────────────────────
def label_boxes(labels):
    """라벨 JSON의 [x, y, w, h] → [x1, y1, x2, y2]"""
    out = []
    for a in labels["annotations"]:
        x, y, w, h = a["bbox"]
        out.append(([x, y, x + w, y + h], a))
    return out


def iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def evaluate(pred, gt, thr=0.5):
    used, matched = set(), []
    for g, ann in gt:
        best, bi = 0.0, -1
        for i, p in enumerate(pred):
            if i in used:
                continue
            v = iou(p, g)
            if v > best:
                best, bi = v, i
        if best >= thr:
            used.add(bi)
            matched.append((ann, best))
        else:
            matched.append((ann, None))
    tp = sum(1 for _, v in matched if v is not None)
    return {
        "gt": len(gt), "pred": len(pred), "tp": tp,
        "recall": tp / len(gt) if gt else 1.0,
        "precision": tp / len(pred) if pred else 1.0,
        "mean_iou": float(np.mean([v for _, v in matched if v is not None])) if tp else 0.0,
        "missed": [(a["id"], a["defect_type"] or "round") for a, v in matched if v is None],
    }


# ── PNG가 없을 때 쓰는 근사 렌더러 (테스트 전용) ─────────────────────────
def render_synthetic(labels, seed=0):
    rng = np.random.default_rng(seed)
    W, H = labels["image"]["width"], labels["image"]["height"]
    p = labels["params"]
    img = np.full((H, W, 3), (0x1b, 0x18, 0x14), np.uint8)           # 배경 #14181b (BGR)
    for i in range(int(p["lines"])):
        cy = i * p["lineGap"] + p["lineGap"] / 2
        bh = max(26, p["lineGap"] - 16)
        top = int(cy - bh / 2)
        img[top:top + int(bh)] = (0x31, 0x2e, 0x2a)                    # 벨트 #2a2e31
        img[max(0, top - 3):top] = (0x54, 0x50, 0x4a)                  # 레일
        cv2.putText(img, f"LINE {i+1:02d}", (8, top + 14), cv2.FONT_HERSHEY_SIMPLEX,
                    0.35, (40, 112, 137), 1, cv2.LINE_AA)               # 반투명 주황 글자
    for a in labels["annotations"]:
        pts = np.array(a["segmentation"][0], np.float32).reshape(-1, 2)
        r = a["diameter_px"] / 2
        if p.get("shadow"):
            sh = img.copy()
            cv2.fillPoly(sh, [(pts + (r * 0.10, r * 0.16)).astype(np.int32)], (0, 0, 0))
            img = cv2.addWeighted(sh, 0.42, img, 0.58, 0)
        poly = pts.astype(np.int32)
        m = np.zeros((H, W), np.uint8)
        cv2.fillPoly(m, [poly], 255)
        img[m > 0] = (44, 93, 150)                                     # 갈색 테두리
        inner = cv2.erode(m, np.ones((5, 5), np.uint8))
        img[inner > 0] = (75, 138, 201)                                # 쿠키 몸통
        x, y, w, h = a["bbox"]
        for _ in range(rng.integers(3, 9)):                            # 초코칩
            cx, cy2 = rng.uniform(x + w * 0.25, x + w * 0.75), rng.uniform(y + h * 0.25, y + h * 0.75)
            if inner[int(cy2), int(cx)]:
                cv2.circle(img, (int(cx), int(cy2)), int(r * 0.08), (17, 36, 64), -1)
    return img


def run_video(args):
    cap = cv2.VideoCapture(args.video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    writer, times, counts = None, [], []
    rows = ["frame,count,ms"]
    i = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        t0 = time.perf_counter()
        boxes, _ = detect_candidates(frame, expected_diameter_px=args.diameter, roi="auto")
        ms = (time.perf_counter() - t0) * 1000
        times.append(ms); counts.append(len(boxes)); rows.append(f"{i},{len(boxes)},{ms:.2f}")
        vis = frame.copy()
        for b in boxes:
            cv2.rectangle(vis, (int(b[0]), int(b[1])), (int(b[2]), int(b[3])), (60, 60, 255), 4)
        cv2.putText(vis, f"frame {i}  cookies {len(boxes)}  {ms:.1f} ms", (20, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 0, 255), 3)
        vis = cv2.resize(vis, (vis.shape[1] // 2, vis.shape[0] // 2))
        if writer is None:
            writer = cv2.VideoWriter(args.out, cv2.VideoWriter_fourcc(*"mp4v"), fps, (vis.shape[1], vis.shape[0]))
        writer.write(vis)
        i += 1
    if writer:
        writer.release()
    csv = args.out.rsplit(".", 1)[0] + ".csv"
    open(csv, "w").write("\n".join(rows))
    t = np.array(times)
    print(f"프레임 {i}/{n} | 검출 시간 평균 {t.mean():.2f} ms, P95 {np.percentile(t, 95):.2f} ms, 최대 {t.max():.2f} ms")
    vals, cnt = np.unique(counts, return_counts=True)
    print("프레임별 검출 개수 분포:", dict(zip(vals.tolist(), cnt.tolist())))
    print("결과 영상:", args.out, "| CSV:", csv)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image")
    ap.add_argument("--labels")
    ap.add_argument("--video", help="폰 촬영 영상: 모든 프레임에 검출을 돌려 결과 영상과 CSV 저장")
    ap.add_argument("--diameter", type=float, default=None, help="화면 속 쿠키 지름(px). 생략하면 프레임마다 자동 추정")
    ap.add_argument("--out", default="detect_result.mp4")
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--save-vis")
    args = ap.parse_args()

    if args.video:
        return run_video(args)
    labels = json.load(open(args.labels, encoding="utf-8"))
    if args.synthetic:
        img = render_synthetic(labels)
    else:
        img = cv2.imread(args.image)
        if img is None:
            raise SystemExit(f"이미지를 읽을 수 없음: {args.image}")
        lw, lh = labels["image"]["width"], labels["image"]["height"]
        if (img.shape[1], img.shape[0]) != (lw, lh):
            print(f"경고: 이미지 크기 {img.shape[1]}x{img.shape[0]} ≠ 라벨 기준 {lw}x{lh}")

    diameter = labels["params"]["size"] * labels["image"].get("dpr", 1)
    t0 = time.perf_counter()
    boxes, _ = detect_candidates(img, expected_diameter_px=diameter)
    ms = (time.perf_counter() - t0) * 1000

    gt = [(b, a) for b, a in label_boxes(labels)]
    res = evaluate(boxes, gt)
    print(f"검출 시간 {ms:.2f} ms | 정답 {res['gt']}개, 검출 {res['pred']}개, 일치 {res['tp']}개")
    print(f"recall {res['recall']:.2f}  precision {res['precision']:.2f}  mean IoU {res['mean_iou']:.3f}")
    if res["missed"]:
        print("놓친 쿠키 (id, 유형):", res["missed"])
    print("prompts 예시:", json.dumps(to_prompts(boxes)[:2], ensure_ascii=False))

    if args.save_vis:
        vis = img.copy()
        for b, _ in gt:
            cv2.rectangle(vis, (int(b[0]), int(b[1])), (int(b[2]), int(b[3])), (80, 200, 80), 1)
        for b in boxes:
            cv2.rectangle(vis, (int(b[0]), int(b[1])), (int(b[2]), int(b[3])), (60, 60, 255), 1)
        cv2.imwrite(args.save_vis, vis)
        print("시각화 저장:", args.save_vis, "(초록 = 정답, 빨강 = 검출)")


if __name__ == "__main__":
    main()
