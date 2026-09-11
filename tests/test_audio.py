import numpy as np

from talk2myagent.audio import Segmenter, dtmf
from talk2myagent.speech import resample


def test_vad_ignores_silence_and_emits_on_pause():
    vad = Segmenter(16000, 0.008, 0.3)
    quiet = np.zeros(320, np.float32)
    voice = np.ones(320, np.float32) * 0.05
    for _ in range(100):
        assert vad.feed(quiet) is None
    for _ in range(20):
        assert vad.feed(voice) is None
    result = None
    for _ in range(20):
        segment = vad.feed(quiet)
        if segment is not None:
            result = segment
    assert result is not None and np.max(result) == 0.05
    assert vad.flush() is None


def test_vad_keeps_final_utterance_without_trailing_silence():
    vad = Segmenter(16000, 0.008, 0.5)
    for _ in range(20):
        vad.feed(np.ones(320, np.float32) * 0.05)
    assert len(vad.flush()) == 6400


def test_neural_activity_ends_turn_despite_background_above_rms_threshold():
    vad = Segmenter(16000, 0.008, 0.7)
    noise = np.full(320, 0.012, np.float32)
    for _ in range(100):
        assert vad.feed(noise, active=False) is None
    for _ in range(50):
        assert vad.feed(noise, active=True) is None
    segments = [vad.feed(noise, active=False) for _ in range(40)]
    assert len([s for s in segments if s is not None]) == 1
    assert vad.last_segment["endpoint_reason"] == "silence"
    assert abs(vad.last_segment["trailing_silence_seconds"] - 0.7) < 0.03


def test_continuous_speech_still_has_a_bounded_segment():
    vad = Segmenter(16000, 0.008, 0.7)
    voice = np.ones(320, np.float32) * 0.05
    segments = [vad.feed(voice, active=True) for _ in range(1001)]
    assert len([s for s in segments if s is not None]) == 1
    assert vad.last_segment["endpoint_reason"] == "max_duration"


def test_dtmf_has_correct_frequency_pair():
    tone = dtmf("5", 48000)[:8640]
    spectrum = abs(np.fft.rfft(tone))
    freqs = np.fft.rfftfreq(len(tone), 1 / 48000)
    peaks = freqs[np.argsort(spectrum)[-8:]]
    assert min(abs(peaks - 770)) < 6
    assert min(abs(peaks - 1336)) < 6
    assert np.max(abs(tone)) <= 0.4


def test_resample_preserves_duration():
    original = np.sin(2 * np.pi * 440 * np.arange(48000) / 48000).astype(np.float32)
    output = resample(original, 48000, 16000)
    assert len(output) == 16000
    assert abs(np.sqrt(np.mean(output**2)) - 0.707) < 0.01
