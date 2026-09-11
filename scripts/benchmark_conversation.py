"""Replay a saved human session through local STT -> LLM -> TTS, without mic/playback.

This measures processing, not a new human interaction or audible end-to-end latency.
All private input and output remain in the ignored runs directory.
"""

import argparse
import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import soundfile as sf

from talk2myagent.config import ROOT, private_dir, settings, write_json
from talk2myagent.conversation import LocalConversation
from talk2myagent.plans import CallPlan
from talk2myagent.speech import Speech


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("--model", help="Override the local model for this benchmark")
    args = parser.parse_args()
    folder = args.session.resolve()
    saved = json.loads((folder / "session.json").read_text())
    original = json.loads((folder / "transcript.json").read_text())["events"]
    plan = CallPlan.model_validate(saved["plan"])
    offset = next(
        e["at"]
        for e in original
        if e["speaker"] == "system" and e["text"].startswith("Recording started")
    )
    audio, rate = sf.read(folder / "remote.wav", dtype="float32")
    config = settings()
    if args.model:
        config.conversation_model = args.model
    speech = Speech(config)
    brain = LocalConversation(config, speech.lock)
    brain.ready()
    speech.synthesize("Ready.")
    speech.transcribe(audio[:rate], rate)
    events = [{"seq": 1, "speaker": "agent", "text": plan.opening}]
    measurements = []
    out = private_dir(ROOT / "runs" / ("benchmark-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S")))
    for old in [e for e in original if e["speaker"] == "remote"]:
        start = max(0, round((old["at"] - offset) * rate))
        # Legacy sessions capped each recorded turn at 20 s; newer events have duration metadata.
        duration = old.get("segment_seconds", 20)
        clip = audio[start : start + round(duration * rate)]
        started = time.monotonic()
        recognized = speech.transcribe(clip, rate)
        events.append({"seq": len(events) + 1, "speaker": "remote", "text": recognized["text"]})
        reply, metrics = brain.respond(plan, events, threading.Event())
        synth_start = time.monotonic()
        generated, output_rate = speech.synthesize(reply.say)
        synth_seconds = time.monotonic() - synth_start
        row = {
            "recipient_seq": events[-1]["seq"],
            "recognized": recognized["text"],
            "reply": reply.model_dump(),
            "stt_seconds": recognized["inference_seconds"],
            **metrics,
            "synthesis_seconds": round(synth_seconds, 3),
            "processing_seconds": round(time.monotonic() - started, 3),
        }
        measurements.append(row)
        events.append({"seq": len(events) + 1, "speaker": "agent", "text": reply.say})
        sf.write(out / f"reply-{len(measurements)}.wav", generated, output_rate)
        write_json(
            out / "benchmark.json",
            {
                "source": str(folder),
                "mode": "offline_recorded_audio_replay",
                "microphone_opened": False,
                "audio_played": False,
                "measurements": measurements,
            },
        )
        print(json.dumps(row), flush=True)
    print(json.dumps({"benchmark_path": str(out / "benchmark.json")}), flush=True)


if __name__ == "__main__":
    main()
