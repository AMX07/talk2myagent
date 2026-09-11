"""Local Silero ONNX speech detection, without a PyTorch runtime.

Model and tensor interface: https://github.com/snakers4/silero-vad (MIT).
The recurrent state belongs to one audio stream and is reset after speaker echo.
"""

from __future__ import annotations

import hashlib
import urllib.request
from pathlib import Path

import numpy as np

from .config import ROOT, private_dir
from .speech import resample

REVISION = "867c2aa692646a1f1de3e94a15c9dd9f614c0acb"
MODEL_SHA256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"
MODEL_URL = (
    f"https://raw.githubusercontent.com/snakers4/silero-vad/{REVISION}"
    "/src/silero_vad/data/silero_vad.onnx"
)


def download_vad() -> Path:
    path = private_dir(ROOT / "models") / "silero_vad.onnx"
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == MODEL_SHA256:
        return path
    with urllib.request.urlopen(MODEL_URL, timeout=60) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != MODEL_SHA256:
        raise RuntimeError("Silero model checksum mismatch; download was not installed.")
    part = path.with_suffix(".partial")
    part.write_bytes(data)
    part.replace(path)
    return path


class SileroDetector:
    def __init__(self, rate: int, threshold: float = 0.5):
        import onnxruntime as ort

        path = ROOT / "models/silero_vad.onnx"
        if not path.exists():
            raise RuntimeError("Speech detector missing. Run: uv run t2ma models")
        options = ort.SessionOptions()
        options.inter_op_num_threads = options.intra_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self.rate, self.threshold = rate, threshold
        self.reset()

    def reset(self):
        self.state = np.zeros((2, 1, 128), np.float32)
        self.context = np.zeros((1, 64), np.float32)
        self.pending = np.zeros(0, np.float32)
        self.probability = 0.0

    def feed(self, block: np.ndarray) -> bool:
        self.pending = np.concatenate((self.pending, resample(block, self.rate, 16000)))
        while len(self.pending) >= 512:
            chunk, self.pending = self.pending[:512], self.pending[512:]
            inputs = np.concatenate((self.context, chunk[None, :]), axis=1)
            output, self.state = self.session.run(
                None, {"input": inputs, "state": self.state, "sr": np.array(16000, np.int64)}
            )
            self.context = inputs[:, -64:]
            self.probability = float(output.item())
        return self.probability >= self.threshold
