import threading

import numpy as np
import sounddevice as sd

from .audio import device_index
from .config import Settings


def loopback_test(config: Settings, seconds: float = 1) -> dict:
    """Write a tone into each virtual bus and read it back from the same device.

    Stream order matters: within one process, opening BlackHole's input before
    its output leaves the input reading silence. Opening the output first and
    letting it run the device clock captures the tone reliably. Across processes
    (the real call, where Phone owns the other end) either order works.
    """
    if not 0.2 <= seconds <= 3:
        raise ValueError("Test duration must be 0.2–3 seconds.")
    if config.input_device == config.output_device:
        raise ValueError("Two distinct virtual buses are required.")
    results = []
    for name in [config.input_device, config.output_device]:
        if "blackhole" not in name.lower():
            raise ValueError("This diagnostic writes a test tone; select BlackHole devices only.")
        incoming, outgoing = device_index(name, "inputs"), device_index(name, "outputs")
        rate = config.sample_rate
        blocks = []
        t = np.arange(int(rate * seconds)) / rate
        audio = (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        with sd.OutputStream(
            device=outgoing, channels=1, samplerate=rate, dtype="float32", blocksize=960
        ) as speaker:
            threading.Event().wait(0.2)
            with sd.InputStream(
                device=incoming,
                channels=1,
                samplerate=rate,
                dtype="float32",
                blocksize=960,
                callback=lambda data, frames, timing, status, target=blocks: target.append(
                    data[:, 0].copy()
                ),
            ):
                speaker.write(audio)
                threading.Event().wait(0.3)
        captured = np.concatenate(blocks) if blocks else np.zeros(1)
        rms = float(np.sqrt(np.mean(captured**2)))
        results.append(
            {
                "device": name,
                "captured_rms": round(rms, 4),
                "captured_peak": round(float(np.max(np.abs(captured))), 4),
                "passed": rms > 0.005,
            }
        )
    return {
        "passed": all(r["passed"] for r in results),
        "buses": results,
        "scope": "Local loopback only. Does not verify Phone routing or remote reception.",
    }
