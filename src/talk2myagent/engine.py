from __future__ import annotations

import html
import json
import queue
import re
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import numpy as np
import soundfile as sf

from .audio import AudioBridge, devices, dtmf, human_devices
from .config import ROOT, Settings, private_dir, write_json
from .plans import CallPlan, CriterionCheck, TestScenario
from .speech import Speech, resample


def utc() -> str:
    return datetime.now(UTC).isoformat()


class Session:
    def __init__(self, folder: Path, plan: CallPlan, mode: str, *, persist: bool = True):
        self.folder, self.plan, self.mode = folder, plan, mode
        self.id = folder.name
        self.state = "prepared"
        self.created_at = utc()
        self.started = time.monotonic()
        self.connected_at: float | None = None
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
        self.cancel_requested = threading.Event()
        self.scenario: dict | None = None
        self.evaluation: list[dict] = []
        self.audio_config: dict | None = None
        self.conversation: dict = {"controller": "codex", "phase": "manual"}
        self.conversation_worker: threading.Thread | None = None
        self.review_required = False
        if persist:
            self.save()

    def elapsed(self) -> float:
        return self.demo_clock if self.mode == "demo" else time.monotonic() - self.started

    def save(self):
        with self.condition:
            write_json(
                self.folder / "session.json",
                {
                    "call_id": self.id,
                    "state": self.state,
                    "mode": self.mode,
                    "created_at": self.created_at,
                    "plan": self.plan.model_dump(),
                    "plan_id": self.plan.fingerprint(),
                    "recording": self.recording,
                    "recording_basis": self.recording_basis,
                    "scenario": self.scenario,
                    "audio_config": self.audio_config,
                    "conversation": self.conversation,
                    "review_required": self.review_required,
                },
            )

    def event(self, speaker: str, text: str, at: float | None = None, **extra) -> dict:
        with self.condition:
            event = {
                "seq": len(self.events) + 1,
                "at": round(self.elapsed() if at is None else max(at, 0), 3),
                "speaker": speaker,
                "text": text,
                "emitted_at": round(self.elapsed(), 3),
                **extra,
            }
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
        self.brain = None
        threading.Thread(target=self._watchdog, daemon=True).start()

    def doctor(self) -> dict:
        audio = devices()
        available = {d["name"] for d in audio}
        missing = [
            n for n in [self.config.input_device, self.config.output_device] if n not in available
        ]
        return {
            "devices": audio,
            "settings": self.config.model_dump(),
            "missing_audio_devices": missing,
            "kokoro_downloaded": all(
                (ROOT / "models" / f).exists() for f in ["kokoro-v1.0.onnx", "voices-v1.0.bin"]
            ),
            "live_audio_devices_present": not missing,
            "routing_verified": False,
            "next_step": "Verify Phone microphone = outgoing bus and Phone/system output = incoming bus. Run loopback-test before a call.",
        }

    def prepare(self, plan: dict, mode: Literal["demo", "live", "roleplay"] = "demo") -> dict:
        plan_obj = CallPlan.model_validate(plan)
        if mode not in ("demo", "live", "roleplay"):
            raise ValueError("Mode must be demo, live, or roleplay.")
        if mode == "live" and plan_obj.is_demo:
            raise ValueError("A demo plan with fictional facts cannot be used for a real call.")
        with self.lock:
            call_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
            folder = private_dir(self.root / call_id)
            call = Session(folder, plan_obj, mode)
            self.sessions[call_id] = call
            call.event("system", "Plan prepared; no phone call placed.")
            return {
                "call_id": call_id,
                "plan_id": plan_obj.fingerprint(),
                "plan": plan_obj.model_dump(),
                "mode": mode,
                "state": call.state,
                "folder": str(folder),
            }

    def test_status(self) -> dict:
        """Enter the voice playground without choosing a task or opening a mic."""
        inventory = devices()
        roleplays = [c for c in self.sessions.values() if c.mode == "roleplay"]
        active = [c for c in roleplays if c.state == "active"]
        ready = [c for c in roleplays if c.state == "prepared"]
        try:
            incoming, outgoing = human_devices()
            error = None
        except ValueError as exc:
            incoming, outgoing, error = None, None, str(exc)
        return {
            "mode": "roleplay",
            "phase": "active" if active else "ready" if ready else "awaiting_task",
            "microphone_opened": bool(active),
            "opened_by_this_request": False,
            "dialing_enabled": False,
            "devices": inventory,
            "suggested_input": incoming,
            "suggested_output": outgoing,
            "device_error": error,
            "sessions": [
                {"call_id": c.id, "state": c.state}
                for c in self.sessions.values()
                if c.mode == "roleplay"
            ],
            "next_step": "Developer supplies any call task in this Codex conversation. Prepare it with test_prepare; start after the developer is ready to act as the recipient.",
        }

    def test_prepare(self, scenario: dict) -> dict:
        parsed = TestScenario.model_validate(scenario)
        with self.lock:
            prepared = self.prepare(parsed.call_plan().model_dump(), "roleplay")
            call = self.get(prepared["call_id"])
            call.scenario = parsed.model_dump()
            call.event(
                "system", "Human role-play ready. Developer's task saved; microphone is closed."
            )
            call.save()
            return {
                **prepared,
                "phase": "ready",
                "recipient_role": parsed.recipient_role,
                "user_request": parsed.user_request,
                "dialing_enabled": False,
                "next_step": "When the developer is ready, use test_start. It opens the mic, starts the requested recording, and speaks the greeting automatically.",
            }

    def test_start(
        self,
        call_id: str,
        plan_id: str,
        record: bool = True,
        audio_mode: Literal["speakers", "headphones"] = "speakers",
        input_device: str | None = None,
        output_device: str | None = None,
        controller: Literal["codex", "local"] = "codex",
    ) -> dict:
        with self.lock:
            call = self.get(call_id)
            if call.mode != "roleplay" or call.scenario is None:
                raise ValueError("Use test_prepare before test_start.")
            if call.state != "prepared" or call.plan.fingerprint() != plan_id:
                raise ValueError(
                    "Test already started or plan_id does not match; do not repeat start."
                )
            if audio_mode not in {"speakers", "headphones"}:
                raise ValueError("audio_mode must be speakers or headphones.")
            self._check_audio_owner()
            if controller not in {"codex", "local"}:
                raise ValueError("controller must be codex or local.")
            if controller == "local":
                self.conversation_ready()
            incoming, outgoing = human_devices(input_device, output_device)
            # Warm inference before the microphone opens, so the greeting is ready promptly.
            self.speech.synthesize("Ready.")
            self.speech.transcribe(np.zeros(16000, dtype=np.float32), 16000)
            if call.cancel_requested.is_set():
                raise ValueError("Test start cancelled before the microphone opened.")
            config = self.config.model_copy(
                update={"input_device": incoming, "output_device": outgoing}
            )
            call.audio_config = {
                "input_device": incoming,
                "output_device": outgoing,
                "audio_mode": audio_mode,
                "sample_rate": config.sample_rate,
            }
            self._start_bridge(
                call, config, speaker_safe=audio_mode == "speakers", allow_shared_device=True
            )
            call.state = "active"
            call.connected_at = time.monotonic()
            call.event(
                "system",
                f"Human role-play started on {incoming} / {outgoing}. You are now the recipient. Say 'stop test' or use test-stop to exit.",
            )
            call.save()
            try:
                if record:
                    self.recording_start(
                        call_id, "Developer explicitly started a recorded local role-play test."
                    )
                greeting = self.say(call_id, call.plan.opening)
                if controller == "local":
                    self.conversation_start(call_id, plan_id)
            except Exception:
                self.finish(
                    call_id,
                    "cancelled" if call.cancel_requested.is_set() else "failed",
                    "Role-play stopped while starting; microphone closed.",
                )
                raise
            return {
                "call_id": call.id,
                "mode": "roleplay",
                "state": call.state,
                "recording": call.recording,
                "audio": call.audio_config,
                "greeting": greeting,
                "cursor": 0,
                "conversation": dict(call.conversation),
                "next_step": (
                    "Local agent owns spoken turns. Use conversation_wait, then review the outcome and evidence with test_finish. Do not alternate say/listen."
                    if controller == "local"
                    else "Listen to the developer as the recipient. Adapt each response to their actual speech until the objective is confirmed, blocked, or they stop the test."
                ),
            }

    def conversation_ready(self) -> dict:
        """Preload a cached model without opening audio or starting a conversation."""
        from .conversation import LocalConversation

        with self.lock:
            if self.brain is None:
                self.brain = LocalConversation(self.config, self.speech.lock)
            return self.brain.ready()

    def conversation_start(self, call_id: str, plan_id: str) -> dict:
        from .conversation import ConversationWorker

        with self.lock:
            call = self._active(call_id)
            if call.plan.fingerprint() != plan_id:
                raise ValueError("Use the exact prepared plan_id for delegation.")
            if call.mode not in {"roleplay", "live"}:
                raise ValueError(
                    "Autonomous conversation requires a human role-play or connected call."
                )
            if call.mode == "live" and not call.recording:
                raise ValueError(
                    "Obtain recording consent and start recording before live delegation."
                )
            if call.conversation_worker is not None:
                raise ValueError("Conversation already delegated; do not start a duplicate worker.")
            self.conversation_ready()
            call.conversation = {
                "controller": "local",
                "phase": "listening",
                "model": self.config.conversation_model,
            }
            call.conversation_worker = ConversationWorker(self, call, self.brain)
            call.event(
                "system",
                "Call plan delegated to the local conversation agent. Codex is outside the spoken-turn loop.",
            )
            call.save()
            call.conversation_worker.start()
            return {"call_id": call.id, "conversation": dict(call.conversation)}

    def conversation_wait(self, call_id: str, timeout_seconds: float = 25) -> dict:
        call = self.get(call_id)
        if not 0 <= timeout_seconds <= 25:
            raise ValueError("timeout_seconds must be 0–25.")
        deadline = time.monotonic() + timeout_seconds
        with call.condition:
            while call.state in {"active", "finishing"} and call.conversation_worker is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                call.condition.wait(timeout=min(remaining, 0.5))
            return {
                "call_id": call.id,
                "state": call.state,
                "conversation": dict(call.conversation),
                "review_required": call.review_required,
                "result": call.result,
            }

    def test_finish(self, call_id: str, outcome: str, summary: str, checks: list[dict]) -> dict:
        call = self.get(call_id)
        if call.mode != "roleplay":
            raise ValueError("test_finish only accepts a role-play session.")
        return self.conversation_review(call_id, outcome, summary, checks)

    def conversation_review(
        self,
        call_id: str,
        outcome: str,
        summary: str,
        checks: list[dict],
        phone_disconnected: bool = False,
    ) -> dict:
        with self.lock:
            call = self.get(call_id)
            if call.result is not None and not call.review_required:
                return call.result
            if outcome not in {"completed", "needs_user", "failed", "cancelled", "interrupted"}:
                raise ValueError("Unknown outcome.")
            if call.conversation_worker and call.state == "active":
                raise ValueError(
                    "Wait for the local conversation to end, or stop it before review."
                )
            parsed = [CriterionCheck.model_validate(c) for c in checks]
            expected = set(range(len(call.plan.success_criteria)))
            if len(parsed) != len(expected) or {c.criterion_index for c in parsed} != expected:
                raise ValueError("Evaluate each success criterion exactly once.")
            remote = {e["seq"] for e in call.events if e["speaker"] == "remote"}
            for check in parsed:
                if not set(check.evidence_seq) <= remote:
                    raise ValueError(
                        "Evidence must cite actual recipient transcript event sequence IDs."
                    )
                if check.verdict == "met" and not check.evidence_seq:
                    raise ValueError("A met criterion requires recipient evidence.")
            if outcome == "completed" and any(c.verdict != "met" for c in parsed):
                raise ValueError("Completion requires every success criterion to be met.")
            call.evaluation = [
                {**c.model_dump(), "criterion": call.plan.success_criteria[c.criterion_index]}
                for c in sorted(parsed, key=lambda c: c.criterion_index)
            ]
            call.review_required = False
            if call.result is None:
                return self.finish(call_id, outcome, summary, phone_disconnected)
            call.state = outcome
            call.conversation["phase"] = "reviewed"
            call.event("system", "Codex reviewed the conversation. " + summary)
            call.result.update(
                outcome=outcome,
                summary=summary,
                evaluation=call.evaluation,
                review_required=False,
                conversation=dict(call.conversation),
                outcome_source="codex_reviewed",
                needs_phone_hangup=call.mode == "live" and not phone_disconnected,
                next_action="End the Phone call now."
                if call.mode == "live" and not phone_disconnected
                else None,
            )
            self._persist_result(call, call.result)
            call.save()
            return call.result

    def test_stop(self, call_id: str | None = None) -> dict:
        if call_id is None:
            active = [
                c for c in self.sessions.values() if c.mode == "roleplay" and c.state == "active"
            ]
            if len(active) != 1:
                return {
                    "stopped": False,
                    "message": "No single active role-play. Supply its call_id to stop a prepared test.",
                }
            call_id = active[0].id
        call = self.get(call_id)
        if call.mode != "roleplay":
            raise ValueError("test_stop cannot affect a real telephone call.")
        call.cancel_requested.set()
        self.interrupt(call_id)
        return self.finish(call_id, "cancelled", "Developer stopped the role-play.")

    def _check_audio_owner(self):
        if any(s.state == "active" and s.bridge is not None for s in self.sessions.values()):
            raise ValueError("Only one telephone or role-play session may own the audio devices.")

    def _start_bridge(self, call, config, **options):
        bridge = AudioBridge(
            config, lambda a, at, meta=None: self._enqueue(call, a, at, meta), call.error, **options
        )
        try:
            bridge.start()
        except Exception:
            bridge.close()
            raise
        call.bridge = bridge
        call.transcriber = threading.Thread(target=self._transcribe, args=(call,), daemon=True)
        call.transcriber.start()

    def get(self, call_id: str) -> Session:
        if call_id not in self.sessions:
            if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{8}", call_id):
                raise ValueError("Invalid call_id.")
            folder = self.root / call_id
            if not (folder / "result.json").exists() or not (folder / "session.json").exists():
                raise ValueError(
                    "Unknown/inactive call. Use result to recover saved artifacts after a service restart."
                )
            saved = json.loads((folder / "session.json").read_text())
            result = json.loads((folder / "result.json").read_text())
            # Restore only a completed audio session for review; never reopen hardware.
            call = Session(
                folder, CallPlan.model_validate(saved["plan"]), saved["mode"], persist=False
            )
            call.state = result["outcome"]
            call.result = result
            call.events = result["transcript"]
            call.created_at = saved["created_at"]
            call.started = time.monotonic() - max(
                (e.get("emitted_at", e["at"]) for e in call.events), default=0
            )
            call.scenario = saved.get("scenario")
            call.audio_config = result.get("audio")
            call.conversation = result.get("conversation", call.conversation)
            call.review_required = result.get("review_required", False)
            call.evaluation = result.get("evaluation", [])
            call.cancel_requested.set()
            self.sessions[call_id] = call
        call = self.sessions[call_id]
        call.last_touch = time.monotonic()
        return call

    def connect(
        self,
        call_id: str,
        plan_id: str,
        authorized: bool = False,
        connected: bool = False,
        routing_verified: bool = False,
    ) -> dict:
        with self.lock:
            call = self.get(call_id)
            if call.state != "prepared":
                raise ValueError("Call has already started or finished; connect is not retryable.")
            if call.plan.fingerprint() != plan_id:
                raise ValueError("Plan changed; use the exact reviewed plan_id.")
            if call.mode == "roleplay":
                raise ValueError(
                    "Use test_start for a human role-play; do not use Phone or connect."
                )
            if call.mode == "live":
                if not (authorized and connected and routing_verified):
                    raise ValueError(
                        "Live audio requires authorization, an observed connected Phone call, and verified routing."
                    )
                self._check_audio_owner()
                self._start_bridge(call, self.config)
            call.state = "active"
            call.connected_at = time.monotonic()
            call.event(
                "system", "Audio session connected. Recording is off until consent is recorded."
            )
            call.save()
            return {
                "call_id": call.id,
                "state": call.state,
                "recording": False,
                "opening": call.plan.opening,
            }

    def dial_request(self, call_id: str, plan_id: str, authorized: bool = False) -> dict:
        call = self.get(call_id)
        if call.mode == "roleplay":
            raise ValueError("Dialing is disabled in human role-play test mode.")
        if call.state != "prepared" or call.plan.fingerprint() != plan_id:
            raise ValueError("Dial requires a prepared call and its reviewed plan_id.")
        if not authorized:
            raise ValueError("The user must authorize the recipient, purpose, and facts disclosed.")
        return {
            "call_id": call.id,
            "phone_number": call.plan.phone_number,
            "phone_source": call.plan.phone_source,
            "mode": call.mode,
            "call_placed": False,
            "instructions": (
                "DEMO ONLY: use connect; do not dial."
                if call.mode == "demo"
                else "Use computer use: Phone > Keypad; enter phone_number; click Call once. "
                "Inspect the connected call state before connect. Never blindly retry dialing."
            ),
        }

    def _active(self, call_id: str) -> Session:
        call = self.get(call_id)
        if call.state != "active":
            raise ValueError(f"Call is {call.state}, not active.")
        return call

    def _enqueue(self, call: Session, audio: np.ndarray, at: float, meta: dict | None = None):
        try:
            call.segments.put_nowait((audio, at, time.monotonic(), meta or {}))
        except queue.Full:
            call.error(
                "Transcription backlog full; some speech was not transcribed. Raw recording may still contain it."
            )

    def _transcribe(self, call: Session):
        while True:
            item = call.segments.get()
            try:
                if item is None:
                    return
                audio, at, queued_at, meta = item
                transcribe_started = time.monotonic()
                result = self.speech.transcribe(audio, self.config.sample_rate)
                if result["text"]:
                    timing = {
                        **meta,
                        "segment_ready_at": round(queued_at - call.started, 3),
                        "transcription_queue_seconds": round(transcribe_started - queued_at, 3),
                    }
                    if "speech_end_at" in timing:
                        timing["speech_end_at"] = round(timing["speech_end_at"] - call.started, 3)
                    call.event(
                        "remote",
                        result["text"],
                        at=at - call.started,
                        inference_seconds=result["inference_seconds"],
                        source="whisper",
                        **timing,
                    )
                    if call.mode == "roleplay" and re.fullmatch(
                        r"(?:please\s+)?(?:stop|end|exit) (?:the )?test[.!?]*",
                        result["text"].strip(),
                        re.IGNORECASE,
                    ):
                        # Finish off this worker so it can drain and join without self-deadlock.
                        call.cancel_requested.set()
                        self.interrupt(call.id)
                        threading.Thread(
                            target=self.finish,
                            args=(
                                call.id,
                                "cancelled",
                                "Developer ended the role-play with the spoken stop command.",
                            ),
                            daemon=True,
                        ).start()
            except Exception as exc:  # noqa: BLE001 - keep the call alive after an inference failure
                call.error(f"Transcription failed: {exc}")
            finally:
                call.segments.task_done()

    def recording_start(self, call_id: str, consent_basis: str) -> dict:
        with self.lock:
            call = self._active(call_id)
            if len(consent_basis.strip()) < 5:
                raise ValueError("Describe the recording consent actually obtained.")
            if call.record_started is not None:
                raise ValueError(
                    "Recording already started. It cannot restart and overwrite prior audio."
                )
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
            if (
                call.conversation_worker
                and threading.current_thread() is not call.conversation_worker
            ):
                raise ValueError(
                    "The local conversation agent owns speech; use interrupt or stop instead."
                )
            synthesis_started = time.monotonic()
            audio, rate = self.speech.synthesize(text)
            synthesis_seconds = time.monotonic() - synthesis_started
            if call.cancel_requested.is_set():
                raise RuntimeError("Role-play stopped; generated speech was not played.")
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
            event = call.event(
                "agent",
                text,
                at=at,
                source="synthesis_text",
                audio_file=path.name,
                played_seconds=round(played, 3),
                interrupted=played < duration - 0.05,
                synthesis_seconds=round(synthesis_seconds, 3),
            )
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
                    return {
                        "call_id": call.id,
                        "state": call.state,
                        "events": new,
                        "cursor": len(call.events),
                        "recording": call.recording,
                        "timed_out": not bool(new),
                        "audio": {
                            "input_rms": call.bridge.last_input_rms,
                            "capturing": call.bridge.last_input_at > 0,
                            "speaking": call.bridge.playing.is_set(),
                        }
                        if call.bridge
                        else None,
                    }
                call.condition.wait(timeout=max(0, deadline - time.monotonic()))

    def keypad(self, call_id: str, digits: str) -> dict:
        if not re.fullmatch(r"[0-9*#ABCD]{1,16}", digits):
            raise ValueError("Provide 1–16 DTMF characters: 0–9, *, #, A–D.")
        call = self._active(call_id)
        if call.mode == "roleplay":
            raise ValueError(
                "Phone keypad is disabled in human role-play. Ask the developer to act out the IVR."
            )
        call.event("system", f"Requested Phone keypad digits: {digits}", kind="keypad_request")
        return {
            "digits": digits,
            "sent": False,
            "instructions": "Use computer use to press these digits on the connected Phone call keypad. Prefer this to in-band tones.",
        }

    def tones(self, call_id: str, digits: str) -> dict:
        with self.lock:
            call = self._active(call_id)
            if call.mode == "roleplay":
                raise ValueError("Phone tones are disabled in human role-play.")
            if not re.fullmatch(r"[0-9*#ABCD]{1,16}", digits):
                raise ValueError("Invalid DTMF digits.")
            audio = dtmf(digits, self.config.sample_rate)
            at = call.elapsed()
            played = (
                call.bridge.play(audio, self.config.sample_rate)
                if call.bridge
                else len(audio) / self.config.sample_rate
            )
            path = call.folder / f"dtmf-{uuid.uuid4().hex[:8]}.wav"
            sf.write(path, audio, self.config.sample_rate)
            call.outgoing.append((at, path, played))
            if call.mode == "demo":
                call.demo_clock += played + 0.35
            call.event("agent", f"[DTMF {digits}]", at=at, source="tone_generation")
            return {
                "sent_audio": True,
                "warning": "In-band tones are experimental; confirm the IVR accepted them. Phone keypad is preferred.",
            }

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
            call.demo_clock += len(audio) / rate + 0.35
            result = self.speech.transcribe(audio, rate)
            event = call.event(
                "remote",
                result["text"],
                at=at,
                source="whisper_of_simulated_audio",
                inference_seconds=result["inference_seconds"],
            )
            return {"event": event, "simulated": True}

    def finish(
        self,
        call_id: str,
        outcome: str = "needs_user",
        summary: str = "",
        phone_disconnected: bool = False,
    ) -> dict:
        with self.lock:
            call = self.get(call_id)
            if call.result is not None:
                return call.result
            if outcome not in {"completed", "needs_user", "failed", "cancelled", "interrupted"}:
                raise ValueError("Unknown outcome.")
            if (
                call.mode == "roleplay"
                and outcome == "completed"
                and (not call.evaluation or any(c["verdict"] != "met" for c in call.evaluation))
            ):
                raise ValueError(
                    "Use test_finish with evidence for each success criterion before claiming completion."
                )
            call.state = "finishing"
            call.cancel_requested.set()
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
                call.error(
                    "Transcription did not finish before export; partial transcript. Recover from remote.wav."
                )
            call.state = outcome
            if call.conversation_worker and not call.review_required:
                call.conversation["phase"] = "stopped"
            call.event("system", "Session finished. " + summary)
            call.save()
            audio_path = self._mix(call)
            with call.condition:
                transcript = sorted(
                    [dict(e) for e in call.events], key=lambda e: (e["at"], e["seq"])
                )
            hangup = call.mode == "live" and not phone_disconnected
            result = {
                "call_id": call.id,
                "mode": call.mode,
                "simulated": call.mode == "demo",
                "test_mode": call.mode == "roleplay",
                "real_world_actions": False if call.mode != "live" else None,
                "scenario": call.scenario,
                "evaluation": call.evaluation,
                "audio": call.audio_config,
                "conversation": dict(call.conversation),
                "review_required": call.review_required,
                "outcome": outcome,
                "summary": summary,
                "outcome_source": "controller_reported",
                "transcript": transcript,
                "transcription_complete": transcription_complete,
                "recording_path": str(audio_path) if audio_path else None,
                "transcript_path": str(call.folder / "transcript.json"),
                "report_path": str(call.folder / "report.html"),
                "needs_phone_hangup": hangup,
                "next_action": "End the call in Phone now; stopping audio does not end the cellular call."
                if hangup
                else None,
            }
            self._persist_result(call, result)
            call.result = result
            with call.condition:
                call.condition.notify_all()
            return result

    def _persist_result(self, call: Session, result: dict):
        with call.condition:
            transcript = sorted([dict(e) for e in call.events], key=lambda e: (e["at"], e["seq"]))
        result["transcript"] = transcript
        write_json(call.folder / "transcript.json", {"mode": call.mode, "events": transcript})
        (call.folder / "transcript.txt").write_text(
            "\n".join(f"[{e['at']:07.2f}s] {e['speaker']}: {e['text']}" for e in transcript) + "\n"
        )
        write_json(call.folder / "result.json", result)
        self._report(call, result)

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
            return {
                "call_id": call_id,
                "state": "interrupted",
                "transcript": events,
                "recording_path": str(folder / "remote.wav")
                if (folder / "remote.wav").exists()
                else None,
                "next_action": "Service restarted; microphone is no longer owned by this session. End any remaining real Phone call. Persisted events are recoverable; raw audio may need WAV-header repair after a hard crash.",
            }
        raise ValueError("No saved call with this ID.")

    def _mix(self, call: Session) -> Path | None:
        if call.record_started is None:
            return None
        rate = 24000
        start, end = call.record_started, call.record_stopped or call.elapsed()
        length = max(1, round((end - start) * rate))
        mix = np.zeros((length, 2), np.float32)

        def add(audio, source_rate, at, channel, played=None):
            data = resample(np.asarray(audio, np.float32), source_rate, rate)
            if played is not None:
                data = data[: int(played * rate)]
            offset = round((at - start) * rate)
            if offset < 0:
                data, offset = data[-offset:], 0
            count = min(len(data), length - offset)
            if count > 0:
                mix[offset : offset + count, channel] += data[:count]

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
        audio = (
            '<audio controls src="recording.wav"></audio>'
            if result["recording_path"]
            else "<p>No recording retained.</p>"
        )
        evaluation = "".join(
            f'<div class="turn"><div class="who">{esc(c["verdict"])}</div><p>{esc(c["criterion"])}</p><p>{esc(c["explanation"])}</p><p class="meta">Transcript evidence: {esc(str(c["evidence_seq"]))}</p></div>'
            for c in result.get("evaluation", [])
        )
        (
            call.folder / "report.html"
        ).write_text(f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>talk2myagent · Call report</title><style>
:root{{font-family:system-ui;color:#e6e9e7;background:#111815}}body{{max-width:860px;margin:60px auto;padding:0 24px}}h1{{font-size:46px;letter-spacing:-2px;margin:16px 0}}.brand{{color:#a8e5ae;font-weight:650;letter-spacing:1px}}.badge{{display:inline-block;border:1px solid #516459;border-radius:20px;padding:6px 12px;font-size:12px}}.meta,.who{{color:#9daaa2;font-size:13px}}.turn{{padding:14px 20px;border-left:2px solid #3c4b42;margin:14px 0;background:#19231e;border-radius:0 12px 12px 0}}.agent{{border-color:#afe5b1}}.system{{background:transparent;font-size:13px}}p{{line-height:1.6;margin:7px 0}}audio{{width:100%;margin:22px 0}}a{{color:#b4e5bd}}details{{padding:20px;background:#1a241f;border-radius:12px;margin:24px 0}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}footer{{color:#9daaa2;font-size:13px;margin:40px 0}}
</style><div class="brand">talk2myagent / CALL NOTES</div><h1>{esc(call.plan.objective)}</h1>
<span class="badge">{"HUMAN ROLE-PLAY · NO PHONE CALL" if call.mode == "roleplay" else "SIMULATED CALL · NO EXTERNAL CONTACT" if call.mode == "demo" else "LIVE AUDIO SESSION"}</span>
<p class="meta">{esc(call.created_at)} · {esc(call.id)}</p><p>{esc(result["summary"])}</p>
{audio}<p class="meta">Remote audio: left channel. Synthesized agent audio: right channel. Agent text is the synthesis input; it does not verify what the recipient heard.</p>
<details><summary>Exact call plan</summary><pre>{esc(json.dumps(call.plan.model_dump(), indent=2))}</pre></details>{evaluation}
{rows}<footer>Local recording and transcription. Status: {esc(result["outcome"])}, reported by the controller. <a href="transcript.json">Transcript JSON</a> · <a href="result.json">Tool result</a></footer></html>""")

    def _watchdog(self):
        while not self.closed.wait(2):
            for call in list(self.sessions.values()):
                if call.state != "active" or call.mode == "demo":
                    continue
                now = time.monotonic()
                if (
                    now - call.last_touch > self.config.idle_seconds
                    or now - (call.connected_at or call.started) > self.config.max_call_seconds
                ):
                    self.interrupt(call.id)
                    self.finish(
                        call.id,
                        "interrupted",
                        "Audio stopped at the inactivity or duration limit. "
                        + (
                            "End Phone call manually."
                            if call.mode == "live"
                            else "Role-play microphone closed."
                        ),
                    )

    def shutdown(self):
        self.closed.set()
        for call in list(self.sessions.values()):
            if call.state in {"active", "prepared"}:
                self.interrupt(call.id)
                self.finish(call.id, "interrupted", "Local service stopped.")
