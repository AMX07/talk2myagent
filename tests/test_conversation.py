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

    def respond(self, plan, events, cancel, on_sentence=None, nudge=None, avoid_ack=""):
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
        def respond(self, plan, events, cancel, on_sentence=None, nudge=None, avoid_ack=""):
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
        def respond(self, plan, events, cancel, on_sentence=None, nudge=None, avoid_ack=""):
            if not self.seen:
                self.seen.append(events)
                call.event("remote", "Correction: pickup is tomorrow at four.")
                if on_sentence:
                    on_sentence("Stale reply")
                return Reply(say="Stale reply", status="continue"), {}
            return super().respond(plan, events, cancel, on_sentence, nudge, avoid_ack)

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


def test_ack_is_streamed_first_and_requests_stay_open(scenario):
    import json as json_module
    import threading

    from talk2myagent.config import Settings
    from talk2myagent.conversation import LocalConversation, SayStream

    full = json_module.dumps(
        {
            "ack": "Sure.",
            "say": "The ticket is B-42. Please give me the pickup time.",
            "status": "resolved",
            "evidence_seq": [2],
        }
    )
    stream, got = SayStream(), []
    for i in range(1, len(full) + 1):
        got += stream.feed(full[:i])
    assert got == ["Sure.", "The ticket is B-42.", "Please give me the pickup time."]
    brain = LocalConversation(Settings(), threading.Lock())
    brain._generate = lambda messages, cancel, on_text=None: (full, {"generation_seconds": 0.1})
    events = [{"seq": 2, "speaker": "remote", "text": "What is the ticket?"}]
    reply, _ = brain.respond(scenario.call_plan(), events, threading.Event())
    assert reply.status == "continue"  # a request means we must hear the answer
    assert reply.say.startswith("Sure. The ticket is B-42.")


def test_worker_breaks_a_loop_after_a_nudged_repeat(engine, scenario):
    class LoopingBrain(Brain):
        def __init__(self):
            super().__init__([])
            self.nudges = []

        def respond(self, plan, events, cancel, on_sentence=None, nudge=None, avoid_ack=""):
            self.nudges.append(nudge)
            if on_sentence:
                on_sentence("Could you confirm the ticket number?")
            return Reply(say="Could you confirm the ticket number?", status="continue"), {}

    brain = LoopingBrain()
    call = autonomous(engine, scenario, brain)
    for text in ["It is B-42.", "Yes, B-42.", "B-42, as I said."]:
        expected = len(brain.nudges) + 1
        call.event("remote", text)
        until(lambda n=expected: len(brain.nudges) >= n)
    until(lambda: call.result is not None)
    assert brain.nudges[:2] == [None, None] and brain.nudges[2] is not None
    assert call.result["conversation"]["proposal"] == "needs_user"
    assert "looped" in call.result["summary"]


def test_an_acknowledgment_is_never_doubled_or_repeated_next_turn(scenario):
    import threading as t

    from talk2myagent.config import Settings
    from talk2myagent.conversation import LocalConversation

    brain = LocalConversation(Settings(), t.Lock())
    canned = (
        '{"ack": "Sure.", "say": "The ticket is B-42.", "status": "continue", "evidence_seq": []}'
    )

    def fake(messages, cancel, on_text=None, max_tokens=None):
        for i in range(1, len(canned) + 1):
            if on_text:
                on_text(canned[:i])
        return canned, {"generation_seconds": 0.1}

    brain._generate = fake
    events = [{"seq": 2, "speaker": "remote", "text": "Which ticket?"}]
    spoken = []
    reply, metrics = brain.respond(scenario.call_plan(), events, t.Event(), spoken.append)
    assert spoken == ["Sure.", "The ticket is B-42."]
    assert reply.say == "Sure. The ticket is B-42."  # the ack is not added a second time
    assert reply.spoken == reply.say and metrics["ack"] == "Sure."

    # The same acknowledgment twice in a row sounds robotic, so it is dropped.
    spoken = []
    reply, metrics = brain.respond(
        scenario.call_plan(), events, t.Event(), spoken.append, avoid_ack="Sure."
    )
    assert spoken == ["The ticket is B-42."] and metrics["ack"] == ""
    assert reply.say == "The ticket is B-42."


def _canned_brain(canned):
    import threading as t

    from talk2myagent.config import Settings
    from talk2myagent.conversation import LocalConversation

    brain = LocalConversation(Settings(), t.Lock())
    brain._generate = lambda messages, cancel, on_text=None, max_tokens=None: (
        canned,
        {"generation_seconds": 0.1},
    )
    return brain


