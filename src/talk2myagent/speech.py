from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

from .config import ROOT, Settings


def resample(audio: np.ndarray, source: int, target: int) -> np.ndarray:
    if source == target:
        return audio.astype(np.float32)
    divisor = int(np.gcd(source, target))
    return resample_poly(audio, target // divisor, source // divisor).astype(np.float32)


class Speech:
    """One shared inference lock; MLX model caches must not be used concurrently."""

    def __init__(self, config: Settings):
        self.config = config
        self._tts = None
        self.lock = threading.Lock()

    def synthesize(self, text: str, voice: str | None = None) -> tuple[np.ndarray, int]:
        with self.lock:
            if self._tts is None:
                from kokoro_onnx import Kokoro

                model = ROOT / "models/kokoro-v1.0.onnx"
                voices = ROOT / "models/voices-v1.0.bin"
                if not model.exists() or not voices.exists():
                    raise RuntimeError("Speech models missing. Run: uv run t2ma models")
                self._tts = Kokoro(str(model), str(voices))
            audio, rate = self._tts.create(text, voice=voice or self.config.voice, lang="en-us")
            return np.asarray(audio, dtype=np.float32), rate

    def transcribe(self, audio: np.ndarray, rate: int) -> dict:
        with self.lock:
            import mlx_whisper

            started = time.monotonic()
            audio = resample(audio, rate, 16000)
            result = mlx_whisper.transcribe(
                audio, path_or_hf_repo=self.config.stt_model, language="en",
                condition_on_previous_text=False, temperature=0,
            )
            text = " ".join(
                s["text"].strip() for s in result.get("segments", [])
                if s.get("no_speech_prob", 0) < 0.8
            ).strip()
            return {"text": text, "inference_seconds": round(time.monotonic() - started, 3)}

    def transcribe_file(self, path: Path) -> dict:
        audio, rate = sf.read(path, dtype="float32", always_2d=True)
        return self.transcribe(audio.mean(axis=1), rate)
