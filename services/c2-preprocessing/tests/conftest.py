import os

# 단위 테스트는 MQTT 브로커 없이 돈다. MQTT 통합 테스트(test_mqtt_integration.py)는 Settings에서 직접 켠다.
os.environ.setdefault("C2_MQTT_ENABLED", "false")
