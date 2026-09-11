from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

from .config import ROOT, private_dir, settings


def download_models():
    from huggingface_hub import snapshot_download

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
    print("Speech models ready. Inference runs locally.")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Local phone audio tools for Codex")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="Run local service on a private Unix socket")
    sub.add_parser("mcp", help="Run the Codex MCP bridge over stdio")
    sub.add_parser("doctor", help="Inspect audio setup")
    sub.add_parser("models", help="Download open speech models")
    sub.add_parser(
        "test", help="Enter human role-play; inspect devices and wait for a task in Codex"
    )
    stop = sub.add_parser(
        "test-stop", help="Stop the active human role-play and close its microphone"
    )
    stop.add_argument("call_id", nargs="?")
    demo = sub.add_parser("demo", help="Run a spoken, transcribed simulation; never places a call")
    demo.add_argument("--action", choices=["return", "cancel"], default="return")
    req = sub.add_parser("request", help="Invoke the persistent local tools without MCP")
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
        elif args.command == "test":
            from .client import request

            print(json.dumps(request("test_status"), indent=2))
            print(
                '\nTest mode ready. In Codex, say "Enter test mode", then describe the task.\n'
                'The agent prepares a plan. Say "Start test" when ready to play the recipient.\n'
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
            from .demo import run_demo
            from .engine import Engine

            engine = Engine(settings())
            try:
                run_demo(engine, args.action)
            finally:
                engine.shutdown()
        elif args.command == "request":
            from .client import request

            data = Path(args.json[1:]).read_text() if args.json.startswith("@") else args.json
            print(json.dumps(request(args.operation, **json.loads(data)), indent=2))
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
