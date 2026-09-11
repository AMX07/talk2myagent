import json
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from talk2myagent import audio as audio_module
from talk2myagent import engine as engine_module
from talk2myagent.audio import AudioBridge, human_devices
from talk2myagent.config import Settings
from talk2myagent.engine import Engine
from talk2myagent.plans import TestScenario as Scenario
from talk2myagent.service import create_app


class Speech:
    def __init__(self):
        self.spoken = []
        self.lock = threading.Lock()
        self.tts_lock = threading.Lock()

    def synthesize(self, text, voice=None):
        self.spoken.append(text)
        return np.ones(2400, np.float32) * 0.1, 24000

    def transcribe(self, audio, rate):
        return {"text": "", "inference_seconds": 0}


class Bridge:
    def __init__(self, config, on_segment, on_error, **options):
        self.config, self.on_segment, self.options = config, on_segment, options
        self.closed = False
        self.playing = threading.Event()
        self.playback_stop = threading.Event()
        self.last_input_rms = 0.02
        self.last_input_at = time.monotonic()

    def start(self):
        pass

    def record(self, path):
        sf.write(path, np.zeros(4800), 48000)

    def stop_recording(self):
        pass

    def play(self, audio, rate):
        return len(audio) / rate

    def begin(self):
        bridge = self

        class Playback:
            written = 0.0

            def write(self, audio, rate):
                self.written += len(audio) / rate
                return not bridge.playback_stop.is_set()

            def finish(self):
                bridge.playing.clear()
                return self.written, bridge.playback_stop.is_set()

        self.playback_stop.clear()
        self.playing.set()
        return Playback()

    def close(self):
        self.closed = True


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_module, "AudioBridge", Bridge)
    monkeypatch.setattr(
        engine_module, "human_devices", lambda *args: ("Physical mic", "Physical speaker")
    )
    value = Engine(root=tmp_path, speech=Speech())
    yield value
    value.shutdown()


@pytest.fixture
def scenario():
    return Scenario(
        user_request="Call the repair shop and find out whether my bike is ready.",
        recipient_role="Bike shop mechanic",
        customer_name="Morgan",
        objective="Get the bike repair status and pickup time",
        facts={"ticket": "B-42"},
        greeting="Hello, I am an AI assistant calling for Morgan about bike repair ticket B-42. Is it ready?",
        dialogue={"ticket": "The repair ticket is B-42."},
        allowed_actions=["Ask about repair status"],
        stop_conditions=["Stop test requested"],
        success_criteria=["Recipient confirms repair status", "Recipient confirms pickup time"],
    )


def prepare(engine, scenario):
    return engine.test_prepare(scenario.model_dump())


def start(engine, scenario, **kwargs):
    kwargs.setdefault("controller", "host")
    p = prepare(engine, scenario)
    return engine.test_start(p["call_id"], p["plan_id"], **kwargs)


def test_accepts_arbitrary_developer_task_without_opening_mic(engine, scenario):
    p = prepare(engine, scenario)
    call = engine.get(p["call_id"])
    assert call.bridge is None
    assert p["phase"] == "ready"
    assert p["user_request"] == scenario.user_request
    assert call.plan.facts == {"ticket": "B-42"}
    assert call.plan.is_demo
    assert (
        json.loads((call.folder / "session.json").read_text())["scenario"]["recipient_role"]
        == "Bike shop mechanic"
    )


def test_starts_with_greeting_and_same_speech_pipeline(engine, scenario):
    p = start(engine, scenario)
    call = engine.get(p["call_id"])
    assert engine.speech.spoken[-1] == scenario.greeting
    assert call.bridge.options == {"speaker_safe": True, "allow_shared_device": True}
    assert call.recording
    assert p["audio"]["input_device"] == "Physical mic"
    with pytest.raises(ValueError, match="already started"):
        engine.test_start(call.id, call.plan.fingerprint(), controller="host")


def test_headphones_enable_full_duplex_and_recording_can_be_off(engine, scenario):
    p = start(engine, scenario, audio_mode="headphones", record=False)
    call = engine.get(p["call_id"])
    assert not call.bridge.options["speaker_safe"]
    assert not call.recording
    assert engine.test_stop(call.id)["recording_path"] is None


def test_roleplay_cannot_dial_or_use_phone_connect(engine, scenario):
    p = prepare(engine, scenario)
    with pytest.raises(ValueError, match="Dialing is disabled"):
        engine.dial_request(p["call_id"], p["plan_id"], authorized=True)
    with pytest.raises(ValueError, match="Use test_start"):
        engine.connect(p["call_id"], p["plan_id"], True, True, True)
    engine.test_start(p["call_id"], p["plan_id"], controller="host")
    for fn in [engine.keypad, engine.tones]:
        with pytest.raises(ValueError, match="disabled"):
            fn(p["call_id"], "123")
    with pytest.raises(ValueError, match="only allowed in demo"):
        engine.simulate_remote(p["call_id"], "Pretend the developer said this")


def test_only_one_audio_session_at_once(engine, scenario):
    start(engine, scenario)
    p = prepare(engine, scenario)
    with pytest.raises(ValueError, match="Only one"):
        engine.test_start(p["call_id"], p["plan_id"], controller="host")


