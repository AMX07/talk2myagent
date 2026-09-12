from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

from .config import ROOT, private_dir, settings


def download_models():
    from huggingface_hub import snapshot_download

    from .vad import download_vad

    folder = private_dir(ROOT / "models")
    for name in ["kokoro-v1.0.onnx", "voices-v1.0.bin"]:
        path = folder / name
        if not path.exists():
            print(f"Downloading {name} from the upstream release…", flush=True)
            part = path.with_suffix(path.suffix + ".partial")
            urllib.request.urlretrieve(
                f"https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/{name}",
                part,
            )
            part.replace(path)
    print(snapshot_download(settings().stt_model), flush=True)
    print(download_vad(), flush=True)
    print("Speech models ready. Inference runs locally.")


def load_json_argument(value: str) -> dict:
    data = Path(value[1:]).read_text() if value.startswith("@") else value
    return json.loads(data)


def follow_call(call_id: str, quiet: bool = False) -> dict:
    """Print transcript lines as they happen and return the final result."""
    from .client import request

    cursor = 0
    while True:
        update = request("call_wait", call_id=call_id, after_seq=cursor, timeout_seconds=25)
        for event in update["events"]:
            if event.get("kind") == "latency":
                if not quiet:
                    latency = event.get("speech_end_to_first_audio_seconds")
                    processing = event.get("processing_seconds_to_first_audio")
                    parts = []
                    if latency is not None:
                        parts.append(f"{latency:.2f}s from their last word to first reply audio")
                    if processing is not None:
                        parts.append(f"{processing:.2f}s thinking+synthesis")
                    if parts:
                        print("        ⏱ " + "; ".join(parts))
                continue
            if event["speaker"] == "system" and quiet:
                continue
            tag = {"agent": "AGENT ", "remote": "THEM  ", "system": "      "}[event["speaker"]]
            print(f"[{event['at']:6.1f}s] {tag} {event['text']}", flush=True)
        cursor = update["cursor"]
        if update["done"]:
            return update["result"]
        if not update["events"] and not quiet:
            print(f"        … phase={update['phase']} state={update['state']}", flush=True)


