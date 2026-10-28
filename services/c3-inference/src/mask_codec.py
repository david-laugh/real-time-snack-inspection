"""Mask encoding for DetectionResult.mask (encoding = "rle").

Format (documented in docs/contract-notes.md):
    {
      "encoding": "rle",
      "width": W, "height": H,
      "data": {"order": "row-major", "counts": [n0, n1, n2, ...]}
    }
counts are alternating run lengths over the row-major flattened mask, starting
with a run of 0s (which may be 0). sum(counts) == W * H.
"""

from __future__ import annotations

from typing import Any, Dict, List

import numpy as np


def rle_encode(mask: np.ndarray) -> Dict[str, Any]:
    h, w = mask.shape
    flat = mask.reshape(-1).astype(np.int8)
    # Indices where the value changes, plus both ends.
    change = np.flatnonzero(np.diff(flat)) + 1
    bounds = np.concatenate(([0], change, [flat.size]))
    counts: List[int] = np.diff(bounds).tolist()
    if flat.size and flat[0] == 1:
        counts.insert(0, 0)  # always start with a (possibly empty) run of zeros
    return {"encoding": "rle", "width": int(w), "height": int(h), "data": {"order": "row-major", "counts": counts}}


def rle_decode(encoded: Dict[str, Any]) -> np.ndarray:
    w, h = encoded["width"], encoded["height"]
    counts = encoded["data"]["counts"]
    values = np.arange(len(counts)) % 2
    flat = np.repeat(values, counts).astype(bool)
    return flat.reshape(h, w)
