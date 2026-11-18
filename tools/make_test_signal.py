#!/usr/bin/env python3
"""Write the stepped-tone test signal used to measure the transmitter chain.

    python3 tools/make_test_signal.py test-signal.wav

Layout (all tones -20 dBFS, both channels unless noted):
    1 s silence, 3 s 1 kHz reference, then 2 s per frequency in TONES with
    0.25 s gaps, 3 s 1 kHz left only, 3 s 1 kHz right only, 5 s silence.
The engine's `radioplayer signal sweep` plays a coarser subset of these tones.
"""
import sys
import wave

import numpy as np

RATE = 44100
LEVEL_DB = -20.0
# Dense around 40-400 Hz, where modulators' audio stages tend to have bumps,
# and 1/3-octave-ish in the treble, where pre-emphasis shows.
TONES = [30, 40, 50, 63, 71, 80, 90, 100, 112, 125, 140, 160, 180, 200, 250, 315,
         400, 500, 800, 1000, 1250, 2000, 3150, 4000, 5000, 6300, 8000, 10000,
         12500, 14000, 15000]


def tone(freq, seconds):
    t = np.arange(int(seconds * RATE)) / RATE
    x = 10 ** (LEVEL_DB / 20) * np.sin(2 * np.pi * freq * t)
    fade = int(0.01 * RATE)
    env = np.ones(len(x))
    env[:fade] = np.linspace(0, 1, fade)
    env[-fade:] = np.linspace(1, 0, fade)
    return x * env


def silence(seconds):
    return np.zeros(int(seconds * RATE))


def main(path):
    both = [silence(1), tone(1000, 3), silence(0.25)]
    for f in TONES:
        both += [tone(f, 2), silence(0.25)]
    mono = np.concatenate(both)
    left = np.concatenate([mono, tone(1000, 3), silence(3.25), silence(5)])
    right = np.concatenate([mono, silence(3.25), tone(1000, 3), silence(5)])
    n = min(len(left), len(right))
    pcm = (np.stack([left[:n], right[:n]], axis=1) * 32767).round().astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    print(f"wrote {path}: {n / RATE:.1f} s")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "test-signal.wav")
