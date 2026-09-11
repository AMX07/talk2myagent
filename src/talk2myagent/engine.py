from __future__ import annotations

import html
import json
import queue
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import numpy as np
import soundfile as sf

from .audio import AudioBridge, devices, dtmf
from .config import ROOT, Settings, private_dir, write_json
from .plans import CallPlan
from .speech import Speech, resample


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class Session:
    def __init__(self, folder: Path, plan: CallPlan, mode: str):
        self.folder, self.plan, self.mode = folder, plan, mode
        self.id = folder.name
        self.state = "prepared"
        self.created_at = utc()
        self.started = time.monotonic()
        self.last_touch = self.started
        self.condition = threading.Condition(threading.RLock())
        self.events: list[dict] = []
        self.bridge: AudioBridge | None = None
        self.segments: queue.Queue = queue.Queue(maxsize=120)
        self.transcriber: threading.Thread | None = None
        self.record_started: float | None = None
        self.record_stopped: float | None = None
        self.recording = False
        self.recording_basis = ""
        self.outgoing: list[tuple[float, Path, float]] = []
        self.remote_demo: list[tuple[float, np.ndarray, int]] = []
        self.demo_clock = 0.0
        self.result: dict | None = None
        self.save()

    def elapsed(self) -> float:
        return self.demo_clock if self.mode == "demo" else time.monotonic() - self.started

    def save(self):
        with self.condition:
            write_json(self.folder / "session.json", {
                "call_id": self.id, "state": self.state, "mode": self.mode,
                "created_at": self.created_at, "plan": self.plan.model_dump(),
                "plan_id": self.plan.fingerprint(), "recording": self.recording,
                "recording_basis": self.recording_basis,
            })

    def event(self, speaker: str, text: str, at: float | None = None, **extra) -> dict:
        with self.condition:
            event = {"seq": len(self.events) + 1, "at": round(self.elapsed() if at is None else max(at, 0), 3),
                     "speaker": speaker, "text": text, **extra}
            self.events.append(event)
            with (self.folder / "events.jsonl").open("a") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
            self.condition.notify_all()
            return event

    def error(self, message: str):
        self.event("system", message, kind="error")


