"""MQTT 설정 우선순위: C2_MQTT_* -> 공용 MQTT_HOST / MQTT_PORT / MQTT_TOPIC_PREFIX -> 기본값."""
import pytest

from c2.config import Settings

VARS = ["C2_MQTT_HOST", "C2_MQTT_PORT", "C2_MQTT_TOPIC", "MQTT_HOST", "MQTT_PORT", "MQTT_TOPIC_PREFIX"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for v in VARS:
        monkeypatch.delenv(v, raising=False)


def _mqtt(s):
    return s.mqtt_host, s.mqtt_port, s.mqtt_topic


def test_defaults():
    assert _mqtt(Settings()) == ("mqtt-broker", 1883, "psf/v1/frames/+")


def test_shared_variables(monkeypatch):
    monkeypatch.setenv("MQTT_HOST", "broker-a")
    monkeypatch.setenv("MQTT_PORT", "1999")
    monkeypatch.setenv("MQTT_TOPIC_PREFIX", "lab/v2/")
    assert _mqtt(Settings()) == ("broker-a", 1999, "lab/v2/frames/+")


def test_c2_variables_take_precedence(monkeypatch):
    monkeypatch.setenv("MQTT_HOST", "broker-a")
    monkeypatch.setenv("MQTT_PORT", "1999")
    monkeypatch.setenv("MQTT_TOPIC_PREFIX", "lab/v2")
    monkeypatch.setenv("C2_MQTT_HOST", "broker-c2")
    monkeypatch.setenv("C2_MQTT_PORT", "2883")
    monkeypatch.setenv("C2_MQTT_TOPIC", "custom/+/frames")
    assert _mqtt(Settings()) == ("broker-c2", 2883, "custom/+/frames")


def test_partial_override(monkeypatch):
    monkeypatch.setenv("MQTT_HOST", "broker-a")
    monkeypatch.setenv("MQTT_PORT", "1999")
    monkeypatch.setenv("C2_MQTT_PORT", "2883")
    assert _mqtt(Settings()) == ("broker-a", 2883, "psf/v1/frames/+")


def test_empty_c2_values_fall_back_to_shared(monkeypatch):
    """.env에 C2_MQTT_HOST= 처럼 빈 값이 있어도 공용 변수를 쓴다."""
    for v in ("C2_MQTT_HOST", "C2_MQTT_PORT", "C2_MQTT_TOPIC"):
        monkeypatch.setenv(v, "")
    monkeypatch.setenv("MQTT_HOST", "broker-a")
    monkeypatch.setenv("MQTT_TOPIC_PREFIX", "lab/v2")
    assert _mqtt(Settings()) == ("broker-a", 1883, "lab/v2/frames/+")


def test_constructor_arguments_win(monkeypatch):
    monkeypatch.setenv("MQTT_HOST", "broker-a")
    s = Settings(mqtt_host="explicit", mqtt_port=11883, mqtt_topic="t/+")
    assert _mqtt(s) == ("explicit", 11883, "t/+")
