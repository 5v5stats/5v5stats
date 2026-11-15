"""Audio/video alignment using the demo's resume moment as a sync marker.

Every recording starts with the demo paused (frozen picture, silent game) and then resumes playback:
sound and motion start at the same in-game moment. Finding that onset in the raw video and in the raw
audio gives their exact offset, independent of capture/encoder start-up delays.
"""
import subprocess

import numpy as np

VIDEO_RATE = 60     # analysis resolution for video (frames per second of the capture)
AUDIO_HOP_MS = 2    # analysis resolution for audio


# runs while CS2 is recording the next clip: keep it from taking CPU time away from the game
LOW_PRIORITY = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)


def _audio_env(ffmpeg, path, hop_ms=AUDIO_HOP_MS):
    raw = subprocess.run([ffmpeg, "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", "48000", "-f", "s16le", "-"],
                         capture_output=True, creationflags=LOW_PRIORITY).stdout
    a = np.frombuffer(raw, np.int16).astype(float)
    hop = 48 * hop_ms
    n = len(a) // hop
    return np.sqrt((a[: n * hop].reshape(n, hop) ** 2).mean(1)) if n else np.zeros(0)


def _video_env(ffmpeg, path, seconds=None):
    w, h = 64, 36
    cmd = [ffmpeg, "-v", "error", "-i", path]
    if seconds:
        cmd += ["-t", str(seconds)]
    raw = subprocess.run(cmd + ["-vf", f"fps={VIDEO_RATE},scale={w}:{h},format=gray", "-f", "rawvideo", "-"],
                         capture_output=True, creationflags=LOW_PRIORITY).stdout
    v = np.frombuffer(raw, np.uint8)
    v = v[: len(v) // (w * h) * (w * h)].reshape(-1, h * w).astype(float)
    return np.r_[0, np.abs(np.diff(v, axis=0)).mean(1)] if len(v) > 1 else np.zeros(0)


def _onset(env, rate, quiet_until_s, min_level, factor=8, search_from_s=0.3):
    """First index after a quiet stretch where the signal jumps well above that stretch's level."""
    q = env[int(0.1 * rate): int(quiet_until_s * rate)]
    if len(q) < 3:
        return None
    thr = max(np.median(q) * factor, min_level)
    for i in range(int(search_from_s * rate), len(env)):
        if env[i] > thr:
            return i / rate
    return None


def video_onset(ffmpeg, raw_video, pre_roll):
    """Seconds into the raw capture where the frozen (paused) picture starts moving, or None."""
    v = _video_env(ffmpeg, raw_video, seconds=pre_roll + 3.0)
    return _onset(v, VIDEO_RATE, quiet_until_s=pre_roll * 0.6, min_level=2.0)


def audio_onset(ffmpeg, wav, pre_roll):
    """Seconds into the raw audio where the game goes from silent (paused) to sound, or None."""
    a = _audio_env(ffmpeg, wav)[: int((pre_roll + 3.0) * 1000 / AUDIO_HOP_MS)]
    return _onset(a, 1000 / AUDIO_HOP_MS, quiet_until_s=pre_roll * 0.6, min_level=150)


def resume_onsets(ffmpeg, raw_video, wav, pre_roll):
    """(video_onset_s, audio_onset_s) of the resume moment in each file, or None if not found clearly."""
    window = pre_roll + 3.0
    v = _video_env(ffmpeg, raw_video, seconds=window)
    a = _audio_env(ffmpeg, wav)
    a = a[: int(window * 1000 / AUDIO_HOP_MS)]
    tv = _onset(v, VIDEO_RATE, quiet_until_s=pre_roll * 0.6, min_level=2.0)
    ta = _onset(a, 1000 / AUDIO_HOP_MS, quiet_until_s=pre_roll * 0.6, min_level=150)
    if tv is None or ta is None:
        return None
    return tv, ta
