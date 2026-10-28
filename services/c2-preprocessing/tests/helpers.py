"""테스트 전용 평가 도구 (reference/c2_bbox_detect.py의 평가 함수와 같은 방식). 서비스 코드에는 넣지 않는다."""
import base64
import json
from pathlib import Path

import cv2
import numpy as np

FIXTURES = Path(__file__).parent / "fixtures"
FRAME_PNG = FIXTURES / "frame_2026-10-28T01-48-28.png"
LABELS_JSON = FIXTURES / "labels_2026-10-28T01-48-30.json"


def load_fixture():
    """(BGR 이미지, 정답 bbox [x1, y1, x2, y2] 목록)"""
    img = cv2.imread(str(FRAME_PNG))
    labels = json.loads(LABELS_JSON.read_text(encoding="utf-8"))
    gt = []
    for a in labels["annotations"]:
        x, y, w, h = a["bbox"]          # 라벨은 [x, y, w, h]
        gt.append([x, y, x + w, y + h])
    return img, gt


def scale_case(img, gt, s):
    interp = cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR
    out = cv2.resize(img, (round(img.shape[1] * s), round(img.shape[0] * s)), interpolation=interp)
    return out, [[v * s for v in b] for b in gt]


def rot90_case(img, gt):
    """시계 방향 90도 회전: 열 [x1, x2) / 행 [y1, y2) -> 열 [H - y2, H - y1) / 행 [x1, x2)."""
    h = img.shape[0]
    return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE), [[h - b[3], b[0], h - b[1], b[2]] for b in gt]


def iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def evaluate(pred, gt, thr=0.5):
    used, ious = set(), []
    for g in gt:
        best, bi = 0.0, -1
        for i, p in enumerate(pred):
            if i in used:
                continue
            v = iou(p, g)
            if v > best:
                best, bi = v, i
        if best >= thr:
            used.add(bi)
            ious.append(best)
    tp = len(ious)
    return {
        "gt": len(gt), "pred": len(pred), "tp": tp,
        "recall": tp / len(gt) if gt else 1.0,
        "precision": tp / len(pred) if pred else 1.0,
        "mean_iou": float(np.mean(ious)) if ious else 0.0,
    }


def png_b64(img):
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return base64.b64encode(buf.tobytes()).decode("ascii")


def make_envelope(img, frame_id=7, fmt="png", session_id="sess-test"):
    if fmt == "png":
        data = png_b64(img)
    else:
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])
        data = base64.b64encode(buf.tobytes()).decode("ascii")
    return {
        "schema_version": "1.0.0",
        "trace_id": f"trace-{frame_id:06d}",
        "session_id": session_id,
        "frame_id": frame_id,
        "captured_at_ms": 1761616110987,
        "source_width": img.shape[1],
        "source_height": img.shape[0],
        "format": fmt,
        "gateway_received_at_ms": 1761616111001,
        "image_b64": data,
    }
