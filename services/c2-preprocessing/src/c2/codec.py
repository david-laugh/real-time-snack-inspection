"""프레임 이미지 디코딩/인코딩.

이미지 전송 방식(JSON 안의 image_b64 vs binary/multipart vs 공유 저장소 참조)은 팀에서 아직 확정 전이다.
전송 방식에 따라 바뀌는 코드는 이 모듈에만 둔다. 방식이 바뀌면 decode_frame / encode_frame만 고치면 된다.
"""
import base64
import binascii

import cv2
import numpy as np

SUPPORTED_INPUT_FORMATS = ("jpeg", "png")   # TODO: raw_bgr / raw_rgb (source_width/height로 reshape)


class DecodeError(ValueError):
    pass


def decode_frame(envelope: dict) -> np.ndarray:
    """frame-envelope에서 BGR 이미지를 꺼낸다."""
    fmt = envelope["format"]
    if fmt not in SUPPORTED_INPUT_FORMATS:
        raise DecodeError(f"unsupported format: {fmt}")
    b64 = envelope.get("image_b64")
    if not b64:
        raise DecodeError("image_b64 is missing")
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise DecodeError(f"invalid base64: {e}") from e
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise DecodeError(f"failed to decode {fmt} image")
    return img


def encode_frame(bgr: np.ndarray, fmt: str = "jpeg", jpeg_quality: int = 90) -> dict:
    """processed-frame에 합칠 이미지 필드를 만든다. 현재는 {"image_b64": ...}."""
    if fmt == "jpeg":
        ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
    elif fmt == "png":
        ok, buf = cv2.imencode(".png", bgr)
    else:
        raise ValueError(f"unsupported output format: {fmt}")
    if not ok:
        raise RuntimeError(f"failed to encode {fmt} image")
    return {"image_b64": base64.b64encode(buf.tobytes()).decode("ascii")}
