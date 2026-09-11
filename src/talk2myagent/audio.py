from __future__ import annotations

import queue
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

from .config import Settings
from .speech import resample


def devices() -> list[dict]:
    return [
        {
            "index": i,
            "name": d["name"],
            "inputs": d["max_input_channels"],
            "outputs": d["max_output_channels"],
            "sample_rate": d["default_samplerate"],
        }
        for i, d in enumerate(sd.query_devices())
    ]


def device_index(name: str, direction: str) -> int:
    matches = [d for d in devices() if d["name"] == name and d[direction] > 0]
    if len(matches) != 1:
        raise ValueError(f"Expected one {direction} device named {name!r}; found {len(matches)}.")
    return matches[0]["index"]


def human_devices(input_name: str | None = None, output_name: str | None = None) -> tuple[str, str]:
    """Pick human-facing hardware, never an existing telephone loopback route."""
    inventory = devices()

    def choose(requested, direction, kind):
        candidates = [
            d
            for d in inventory
            if d[direction] > 0
            and not any(
                word in d["name"].lower()
                for word in ("blackhole", "loopback", "aggregate", "multi-output")
            )
        ]
        if requested:
            matches = [d for d in candidates if d["name"] == requested]
            if len(matches) != 1:
                raise ValueError(f"Select a physical {kind} device from phone_test_status.")
            return matches[0]["name"]
        try:
            default_name = sd.query_devices(kind=kind)["name"]
        except sd.PortAudioError:
            default_name = None
        if any(d["name"] == default_name for d in candidates):
            return default_name
        builtin = next((d["name"] for d in candidates if "macbook" in d["name"].lower()), None)
        if builtin:
            return builtin
        if len(candidates) == 1:
            return candidates[0]["name"]
        raise ValueError(f"Select a physical {kind} device from phone_test_status.")

    return choose(input_name, "inputs", "input"), choose(output_name, "outputs", "output")


class Segmenter:
    """Turn endpointing; activity comes from neural VAD or explicit RMS fallback."""

    def __init__(self, rate: int, threshold: float, silence: float):
        self.rate, self.threshold, self.silence = rate, threshold, silence
        self.preroll: deque[np.ndarray] = deque(maxlen=10)
        self.parts: list[np.ndarray] = []
        self.quiet = self.voiced = 0.0
        self.last_segment: dict = {}
        self.samples = 0

    def feed(self, block: np.ndarray, active: bool | None = None) -> np.ndarray | None:
        duration = len(block) / self.rate
        if active is None:
            active = float(np.sqrt(np.mean(block * block))) >= self.threshold
        if not self.parts:
            if active:
                self.parts = list(self.preroll)
                self.samples = sum(map(len, self.parts))
                self.preroll.clear()
            else:
                self.preroll.append(block)
                return None
        self.parts.append(block)
        self.samples += len(block)
        self.voiced += duration if active else 0
        self.quiet = 0 if active else self.quiet + duration
        if self.quiet >= self.silence or self.samples >= self.rate * 20:
            self.last_segment = {
                "trailing_silence_seconds": self.quiet,
                "endpoint_reason": "silence" if self.quiet >= self.silence else "max_duration",
            }
            audio = np.concatenate(self.parts) if self.voiced >= 0.15 else None
            self.parts = []
            self.samples = 0
            self.quiet = self.voiced = 0.0
            return audio
        return None

    def flush(self) -> np.ndarray | None:
        self.last_segment = {"trailing_silence_seconds": self.quiet, "endpoint_reason": "flush"}
        audio = np.concatenate(self.parts) if self.parts and self.voiced >= 0.15 else None
        self.parts = []
        self.samples = 0
        self.voiced = self.quiet = 0.0
        return audio