def print_result(result: dict) -> None:
    latency = result.get("latency") or {}
    print("\n=== RESULT ===")
    print(f"outcome: {result['outcome']} (source: {result.get('outcome_source')})")
    print(f"summary: {result['summary']}")
    if result.get("review_required"):
        print("review_required: the host agent should judge the criteria with call_review.")
    if latency.get("median_speech_end_to_first_audio_seconds") is not None:
        print(
            f"latency: median {latency['median_speech_end_to_first_audio_seconds']}s, "
            f"max {latency['max_speech_end_to_first_audio_seconds']}s over {latency['turns']} turns"
        )
    if latency.get("median_processing_seconds_to_first_audio") is not None:
        print(
            f"processing: median {latency['median_processing_seconds_to_first_audio']}s "
            "from transcript to first reply audio"
        )
    if result.get("needs_phone_hangup"):
        print("!! The Phone call may still be connected. End it in the Phone app now.")
    for key in ("recording_path", "transcript_path", "report_path"):
        if result.get(key):
            print(f"{key}: {result[key]}")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Local phone-call agent for macOS")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="Run local service on a private Unix socket")
    sub.add_parser("mcp", help="Run the MCP bridge over stdio (Codex, OpenCode, Claude Code)")
    sub.add_parser("doctor", help="Inspect audio, models, and permissions")
    sub.add_parser("models", help="Download open speech models")
    sub.add_parser("conversation-model", help="Download the configured local conversation model")
    sub.add_parser("install", help="Register the MCP server and skill with OpenCode/Claude/Codex")
    sub.add_parser("phone-ui", help="Dump the Phone app's accessibility labels (diagnostic)")
    sub.add_parser("phone-state", help="Read the Phone app's current call state")
    sub.add_parser("audio-restore", help="Restore audio defaults saved before a call")

    plan = sub.add_parser("plan", help="Draft a call plan with the local model")
    plan.add_argument("--task", required=True)
    plan.add_argument("--number", required=True, help="E.164, e.g. +18882804331")
    plan.add_argument("--source", required=True, help="Where the number was verified")
    plan.add_argument("--name", required=True, help="Customer name")
    plan.add_argument("--company", required=True)
    plan.add_argument("--fact", action="append", default=[], help="key=value, repeatable")
    plan.add_argument("--out", type=Path, help="Write the plan JSON here")

    call = sub.add_parser("call", help="Place and conduct a whole call from a plan")
    call.add_argument("--plan", required=True, help="Plan JSON, or @/path/to/plan.json")
    call.add_argument("--demo", action="store_true", help="Rehearse against a local persona")
    call.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    call.add_argument("--no-monitor", action="store_true", help="Do not play the call aloud")
    call.add_argument("--recording", choices=["ask", "off", "on"], default="on")
    call.add_argument("--play", action="store_true", help="Demo only: play both voices aloud")
    call.add_argument("--recipient-brief", help="Demo only: persona instructions")

    hangup = sub.add_parser("hangup", help="End a call started with `call`")
    hangup.add_argument("call_id")

    sub.add_parser("live", help="Open the live call view in a browser and keep it running")

    roleplay = sub.add_parser(
        "roleplay", help="Talk to the agent yourself: you play the person it calls"
    )
    roleplay.add_argument("--scenario", help="Scenario JSON, or @/path/to/scenario.json")
    roleplay.add_argument("--task", help="What the agent should accomplish (drafted locally)")
    roleplay.add_argument("--recipient", help="Who you will play, e.g. 'Amazon support'")
    roleplay.add_argument("--name", help="The customer the agent acts for")
    roleplay.add_argument("--fact", action="append", default=[], help="key=value, repeatable")
    roleplay.add_argument("--headphones", action="store_true", help="Allow interrupting the agent")
    roleplay.add_argument("--no-view", action="store_true", help="Skip the browser view")

    sub.add_parser("test", help="Enter human role-play; inspect devices and wait for a task")
    stop = sub.add_parser("test-stop", help="Stop the active human role-play")
    stop.add_argument("call_id", nargs="?")

    demo = sub.add_parser("demo", help="Autonomous rehearsal with a local simulated rep")
    demo.add_argument("--action", choices=["return", "cancel"], default="return")
    demo.add_argument("--play", action="store_true", help="Play both voices aloud")
    demo.add_argument("--scripted", action="store_true", help="Run the fixed regression script")

    req = sub.add_parser("request", help="Invoke the persistent local service without MCP")
    req.add_argument("operation")
    req.add_argument("--json", default="{}", help="Arguments JSON, or @/absolute/path.json")
    transcribe = sub.add_parser("transcribe", help="Transcribe an existing local audio file")
    transcribe.add_argument("file", type=Path)
    test = sub.add_parser(
        "loopback-test", help="Verify both virtual audio buses without making a call"
    )
    test.add_argument("--seconds", type=float, default=1)
    args = parser.parse_args()
    try:
        if args.command == "serve":
            from .service import serve

            serve()
        elif args.command == "mcp":
            from .mcp_server import main as mcp_main

            mcp_main()
        elif args.command == "models":
            download_models()
        elif args.command == "conversation-model":
            from .conversation import resolve_model

            print(resolve_model(settings().conversation_model, download=True))
        elif args.command == "install":
            from .install import install_all

            print(json.dumps(install_all(), indent=2))
        elif args.command == "phone-ui":
            from .macphone import phone_ui_dump

            out = private_dir(ROOT / ".runtime") / "phone-ui.json"
            report = phone_ui_dump(out)
            print(json.dumps(report["state"], indent=2))
            print(f"Full element dump: {out}")
        elif args.command == "phone-state":
            from .client import request

            print(json.dumps(request("phone_state"), indent=2))
        elif args.command == "audio-restore":
            from .client import request

            print(json.dumps(request("audio_restore"), indent=2))
        elif args.command == "plan":
            from .client import request

            facts = dict(item.split("=", 1) for item in args.fact)
            result = request(
                "plan_from_task",
                task=args.task,
                phone_number=args.number,
                phone_source=args.source,
                customer_name=args.name,
                company=args.company,
                facts=facts,
            )
            text = json.dumps(result["plan"], indent=2)
            if args.out:
                args.out.write_text(text + "\n")
                print(f"Plan written to {args.out}")
            print(text)
        elif args.command == "call":
            from .client import request

            plan = load_json_argument(args.plan)
            mode = "demo" if args.demo else "live"
            print(json.dumps(plan, indent=2))
            if mode == "live" and not args.yes:
                answer = input(
                    f"\nDial {plan.get('phone_number')} ({plan.get('company')}) and share the "
                    "facts above with them? [y/N] "
                )
                if answer.strip().lower() not in {"y", "yes"}:
                    raise SystemExit("Not authorized; nothing dialed.")
            started = request(
                "call_start",
                plan=plan,
                mode=mode,
                authorized=mode == "live",
                recording=args.recording,
                monitor=not args.no_monitor,
                play=args.play,
                recipient_brief=args.recipient_brief,
            )
            print(f"\ncall {started['call_id']} started (mode={mode}); Ctrl-C hangs up.\n")
            try:
                result = follow_call(started["call_id"])
            except KeyboardInterrupt:
                print("\nHanging up…")
                request("hangup", call_id=started["call_id"])
                result = request("result", call_id=started["call_id"])
            print_result(result)
        elif args.command == "hangup":
            from .client import request

            print(json.dumps(request("hangup", call_id=args.call_id), indent=2))
        elif args.command == "live":
            from .live import start_viewer

            server, url = start_viewer()
            print(f"Live call view: {url}\nLeave this running; press Ctrl-C to close it.")
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                server.shutdown()
                print("\nViewer closed.")
        elif args.command == "roleplay":
            from .client import request

            if args.scenario:
                scenario = load_json_argument(args.scenario)
                prepared = request("test_prepare", scenario=scenario)
                args.recipient = scenario["recipient_role"]
            elif args.task and args.recipient and args.name:
                print("Drafting the call plan locally…", flush=True)
                prepared = request(
                    "roleplay_from_task",
                    task=args.task,
                    recipient_role=args.recipient,
                    customer_name=args.name,
                    facts=dict(item.split("=", 1) for item in args.fact),
                )
            else:
                raise ValueError("Pass --scenario, or --task with --recipient and --name.")
            print(json.dumps(prepared["plan"], indent=2))
            url = None
            if not args.no_view:
                from .live import start_viewer

                _server, url = start_viewer()
            started = request(
                "test_start",
                call_id=prepared["call_id"],
                plan_id=prepared["plan_id"],
                controller="local",
                audio_mode="headphones" if args.headphones else "speakers",
            )
            print(f"\nRole-play {started['call_id']} live on {started['audio']['input_device']}.")
            if url:
                print(f"Live view: {url}")
            print(
                "You are now the "
                + args.recipient
                + '. Speak after the agent stops. Say "stop test" to end.\n'
            )
            try:
                result = follow_call(started["call_id"])
            except KeyboardInterrupt:
                print("\nStopping the test…")
                request("test_stop", call_id=started["call_id"])
                result = request("result", call_id=started["call_id"])
            print_result(result)
        elif args.command == "test":
            from .client import request

            print(json.dumps(request("test_status"), indent=2))
            print(
                '\nTest mode ready. In your agent, say "Enter test mode", then describe the task.\n'
                "The agent prepares a plan and starts speaking. Ask it to wait if you want to review first.\n"
                'Use "stop test" aloud or run: uv run t2ma test-stop'
            )
        elif args.command == "test-stop":
            from .client import request

            print(json.dumps(request("test_stop", call_id=args.call_id), indent=2))
        elif args.command == "doctor":
            from .engine import Engine

            e = Engine(settings())
            try:
                print(json.dumps(e.doctor(), indent=2))
            finally:
                e.shutdown()
        elif args.command == "demo":
            if args.scripted:
                from .demo import run_demo
                from .engine import Engine

                engine = Engine(settings())
                try:
                    run_demo(engine, args.action)
                finally:
                    engine.shutdown()
            else:
                from .client import request
                from .plans import amazon_plan

                plan = amazon_plan(
                    args.action,
                    "Alex Demo",
                    "DEMO-1234",
                    "coffee grinder",
                    "It arrived damaged.",
                    "+12025550123",
                    "Reserved fictional demonstration number; never dial.",
                    is_demo=True,
                )
                started = request("call_start", plan=plan.model_dump(), mode="demo", play=args.play)
                print(f"Autonomous rehearsal {started['call_id']} (no call placed)\n")
                begun = time.monotonic()
                result = follow_call(started["call_id"])
                print_result(result)
                print(f"wall_clock_seconds: {time.monotonic() - begun:.1f}")
        elif args.command == "request":
            from .client import request

            print(json.dumps(request(args.operation, **load_json_argument(args.json)), indent=2))
        elif args.command == "transcribe":
            from .speech import Speech

            print(json.dumps(Speech(settings()).transcribe_file(args.file.resolve()), indent=2))
        elif args.command == "loopback-test":
            from .diagnostics import loopback_test

            result = loopback_test(settings(), args.seconds)
            print(json.dumps(result, indent=2))
            if not result["passed"]:
                raise SystemExit(1)
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"talk2myagent: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
