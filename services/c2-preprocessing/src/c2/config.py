"""환경변수(C2_ 접두어) 기반 설정."""
from typing import Annotated, Literal, Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _parse_ints(v, n):
    if isinstance(v, str):
        v = [int(p) for p in v.split(",")]
    v = tuple(int(p) for p in v)
    if len(v) != n:
        raise ValueError(f"expected {n} comma-separated integers")
    return v


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="C2_", extra="ignore", populate_by_name=True)

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
    # C1의 C2 대기 타임아웃(C1_TIMEOUT=5s)보다 짧아야 C3 지연이 C1에서 timeout이 아니라 C2의 502로 기록된다.
    infer_timeout_s: float = 3.0

    # ---- C1 -> C2 MQTT 수신 ----
    # 우선순위: C2_MQTT_HOST / C2_MQTT_PORT / C2_MQTT_TOPIC
    #        -> 공용 MQTT_HOST / MQTT_PORT / {MQTT_TOPIC_PREFIX}/frames/+  (.env가 env_file로 모든 컨테이너에 주입)
    #        -> 기본값 mqtt-broker / 1883 / psf/v1/frames/+
    # 빈 값("")은 지정하지 않은 것으로 본다. 최종 값은 _resolve_mqtt()에서 mqtt_host/port/topic에 채운다.
    mqtt_enabled: bool = True
    mqtt_host: Optional[str] = None
    mqtt_port: Optional[int] = None
    mqtt_topic: Optional[str] = None
    shared_mqtt_host: Optional[str] = Field(None, validation_alias="MQTT_HOST")
    shared_mqtt_port: Optional[int] = Field(None, validation_alias="MQTT_PORT")
    shared_mqtt_topic_prefix: Optional[str] = Field(None, validation_alias="MQTT_TOPIC_PREFIX")
    mqtt_qos: int = 0
    mqtt_keepalive_s: int = 30
    mqtt_client_id: Optional[str] = None      # 비우면 c2-preprocessing-<hostname>
    mqtt_reconnect_min_s: float = 1.0
    mqtt_reconnect_max_s: float = 10.0

    # 동시에 처리·전달하는 프레임 수(워커 수). C3 복제본 수 이상 권장.
    max_inflight: int = 4
    # captured_at_ms 기준으로 이보다 오래된 프레임은 버린다. 0이면 끔.
    max_frame_age_ms: int = 500
    # 프레임별 이벤트 CSV 경로. 비우면 기록 안 함.
    event_log: Optional[str] = None

    # ---- C2 -> C4 리사이즈 프레임 전송 (C4가 C3 결과를 이 이미지 위에 그린다) ----
    # 빈 값이면 기능 끔. C3 전송과 동시에 보내고 응답을 기다리지 않는다.
    c4_frame_url: Optional[str] = "http://c4-visualizer:8003/api/camera/frame"
    c4_max_inflight: int = 2          # C4 동시 전송 한도. 차 있으면 그 프레임은 보내지 않음(c4_skipped)
    c4_timeout_s: float = 1.0

    output_image_format: Literal["jpeg", "png"] = "jpeg"
    jpeg_quality: int = 90

    validate_output: bool = True

    @field_validator("expected_diameter_px", "mqtt_client_id", "event_log", "c4_frame_url", "mqtt_host", "mqtt_port", "mqtt_topic",
                     "shared_mqtt_host", "shared_mqtt_port", "shared_mqtt_topic_prefix", mode="before")
    @classmethod
    def _empty_is_none(cls, v):
        return None if isinstance(v, str) and not v.strip() else v

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

    @model_validator(mode="after")
    def _resolve_mqtt(self):
        self.mqtt_host = self.mqtt_host or self.shared_mqtt_host or "mqtt-broker"
        self.mqtt_port = self.mqtt_port or self.shared_mqtt_port or 1883
        if not self.mqtt_topic:
            prefix = (self.shared_mqtt_topic_prefix or "psf/v1").rstrip("/")
            self.mqtt_topic = f"{prefix}/frames/+"
        return self