class Engine:
    def __init__(self, config: Settings | None = None, root: Path | None = None, speech=None):
        self.config = config or Settings()
        self.root = private_dir((root or ROOT) / "runs")
        self.speech = speech or Speech(self.config)
        self.sessions: dict[str, Session] = {}
        self.lock = threading.RLock()
        self.closed = threading.Event()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def doctor(self) -> dict:
        audio = devices()
        available = {d["name"] for d in audio}
        missing = [n for n in [self.config.input_device, self.config.output_device] if n not in available]
        return {
            "devices": audio, "settings": self.config.model_dump(),
            "missing_audio_devices": missing,
            "kokoro_downloaded": all((ROOT / "models" / f).exists() for f in ["kokoro-v1.0.onnx", "voices-v1.0.bin"]),
            "live_audio_devices_present": not missing,
            "routing_verified": False,
            "next_step": "Verify Phone microphone = outgoing bus and Phone/system output = incoming bus. Run loopback-test before a call.",
        }

    def prepare(self, plan: dict, mode: Literal["demo", "live"] = "demo") -> dict:
        plan_obj = CallPlan.model_validate(plan)
        if mode not in ("demo", "live"):
            raise ValueError("Mode must be demo or live.")
        if mode == "live" and plan_obj.is_demo:
            raise ValueError("A demo plan with fictional facts cannot be used for a real call.")
        with self.lock:
            call_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
            folder = private_dir(self.root / call_id)
            call = Session(folder, plan_obj, mode)
            self.sessions[call_id] = call
            call.event("system", "Plan prepared; no phone call placed.")
            return {"call_id": call_id, "plan_id": plan_obj.fingerprint(), "plan": plan_obj.model_dump(),
                    "mode": mode, "state": call.state, "folder": str(folder)}

    def get(self, call_id: str) -> Session:
        if call_id not in self.sessions:
            raise ValueError("Unknown/inactive call. Use result to recover saved artifacts after a service restart.")
        call = self.sessions[call_id]
        call.last_touch = time.monotonic()
        return call

    def connect(self, call_id: str, plan_id: str, authorized: bool = False,
                connected: bool = False, routing_verified: bool = False) -> dict:
        with self.lock:
            call = self.get(call_id)
            if call.state != "prepared":
                raise ValueError("Call has already started or finished; connect is not retryable.")
            if call.plan.fingerprint() != plan_id:
                raise ValueError("Plan changed; use the exact reviewed plan_id.")
            if call.mode == "live":
                if not (authorized and connected and routing_verified):
                    raise ValueError("Live audio requires authorization, an observed connected Phone call, and verified routing.")
                if any(s.state == "active" and s.mode == "live" for s in self.sessions.values()):
                    raise ValueError("Only one live call may own the audio devices.")
                bridge = AudioBridge(self.config, lambda a, at: self._enqueue(call, a, at), call.error)
                try:
                    bridge.start()
                except Exception:
                    bridge.close()
                    raise
                call.bridge = bridge
                call.transcriber = threading.Thread(target=self._transcribe, args=(call,), daemon=True)
                call.transcriber.start()
            call.state = "active"
            call.event("system", "Audio session connected. Recording is off until consent is recorded.")
            call.save()
            return {"call_id": call.id, "state": call.state, "recording": False, "opening": call.plan.opening}

    def dial_request(self, call_id: str, plan_id: str, authorized: bool = False) -> dict:
        call = self.get(call_id)
        if call.state != "prepared" or call.plan.fingerprint() != plan_id:
            raise ValueError("Dial requires a prepared call and its reviewed plan_id.")
        if not authorized:
            raise ValueError("The user must authorize the recipient, purpose, and facts disclosed.")
        return {
            "call_id": call.id, "phone_number": call.plan.phone_number,
            "phone_source": call.plan.phone_source, "mode": call.mode,
            "call_placed": False,
            "instructions": (
                "DEMO ONLY: use connect; do not dial."
                if call.mode == "demo" else
                "Use computer use: Phone > Keypad; enter phone_number; click Call once. "
                "Inspect the connected call state before connect. Never blindly retry dialing."
            ),
        }

    def _active(self, call_id: str) -> Session:
        call = self.get(call_id)
        if call.state != "active":
            raise ValueError(f"Call is {call.state}, not active.")
        return call

    def _enqueue(self, call: Session, audio: np.ndarray, at: float):
        try:
            call.segments.put_nowait((audio, at))
        except queue.Full:
            call.error("Transcription backlog full; some speech was not transcribed. Raw recording may still contain it.")

    def _transcribe(self, call: Session):
        while True:
            item = call.segments.get()
            try:
                if item is None:
                    return
                audio, at = item
                result = self.speech.transcribe(audio, self.config.sample_rate)
                if result["text"]:
                    call.event("remote", result["text"], at=at-call.started,
                               inference_seconds=result["inference_seconds"], source="whisper")
            except Exception as exc:
                call.error(f"Transcription failed: {exc}")
            finally:
                call.segments.task_done()

    def recording_start(self, call_id: str, consent_basis: str) -> dict:
        with self.lock:
            call = self._active(call_id)
            if len(consent_basis.strip()) < 5:
                raise ValueError("Describe the recording consent actually obtained.")
            if call.record_started is not None:
                raise ValueError("Recording already started. It cannot restart and overwrite prior audio.")
            if call.bridge:
                call.bridge.record(call.folder / "remote.wav")
            call.record_started = call.elapsed()
            call.recording = True
            call.recording_basis = consent_basis
            call.event("system", f"Recording started: {consent_basis}")
            call.save()
            return {"recording": True, "recording_start_seconds": call.record_started}

    def recording_stop(self, call_id: str) -> dict:
        with self.lock:
            call = self._active(call_id)
            if call.bridge:
                call.bridge.stop_recording()
            if call.recording:
                call.record_stopped = call.elapsed()
            call.recording = False
            call.event("system", "Recording stopped.")
            call.save()
            return {"recording": False}

    def say(self, call_id: str, text: str) -> dict:
        if not text.strip() or len(text) > 600:
            raise ValueError("Speak one short turn of 1–600 characters.")
        with self.lock:
            call = self._active(call_id)
            audio, rate = self.speech.synthesize(text)
            at = call.elapsed()
            path = call.folder / f"agent-{uuid.uuid4().hex[:8]}.wav"
            # Preserve the generated waveform, never claim it verifies what the recipient heard.
            sf.write(path, audio, rate, subtype="PCM_16")
            duration = len(audio) / rate
            played = call.bridge.play(audio, rate) if call.bridge else duration
            call.outgoing.append((at, path, played))
            if call.mode == "demo":
                call.demo_clock += duration + 0.35
            call.last_touch = time.monotonic()
            event = call.event("agent", text, at=at, source="synthesis_text", audio_file=path.name,
                               played_seconds=round(played, 3), interrupted=played < duration-0.05)
            return {"event": event, "duration_seconds": duration, "mode": call.mode}

    def interrupt(self, call_id: str) -> dict:
        call = self.get(call_id)
        if call.bridge:
            call.bridge.playback_stop.set()
        return {"playback_stop_requested": True}

    def listen(self, call_id: str, after_seq: int = 0, timeout_seconds: float = 15) -> dict:
        call = self.get(call_id)
        if after_seq < 0 or not 0 <= timeout_seconds <= 25:
            raise ValueError("after_seq must be nonnegative; timeout_seconds must be 0–25.")
        deadline = time.monotonic() + timeout_seconds
        with call.condition:
            while True:
                new = [e for e in call.events if e["seq"] > after_seq]
                if new or call.state != "active" or time.monotonic() >= deadline:
                    return {"call_id": call.id, "state": call.state,
                            "events": new, "cursor": len(call.events), "recording": call.recording,
                            "timed_out": not bool(new)}
                call.condition.wait(timeout=max(0, deadline - time.monotonic()))

    def keypad(self, call_id: str, digits: str) -> dict:
        if not re.fullmatch(r"[0-9*#ABCD]{1,16}", digits):
            raise ValueError("Provide 1–16 DTMF characters: 0–9, *, #, A–D.")
        call = self._active(call_id)
        call.event("system", f"Requested Phone keypad digits: {digits}", kind="keypad_request")
        return {"digits": digits, "sent": False,
                "instructions": "Use computer use to press these digits on the connected Phone call keypad. Prefer this to in-band tones."}

    def tones(self, call_id: str, digits: str) -> dict:
        with self.lock:
            call = self._active(call_id)
            if not re.fullmatch(r"[0-9*#ABCD]{1,16}", digits):
                raise ValueError("Invalid DTMF digits.")
            audio = dtmf(digits, self.config.sample_rate)
            at = call.elapsed()
            played = call.bridge.play(audio, self.config.sample_rate) if call.bridge else len(audio)/self.config.sample_rate
            path = call.folder / f"dtmf-{uuid.uuid4().hex[:8]}.wav"
            sf.write(path, audio, self.config.sample_rate)
            call.outgoing.append((at, path, played))
            if call.mode == "demo":
                call.demo_clock += played + 0.35
            call.event("agent", f"[DTMF {digits}]", at=at, source="tone_generation")
            return {"sent_audio": True, "warning": "In-band tones are experimental; confirm the IVR accepted them. Phone keypad is preferred."}

    def simulate_remote(self, call_id: str, text: str) -> dict:
        with self.lock:
            call = self._active(call_id)
            if call.mode != "demo":
                raise ValueError("Synthetic remote speech is only allowed in demo sessions.")
            if not text.strip() or len(text) > 1200:
                raise ValueError("Provide 1–1200 characters of simulated support dialogue.")
            audio, rate = self.speech.synthesize(text, voice="am_michael")
            at = call.elapsed()
            if call.recording:
                call.remote_demo.append((at, audio, rate))
            call.demo_clock += len(audio)/rate + 0.35
            result = self.speech.transcribe(audio, rate)
            event = call.event("remote", result["text"], at=at,
                               source="whisper_of_simulated_audio", inference_seconds=result["inference_seconds"])
            return {"event": event, "simulated": True}

    def finish(self, call_id: str, outcome: str = "needs_user", summary: str = "",
               phone_disconnected: bool = False) -> dict:
        with self.lock:
            call = self.get(call_id)
            if call.result is not None:
                return call.result
            if outcome not in {"completed", "needs_user", "failed", "cancelled", "interrupted"}:
                raise ValueError("Unknown outcome.")
            call.state = "finishing"
            if call.bridge:
                call.bridge.close()
            if call.recording:
                call.record_stopped = call.elapsed()
            call.recording = False
            transcription_complete = True
            if call.transcriber:
                try:
                    call.segments.put(None, timeout=2)
                except queue.Full:
                    transcription_complete = False
                call.transcriber.join(timeout=20)
                transcription_complete = transcription_complete and not call.transcriber.is_alive()
            if not transcription_complete:
                call.error("Transcription did not finish before export; partial transcript. Recover from remote.wav.")
            call.state = outcome
            call.event("system", "Session finished. " + summary)
            call.save()
            audio_path = self._mix(call)
            with call.condition:
                transcript = sorted([dict(e) for e in call.events], key=lambda e: (e["at"], e["seq"]))
            hangup = call.mode == "live" and not phone_disconnected
            result = {
                "call_id": call.id, "mode": call.mode, "simulated": call.mode == "demo",
                "outcome": outcome, "summary": summary, "outcome_source": "controller_reported",
                "transcript": transcript, "transcription_complete": transcription_complete,
                "recording_path": str(audio_path) if audio_path else None,
                "transcript_path": str(call.folder / "transcript.json"),
                "report_path": str(call.folder / "report.html"),
                "needs_phone_hangup": hangup,
                "next_action": "End the call in Phone now; stopping audio does not end the cellular call." if hangup else None,
            }
            write_json(call.folder / "transcript.json", {"mode": call.mode, "events": transcript})
            (call.folder / "transcript.txt").write_text("\n".join(
                f"[{e['at']:07.2f}s] {e['speaker']}: {e['text']}" for e in transcript
            ) + "\n")
            write_json(call.folder / "result.json", result)
            self._report(call, result)
            call.result = result
            return result

    def result(self, call_id: str) -> dict:
        if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{8}", call_id):
            raise ValueError("Invalid call_id.")
        call = self.sessions.get(call_id)
        if call:
            call.last_touch = time.monotonic()
        p = self.root / call_id / "result.json"
        if p.exists():
            return json.loads(p.read_text())
        if call:
            return {"state": call.state, "call_id": call.id, "events": list(call.events)}
        folder = self.root / call_id
        if (folder / "session.json").exists():
            events = []
            if (folder / "events.jsonl").exists():
                for line in (folder / "events.jsonl").read_text().splitlines():
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            return {"call_id": call_id, "state": "interrupted", "transcript": events,
                    "recording_path": str(folder / "remote.wav") if (folder / "remote.wav").exists() else None,
                    "next_action": "Service restarted; end any remaining Phone call. Persisted events are recoverable; raw audio may need WAV-header repair after a hard crash."}
        raise ValueError("No saved call with this ID.")

    def _mix(self, call: Session) -> Path | None:
        if call.record_started is None:
            return None
        rate = 24000
        start, end = call.record_started, call.record_stopped or call.elapsed()
        length = max(1, int((end - start) * rate))
        mix = np.zeros((length, 2), np.float32)

        def add(audio, source_rate, at, channel, played=None):
            data = resample(np.asarray(audio, np.float32), source_rate, rate)
            if played is not None:
                data = data[:int(played*rate)]
            offset = int((at-start)*rate)
            if offset < 0:
                data, offset = data[-offset:], 0
            count = min(len(data), length-offset)
            if count > 0:
                mix[offset:offset+count, channel] += data[:count]

        remote = call.folder / "remote.wav"
        if remote.exists():
            a, r = sf.read(remote, dtype="float32")
            add(a, r, start, 0)
        for at, a, r in call.remote_demo:
            add(a, r, at, 0)
        for at, path, played in call.outgoing:
            a, r = sf.read(path, dtype="float32")
            add(a, r, at, 1, played)
        if call.mode == "demo":
            sf.write(remote, mix[:, 0], rate, subtype="PCM_16")
        path = call.folder / "recording.wav"
        sf.write(path, np.clip(mix, -1, 1), rate, subtype="PCM_16")
        return path

    @staticmethod
    def _report(call: Session, result: dict):
        esc = html.escape
        rows = "".join(
            f'<div class="turn {esc(e["speaker"])}"><div class="who">{e["at"]:.1f}s · {esc(e["speaker"])}</div><p>{esc(e["text"])}</p></div>'
            for e in result["transcript"]
        )
        audio = '<audio controls src="recording.wav"></audio>' if result["recording_path"] else '<p>No recording retained.</p>'
        (call.folder / "report.html").write_text(f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>talk2myagent · Call report</title><style>
:root{{font-family:system-ui;color:#e6e9e7;background:#111815}}body{{max-width:860px;margin:60px auto;padding:0 24px}}h1{{font-size:46px;letter-spacing:-2px;margin:16px 0}}.brand{{color:#a8e5ae;font-weight:650;letter-spacing:1px}}.badge{{display:inline-block;border:1px solid #516459;border-radius:20px;padding:6px 12px;font-size:12px}}.meta,.who{{color:#9daaa2;font-size:13px}}.turn{{padding:14px 20px;border-left:2px solid #3c4b42;margin:14px 0;background:#19231e;border-radius:0 12px 12px 0}}.agent{{border-color:#afe5b1}}.system{{background:transparent;font-size:13px}}p{{line-height:1.6;margin:7px 0}}audio{{width:100%;margin:22px 0}}a{{color:#b4e5bd}}details{{padding:20px;background:#1a241f;border-radius:12px;margin:24px 0}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}footer{{color:#9daaa2;font-size:13px;margin:40px 0}}
</style><div class="brand">talk2myagent / CALL NOTES</div><h1>{esc(call.plan.objective)}</h1>
<span class="badge">{"SIMULATED CALL · NO AMAZON CONTACT" if call.mode == "demo" else "LIVE AUDIO SESSION"}</span>
<p class="meta">{esc(call.created_at)} · {esc(call.id)}</p><p>{esc(result["summary"])}</p>
{audio}<p class="meta">Remote audio: left channel. Synthesized agent audio: right channel. Agent text is the synthesis input; it does not verify what the recipient heard.</p>
<details><summary>Exact call plan</summary><pre>{esc(json.dumps(call.plan.model_dump(), indent=2))}</pre></details>
{rows}<footer>Local recording and transcription. Status: {esc(result['outcome'])}, reported by the controller. <a href="transcript.json">Transcript JSON</a> · <a href="result.json">Tool result</a></footer></html>''')

    def _watchdog(self):
        while not self.closed.wait(2):
            for call in list(self.sessions.values()):
                if call.state != "active" or call.mode != "live":
                    continue
                now = time.monotonic()
                if now-call.last_touch > self.config.idle_seconds or now-call.started > self.config.max_call_seconds:
                    self.interrupt(call.id)
                    self.finish(call.id, "interrupted", "Audio stopped at the inactivity or duration limit. End Phone call manually.")

    def shutdown(self):
        self.closed.set()
        for call in list(self.sessions.values()):
            if call.state in {"active", "prepared"}:
                self.interrupt(call.id)
                self.finish(call.id, "interrupted", "Local service stopped.")
