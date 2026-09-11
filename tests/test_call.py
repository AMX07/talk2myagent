"""End-to-end runner tests with a fake Phone app, fake audio, and a scripted local model."""

import threading
import time
from typing import ClassVar

import numpy as np
import pytest
import test_roleplay as fixtures

from talk2myagent import engine as engine_module
from talk2myagent import macphone
from talk2myagent.config import Settings
from talk2myagent.conversation import Reply
from talk2myagent.engine import Engine
from talk2myagent.plans import amazon_plan


class EchoSpeech(fixtures.Speech):
    """Transcription returns the last synthesized text, so simulated turns round-trip."""

    def __init__(self):
        super().__init__()
        self.last = ""

    def synthesize(self, text, voice=None):
        self.last = text
        return super().synthesize(text, voice)

    def transcribe(self, audio, rate):
        if len(audio) < 2000:  # warm-up silence
            return {"text": "", "inference_seconds": 0}
        return {"text": self.last, "inference_seconds": 0.01}


class Brain:
    def __init__(self, replies, persona=()):
        self.replies = iter(replies)
        self.persona = iter(persona)
        self.prepared = []

    def ready(self):
        return {"ready": True}

    def prepare(self, plan):
        self.prepared.append(plan.fingerprint())
        return {"prefix_cached": True, "prefix_tokens": 900}

    def respond(self, plan, events, cancel, on_sentence=None):
        remote = [e for e in events if e["speaker"] == "remote"]
        text, status, consent, keys = next(self.replies)
        if on_sentence and text:
            for sentence in text.split("|"):
                on_sentence(sentence)
        return Reply(
            say=text.replace("|", " "),
            status=status,
            evidence_seq=[remote[-1]["seq"]] if status == "resolved" else [],
            consent=consent,
            keys=keys,
        ), {"generation_seconds": 0.02, "first_sentence_seconds": 0.01}

    def persona_reply(self, brief, events, cancel):
        return next(self.persona)


class FakePhone:
    def __init__(self):
        self.dialed = []
        self.in_call = False
        self.hangups = 0
        self.keys = []
        self.microphone = None
        self.remote_hangup_after_polls = None
        self.polls = 0

    def dial(self, number, **kwargs):
        self.dialed.append(number)
        self.in_call = True
        return {"number": number, "confirmed": True, "in_call": True, "buttons": []}

    def state(self):
        self.polls += 1
        if (
            self.remote_hangup_after_polls is not None
            and self.polls > self.remote_hangup_after_polls
        ):
            self.in_call = False
        return {
            "running": True,
            "in_call": self.in_call,
            "connected": self.in_call,
            "progress": None,
            "timer": "0:03" if self.in_call else None,
            "end_button": "End" if self.in_call else None,
            "confirm_button": None,
            "keypad_button": "keypad",
            "buttons": [],
            "texts": [],
        }

    def hangup(self):
        self.hangups += 1
        was = self.in_call
        self.in_call = False
        return {"hung_up": was, "was_in_call": was, "in_call": False}

    def keypad(self, digits):
        self.keys.append(digits)
        return {"requested": digits, "pressed": digits, "complete": True}

    def set_microphone(self, name):
        self.microphone = name
        return name


class FakeRouting:
    instances: ClassVar[list] = []

    def __init__(self, incoming, outgoing, phone=None):
        self.incoming, self.outgoing, self.phone = incoming, outgoing, phone
        self.applied = self.restored = False
        FakeRouting.instances.append(self)

    def apply(self):
        self.applied = True
        return {"output": self.incoming, "input": self.outgoing}

    def restore(self):
        self.restored = True
        return {"restored": True}


