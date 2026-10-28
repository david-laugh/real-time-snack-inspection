"""Pydantic mirrors of shared/schemas/*.json used by C3.

The JSON Schema files in shared/schemas/ are the source of truth. These models
reproduce them field-for-field (including `additionalProperties: false` via
extra="forbid"); tests validate C3 output against the real schema files.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

BBox = Annotated[List[float], Field(min_length=4, max_length=4)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# processed-frame.schema.json  (C2 -> Nginx -> C3)
# --------------------------------------------------------------------------
class Prompt(_Strict):
    candidate_id: str
    type: Literal["box"]
    bbox: BBox


class ProcessedFrame(_Strict):
    schema_version: str
    trace_id: str
    session_id: str
    frame_id: int = Field(ge=0)
    captured_at_ms: int
    preprocess_started_at_ms: int
    preprocess_finished_at_ms: int
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    prompts: List[Prompt]
    # Optional in the schema (transport TBD). C3 requires it: see docs/contract-notes.md.
    image_b64: Optional[str] = None


# --------------------------------------------------------------------------
# detection-result.schema.json  (C3 -> C4)
# --------------------------------------------------------------------------
class Status(str, Enum):
    """Wire values are lower-case per detection-result.schema.json."""

    NORMAL = "normal"
    DEFECT = "defect"
    UNKNOWN = "unknown"


class DefectType(str, Enum):
    BROKEN = "broken"
    SPOT_DEFECT = "spot_defect"


class Mask(_Strict):
    encoding: Literal["rle", "polygon", "png_b64"]
    width: Optional[int] = Field(default=None, ge=1)
    height: Optional[int] = Field(default=None, ge=1)
    data: object


class Detection(_Strict):
    object_id: str
    bbox: BBox
    status: Status
    defect_type: Optional[DefectType]
    defect_score: float = Field(ge=0.0, le=1.0)
    mask: Optional[Mask]


class LatencyBreakdown(_Strict):
    """Per-frame pipeline segments computed by C3 (ms). null when a clock went backwards."""

    capture_to_preprocess: Optional[float] = None  # C1 upload + C1 + MQTT + C2 slot wait
    preprocess: Optional[float] = None  # C2 processing
    c2_to_c3: Optional[float] = None  # C2 forward -> Nginx -> C3 request received
    c3_queue: Optional[float] = None  # waiting for this replica's SAM2 predictor
    c3_service: Optional[float] = None  # C3 processing without queue
    c3_total: Optional[float] = None  # C3 received -> result built
    capture_to_c3_done: Optional[float] = None


class InferenceTimings(_Strict):
    """C3 internal stage timings (ms), same values as the X-C3-*-Ms headers."""

    lock_wait: float
    decode: float
    encoder: float
    segmentation: float
    defect_analysis: float
    mask_encode: float
    total: float


class DetectionResult(_Strict):
    schema_version: str
    trace_id: str
    session_id: str
    frame_id: int = Field(ge=0)
    captured_at_ms: int
    inference_container_id: str
    inference_started_at_ms: int
    inference_finished_at_ms: int
    inference_latency_ms: float = Field(ge=0.0)
    detections: List[Detection]
    # --- optional, detection-result 1.1.0 (latency breakdown for C4) ---
    preprocess_started_at_ms: Optional[int] = None  # propagated from processed-frame
    preprocess_finished_at_ms: Optional[int] = None  # propagated from processed-frame
    inference_received_at_ms: Optional[int] = None  # C3 got the HTTP request
    latency_breakdown_ms: Optional[LatencyBreakdown] = None
    inference_timings_ms: Optional[InferenceTimings] = None