class Monitor:
    """Plays a copy of both call sides on a human-facing device so the room can listen."""

    def __init__(self, device_name: str, rate: int):
        self.rate = rate
        self.lock = threading.Lock()
        self.buffer = np.zeros(0, np.float32)
        self.stream = sd.OutputStream(
            device=device_index(device_name, "outputs"),
            samplerate=rate,
            channels=1,
            dtype="float32",
            blocksize=960,
            callback=self._callback,
        )
        self.stream.start()

    def _callback(self, outdata, frames, timing, status):
        with self.lock:
            chunk, self.buffer = self.buffer[:frames], self.buffer[frames:]
        outdata[:, 0] = 0
        outdata[: len(chunk), 0] = chunk

    def feed(self, block: np.ndarray, rate: int | None = None):
        if rate and rate != self.rate:
            block = resample(block, rate, self.rate)
        with self.lock:
            if len(self.buffer) > self.rate * 8:  # never let the monitor lag the call
                self.buffer = np.zeros(0, np.float32)
            self.buffer = np.concatenate((self.buffer, block.astype(np.float32)))

    def drain(self, timeout: float = 15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if len(self.buffer) == 0:
                    return
            time.sleep(0.02)

    def close(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:  # noqa: BLE001, S110 - closing is best effort
            pass


class Playback:
    """One outgoing utterance, written in chunks as sentences are synthesized."""

    def __init__(self, bridge: AudioBridge):
        self.bridge = bridge
        self.rate = bridge.config.sample_rate
        self.stream = None
        self.written = 0
        self.underflows = 0
        self.interrupted = False
        self.started_at = time.monotonic()
        bridge.playback_stop.clear()
        with bridge.suppression_lock:
            bridge.play_started = self.started_at
            bridge.playing.set()

    def write(self, audio: np.ndarray, rate: int) -> bool:
        if self.interrupted:
            return False
        samples = resample(audio, rate, self.rate)
        if self.stream is None:
            self.stream = sd.OutputStream(
                device=device_index(self.bridge.config.output_device, "outputs"),
                samplerate=self.rate,
                channels=1,
                dtype="float32",
                blocksize=960,
            )
            self.stream.start()
        for offset in range(0, len(samples), 960):
            if self.bridge.playback_stop.is_set():
                self.interrupted = True
                return False
            block = samples[offset : offset + 960]
            if self.stream.write(block):
                self.underflows += 1
            if self.bridge.monitor:
                self.bridge.monitor.feed(block)
            self.written += len(block)
        return True

    def finish(self) -> tuple[float, bool]:
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:  # noqa: BLE001, S110 - device may already be gone
                pass
        with self.bridge.suppression_lock:
            self.bridge.suppression_windows.append((self.started_at, time.monotonic() + 0.45))
            self.bridge.playing.clear()
        return self.written / self.rate, self.interrupted


class AudioBridge:
    def __init__(
        self,
        config: Settings,
        on_segment,
        on_error,
        *,
        speaker_safe: bool = False,
        allow_shared_device: bool = False,
        monitor_device: str | None = None,
    ):
        self.config, self.on_segment, self.on_error = config, on_segment, on_error
        self.blocks: queue.Queue = queue.Queue(maxsize=500)
        self.stop = threading.Event()
        self.record_lock = threading.Lock()
        self.writer = None
        self.thread = None
        self.stream = None
        self.playback_stop = threading.Event()
        self.playing = threading.Event()
        self.speaker_safe = speaker_safe
        self.allow_shared_device = allow_shared_device
        self.suppression_lock = threading.Lock()
        self.suppression_windows: deque[tuple[float, float]] = deque(maxlen=100)
        self.play_started = 0.0
        self.last_input_rms = 0.0
        self.last_input_at = 0.0
        self.detector = None
        self.last_voice_at = 0.0
        self.monitor_device = monitor_device
        self.monitor: Monitor | None = None

    def start(self):
        if self.config.vad_backend == "silero":
            from .vad import SileroDetector

            self.detector = SileroDetector(self.config.sample_rate, self.config.vad_probability)
        if self.config.input_device == self.config.output_device and not self.allow_shared_device:
            raise ValueError("Input and output must be different audio buses to prevent feedback.")
        incoming = device_index(self.config.input_device, "inputs")
        device_index(self.config.output_device, "outputs")
        self.stream = sd.InputStream(
            device=incoming,
            samplerate=self.config.sample_rate,
            channels=1,
            dtype="float32",
            blocksize=int(self.config.sample_rate * 0.02),
            callback=self._callback,
        )
        self.stream.start()
        if self.monitor_device:
            self.monitor = Monitor(self.monitor_device, self.config.sample_rate)
        self.thread = threading.Thread(target=self._consume, daemon=True)
        self.thread.start()

    def _callback(self, indata, frames, timing, status):
        if status:
            self.on_error(f"Audio input: {status}")
        try:
            self.blocks.put_nowait(
                (indata[:, 0].copy(), time.monotonic() - frames / self.config.sample_rate)
            )
        except queue.Full:
            self.on_error("Audio capture overflow; recording/transcript may have a gap.")

    def suppresses(self, at: float) -> bool:
        if not self.speaker_safe:
            return False
        with self.suppression_lock:
            return (self.playing.is_set() and at >= self.play_started) or any(
                start <= at <= end for start, end in self.suppression_windows
            )

    def _consume(self):
        vad = Segmenter(
            self.config.sample_rate, self.config.speech_threshold, self.config.silence_seconds
        )
        overlap = 0.0
        was_suppressed = False
        try:
            while not self.stop.is_set() or not self.blocks.empty():
                try:
                    block, at = self.blocks.get(timeout=0.1)
                except queue.Empty:
                    continue
                self.last_input_rms = float(np.sqrt(np.mean(block * block)))
                self.last_input_at = time.monotonic()
                suppressed = self.suppresses(at)
                if suppressed:
                    # Keep file timing continuous, but do not record/transcribe speaker echo.
                    block = np.zeros_like(block)
                with self.record_lock:
                    if self.writer:
                        self.writer.write(block)
                        self.writer.flush()
                if suppressed:
                    if not was_suppressed:
                        vad = Segmenter(
                            self.config.sample_rate,
                            self.config.speech_threshold,
                            self.config.silence_seconds,
                        )
                        if self.detector:
                            self.detector.reset()
                    was_suppressed = True
                    continue
                was_suppressed = False
                if self.monitor:
                    self.monitor.feed(block)
                active = (
                    self.detector.feed(block)
                    if self.detector
                    else self.last_input_rms >= self.config.speech_threshold
                )
                if active:
                    self.last_voice_at = at + len(block) / self.config.sample_rate
                if self.playing.is_set() and active:
                    overlap += len(block) / self.config.sample_rate
                    if overlap >= self.config.barge_in_seconds:
                        self.playback_stop.set()
                else:
                    overlap = 0.0
                segment = vad.feed(block, active=active)
                if segment is not None:
                    end = at + len(block) / self.config.sample_rate
                    self.on_segment(
                        segment,
                        end - len(segment) / self.config.sample_rate,
                        {
                            **vad.last_segment,
                            "segment_seconds": len(segment) / self.config.sample_rate,
                            "speech_end_at": end - vad.last_segment["trailing_silence_seconds"],
                            "vad_backend": self.config.vad_backend,
                        },
                    )
            segment = vad.flush()
            if segment is not None:
                self.on_segment(segment, time.monotonic() - len(segment) / self.config.sample_rate)
        except Exception as exc:  # noqa: BLE001 - report hardware/codec failure from this worker
            self.on_error(f"Audio consumer failed: {exc}")

    def record(self, path: Path):
        with self.record_lock:
            self.writer = sf.SoundFile(
                path, mode="w", samplerate=self.config.sample_rate, channels=1, subtype="PCM_16"
            )

    def stop_recording(self):
        with self.record_lock:
            if self.writer:
                self.writer.close()
                self.writer = None

    def begin(self) -> Playback:
        return Playback(self)

    def play(self, audio: np.ndarray, rate: int) -> float:
        playback = self.begin()
        try:
            playback.write(audio, rate)
        finally:
            played, _ = playback.finish()
        return played

    def close(self):
        self.playback_stop.set()
        if self.stream:
            self.stream.stop()
            self.stream.close()
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)
        self.stop_recording()
        if self.monitor:
            self.monitor.close()
            self.monitor = None


def dtmf(digits: str, rate: int = 48000) -> np.ndarray:
    rows, cols = [697, 770, 852, 941], [1209, 1336, 1477, 1633]
    keys = ["123A", "456B", "789C", "*0#D"]
    sounds = []
    for digit in digits:
        positions = [
            (r, c) for r, row in enumerate(keys) for c, key in enumerate(row) if key == digit
        ]
        if not positions:
            raise ValueError("DTMF supports 0-9, *, #, A-D only.")
        r, c = positions[0]
        t = np.arange(int(rate * 0.18)) / rate
        tone = 0.2 * (np.sin(2 * np.pi * rows[r] * t) + np.sin(2 * np.pi * cols[c] * t))
        ramp = min(240, len(tone) // 2)
        tone[:ramp] *= np.linspace(0, 1, ramp)
        tone[-ramp:] *= np.linspace(1, 0, ramp)
        sounds.extend([tone, np.zeros(int(rate * 0.12))])
    return np.concatenate(sounds).astype(np.float32) if sounds else np.zeros(0, np.float32)
