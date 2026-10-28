"""
detection-result 스키마에 대응하는 Pydantic 모델.
shared/schemas/detection-result.schema.json 기준.
"""

from typing import Literal

from pydantic import BaseModel, Field


class CameraFrame(BaseModel):
    trace_id: str
    session_id: str
    frame_id: int
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    gateway_received_at_ms: int | None = Field(default=None, ge=0)
    preprocess_started_at_ms: int | None = Field(default=None, ge=0)
    preprocess_finished_at_ms: int | None = Field(default=None, ge=0)
    prompts: list[dict]
    image_b64: str = Field(min_length=1)


class Detection(BaseModel):
    object_id: str
    bbox: list[float]
    status: Literal["normal", "defect", "unknown"]
    defect_type: str | None = None
    defect_score: float = 0.0
    mask: dict | None = None


class DetectionResult(BaseModel):
    schema_version: str = "1.0.0"
    trace_id: str
    session_id: str
    frame_id: int
    captured_at_ms: int
    inference_container_id: str
    inference_started_at_ms: int
    inference_finished_at_ms: int
    inference_latency_ms: float
    detections: list[Detection]

    preprocess_started_at_ms: int | None = None
    preprocess_finished_at_ms: int | None = None
    inference_received_at_ms: int | None = None
    latency_breakdown_ms: dict[str, float | None] | None = None
    inference_timings_ms: dict[str, float | None] | None = None
