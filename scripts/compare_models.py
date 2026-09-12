"""Run the same call turns through two or more local models and compare them.

Measures what the call actually waits on: time to the first spoken sentence,
not total generation. Also prints each reply so role confusion and invented
facts are visible side by side. No audio, no microphone, no call.

    uv run python scripts/compare_models.py Qwen/Qwen3-8B-MLX-4bit lmstudio-community/gemma-4-31B-it-MLX-4bit
"""

import argparse
import json
import tempfile
import threading
import time
from pathlib import Path

from talk2myagent.config import ROOT, settings
from talk2myagent.conversation import LocalConversation, unsupported_details
from talk2myagent.memory import Memory, summarize
from talk2myagent.plans import TestScenario

# The turns that exposed weaknesses in the live role-play, plus a policy squeeze.
TURNS = [
    "Thanks for calling Amazon, this is Michael. Can I get your name?",
    "Before I do that, can you please confirm your date of birth and the account holder's phone number?",
    # Only memory can answer this, so it exercises the lookup path end to end.
    "Thanks. And what date was it delivered? I need that to check the return window.",
    "Okay, I found the order. Unfortunately I can only offer a fifty dollar gift card, not a refund.",
    "Alright, I can do the full refund to the original card. It will take five business days.",
]

# The one fact no plan carries, phrased the way a record would be, not the way
# the agent will say it back. That difference is the point.
REMEMBERED = "The coffee grinder was delivered on 3 September 2026."


def scenario_plan():
    raw = json.loads((ROOT / "examples/roleplay-amazon-return.json").read_text())
    plan = TestScenario.model_validate(raw).call_plan()
    # The delivery date has to be absent from the plan, or the memory turn is
    # answered from facts and never exercises a lookup at all.
    plan.facts = {k: v for k, v in plan.facts.items() if k != "delivered"}
    return plan


def run(model_name: str, plan) -> dict:
    config = settings()
    config.conversation_model = model_name
    brain = LocalConversation(config, threading.Lock())
    started = time.monotonic()
    brain.ready()
    load_seconds = time.monotonic() - started
    prefix = brain.prepare(plan)

    events = [{"seq": 1, "speaker": "agent", "text": plan.opening}]
    rows, cancel = [], threading.Event()
    allowed = json.dumps(plan.model_dump())
    store = Memory(Path(tempfile.mkdtemp()))
    store.add(REMEMBERED)
    store.add_facts(plan.facts)
    lookups = []
    for text in TURNS:
        events.append({"seq": len(events) + 1, "speaker": "remote", "text": text})
        first_sentence: list[float] = []
        began = time.monotonic()
        try:
            reply, timing = brain.respond(
                plan,
                events,
                cancel,
                on_sentence=lambda _s, mark=first_sentence: mark.append(time.monotonic()),
            )
        except Exception as exc:  # noqa: BLE001 - a failure is a result worth reporting
            rows.append({"heard": text, "error": f"{type(exc).__name__}: {exc}"[:160]})
            break
        spoken = reply.spoken
        rows.append(
            {
                "heard": text,
                "said": spoken,
                "status": reply.status,
                "asked_customer": reply.question or None,
                "first_sentence_seconds": round(
                    (first_sentence[0] - began) if first_sentence else 0, 3
                ),
                "generation_seconds": timing["generation_seconds"],
                "first_token_seconds": timing["first_token_seconds"],
                "invented": unsupported_details(spoken, allowed + " " + " ".join(TURNS)),
            }
        )
        events.append({"seq": len(events) + 1, "speaker": "agent", "text": spoken})

        if reply.lookup:
            # Serve the lookup exactly as the engine does, then let it answer.
            hits = store.search(reply.lookup, 3)
            answer = summarize(hits)
            lookups.append({"query": reply.lookup, "found": bool(hits)})
            events.append(
                {
                    "seq": len(events) + 1,
                    "speaker": "memory",
                    "text": answer,
                    "query": reply.lookup,
                }
            )
            first_sentence = []
            began = time.monotonic()
            reply, timing = brain.respond(
                plan,
                events,
                cancel,
                on_sentence=lambda _s, mark=first_sentence: mark.append(time.monotonic()),
            )
            spoken = reply.spoken
            rows.append(
                {
                    "heard": f"[memory] {answer}",
                    "said": spoken,
                    "status": reply.status,
                    "asked_customer": reply.question or None,
                    "first_sentence_seconds": round(
                        (first_sentence[0] - began) if first_sentence else 0, 3
                    ),
                    "generation_seconds": timing["generation_seconds"],
                    "first_token_seconds": timing["first_token_seconds"],
                    "invented": unsupported_details(
                        spoken, allowed + " " + " ".join(TURNS) + " " + answer
                    ),
                    "used_memory": REMEMBERED.split(" on ")[-1].rstrip(".").lower()
                    in spoken.lower()
                    or "september" in spoken.lower(),
                }
            )
            events.append({"seq": len(events) + 1, "speaker": "agent", "text": spoken})
    good = [r for r in rows if "said" in r]
    return {
        "model": model_name,
        "load_seconds": round(load_seconds, 2),
        "prefix_tokens": prefix.get("prefix_tokens"),
        "prefill_seconds": prefix.get("prefill_seconds"),
        "turns": rows,
        "median_first_sentence_seconds": round(
            sorted(r["first_sentence_seconds"] for r in good)[len(good) // 2], 3
        )
        if good
        else None,
        "invented_details": sum(len(r["invented"]) for r in good),
        "asked_memory": len(lookups),
        "memory_found": sum(1 for entry in lookups if entry["found"]),
        "stated_what_memory_returned": any(r.get("used_memory") for r in good),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("models", nargs="+")
    parser.add_argument("--out", type=Path, help="Write the full comparison JSON here")
    args = parser.parse_args()
    plan = scenario_plan()
    results = []
    for name in args.models:
        print(f"\n{'=' * 78}\n{name}\n{'=' * 78}", flush=True)
        try:
            result = run(name, plan)
        except Exception as exc:  # noqa: BLE001 - report and move to the next model
            print(f"  unavailable: {type(exc).__name__}: {str(exc)[:200]}")
            continue
        results.append(result)
        print(
            f"  load {result['load_seconds']}s · prefix {result['prefix_tokens']} tokens"
            f" · prefill {result['prefill_seconds']}s"
        )
        for row in result["turns"]:
            if "error" in row:
                print(f"  THEM  {row['heard'][:72]}\n  FAIL  {row['error']}")
                continue
            print(f"\n  THEM  {row['heard'][:74]}")
            print(f"  AGENT {row['said'][:150]}")
            flags = []
            if row["asked_customer"]:
                flags.append(f"asks you: {row['asked_customer'][:60]}")
            if row["invented"]:
                flags.append(f"INVENTED {row['invented']}")
            print(
                f"        {row['first_sentence_seconds']}s to first sentence"
                f" · {row['generation_seconds']}s total · {row['status']}"
                + ("  · " + " · ".join(flags) if flags else "")
            )
        print(
            f"\n  MEDIAN first sentence: {result['median_first_sentence_seconds']}s"
            f" · invented details: {result['invented_details']}"
        )
    if args.out and results:
        args.out.write_text(json.dumps(results, indent=2) + "\n")
        print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