def test_a_date_from_memory_is_speakable_but_an_invented_one_is_not(scenario):
    import threading as t

    from talk2myagent.conversation import FALLBACK

    canned = '{"ack":"","say":"It was delivered on 3 September 2026.","status":"continue","evidence_seq":[]}'
    asked = [
        {"seq": 1, "speaker": "agent", "text": "One moment, let me check that."},
        {"seq": 2, "speaker": "remote", "text": "When was it delivered?"},
    ]
    remembered = {
        "seq": 3,
        "speaker": "memory",
        "text": "The coffee grinder was delivered on 3 September 2026.",
        "query": "delivery date",
    }

    # With the lookup in context the date is the customer's own record, so it is said.
    reply, metrics = _canned_brain(canned).respond(
        scenario.call_plan(), [*asked, remembered], t.Event()
    )
    assert reply.say == "It was delivered on 3 September 2026."
    assert "blocked_details" not in metrics

    # Without it, the same sentence is the model inventing a date, and is blocked.
    reply, metrics = _canned_brain(canned).respond(scenario.call_plan(), asked, t.Event())
    assert reply.say == FALLBACK
    # The date matcher and the digit-run matcher can both fire on one span, so the
    # report lists fragments; what matters is that the sentence never got spoken.
    assert any("2026" in detail for detail in metrics["blocked_details"])


def test_a_lookup_cannot_be_used_to_end_the_call(scenario):
    import threading as t

    canned = (
        '{"ack":"","say":"One moment.","status":"resolved","evidence_seq":[2],'
        '"lookup":"the delivery date"}'
    )
    events = [{"seq": 2, "speaker": "remote", "text": "When was it delivered?"}]
    reply, _ = _canned_brain(canned).respond(scenario.call_plan(), events, t.Event())
    # You cannot declare success on a question you have not answered yet.
    assert reply.lookup == "the delivery date" and reply.status == "continue"


def test_memory_results_reach_the_model_as_their_own_kind_of_message(scenario):
    from talk2myagent.conversation import messages_for

    messages = messages_for(
        scenario.call_plan(),
        [
            {"seq": 2, "speaker": "remote", "text": "When was it delivered?"},
            {"seq": 3, "speaker": "memory", "text": "Delivered 3 September.", "query": "delivery"},
        ],
    )
    # The instructions mention memory_result too; take the payload, not the rules.
    memory_message = next(m for m in messages if m["content"].startswith('{"memory_result"'))
    assert memory_message["role"] == "system"
    assert '"looked_up": "delivery"' in memory_message["content"]


def test_saying_it_will_check_without_naming_a_lookup_still_searches(scenario):
    import threading as t

    canned = (
        '{"ack":"Sure.","say":"Let me check that for you.","status":"continue","evidence_seq":[]}'
    )
    events = [{"seq": 2, "speaker": "remote", "text": "What is the delivery date on that order?"}]
    reply, metrics = _canned_brain(canned).respond(scenario.call_plan(), events, t.Event())
    # Smaller models announce the check and forget the field; the intent still counts.
    assert reply.lookup == "What is the delivery date on that order?"
    assert metrics["lookup_from"] == "check_phrase"


def test_checking_with_the_customer_is_a_decision_not_a_memory_lookup(scenario):
    import threading as t

    canned = (
        '{"ack":"","say":"Let me check that with him, could you hold?","status":"needs_user",'
        '"evidence_seq":[],"question":"They want a 20% fee. Accept?","options":["Yes","No"]}'
    )
    events = [{"seq": 2, "speaker": "remote", "text": "There is a twenty percent fee."}]
    reply, metrics = _canned_brain(canned).respond(scenario.call_plan(), events, t.Event())
    # A question for the customer must not be hijacked into a memory search.
    assert reply.lookup == "" and reply.question.startswith("They want")
    assert "lookup_from" not in metrics


def test_a_date_is_the_same_date_however_it_is_written():
    from talk2myagent.conversation import unsupported_details

    known = "The coffee grinder was delivered on 3 September 2026."
    for rewritten in [
        "It was delivered on September 3rd, 2026.",
        "Delivered 3 September 2026.",
        "It arrived on Sept 3, 2026.",
        "It shipped on 9/3/2026.",
        "It was delivered on September 3rd.",  # the year is implied by the record
    ]:
        assert unsupported_details(rewritten, known) == [], rewritten

    # A different day, or a different date entirely, is still an invention.
    assert unsupported_details("It came on 4 September 2026.", known) == ["4 September 2026"]
    assert unsupported_details("It arrived on June 15, 2024.", known) == ["June 15, 2024"]


def test_one_bad_date_is_reported_once_not_as_two_problems():
    from talk2myagent.conversation import unsupported_details

    # The digit matcher used to fire inside the date and report "15, 2024" too.
    assert unsupported_details("Ordered June 15, 2024.", "nothing relevant") == ["June 15, 2024"]
