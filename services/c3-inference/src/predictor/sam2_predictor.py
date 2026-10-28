"""Real SAM2 backend: frame-independent SAM2ImagePredictor (plan A).

- Model is built once in load() (startup), then warmed up.
- set_image() runs the Hiera image encoder ONCE per frame.
- predict_boxes() runs the prompt encoder + mask decoder for all boxes of the
  frame in batches of `box_batch_size`, reusing the cached embedding.
- Everything runs under torch.inference_mode(); optional autocast
  (bfloat16 / float16) on CUDA.

No video predictor / inference_state / per-session memory is kept, so any
replica can serve any frame (compatible with Nginx round-robin).
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from typing import Any, Dict, Optional

import numpy as np

from .base import LoadInfo, SegmentationOutput, SegmentationPredictor

log = logging.getLogger(__name__)


class Sam2ImagePredictorBackend(SegmentationPredictor):
    name = "sam2"

    def __init__(
        self,
        model_cfg: str,
        checkpoint_path: str,
        device: str = "auto",
        amp_dtype: str = "auto",
        warmup_iters: int = 2,
        warmup_image_size: int = 1024,
        box_batch_size: int = 16,
    ) -> None:
        self.model_cfg = model_cfg
        self.checkpoint_path = checkpoint_path
        self.requested_device = device
        self.requested_amp = amp_dtype
        self.warmup_iters = warmup_iters
        self.warmup_image_size = warmup_image_size
        self.box_batch_size = max(1, box_batch_size)

        self._torch = None
        self._predictor = None
        self._device = None
        self._amp_dtype = None  # torch dtype or None
        self._ready = False

    # ------------------------------------------------------------------ load
    def _resolve_device(self, torch):
        req = self.requested_device
        if req == "auto":
            return torch.device("cuda" if torch.cuda.is_available() else "cpu")
        if req.startswith("cuda"):
            if not torch.cuda.is_available():
                raise RuntimeError(
                    f"C3_DEVICE={req} but CUDA is not available "
                    "(no GPU visible to the container? check NVIDIA Container Toolkit / compose GPU override)"
                )
            return torch.device(req)
        if req == "cpu":
            return torch.device("cpu")
        raise ValueError(f"C3_DEVICE must be auto|cpu|cuda|cuda:N, got {req!r}")

    def _resolve_amp(self, torch):
        if self._device.type != "cuda":
            return None  # autocast is only applied on CUDA
        req = self.requested_amp
        if req == "none":
            return None
        if req == "auto":
            return torch.bfloat16 if torch.cuda.is_bf16_supported() else None
        if req == "bfloat16":
            if not torch.cuda.is_bf16_supported():
                log.warning("bfloat16 not supported on this GPU; falling back to float16 autocast")
                return torch.float16
            return torch.bfloat16
        return torch.float16

    def load(self) -> LoadInfo:
        t0 = time.perf_counter()
        import torch
        from sam2.build_sam import build_sam2
        from sam2.sam2_image_predictor import SAM2ImagePredictor

        self._torch = torch
        if not os.path.isfile(self.checkpoint_path):
            raise FileNotFoundError(
                f"SAM2 checkpoint not found: {self.checkpoint_path} "
                "(place it in services/c3-inference/models/, see README)"
            )
        self._device = self._resolve_device(torch)
        self._amp_dtype = self._resolve_amp(torch)

        if self._device.type == "cuda":
            major, _ = torch.cuda.get_device_capability(self._device)
            if major >= 8:  # Ampere+: TF32 matmul/conv for the fp32 parts
                torch.backends.cuda.matmul.allow_tf32 = True
                torch.backends.cudnn.allow_tf32 = True

        model = build_sam2(self.model_cfg, self.checkpoint_path, device=str(self._device))
        self._predictor = SAM2ImagePredictor(model)
        self._sync()
        model_load_ms = (time.perf_counter() - t0) * 1000.0
        log.info(
            "SAM2 loaded cfg=%s ckpt=%s device=%s amp=%s in %.1f ms",
            self.model_cfg, self.checkpoint_path, self._device, self._amp_dtype, model_load_ms,
        )

        warmup_ms = self._warmup()
        self._ready = True
        return LoadInfo(model_load_ms=model_load_ms, warmup_ms=warmup_ms, details=self.describe())

    def _warmup(self) -> float:
        if self.warmup_iters <= 0:
            return 0.0
        s = self.warmup_image_size
        rng = np.random.default_rng(0)
        image = rng.integers(0, 255, size=(s, s, 3), dtype=np.uint8)
        n = min(4, self.box_batch_size)
        boxes = np.array(
            [[s * 0.1 + i * 10, s * 0.1, s * 0.5 + i * 10, s * 0.5] for i in range(n)], dtype=np.float32
        )
        t0 = time.perf_counter()
        for i in range(self.warmup_iters):
            t_iter = time.perf_counter()
            self.set_image(image)
            self.predict_boxes(boxes)
            log.info("SAM2 warm-up %d/%d: %.1f ms", i + 1, self.warmup_iters, (time.perf_counter() - t_iter) * 1000)
        return (time.perf_counter() - t0) * 1000.0

    # ------------------------------------------------------------- inference
    @property
    def is_ready(self) -> bool:
        return self._ready

    def _sync(self) -> None:
        if self._device is not None and self._device.type == "cuda":
            self._torch.cuda.synchronize(self._device)

    def _ctx(self):
        torch = self._torch
        stack = contextlib.ExitStack()
        stack.enter_context(torch.inference_mode())
        if self._amp_dtype is not None:
            stack.enter_context(torch.autocast(device_type="cuda", dtype=self._amp_dtype))
        return stack

    def set_image(self, image_rgb: np.ndarray) -> None:
        with self._ctx():
            self._predictor.set_image(image_rgb)
        self._sync()  # encoder_ms must include the GPU work, not just the launch

    def predict_boxes(self, boxes_xyxy: np.ndarray) -> SegmentationOutput:
        p = self._predictor
        torch = self._torch
        n = len(boxes_xyxy)
        h, w = p._orig_hw[-1]
        masks_out = np.zeros((n, h, w), dtype=bool)
        scores_out = np.zeros((n,), dtype=np.float32)
        with self._ctx():
            for start in range(0, n, self.box_batch_size):
                chunk = boxes_xyxy[start:start + self.box_batch_size]
                # Same steps as SAM2ImagePredictor.predict(), but masks are thresholded
                # on the GPU and copied back as bool (1 byte/px instead of float32).
                _, unnorm_coords, labels, unnorm_box = p._prep_prompts(
                    None, None, chunk, None, normalize_coords=True
                )
                masks, iou_pred, _ = p._predict(
                    unnorm_coords, labels, unnorm_box, None, multimask_output=False, return_logits=False
                )
                # masks: (B, 1, H, W) bool, iou_pred: (B, 1)
                masks_out[start:start + len(chunk)] = masks[:, 0].to(torch.bool).cpu().numpy()
                scores_out[start:start + len(chunk)] = iou_pred[:, 0].float().cpu().numpy()
        return SegmentationOutput(masks=masks_out, mask_scores=scores_out)

    # ------------------------------------------------------------- reporting
    def describe(self) -> Dict[str, Any]:
        info: Dict[str, Any] = {
            "predictor": self.name,
            "model_cfg": self.model_cfg,
            "checkpoint_path": self.checkpoint_path,
            "requested_device": self.requested_device,
            "device": str(self._device) if self._device is not None else None,
            "amp_dtype": str(self._amp_dtype).replace("torch.", "") if self._amp_dtype is not None else "none",
            "box_batch_size": self.box_batch_size,
        }
        if self._device is not None and self._device.type == "cuda":
            info["gpu_name"] = self._torch.cuda.get_device_name(self._device)
        return info

    def gpu_memory(self) -> Optional[Dict[str, Any]]:
        if self._device is None or self._device.type != "cuda":
            return None
        torch = self._torch
        mib = 1024 * 1024
        out: Dict[str, Any] = {
            # This process (this replica) only.
            "torch_allocated_mib": round(torch.cuda.memory_allocated(self._device) / mib, 1),
            "torch_reserved_mib": round(torch.cuda.memory_reserved(self._device) / mib, 1),
            "torch_max_allocated_mib": round(torch.cuda.max_memory_allocated(self._device) / mib, 1),
        }
        out.update(_nvml_device_memory(self._device.index or 0))
        return out


def _nvml_device_memory(index: int) -> Dict[str, Any]:
    """Whole-GPU usage (all replicas sharing the device). Optional dependency."""
    try:
        import pynvml

        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(index)
        mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
        util = pynvml.nvmlDeviceGetUtilizationRates(handle)
        mib = 1024 * 1024
        return {
            "device_used_mib": round(mem.used / mib, 1),
            "device_total_mib": round(mem.total / mib, 1),
            "device_utilization_pct": util.gpu,
        }
    except Exception:  # pynvml missing / no driver access: torch stats are enough
        return {}
