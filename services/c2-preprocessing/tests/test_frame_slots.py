"""세션별 최신 1장 보관(FrameSlots)과 이벤트 CSV. 브로커 없이 돈다."""
import asyncio
import csv

from c2.events import EventLog
from c2.mqtt_ingest import FrameSlots, _looks_like_envelope
from c2.pipeline import now_ms


def env(session, frame_id, age_ms=0):
    return {"trace_id": f"{session}-{frame_id}", "session_id": session, "frame_id": frame_id,
            "captured_at_ms": now_ms() - age_ms, "gateway_received_at_ms": now_ms()}


def test_keeps_only_latest_frame_per_session():
    async def scenario():
        events = EventLog()
        slots = FrameSlots(events, max_age_ms=500)
        for i in range(3):
            slots.put(env("a", i))
        slots.put(env("b", 0))
        got = [await slots.get(), await slots.get()]
        return events, slots, got

    events, slots, got = asyncio.run(scenario())
    assert [(e["session_id"], e["frame_id"]) for e in got] == [("a", 2), ("b", 0)]
    assert events.counts["dropped_stale"] == 2 and len(slots) == 0


def test_drops_old_frames_on_put_and_on_get():
    async def scenario():
        events = EventLog()
        slots = FrameSlots(events, max_age_ms=100)
        slots.put(env("a", 0, age_ms=1000))            # 보관 시점에 이미 오래됨
        slots.put(env("b", 0))
        await asyncio.sleep(0.2)                       # 기다리는 동안 오래됨
        slots.put(env("c", 0))
        return events, await slots.get()

    events, got = asyncio.run(scenario())
    assert got["session_id"] == "c"
    assert events.counts["dropped_old"] == 2


def test_age_check_disabled_with_zero():
    async def scenario():
        events = EventLog()
        slots = FrameSlots(events, max_age_ms=0)
        slots.put(env("a", 0, age_ms=10_000))
        return events, await slots.get()

    events, got = asyncio.run(scenario())
    assert got["frame_id"] == 0 and events.counts["dropped_old"] == 0


def test_envelope_precheck():
    assert _looks_like_envelope(env("a", 1))
    assert not _looks_like_envelope({"session_id": "a", "captured_at_ms": "123"})
    assert not _looks_like_envelope({"session_id": "a", "captured_at_ms": True})
    assert not _looks_like_envelope([1, 2])


def test_event_log_csv(tmp_path):
    path = tmp_path / "sub" / "c2_events.csv"
    log = EventLog(str(path))
    log.write(env("a", 1), "forwarded", "mqtt", preprocess_ms=12, infer_rtt_ms=3.456)
    log.write(None, "error", "mqtt", detail="invalid JSON")
    log.close()
    EventLog(str(path)).close()                        # 다시 열어도 헤더를 중복으로 쓰지 않는다
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert [r["event"] for r in rows] == ["forwarded", "error"]
    assert rows[0]["trace_id"] == "a-1" and rows[0]["infer_rtt_ms"] == "3.5" and rows[0]["source"] == "mqtt"
    assert rows[1]["detail"] == "invalid JSON"
