from __future__ import annotations

from ..config import Settings
from .base import LoadInfo, SegmentationOutput, SegmentationPredictor
from .mock_predictor import MockPredictor

__all__ = ["LoadInfo", "SegmentationOutput", "SegmentationPredictor", "MockPredictor", "build_predictor"]


def build_predictor(settings: Settings) -> SegmentationPredictor:
    """Select the backend from C3_PREDICTOR. torch/sam2 are imported lazily."""
    if settings.predictor == "mock":
        return MockPredictor(
            encoder_delay_ms=settings.mock_encoder_delay_ms,
            segment_delay_ms=settings.mock_segment_delay_ms,
        )
    from .sam2_predictor import Sam2ImagePredictorBackend

    return Sam2ImagePredictorBackend(
        model_cfg=settings.sam2_model_cfg,
        checkpoint_path=settings.sam2_checkpoint_path,
        device=settings.device,
        amp_dtype=settings.amp_dtype,
        warmup_iters=settings.warmup_iters,
        warmup_image_size=settings.warmup_image_size,
        box_batch_size=settings.box_batch_size,
    )
