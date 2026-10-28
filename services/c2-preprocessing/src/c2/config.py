"""환경변수(C2_ 접두어) 기반 설정."""
from typing import Annotated, Literal, Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _parse_ints(v, n):
    if isinstance(v, str):
        v = [int(p) for p in v.split(",")]
    v = tuple(int(p) for p in v)
    if len(v) != n:
        raise ValueError(f"expected {n} comma-separated integers")
    return v


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="C2_", extra="ignore")

    # 긴 변 기준 리사이즈(px). 0이면 리사이즈하지 않는다. 원본이 더 작으면 확대하지 않는다.
    resize_long_side: int = 1024

    # 원본 좌표 기준 쿠키 지름(px). 비워두면 프레임마다 자동 추정 + 세션별 EMA 평활화.
    expected_diameter_px: Optional[float] = None
    diameter_ema_alpha: float = 0.2
    diameter_max_sessions: int = 256

    # "H,S,V" (OpenCV: H 0~180, S/V 0~255)
    hsv_low: Annotated[tuple[int, int, int], NoDecode] = (5, 80, 50)
    hsv_high: Annotated[tuple[int, int, int], NoDecode] = (30, 255, 255)

    # auto: 컨베이어 영역 자동 검출 / fixed: C2_ROI 좌표 사용 / off: 화면 전체
    roi_mode: Literal["auto", "fixed", "off"] = "auto"
    # fixed 모드용 "x1,y1,x2,y2" (원본 좌표). 빈 값이면 화면 전체.
    roi: Annotated[Optional[tuple[int, int, int, int]], NoDecode] = None

    infer_url: str = "http://nginx-inference-lb:8080/infer"
    infer_timeout_s: float = 5.0

    output_image_format: Literal["jpeg", "png"] = "jpeg"
    jpeg_quality: int = 90

    validate_output: bool = True

    @field_validator("expected_diameter_px", mode="before")
    @classmethod
    def _empty_is_none(cls, v):
        return None if v == "" else v

    @field_validator("hsv_low", "hsv_high", mode="before")
    @classmethod
    def _hsv(cls, v):
        return _parse_ints(v, 3)

    @field_validator("roi", mode="before")
    @classmethod
    def _roi(cls, v):
        return None if v in (None, "") else _parse_ints(v, 4)

    @field_validator("roi_mode", "output_image_format", mode="before")
    @classmethod
    def _lower(cls, v):
        return v.lower() if isinstance(v, str) else v
