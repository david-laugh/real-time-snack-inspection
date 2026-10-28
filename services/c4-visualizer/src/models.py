"""
detection-result 스키마에 대응하는 Pydantic 모델.
shared/schemas/detection-result.schema.json 기준.
"""

from pydantic import BaseModel


class Detection(BaseModel):
    object_id: str
    bbox: list[float]
    status: str          # normal | defect | unknown
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
