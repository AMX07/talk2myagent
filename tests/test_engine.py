import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from talk2myagent.engine import Engine
from talk2myagent.plans import CallPlan, amazon_plan
from talk2myagent.service import create_app


class FakeSpeech:
    def synthesize(self, text, voice=None):
        return (np.ones(2400) * 0.1).astype(np.float32), 24000

    def transcribe(self, audio, rate):
        return {"text": "Recognized audio, not fixture text", "inference_seconds": 0.01}


@pytest.fixture
def engine(tmp_path):
    engine = Engine(root=tmp_path, speech=FakeSpeech())
    yield engine
    engine.shutdown()


@pytest.fixture
def plan():
    return amazon_plan(
        "return",
        "Demo User",
        "DEMO-42",
        "grinder",
        "Damaged",
        "+12025550123",
        "Fictional example",
        is_demo=True,
    )


def active(engine, plan):
    p = engine.prepare(plan.model_dump())
    engine.connect(p["call_id"], p["plan_id"])
    return p["call_id"]


def test_demo_plan_cannot_go_live(engine, plan):
    with pytest.raises(ValueError, match="fictional"):
        engine.prepare(plan.model_dump(), "live")


@pytest.mark.parametrize(
    "number", ["911", "+911", "+12025550123;echo evil", "1-888-123-1234", "", "+012345678"]
)
def test_phone_validation(plan, number):
    data = plan.model_dump()
    data["phone_number"] = number
    with pytest.raises(ValueError):
        CallPlan.model_validate(data)


def test_plan_fingerprint_tracks_scope(plan):
    old = plan.fingerprint()
    plan.allowed_actions.append("new action")
    assert plan.fingerprint() != old


def test_live_connect_needs_observed_connection(engine, plan):
    plan.is_demo = False
    p = engine.prepare(plan.model_dump(), "live")
    with pytest.raises(ValueError, match="observed connected"):
        engine.connect(p["call_id"], p["plan_id"], authorized=True)
    assert engine.result(p["call_id"])["state"] == "prepared"


def test_connect_is_not_repeatable(engine, plan):
    p = engine.prepare(plan.model_dump())
    with pytest.raises(ValueError, match="Plan changed"):
        engine.connect(p["call_id"], "bad-id")
    engine.connect(p["call_id"], p["plan_id"])
    with pytest.raises(ValueError, match="not retryable"):
        engine.connect(p["call_id"], p["plan_id"])


def test_no_recording_before_consent(engine, plan):
    cid = active(engine, plan)
    engine.simulate_remote(cid, "This should not enter saved call audio")
    result = engine.finish(cid)
    assert result["recording_path"] is None
    assert not (Path(result["report_path"]).parent / "remote.wav").exists()


def test_recording_window_and_idempotent_finish(engine, plan):
    cid = active(engine, plan)
    engine.say(cid, "Before consent")
    engine.recording_start(cid, "Fictional demo only")
    engine.simulate_remote(cid, "A remote utterance")
    engine.say(cid, "A reply")
    engine.recording_stop(cid)
    engine.say(cid, "After withdrawal")
    with pytest.raises(ValueError, match="overwrite"):
        engine.recording_start(cid, "New consent")
    result = engine.finish(cid, "completed", "Simulation")
    assert engine.finish(cid) == result
    audio, rate = sf.read(result["recording_path"])
    assert audio.shape == (int(0.9 * rate), 2)
    assert np.max(audio[:, 0]) > 0.09 and np.max(audio[:, 1]) > 0.09
    assert result["simulated"]
    assert not result["needs_phone_hangup"]
    remote = [e for e in result["transcript"] if e["speaker"] == "remote"]
    assert remote[0]["text"] == "Recognized audio, not fixture text"
    assert Path(result["transcript_path"]).exists()


def test_cursor_consumption(engine, plan):
    cid = active(engine, plan)
    first = engine.listen(cid, timeout_seconds=0)
    assert first["events"]
    second = engine.listen(cid, after_seq=first["cursor"], timeout_seconds=0)
    assert second["events"] == [] and second["timed_out"]
    engine.simulate_remote(cid, "Support")
    third = engine.listen(cid, after_seq=first["cursor"], timeout_seconds=0)
    assert len(third["events"]) == 1


def test_keypad_is_explicitly_not_sent(engine, plan):
    cid = active(engine, plan)
    assert engine.keypad(cid, "12#")["sent"] is False
    with pytest.raises(ValueError):
        engine.keypad(cid, "x")


def test_result_after_restart(engine, plan):
    cid = active(engine, plan)
    engine.get(cid).event("remote", "Case number 123")
    engine.sessions.clear()
    result = engine.result(cid)
    assert result["state"] == "interrupted"
    assert result["transcript"][-1]["text"] == "Case number 123"
    with pytest.raises(ValueError):
        engine.result("../../etc/passwd")


def test_artifact_escaping(engine, plan):
    cid = active(engine, plan)
    engine.get(cid).event("remote", '<script>alert("x")</script>')
    result = engine.finish(cid, summary="<img src=x onerror=alert(1)>")
    report = Path(result["report_path"]).read_text()
    assert "<script>" not in report and "<img src=x" not in report
    assert "&lt;script&gt;" in report


def test_rpc_validates_and_returns_artifacts(engine, plan):
    with TestClient(create_app(engine)) as client:
        bad = client.post("/rpc", json={"operation": "__getattribute__", "arguments": {}})
        assert bad.status_code == 400
        bad = client.post(
            "/rpc", json={"operation": "prepare", "arguments": {"plan": {}, "mode": "live"}}
        )
        assert bad.status_code == 400
        prepared = client.post(
            "/rpc", json={"operation": "prepare", "arguments": {"plan": plan.model_dump()}}
        )
        assert prepared.status_code == 200
        cid = prepared.json()["call_id"]
        final = client.post("/rpc", json={"operation": "finish", "arguments": {"call_id": cid}})
        assert final.status_code == 200
        assert json.loads(Path(final.json()["transcript_path"]).read_text())["mode"] == "demo"
