"""개발용 프레임 발행기: 이미지 1장을 frame-envelope로 감싸 MQTT로 일정 fps 발행한다 (C1 대역).

  python dev/publish_frames.py --fps 30 --seconds 5                       # localhost:11883 (dev override 브로커)
  python dev/publish_frames.py --image tests/fixtures/phone/phone_frame_0297.jpg --session cam1

호스트의 localhost:1883에는 발행하지 않는다. 같은 PC의 다른 프로젝트 브로커일 수 있기 때문이다.
dev/docker-compose.override.yml의 c2-dev-broker(호스트 11883)나 직접 띄운 테스트 브로커를 쓴다.
"""
import argparse
import base64
import json
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
DEFAULT_IMAGE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "phone" / "phone_frame_0297.jpg"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=11883)
    ap.add_argument("--topic-prefix", default="psf/v1")
    ap.add_argument("--session", default="dev-pub")
    ap.add_argument("--image", type=Path, default=DEFAULT_IMAGE, help="JPEG 파일 (그대로 image_b64로 보냄)")
    ap.add_argument("--width", type=int, default=1080, help="source_width (이미지 실제 크기)")
    ap.add_argument("--height", type=int, default=1920, help="source_height (이미지 실제 크기)")
    ap.add_argument("--fps", type=float, default=30)
    ap.add_argument("--seconds", type=float, default=5)
    a = ap.parse_args()
    if a.host in LOCAL_HOSTS and a.port == 1883:
        sys.exit("refusing to publish to localhost:1883 (may be another project's broker). Use --port 11883.")

    b64 = base64.b64encode(a.image.read_bytes()).decode()
    topic = f"{a.topic_prefix.rstrip('/')}/frames/{a.session}"
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"c2-dev-pub-{a.session}")
    c.connect(a.host, a.port)
    c.loop_start()
    n = int(a.fps * a.seconds)
    t0 = time.monotonic()
    for i in range(n):
        time.sleep(max(0.0, t0 + i / a.fps - time.monotonic()))
        now = int(time.time() * 1000)
        c.publish(topic, json.dumps({
            "schema_version": "1.0.0", "trace_id": f"{a.session}-{i:06d}", "session_id": a.session, "frame_id": i,
            "captured_at_ms": now, "source_width": a.width, "source_height": a.height, "format": "jpeg",
            "gateway_received_at_ms": now, "image_b64": b64}), qos=0)
    time.sleep(0.5)
    c.disconnect()
    c.loop_stop()
    print(f"published {n} frames to {a.host}:{a.port} {topic} at {a.fps} fps")


if __name__ == "__main__":
    main()
