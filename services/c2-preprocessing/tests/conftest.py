import os

# 단위 테스트는 MQTT 브로커 없이 돈다. MQTT 통합 테스트(test_mqtt_integration.py)는 Settings에서 직접 켠다.
os.environ.setdefault("C2_MQTT_ENABLED", "false")
# C4 프레임 전송도 기본으로 끈다. C4 테스트(test_c4_frames.py)는 Settings에서 직접 켠다.
os.environ.setdefault("C2_C4_FRAME_URL", "")
