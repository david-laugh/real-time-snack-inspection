"""Send synthetic ProcessedFrame requests to a running C3 (or the Nginx LB).

    python scripts/send_frames.py --url http://localhost:8080/infer -n 30 --boxes 4

Prints per-frame status / replica / C3 stage timings and a median summary.
Uses only the standard library + numpy/opencv (for JPEG encoding).
"""

import argparse
import base64
import json
import statistics
import time
import urllib.error
import urllib.request
import uuid

import cv2
import numpy as np

STAGES = ["Decode", "Encoder", "Segmentation", "Defect-Analysis", "Total-Inference"]


def make_frame(frame_id: int, size: int, n_boxes: int, session_id: str) -> dict:
    img = np.full((size, size, 3), 70, np.uint8)
    prompts = []
    r = size // 10
    for i in range(n_boxes):
        cx = int(size * (0.15 + 0.23 * (i % 4)))
        cy = int(size * (0.25 + 0.4 * ((i // 4) % 2)))
        cv2.circle(img, (cx, cy), r, (110, 170, 210), -1)  # BGR snack colour
        if i % 3 == 2:
            cv2.circle(img, (cx, cy), r // 3, (20, 30, 45), -1)  # dark spot
        prompts.append({"candidate_id": f"c{i}", "type": "box", "bbox": [cx - r - 8, cy - r - 8, cx + r + 8, cy + r + 8]})
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    now = int(time.time() * 1000)
    return {
        "schema_version": "1.0.0",
        "trace_id": str(uuid.uuid4()),
        "session_id": session_id,
        "frame_id": frame_id,
        "captured_at_ms": now,
        "preprocess_started_at_ms": now,
        "preprocess_finished_at_ms": now,
        "width": size,
        "height": size,
        "prompts": prompts,
        "image_b64": base64.b64encode(buf.tobytes()).decode(),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8002/infer")
    ap.add_argument("-n", type=int, default=20)
    ap.add_argument("--boxes", type=int, default=4)
    ap.add_argument("--size", type=int, default=1024)
    args = ap.parse_args()

    session = f"sim-{uuid.uuid4().hex[:8]}"
    samples = {s: [] for s in STAGES}
    rtts = []
    for i in range(args.n):
        frame = make_frame(i, args.size, args.boxes, session)
        req = urllib.request.Request(
            args.url,
            data=json.dumps(frame).encode(),
            headers={"Content-Type": "application/json", "X-Trace-Id": frame["trace_id"],
                     "X-Frame-Id": str(i), "X-Session-Id": session},
        )
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body, headers, status = json.loads(resp.read()), resp.headers, resp.status
        except urllib.error.HTTPError as e:
            print(f"frame {i}: HTTP {e.code} {e.read()[:300]!r}")
            continue
        rtt = (time.perf_counter() - t0) * 1000
        rtts.append(rtt)
        for s in STAGES:
            samples[s].append(float(headers[f"X-C3-{s}-Ms"]))
        verdicts = [(d["object_id"], d["status"], d["defect_type"], d["defect_score"]) for d in body["detections"]]
        print(f"frame {i}: {status} {body['inference_container_id']} rtt={rtt:.1f}ms "
              f"total={headers['X-C3-Total-Inference-Ms']}ms {verdicts if i == 0 else ''}")
    if rtts:
        print("\nmedian ms:", {s: round(statistics.median(v), 2) for s, v in samples.items()},
              "rtt:", round(statistics.median(rtts), 2))


if __name__ == "__main__":
    main()
