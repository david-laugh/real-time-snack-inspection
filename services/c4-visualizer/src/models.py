"""
detection-result 스키마에 대응하는 Pydantic 모델.
shared/schemas/detection-result.schema.json 기준.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Prompt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    candidate_id: str
    type: Literal["box"]
    bbox: list[float] = Field(min_length=4, max_length=4)


class CameraFrame(BaseModel):
    """shared/schemas/camera-frame.schema.json: 10 required fields, no extras."""
    model_config = ConfigDict(extra="forbid", strict=True)

    trace_id: str
    session_id: str
    frame_id: int = Field(ge=0)
    gateway_received_at_ms: int
    preprocess_started_at_ms: int
    preprocess_finished_at_ms: int
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    prompts: list[Prompt]
    image_b64: str = Field(min_length=1)


class ScanLine(BaseModel):
    ratio: float = Field(gt=0, lt=1)


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
