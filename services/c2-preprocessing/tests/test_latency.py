"""프레임당 처리 시간 측정. 30fps 예산(33 ms) 안에 들어오는지 확인하고 결과를 출력한다 (pytest -s로 보기)."""
import time

import numpy as np
import pytest

from c2.config import Settings
from c2.pipeline import DiameterTracker, build_processed_frame, detect_frame
from helpers import load_fixture, make_envelope, scale_case

BUDGET_MS = 33.0
N = 60


def _stats(times):
    t = np.array(times)
    return t.mean(), np.percentile(t, 95), t.max()


@pytest.mark.parametrize("long_side", [960, 1024])
def test_frame_latency(long_side):
    img, gt = load_fixture()
    # 실제 입력처럼 긴 변보다 큰 원본(1.8배, 1881x810)에서 리사이즈부터 시작
    src, _ = scale_case(img, gt, 1.8)
    settings = Settings(resize_long_side=long_side)
    tracker = DiameterTracker()
    envelope = make_envelope(src, fmt="jpeg")

    for _ in range(5):                                    # warm-up
        build_processed_frame(envelope, settings, tracker)

    detect_t, total_t = [], []
    for _ in range(N):
        t0 = time.perf_counter()
        detect_frame(src, settings, "s", tracker)          # resize + 검출
        detect_t.append((time.perf_counter() - t0) * 1000)
        t0 = time.perf_counter()
        build_processed_frame(envelope, settings, tracker)  # decode + resize + 검출 + encode
        total_t.append((time.perf_counter() - t0) * 1000)

    d, t = _stats(detect_t), _stats(total_t)
    print(f"\n[latency] long_side={long_side} src={src.shape[1]}x{src.shape[0]} n={N}"
          f"\n  resize+detect             mean {d[0]:.2f} / p95 {d[1]:.2f} / max {d[2]:.2f} ms"
          f"\n  decode+detect+encode(all) mean {t[0]:.2f} / p95 {t[1]:.2f} / max {t[2]:.2f} ms")
    assert d[0] < BUDGET_MS
