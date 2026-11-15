"""Independent A/V sync check using the demo's weapon_fire ticks as ground truth.
For each shot of the spectated player: find the nearest audio onset and visual onset in the clip.
Usage: python verify_sync.py <demo.dem> <clips.json>  (single-segment clips only)"""
import json
import sys

import numpy as np
from demoparser2 import DemoParser

sys.path.insert(0, r"C:\cs2render")
import sync  # noqa: E402

FF = r"C:\cs2render\hlae\ffmpeg\bin\ffmpeg.exe"
demo, clips_json = sys.argv[1], sys.argv[2]
fires = DemoParser(demo).parse_event("weapon_fire")
fires["user_steamid"] = fires["user_steamid"].astype(str)


def rises(env, rate):
    d = np.maximum(np.diff(env, prepend=env[:1]), 0)
    thr = np.percentile(d, 97)
    return np.array([i / rate for i in range(1, len(d)) if d[i] > thr and d[i] >= d[i - 1]])


for c in json.load(open(clips_json, encoding="utf-8")):
    if len(c["segments"]) != 1:
        continue
    s0, s1 = c["segments"][0]
    shots = fires[(fires["user_steamid"] == c["steamid"]) & (fires["tick"] >= s0) & (fires["tick"] <= s1)]
    shots = shots[~shots["weapon"].str.contains("knife|grenade|flash|smoke|molotov|decoy", case=False)]
    t_shots = (shots["tick"].to_numpy() - s0) / 64
    a_on = rises(sync._audio_env(FF, c["file"], 2), 500)
    v_on = rises(sync._video_env(FF, c["file"]), 60)
    da = [a_on[np.argmin(abs(a_on - t))] - t for t in t_shots if len(a_on) and min(abs(a_on - t)) < 0.15]
    dv = [v_on[np.argmin(abs(v_on - t))] - t for t in t_shots if len(v_on) and min(abs(v_on - t)) < 0.15]
    if da and dv:
        print(f"{c['file'].split(chr(92))[-1]:40} shots {len(t_shots):3}  audio {np.median(da) * 1000:+5.0f} ms  "
              f"video {np.median(dv) * 1000:+5.0f} ms  ->  A/V offset {(np.median(da) - np.median(dv)) * 1000:+5.0f} ms")
