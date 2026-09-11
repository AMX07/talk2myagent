from __future__ import annotations

import threading
import time
from collections import OrderedDict
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


# Whisper sometimes emits these on breath noise or hold music; drop them when alone.
HALLUCINATIONS = {
    "thank you.",
    "thanks for watching.",
    "thank you for watching.",
    "you",
    "bye.",
    ".",
    "thanks.",
    "subtitles by the amazing.org community",
}


class Speech:
    """Two inference locks: MLX (Whisper + conversation model) and ONNX (Kokoro).

    MLX model caches must not be used concurrently; Kokoro runs on the CPU
    through ONNX Runtime and may overlap with MLX work, which is what allows
    synthesis of one sentence while the next is still being generated.
    """

    def __init__(self, config: Settings):
        self.config = config
        self._tts = None
        self.lock = threading.Lock()
        self.tts_lock = threading.Lock()
        self.cache: OrderedDict[tuple[str, str], tuple[np.ndarray, int]] = OrderedDict()

    def _kokoro(self):
        if self._tts is None:
            from kokoro_onnx import Kokoro

            model = ROOT / "models/kokoro-v1.0.onnx"
            voices = ROOT / "models/voices-v1.0.bin"
            if not model.exists() or not voices.exists():
                raise RuntimeError("Speech models missing. Run: uv run t2ma models")
            self._tts = Kokoro(str(model), str(voices))
        return self._tts

    def synthesize(self, text: str, voice: str | None = None) -> tuple[np.ndarray, int]:
        key = (voice or self.config.voice, text.strip())
        with self.tts_lock:
            cached = self.cache.get(key)
            if cached is not None:
                self.cache.move_to_end(key)
                return cached
            audio, rate = self._kokoro().create(text, voice=key[0], lang="en-us")
            result = (np.asarray(audio, dtype=np.float32), rate)
            self.cache[key] = result
            while len(self.cache) > 48:
                self.cache.popitem(last=False)
            return result

    def prewarm(self, texts: list[str], voice: str | None = None) -> None:
        for text in texts:
            if text.strip():
                self.synthesize(text, voice)

    def transcribe(self, audio: np.ndarray, rate: int) -> dict:
        with self.lock:
            import mlx_whisper

            started = time.monotonic()
            audio = resample(audio, rate, 16000)
            result = mlx_whisper.transcribe(
                audio,
                path_or_hf_repo=self.config.stt_model,
                language="en",
                condition_on_previous_text=False,
                temperature=0,
            )
            text = " ".join(
                s["text"].strip()
                for s in result.get("segments", [])
                if s.get("no_speech_prob", 0) < 0.8
            ).strip()
            if text.lower() in HALLUCINATIONS:
                text = ""
            return {"text": text, "inference_seconds": round(time.monotonic() - started, 3)}

    def transcribe_file(self, path: Path) -> dict:
        audio, rate = sf.read(path, dtype="float32", always_2d=True)
        return self.transcribe(audio.mean(axis=1), rate)
