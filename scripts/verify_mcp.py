"""Exercise the MCP transport end to end with the official client (no phone call placed).

Checks that every host-facing tool is exposed, runs the legacy scripted speech
round-trip, and starts an autonomous demo rehearsal through the one-shot tools,
following it to completion. Private artifacts stay under runs/.
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED = {
    "phone_doctor",
    "phone_state",
    "phone_plan_draft",
    "phone_call_start",
    "phone_call_wait",
    "phone_call_status",
    "phone_hangup",
    "phone_call_review",
    "phone_result",
    "phone_test_mode",
    "phone_test_prepare",
    "phone_test_start",
    "phone_conversation_wait",
    "phone_test_finish",
    "phone_test_stop",
    "phone_prepare",
    "phone_dial_request",
    "phone_connect",
    "phone_conversation_start",
    "phone_recording_start",
    "phone_recording_stop",
    "phone_say",
    "phone_listen",
    "phone_keypad",
    "phone_interrupt",
    "phone_finish",
    "phone_simulate_remote",
}


async def main(full_demo: bool):
    root = Path(__file__).resolve().parents[1]
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "talk2myagent", "mcp"],
        env={"T2MA_ROOT": str(root), "HF_HUB_OFFLINE": "1"},
    )
    async with (
        stdio_client(parameters) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        listing = await session.list_tools()
        names = {tool.name for tool in listing.tools}
        missing = EXPECTED - names
        assert not missing, f"Missing tools: {missing}"

        async def call(name, arguments):
            result = await session.call_tool(name, arguments)
            assert not result.isError, result
            return json.loads(result.content[0].text)

        doctor = await call("phone_doctor", {})
        demo_plan = json.loads((root / "examples/demo-request.json").read_text())["plan"]

        # Legacy manual flow: scripted speech round-trip.
        prepared = await call("phone_prepare", {"plan": demo_plan, "mode": "demo"})
        cid = prepared["call_id"]
        await call("phone_connect", {"call_id": cid, "plan_id": prepared["plan_id"]})
        await call(
            "phone_recording_start",
            {"call_id": cid, "consent_basis": "Synthetic MCP verification; no real participants."},
        )
        await call(
            "phone_say", {"call_id": cid, "text": "Can you help return a damaged coffee grinder?"}
        )
        heard = await call(
            "phone_simulate_remote",
            {"call_id": cid, "text": "Yes. I can help you return that damaged coffee grinder."},
        )
        assert "grinder" in heard["event"]["text"].lower()
        legacy = await call(
            "phone_finish",
            {"call_id": cid, "outcome": "completed", "summary": "MCP transport verification."},
        )
        assert Path(legacy["recording_path"]).exists() and legacy["simulated"]

        # Role-play preparation never opens the microphone.
        roleplay = await call(
            "phone_test_prepare",
            {
                "scenario": {
                    "user_request": "Ask a bicycle mechanic whether ticket B-42 is ready.",
                    "recipient_role": "Bicycle mechanic",
                    "customer_name": "Morgan",
                    "objective": "Find the repair status",
                    "facts": {"ticket": "B-42"},
                    "greeting": "Hello, I am calling for Morgan about bicycle repair B-42. Is it ready?",
                    "dialogue": {"ticket": "B-42"},
                    "allowed_actions": ["Ask for repair status"],
                    "stop_conditions": ["Developer stops the test"],
                    "success_criteria": ["Recipient confirms repair status"],
                }
            },
        )
        stopped = await call("phone_test_stop", {"call_id": roleplay["call_id"]})
        assert stopped["test_mode"] and stopped["outcome"] == "cancelled"

        # One-shot autonomous flow in demo mode: starts immediately, streams events.
        started = await call("phone_call_start", {"plan": demo_plan, "mode": "demo"})
        assert started["phase"] in {"preparing", "talking"}
        with_live_number = dict(demo_plan, is_demo=False)
        refused = await session.call_tool(
            "phone_call_start", {"plan": with_live_number, "mode": "live", "authorized": False}
        )
        assert refused.isError, "Unauthorized live calls must be refused"
        cursor = 0
        result = None
        if full_demo:
            while True:
                update = await call(
                    "phone_call_wait",
                    {"call_id": started["call_id"], "after_seq": cursor, "timeout_seconds": 25},
                )
                cursor = update["cursor"]
                if update["done"]:
                    result = update["result"]
                    break
            assert result["simulated"] and result["review_required"]
        else:
            hung = await call("phone_hangup", {"call_id": started["call_id"]})
            assert hung["state"] in {"cancelled", "finishing", "needs_user", "failed"}
        print(
            json.dumps(
                {
                    "verified_tools": len(names),
                    "live_call_ready": doctor["live_call_ready"],
                    "accessibility_enabled": doctor["accessibility_enabled"],
                    "legacy_recording": legacy["recording_path"],
                    "demo_call_id": started["call_id"],
                    "demo_outcome": result["outcome"] if result else "hung up early",
                    "demo_latency": result["latency"] if result else None,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    asyncio.run(main(full_demo="--full" in sys.argv))
