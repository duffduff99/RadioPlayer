#!/usr/bin/env python3
"""Find the test-signal tones in a recording and report each one's level.

    python3 tools/analyze_recording.py recording.wav

Works on recordings of `radioplayer signal sweep` or of the file written by
make_test_signal.py. Levels are relative to the 1 kHz tone. Needs numpy and
a 16/24/32-bit PCM WAV file.
"""
import sys
import wave

import numpy as np

TONES = [30, 50, 80, 125, 200, 315, 500, 800, 1000, 1250, 2000, 3150, 5000, 6300,
         8000, 10000, 12500, 14000, 15000]


def read_wav(path):
    with wave.open(path, "rb") as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    raw = raw[: len(raw) // (width * channels) * width * channels]
    if width == 2:
        x = np.frombuffer(raw, "<i2").astype(float) / 2 ** 15
    elif width == 3:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        x = np.where(v >= 1 << 23, v - (1 << 24), v) / 2 ** 23
    elif width == 4:
        x = np.frombuffer(raw, "<i4").astype(float) / 2 ** 31
    else:
        raise SystemExit(f"unsupported sample width: {width} bytes")
    return rate, x.reshape(-1, channels)


def tone_levels(rate, x):
    """Best (loudest steady) level in dB of each tone, from 0.5 s windows."""
    win = int(0.5 * rate)
    window = np.hanning(win)
    freqs = np.fft.rfftfreq(win, 1 / rate)
    best = {}
    for start in range(0, len(x) - win, win // 4):
        seg = x[start:start + win]
        rms = np.sqrt(np.mean(seg ** 2))
        if rms < 1e-5:
            continue
        spec = np.abs(np.fft.rfft(seg * window))
        peak = freqs[np.argmax(spec)]
        # Only count windows that are one clean tone.
        if spec.max() ** 2 < 0.9 * np.sum(spec ** 2) * 0.5:
            continue
        for f in TONES:
            if abs(peak - f) <= max(3.0, f * 0.02):
                best[f] = max(best.get(f, -200.0), 20 * np.log10(rms * np.sqrt(2)))
    return best


def main(path):
    rate, x = read_wav(path)
    print(f"{path}: {len(x) / rate:.1f} s, {rate} Hz, {x.shape[1]} channel(s)")
    for ch in range(x.shape[1]):
        levels = tone_levels(rate, x[:, ch])
        if 1000 not in levels:
            print(f"channel {ch + 1}: no 1 kHz reference tone found")
            continue
        ref = levels[1000]
        print(f"channel {ch + 1}: 1 kHz at {ref:.1f} dBFS peak; other tones relative to it:")
        for f in TONES:
            print(f"  {f:>6} Hz  " + (f"{levels[f] - ref:+6.1f} dB" if f in levels else "   (not found)"))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
