"""Estimate audio/video offset of a clip: cross-correlate audio loudness with visual change.
Positive result = audio is late. Usage: python check_sync.py clip.mp4 [more.mp4 ...]"""
import subprocess
import sys

import numpy as np

FF = r"C:\cs2render\hlae\ffmpeg\bin\ffmpeg.exe"
FPS = 60


def audio_env(path):
    raw = subprocess.run([FF, "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", "48000", "-f", "s16le", "-"],
                         capture_output=True).stdout
    a = np.frombuffer(raw, np.int16).astype(float)
    hop = 48000 // FPS
    n = len(a) // hop
    return np.sqrt((a[: n * hop].reshape(n, hop) ** 2).mean(1))


def video_env(path):
    w, h = 64, 36
    raw = subprocess.run([FF, "-v", "error", "-i", path, "-vf", f"fps={FPS},scale={w}:{h},format=gray",
                          "-f", "rawvideo", "-"], capture_output=True).stdout
    v = np.frombuffer(raw, np.uint8).reshape(-1, h * w).astype(float)
    return np.r_[0, np.abs(np.diff(v, axis=0)).mean(1)]


def onsets(x):
    d = np.maximum(np.diff(x, prepend=x[0]), 0)  # rises only (shots, impacts)
    return (d - d.mean()) / (d.std() + 1e-9)


for path in sys.argv[1:]:
    a, v = onsets(audio_env(path)), onsets(video_env(path))
    n = min(len(a), len(v))
    a, v = a[:n], v[:n]
    lags = range(-30, 31)  # +-500 ms
    scores = [np.dot(a[max(0, L):n + min(0, L)], v[max(0, -L):n - max(0, L)]) for L in lags]
    best = list(lags)[int(np.argmax(scores))]
    print(f"{path}: audio is {'late' if best > 0 else 'early'} by {abs(best) * 1000 / FPS:.0f} ms "
          f"(confidence {max(scores) / (np.mean(np.abs(scores)) + 1e-9):.1f}x)")
