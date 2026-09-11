"""Exercise the installed transport and private daemon using the official MCP client."""

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    root = Path(__file__).resolve().parents[1]
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "talk2myagent", "mcp"],
        env={"T2MA_ROOT": str(root)},
    )
    async with (
        stdio_client(parameters) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        listing = await session.list_tools()
        names = {tool.name for tool in listing.tools}
        assert {"phone_prepare", "phone_say", "phone_listen", "phone_finish"} <= names

        async def call(name, arguments):
            result = await session.call_tool(name, arguments)
            assert not result.isError, result
            return json.loads(result.content[0].text)

        prepared = await call(
            "phone_prepare", json.loads((root / "examples/demo-request.json").read_text())
        )
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
        events = await call("phone_listen", {"call_id": cid, "timeout_seconds": 0})
        assert any(e["speaker"] == "remote" for e in events["events"])
        result = await call(
            "phone_finish",
            {
                "call_id": cid,
                "outcome": "completed",
                "summary": "MCP transport and local speech verification only.",
            },
        )
        assert Path(result["recording_path"]).exists()
        assert result["simulated"]
        assert {
            "phone_test_mode",
            "phone_test_prepare",
            "phone_test_start",
            "phone_test_finish",
            "phone_test_stop",
        } <= names
        test_status = await call("phone_test_mode", {})
        assert not test_status["opened_by_this_request"]
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
        assert roleplay["phase"] == "ready" and not roleplay["dialing_enabled"]
        stopped = await call("phone_test_stop", {"call_id": roleplay["call_id"]})
        assert stopped["test_mode"] and stopped["outcome"] == "cancelled"
        assert not stopped["recording_path"]  # Preparation does not open the mic.
        print(
            json.dumps(
                {
                    "verified_tools": len(names),
                    "call_id": cid,
                    "recognized": heard["event"]["text"],
                    "recording": result["recording_path"],
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
