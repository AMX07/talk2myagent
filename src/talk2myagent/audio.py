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
        {"index": i, "name": d["name"], "inputs": d["max_input_channels"],
         "outputs": d["max_output_channels"], "sample_rate": d["default_samplerate"]}
        for i, d in enumerate(sd.query_devices())
    ]


def device_index(name: str, direction: str) -> int:
    matches = [d for d in devices() if d["name"] == name and d[direction] > 0]
    if len(matches) != 1:
        raise ValueError(f"Expected one {direction} device named {name!r}; found {len(matches)}.")
    return matches[0]["index"]


class Segmenter:
    """RMS VAD for the MVP. Silence is not sent to Whisper."""

    def __init__(self, rate: int, threshold: float, silence: float):
        self.rate, self.threshold, self.silence = rate, threshold, silence
        self.preroll: deque[np.ndarray] = deque(maxlen=5)
        self.parts: list[np.ndarray] = []
        self.quiet = self.voiced = 0.0

    def feed(self, block: np.ndarray) -> np.ndarray | None:
        duration = len(block) / self.rate
        active = float(np.sqrt(np.mean(block * block))) >= self.threshold
        if not self.parts:
            if active:
                self.parts = list(self.preroll)
                self.preroll.clear()
            else:
                self.preroll.append(block)
                return None
        self.parts.append(block)
        self.voiced += duration if active else 0
        self.quiet = 0 if active else self.quiet + duration
        if self.quiet >= self.silence or sum(map(len, self.parts)) >= self.rate * 20:
            audio = np.concatenate(self.parts) if self.voiced >= 0.15 else None
            self.parts = []
            self.quiet = self.voiced = 0.0
            return audio
        return None

    def flush(self) -> np.ndarray | None:
        audio = np.concatenate(self.parts) if self.parts and self.voiced >= 0.15 else None
        self.parts = []
        self.voiced = self.quiet = 0.0
        return audio


class AudioBridge:
    def __init__(self, config: Settings, on_segment, on_error):
        self.config, self.on_segment, self.on_error = config, on_segment, on_error
        self.blocks: queue.Queue = queue.Queue(maxsize=500)
        self.stop = threading.Event()
        self.record_lock = threading.Lock()
        self.writer = None
        self.thread = None
        self.stream = None
        self.playback_stop = threading.Event()

    def start(self):
        if self.config.input_device == self.config.output_device:
            raise ValueError("Input and output must be different audio buses to prevent feedback.")
        incoming = device_index(self.config.input_device, "inputs")
        device_index(self.config.output_device, "outputs")
        self.stream = sd.InputStream(
            device=incoming, samplerate=self.config.sample_rate, channels=1,
            dtype="float32", blocksize=int(self.config.sample_rate * 0.02),
            callback=self._callback,
        )
        self.stream.start()
        self.thread = threading.Thread(target=self._consume, daemon=True)
        self.thread.start()

    def _callback(self, indata, frames, timing, status):
        if status:
            self.on_error(f"Audio input: {status}")
        try:
            self.blocks.put_nowait((indata[:, 0].copy(), time.monotonic() - frames / self.config.sample_rate))
        except queue.Full:
            self.on_error("Audio capture overflow; recording/transcript may have a gap.")

    def _consume(self):
        vad = Segmenter(self.config.sample_rate, self.config.speech_threshold, self.config.silence_seconds)
        try:
            while not self.stop.is_set() or not self.blocks.empty():
                try:
                    block, at = self.blocks.get(timeout=0.1)
                except queue.Empty:
                    continue
                with self.record_lock:
                    if self.writer:
                        self.writer.write(block)
                        self.writer.flush()
                segment = vad.feed(block)
                if segment is not None:
                    self.on_segment(segment, at + len(block)/self.config.sample_rate - len(segment)/self.config.sample_rate)
            segment = vad.flush()
            if segment is not None:
                self.on_segment(segment, time.monotonic() - len(segment)/self.config.sample_rate)
        except Exception as exc:
            self.on_error(f"Audio consumer failed: {exc}")

    def record(self, path: Path):
        with self.record_lock:
            self.writer = sf.SoundFile(path, mode="w", samplerate=self.config.sample_rate,
                                       channels=1, subtype="PCM_16")

    def stop_recording(self):
        with self.record_lock:
            if self.writer:
                self.writer.close()
                self.writer = None

    def play(self, audio: np.ndarray, rate: int) -> float:
        outgoing = device_index(self.config.output_device, "outputs")
        samples = resample(audio, rate, self.config.sample_rate)
        self.playback_stop.clear()
        written = 0
        with sd.OutputStream(device=outgoing, samplerate=self.config.sample_rate,
                             channels=1, dtype="float32", blocksize=960) as stream:
            for offset in range(0, len(samples), 960):
                if self.playback_stop.is_set():
                    break
                block = samples[offset:offset + 960]
                underflow = stream.write(block)
                if underflow:
                    self.on_error("Audio output underflow; remote party may have heard a gap.")
                written += len(block)
        return written / self.config.sample_rate

    def close(self):
        self.playback_stop.set()
        if self.stream:
            self.stream.stop()
            self.stream.close()
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)
        self.stop_recording()


def dtmf(digits: str, rate: int = 48000) -> np.ndarray:
    rows, cols = [697, 770, 852, 941], [1209, 1336, 1477, 1633]
    keys = ["123A", "456B", "789C", "*0#D"]
    sounds = []
    for digit in digits:
        positions = [(r, c) for r, row in enumerate(keys) for c, key in enumerate(row) if key == digit]
        if not positions:
            raise ValueError("DTMF supports 0-9, *, #, A-D only.")
        r, c = positions[0]
        t = np.arange(int(rate * 0.18)) / rate
        tone = 0.2 * (np.sin(2*np.pi*rows[r]*t) + np.sin(2*np.pi*cols[c]*t))
        ramp = min(240, len(tone)//2)
        tone[:ramp] *= np.linspace(0, 1, ramp)
        tone[-ramp:] *= np.linspace(1, 0, ramp)
        sounds.extend([tone, np.zeros(int(rate * 0.12))])
    return np.concatenate(sounds).astype(np.float32) if sounds else np.zeros(0, np.float32)