def test_completion_requires_recipient_evidence_for_all_criteria(engine, scenario):
    p = start(engine, scenario)
    cid = p["call_id"]
    with pytest.raises(ValueError, match="Use test_finish"):
        engine.finish(cid, "completed")
    with pytest.raises(ValueError, match="exactly once"):
        engine.test_finish(cid, "completed", "Done", [])
    checks = [
        {"criterion_index": i, "verdict": "met", "evidence_seq": [999], "explanation": "Confirmed"}
        for i in range(2)
    ]
    with pytest.raises(ValueError, match="actual recipient"):
        engine.test_finish(cid, "completed", "Done", checks)
    event = engine.get(cid).event("remote", "The bike is repaired. You can collect it at 4 pm.")
    for check in checks:
        check["evidence_seq"] = [event["seq"]]
    result = engine.test_finish(cid, "completed", "Bike repaired; collect at 4 pm.", checks)
    assert result["test_mode"] and not result["real_world_actions"]
    assert not result["needs_phone_hangup"]
    assert len(result["evaluation"]) == 2
    report = Path(result["report_path"]).read_text()
    assert "HUMAN ROLE-PLAY" in report and "pickup time" in report
    assert engine.get(cid).bridge.closed


def test_unmet_objective_cannot_be_reported_as_success(engine, scenario):
    cid = start(engine, scenario)["call_id"]
    checks = [
        {"criterion_index": i, "verdict": "unknown", "explanation": "Recipient could not confirm"}
        for i in range(2)
    ]
    with pytest.raises(ValueError, match="every success criterion"):
        engine.test_finish(cid, "completed", "Done", checks)
    result = engine.test_finish(cid, "needs_user", "Shop needs more information.", checks)
    assert result["outcome"] == "needs_user"


def test_stop_closes_microphone_and_preserves_partial_transcript(engine, scenario):
    cid = start(engine, scenario)["call_id"]
    call = engine.get(cid)
    call.event("remote", "Let me check the ticket.")
    result = engine.test_stop()
    assert call.bridge.closed and result["outcome"] == "cancelled"
    assert any(e["text"] == "Let me check the ticket." for e in result["transcript"])
    assert engine.test_stop(cid) == result


def test_no_input_never_means_success(engine, scenario):
    cid = start(engine, scenario)["call_id"]
    cursor = engine.listen(cid, timeout_seconds=0)["cursor"]
    result = engine.listen(cid, after_seq=cursor, timeout_seconds=0)
    assert result["timed_out"] and result["state"] == "active"
    assert result["audio"]["capturing"]


def test_spoken_stop_is_executed_without_waiting_for_controller(engine, scenario):
    engine.speech.transcribe = lambda *args: {"text": "Stop test.", "inference_seconds": 0}
    cid = start(engine, scenario)["call_id"]
    call = engine.get(cid)
    call.bridge.on_segment(np.ones(16000), time.monotonic())
    deadline = time.monotonic() + 3
    while call.result is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert call.result and call.result["outcome"] == "cancelled"
    assert call.bridge.closed


def test_roleplay_rpc_contract(engine, scenario):
    with TestClient(create_app(engine)) as client:
        p = client.post(
            "/rpc",
            json={"operation": "test_prepare", "arguments": {"scenario": scenario.model_dump()}},
        )
        assert p.status_code == 200
        cid = p.json()["call_id"]
        result = client.post(
            "/rpc",
            json={
                "operation": "test_start",
                "arguments": {"call_id": cid, "plan_id": p.json()["plan_id"], "controller": "host"},
            },
        )
        assert result.status_code == 200
        assert result.json()["greeting"]["event"]["text"] == scenario.greeting
        assert client.post("/rpc", json={"operation": "test_stop"}).json()["outcome"] == "cancelled"


def test_device_selection_avoids_phone_loopback(monkeypatch):
    monkeypatch.setattr(
        audio_module,
        "devices",
        lambda: [
            {"name": "BlackHole 2ch", "inputs": 2, "outputs": 2},
            {"name": "MacBook Pro Microphone", "inputs": 1, "outputs": 0},
            {"name": "MacBook Pro Speakers", "inputs": 0, "outputs": 2},
        ],
    )
    monkeypatch.setattr(
        audio_module.sd, "query_devices", lambda **kwargs: {"name": "BlackHole 2ch"}
    )
    assert human_devices() == ("MacBook Pro Microphone", "MacBook Pro Speakers")
    with pytest.raises(ValueError, match="physical"):
        human_devices("BlackHole 2ch")


def test_speaker_mode_suppresses_playback_and_tail_not_future_human():
    bridge = AudioBridge(Settings(), lambda *args: None, lambda *args: None, speaker_safe=True)
    bridge.play_started = 10
    bridge.playing.set()
    assert not bridge.suppresses(9)
    assert bridge.suppresses(10.1)
    bridge.suppression_windows.append((10, 12.45))
    bridge.playing.clear()
    assert bridge.suppresses(12.3)
    assert not bridge.suppresses(12.5)
    bridge.speaker_safe = False
    assert not bridge.suppresses(11)


def test_stop_during_model_warmup_never_opens_microphone(engine, scenario):
    p = prepare(engine, scenario)
    call = engine.get(p["call_id"])
    warming, release = threading.Event(), threading.Event()
    original = engine.speech.synthesize
    errors, results = [], []

    def blocked_synthesis(*args, **kwargs):
        warming.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    def start_test():
        try:
            engine.test_start(call.id, p["plan_id"], controller="host")
        except ValueError as exc:
            errors.append(str(exc))

    engine.speech.synthesize = blocked_synthesis
    starter = threading.Thread(target=start_test)
    starter.start()
    assert warming.wait(1)
    stopper = threading.Thread(target=lambda: results.append(engine.test_stop(call.id)))
    stopper.start()
    assert call.cancel_requested.wait(1)
    release.set()
    starter.join(2)
    stopper.join(2)
    assert call.bridge is None
    assert results[0]["outcome"] == "cancelled"
    assert "before the microphone opened" in errors[0]
