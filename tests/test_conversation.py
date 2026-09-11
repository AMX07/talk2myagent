import threading
import time

import pytest
import test_roleplay as fixtures

from talk2myagent.conversation import Reply, messages_for

engine, scenario, prepare = fixtures.engine, fixtures.scenario, fixtures.prepare


class Brain:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.seen = []

    def ready(self):
        return {"ready": True}

    def prepare(self, plan):
        return {"prefix_cached": False}

    def respond(self, plan, events, cancel, on_sentence=None):
        self.seen.append((plan, events))
        remote = [e for e in events if e["speaker"] == "remote"]
        text, status = next(self.replies)
        if on_sentence:
            on_sentence(text)
        return Reply(say=text, status=status, evidence_seq=[remote[-1]["seq"]]), {
            "generation_seconds": 0.01
        }


def until(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("Worker did not reach expected state")
        time.sleep(0.01)


def autonomous(engine, scenario, brain):
    engine.brain = brain
    p = prepare(engine, scenario)
    engine.test_start(p["call_id"], p["plan_id"], controller="local")
    return engine.get(p["call_id"])


def test_local_worker_takes_multiple_turns_without_codex_and_requires_review(engine, scenario):
    brain = Brain(
        [
            ("The ticket is B-42.", "continue"),
            ("Thank you. Morgan will collect it at four.", "resolved"),
        ]
    )
    call = autonomous(engine, scenario, brain)
    call.event("remote", "What is the ticket number?")
    until(lambda: "The ticket is B-42." in engine.speech.spoken)
    confirmation = call.event("remote", "The bike is repaired and ready for pickup at four.")
    until(lambda: call.result is not None)
    assert call.bridge.closed
    assert call.result["outcome"] == "needs_user"
    assert call.result["review_required"]
    assert call.result["conversation"]["proposal"] == "resolved"
    assert len(brain.seen) == 2
    assert brain.seen[0][0].facts == scenario.facts
    checks = [
        {
            "criterion_index": i,
            "verdict": "met",
            "evidence_seq": [confirmation["seq"]],
            "explanation": "Confirmed by recipient",
        }
        for i in range(2)
    ]
    result = engine.test_finish(call.id, "completed", "Bike ready at four.", checks)
    assert result["outcome"] == "completed" and not result["review_required"]
    assert result["outcome_source"] == "host_reviewed"
    assert result["transcript"][-1]["text"].startswith("Host reviewed")


def test_rejects_duplicate_delegation_and_second_speaker(engine, scenario):
    call = autonomous(engine, scenario, Brain([]))
    with pytest.raises(ValueError, match="duplicate"):
        engine.conversation_start(call.id, call.plan.fingerprint())
    with pytest.raises(ValueError, match="owns speech"):
        engine.say(call.id, "An unwanted overlapping reply")
    engine.test_stop(call.id)
    assert call.bridge.closed


def test_stop_during_generation_never_plays_late_reply(engine, scenario):
    generating = threading.Event()

    class SlowBrain(Brain):
        def respond(self, plan, events, cancel, on_sentence=None):
            generating.set()
            cancel.wait(3)
            return Reply(say="Too late", status="continue"), {}

    call = autonomous(engine, scenario, SlowBrain([]))
    call.event("remote", "A question")
    assert generating.wait(2)
    result = engine.test_stop(call.id)
    call.conversation_worker.join(2)
    assert result["outcome"] == "cancelled"
    assert "Too late" not in engine.speech.spoken
    assert call.bridge.closed


def test_new_recipient_information_discards_stale_generated_reply(engine, scenario):
    class UpdatingBrain(Brain):
        def respond(self, plan, events, cancel, on_sentence=None):
            if not self.seen:
                self.seen.append(events)
                call.event("remote", "Correction: pickup is tomorrow at four.")
                if on_sentence:
                    on_sentence("Stale reply")
                return Reply(say="Stale reply", status="continue"), {}
            return super().respond(plan, events, cancel, on_sentence)

    brain = UpdatingBrain([("Tomorrow at four, thank you.", "resolved")])
    call = autonomous(engine, scenario, brain)
    call.event("remote", "The repair is finished; pickup is today.")
    until(lambda: call.result is not None)
    assert "Stale reply" not in engine.speech.spoken
    assert "Tomorrow at four, thank you." in engine.speech.spoken


def test_local_error_closes_audio_without_claiming_success(engine, scenario):
    call = autonomous(engine, scenario, Brain([]))
    call.event("remote", "What is the ticket?")
    until(lambda: call.result is not None)
    assert call.bridge.closed
    assert call.result["conversation"]["proposal"] == "failed"
    assert call.result["outcome"] != "completed"


def test_pending_review_survives_service_restart_without_reopening_audio(engine, scenario):
    call = autonomous(engine, scenario, Brain([("Thank you, goodbye.", "resolved")]))
    confirmation = call.event("remote", "The bike is repaired and available at four.")
    until(lambda: call.result is not None)
    call.conversation_worker.join(2)
    engine.sessions.clear()
    checks = [
        {
            "criterion_index": i,
            "verdict": "met",
            "evidence_seq": [confirmation["seq"]],
            "explanation": "Confirmed",
        }
        for i in range(2)
    ]
    result = engine.test_finish(call.id, "completed", "Bike ready.", checks)
    assert result["outcome"] == "completed"
    assert engine.get(call.id).bridge is None
    assert result["recording_path"] == call.result["recording_path"]


def test_plan_handoff_distinguishes_recipient_and_caller(scenario):
    messages = messages_for(
        scenario.call_plan(),
        [
            {"seq": 1, "speaker": "agent", "text": "Is the repair ready?"},
            {"seq": 2, "speaker": "remote", "text": "The bike is repaired."},
        ],
    )
    assert messages[-2]["role"] == "user" and '"recipient_event_id": 2' in messages[-2]["content"]
    assert messages[-3]["role"] == "assistant"
    assert scenario.objective in messages[0]["content"]
    assert "B-42" in messages[0]["content"]


def test_fact_guard_blocks_invented_details_and_keeps_transcript_facts():
    from talk2myagent.conversation import unsupported_details

    allowed = '{"facts": {"order_id": "DEMO-1234", "email": "a@b.com"}} case R789,012 March 14 1969'
    assert unsupported_details("The order is DEMO-1234 and the email is a@b.com.", allowed) == []
    assert unsupported_details("Case R789,012 is done.", allowed) == []
    assert unsupported_details("Born March 14, 1969.", allowed) == []
    assert unsupported_details("The email is alex.demo@example.com.", allowed) == [
        "alex.demo@example.com"
    ]
    assert unsupported_details("Card ending 4031.", allowed) == ["4031"]
    assert "June 15, 1990" in unsupported_details("Born June 15, 1990.", allowed)
    assert unsupported_details("Refund in 3-5 business days.", allowed) == []


def test_streaming_reply_replaces_hallucinated_sentence_with_fallback(scenario):
    import threading

    from talk2myagent.config import Settings
    from talk2myagent.conversation import FALLBACK, LocalConversation

    brain = LocalConversation(Settings(), threading.Lock())
    canned = (
        '{"say": "Sure. The customer\'s email is alex.demo@example.com and the ticket is B-42. '
        'Is there anything else I can help you with?", "status": "resolved", "evidence_seq": [2]}'
    )

    def fake_generate(messages, cancel, on_text=None):
        for i in range(1, len(canned) + 1):
            if on_text:
                on_text(canned[:i])
        return canned, {"generation_seconds": 0.1}

    brain._generate = fake_generate
    events = [
        {"seq": 1, "speaker": "agent", "text": "Hello"},
        {"seq": 2, "speaker": "remote", "text": "What is the email?"},
    ]
    spoken = []
    reply, metrics = brain.respond(scenario.call_plan(), events, threading.Event(), spoken.append)
    assert spoken == ["Sure.", FALLBACK]
    assert reply.say == "Sure. " + FALLBACK and reply.status == "continue"
    assert metrics["blocked_details"] == ["alex.demo@example.com"]
    # Non-streaming path applies the same guard and drops the role-reversed closing question.
    clean = '{"say": "Sure. The ticket is B-42. Is there anything else I can help you with?", "status": "continue", "evidence_seq": []}'
    brain._generate = lambda messages, cancel, on_text=None: (clean, {"generation_seconds": 0.1})
    reply, _ = brain.respond(scenario.call_plan(), events, threading.Event())
    assert reply.say == "Sure. The ticket is B-42."
