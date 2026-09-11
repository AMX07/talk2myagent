from __future__ import annotations

import json

from .engine import Engine
from .plans import amazon_plan


def run_demo(engine: Engine, action: str = "return") -> dict:
    plan = amazon_plan(action, "Alex Demo", "DEMO-1234", "coffee grinder", "It arrived damaged.",
                       "+12025550123", "Reserved fictional demonstration number; never dial.", is_demo=True)
    prepared = engine.prepare(plan.model_dump(), "demo")
    call_id = prepared["call_id"]
    print(f"Simulated Amazon {action} · {call_id}", flush=True)
    engine.connect(call_id, prepared["plan_id"])
    try:
        engine.say(call_id, plan.opening)
        heard = engine.simulate_remote(call_id, "Yes, recording is fine for this simulated call. How can I help?")
        print("Support:", heard["event"]["text"], flush=True)
        engine.recording_start(call_id, "Synthetic rehearsal only; no real participants.")
        engine.say(call_id, plan.dialogue["after_recording_consent"])
        heard = engine.simulate_remote(call_id, "I can help with that. What is the order number?")
        print("Support:", heard["event"]["text"], flush=True)
        engine.say(call_id, plan.dialogue["order_id"])
        if action == "return":
            response = "The return is eligible with no fee. A prepaid label will be sent to the account email. Shall I proceed?"
            final = "The return is confirmed. Your case number is DEMO 4821. Please use the prepaid label within thirty days."
        else:
            response = "This item has not shipped. I can cancel it with no fee. Shall I proceed?"
            final = "The cancellation is confirmed. Your case number is DEMO 4821. A confirmation will appear in your account."
        heard = engine.simulate_remote(call_id, response)
        print("Support:", heard["event"]["text"], flush=True)
        engine.say(call_id, f"Yes, please {action} that exact coffee grinder with no fee.")
        heard = engine.simulate_remote(call_id, final)
        print("Support:", heard["event"]["text"], flush=True)
        engine.say(call_id, "Thank you. I will pass the confirmation and next steps to Alex.")
        result = engine.finish(call_id, "completed", f"Scripted rehearsal of Amazon {action}. No order was changed and no call was placed.")
        print(json.dumps({k: v for k, v in result.items() if k != "transcript"}, indent=2), flush=True)
        return result
    except BaseException:
        engine.finish(call_id, "failed", "Rehearsal failed or was interrupted; inspect transcript and logs.")
        raise