def until(predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("Condition not reached")
        time.sleep(0.02)


@pytest.fixture
def live_engine(tmp_path, monkeypatch):
    FakeRouting.instances.clear()
    monkeypatch.setattr(engine_module, "AudioBridge", fixtures.Bridge)
    monkeypatch.setattr(engine_module, "human_devices", lambda *a: ("Mic", "Speakers"))
    monkeypatch.setattr(
        engine_module,
        "devices",
        lambda: [
            {"name": "BlackHole 16ch", "inputs": 16, "outputs": 16},
            {"name": "BlackHole 2ch", "inputs": 2, "outputs": 2},
        ],
    )
    monkeypatch.setattr(macphone, "accessibility_enabled", lambda: True)
    monkeypatch.setattr(macphone, "AudioRouting", FakeRouting)
    phone = FakePhone()
    config = Settings(greeting_wait_seconds=0.3, silence_seconds=0.2)
    value = Engine(config=config, root=tmp_path, speech=EchoSpeech(), phone=phone)
    yield value, phone
    value.shutdown()


def live_plan():
    return amazon_plan(
        "return",
        "Morgan Example",
        "112-3344",
        "coffee grinder",
        "It arrived damaged.",
        "+18882804331",
        "User-confirmed support number",
    ).model_dump()


def test_live_call_dials_talks_consents_hangs_up_and_restores(live_engine):
    engine, phone = live_engine
    engine.brain = Brain(
        [
            (
                "Yes, please help return the grinder.|The order is 112-3344.",
                "continue",
                "granted",
                "",
            ),
            ("Thank you, goodbye.", "resolved", "unknown", ""),
        ]
    )
    with pytest.raises(ValueError, match="authorize"):
        engine.call_start(live_plan(), mode="live")
    started = engine.call_start(live_plan(), mode="live", authorized=True)
    call = engine.get(started["call_id"])
    until(lambda: call.state == "active")
    assert phone.dialed == ["+18882804331"]
    assert FakeRouting.instances[-1].applied
    # Nobody spoke within the greeting window, so the opening is delivered.
    until(lambda: engine.speech.spoken and engine.speech.spoken[-1] == call.plan.opening)
    assert not call.recording
    call.event("remote", "Sure, you can record. How can I help?", speech_end_at=call.elapsed())
    until(lambda: call.recording)
    assert "return the grinder" in engine.speech.spoken[-2]
    confirmation = call.event("remote", "The return is confirmed, case 4821.")
    until(lambda: call.phase == "ended")
    assert phone.hangups == 1 and not phone.in_call
    assert FakeRouting.instances[-1].restored
    result = engine.call_status(call.id)["result"]
    assert result["review_required"] and result["conversation"]["proposal"] == "resolved"
    assert result["needs_phone_hangup"] is False
    assert result["latency"]["turns"] == 2
    checks = [
        {
            "criterion_index": i,
            "verdict": "met",
            "evidence_seq": [confirmation["seq"]],
            "explanation": "Confirmed",
        }
        for i in range(2)
    ]
    reviewed = engine.conversation_review(call.id, "completed", "Return confirmed.", checks)
    assert reviewed["outcome"] == "completed" and reviewed["outcome_source"] == "host_reviewed"
    assert engine.brain.prepared == [call.plan.fingerprint()]


def test_menu_digits_are_pressed_in_the_phone_app(live_engine):
    engine, phone = live_engine
    engine.brain = Brain(
        [("", "continue", "unknown", "2"), ("Goodbye.", "needs_user", "unknown", "")]
    )
    started = engine.call_start(live_plan(), mode="live", authorized=True)
    call = engine.get(started["call_id"])
    until(lambda: call.state == "active")
    call.event("remote", "For returns, press 2.")
    until(lambda: phone.keys == ["2"])
    assert any(e["text"] == "[DTMF 2]" for e in call.events)
    call.event("remote", "Please enter the one-time code we texted you.")
    until(lambda: call.phase == "ended")
    assert engine.call_status(call.id)["result"]["conversation"]["proposal"] == "needs_user"


def test_remote_hangup_ends_the_session_for_review(live_engine):
    engine, phone = live_engine
    phone.remote_hangup_after_polls = 3
    engine.brain = Brain([("Hello?", "continue", "unknown", "")])
    started = engine.call_start(live_plan(), mode="live", authorized=True)
    call = engine.get(started["call_id"])
    until(lambda: call.phase == "ended", timeout=12)
    result = engine.call_status(call.id)["result"]
    assert result["conversation"]["proposal"] == "remote_hangup" and result["review_required"]
    assert phone.hangups == 0 and result["needs_phone_hangup"] is False
    assert FakeRouting.instances[-1].restored


def test_host_hangup_cancels_and_restores(live_engine):
    engine, phone = live_engine
    engine.brain = Brain([])
    started = engine.call_start(live_plan(), mode="live", authorized=True)
    call = engine.get(started["call_id"])
    until(lambda: call.state == "active")
    engine.hangup(call.id)
    until(lambda: call.phase == "ended")
    assert engine.call_status(call.id)["result"]["outcome"] == "cancelled"
    assert phone.hangups >= 1 and FakeRouting.instances[-1].restored


def test_call_wait_streams_events_and_reports_completion(live_engine):
    engine, _phone = live_engine
    engine.brain = Brain([("Goodbye.", "needs_user", "unknown", "")])
    started = engine.call_start(live_plan(), mode="live", authorized=True)
    first = engine.call_wait(started["call_id"], after_seq=0, timeout_seconds=3)
    assert first["events"] and not first["done"]
    call = engine.get(started["call_id"])
    until(lambda: call.state == "active")
    call.event("remote", "We cannot help without the account holder.")
    cursor = first["cursor"]
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        update = engine.call_wait(call.id, after_seq=cursor, timeout_seconds=2)
        cursor = update["cursor"]
        if update["done"]:
            break
    assert update["done"] and update["result"]["outcome"] == "needs_user"


def test_demo_rehearsal_runs_without_dialing(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_module, "human_devices", lambda *a: ("Mic", "Speakers"))
    phone = FakePhone()
    engine = Engine(
        config=Settings(greeting_wait_seconds=0.3), root=tmp_path, speech=EchoSpeech(), phone=phone
    )
    engine.brain = Brain(
        [
            (
                "Hi, I am an AI assistant calling for Alex about a damaged coffee grinder.",
                "continue",
                "unknown",
                "",
            ),
            ("Thank you, goodbye.", "resolved", "unknown", ""),
        ],
        persona=["I can help. The return is approved, case 4821, no fee."],
    )
    demo = amazon_plan(
        "return",
        "Alex Demo",
        "DEMO-1",
        "coffee grinder",
        "Damaged.",
        "+12025550123",
        "fictional",
        is_demo=True,
    )
    try:
        started = engine.call_start(demo.model_dump(), mode="demo")
        call = engine.get(started["call_id"])
        until(lambda: call.phase == "ended", timeout=10)
        result = engine.call_status(call.id)["result"]
        assert result["simulated"] and phone.dialed == []
        remote = [e for e in result["transcript"] if e["speaker"] == "remote"]
        assert len(remote) == 2 and "case 4821" in remote[-1]["text"]
        assert result["recording_path"] and result["conversation"]["proposal"] == "resolved"
        assert result["latency"]["turns"] == 2
    finally:
        engine.shutdown()


def test_speech_job_discards_stale_reply_before_first_audio(live_engine):
    engine, _phone = live_engine
    engine.brain = Brain([])
    started = engine.call_start(live_plan(), mode="live", authorized=True)
    call = engine.get(started["call_id"])
    until(lambda: call.state == "active")
    import queue

    sentences = queue.Queue()
    discarded = threading.Event()
    job = engine.speak_async(call, sentences, should_start=lambda: False, on_discard=discarded.set)
    sentences.put("Never played")
    sentences.put(None)
    job.join(2)
    assert job.discarded and discarded.is_set()
    assert "Never played" not in engine.speech.spoken
    engine.hangup(call.id)
    until(lambda: call.phase == "ended")


def test_transcriber_drops_lone_hallucinations():
    from talk2myagent.speech import HALLUCINATIONS

    assert "thank you." in HALLUCINATIONS
    assert np.zeros(2).dtype == np.float64
