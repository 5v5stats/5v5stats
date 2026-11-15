"""Match summary page for a CS2 demo (uses stats.py): hero banner, MVP card with their best clip,
leader cards, sortable team tables and the rendered highlight clips.

Usage: python scoreboard_html.py <demo.dem> [out.html]
If out.html sits next to a render's clips.json, the clips are shown (and thumbnails made) too.
"""
import datetime
import json
import os
import re
import subprocess
import sys

import avatars
import counters
import stats

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "web"))
from matchpage import esc, render, summary  # noqa: E402,F401  (page design lives in web/matchpage.py)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.abspath(__file__))
FFMPEG = os.path.join(ROOT, "hlae", "ffmpeg", "bin", "ffmpeg.exe")


def played_at(demo_path):
    """When the match was played, from the demo's name (2026-11-08_06-03-46...), else the file time. ISO, local."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})", os.path.basename(demo_path))
    if m:
        return f"{m.group(1)}T{m.group(2)}:{m.group(3)}:{m.group(4)}"
    return datetime.datetime.fromtimestamp(os.path.getmtime(demo_path)).isoformat(timespec="seconds")


def opening_kills(demo_path, rounds):
    """Kills that were the first death of their round, per attacker steamid."""
    from demoparser2 import DemoParser
    d = DemoParser(demo_path).parse_event("player_death")
    d["attacker_steamid"] = d["attacker_steamid"].astype(str)
    d["user_steamid"] = d["user_steamid"].astype(str)
    out = {}
    for r in rounds:
        rd = d[(d["tick"] > r["start"]) & (d["tick"] <= r["end"])].sort_values("tick")
        if rd.empty:
            continue
        k = rd.iloc[0]
        a = k["attacker_steamid"]
        if a not in ("nan", "None", "0", "") and a != k["user_steamid"]:  # not a suicide / world kill
            out[a] = out.get(a, 0) + 1
    return out


def thumb(video, jpg, t=4.5):
    """One frame from a clip (after the title card), for posters and backgrounds."""
    if os.path.isfile(jpg) and os.path.getmtime(jpg) >= os.path.getmtime(video):
        return True
    os.makedirs(os.path.dirname(jpg), exist_ok=True)
    r = subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", str(t), "-i", video, "-frames:v", "1",
                        "-vf", "scale=960:-2", "-q:v", "4", jpg], capture_output=True)
    return r.returncode == 0 and os.path.isfile(jpg)


def load_clips(folder):
    """Rendered clips of a match folder (from clips.json), with thumbnails made next to them."""
    cj = os.path.join(folder, "clips.json")
    if not os.path.isfile(cj):
        return []
    with open(cj, encoding="utf-8") as f:
        raw = [c for c in json.load(f) if c and os.path.isfile(c.get("file", ""))]
    clips = []
    for i, c in enumerate(raw, 1):
        t1 = os.path.join(folder, "thumbs", f"{i:02d}.jpg")
        t2 = os.path.join(folder, "thumbs", f"{i:02d}_late.jpg")
        clips.append({"player": c["player"], "steamid": str(c.get("steamid", "")), "label": c["label"],
                      "round": c["round"], "score": c["score"], "file": os.path.basename(c["file"]),
                      "thumb": f"thumbs/{i:02d}.jpg" if thumb(c["file"], t1) else "",
                      "thumb2": f"thumbs/{i:02d}_late.jpg" if thumb(c["file"], t2, 9) else ""})
    return clips


def collect(demo_path, folder=None):
    """Everything a match page shows, as plain data (stored as-is by the web app's database).
    `folder` = the render output folder: its clips are included and profile pictures copied into it."""
    res = stats.analyse(demo_path)
    players, teams, rounds = res["players"], res["teams"], res["rounds"]
    order = sorted(teams.values(), key=lambda t: -t["score"])
    active = [s for s in players if players[s]["rounds"]]
    opening = opening_kills(demo_path, rounds)
    from demoparser2 import DemoParser
    header = DemoParser(demo_path).parse_header()
    m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(demo_path))
    team_of = {s: k for k, t in enumerate(order) for s in t["players"]}
    pl = {}
    for sid in active:
        p = players[sid]
        pl[sid] = {"name": p["name"], "team": min(team_of.get(sid, 0), 1), "rating": p["rating"],
                   "swing": p["swing_per_round"], "kills": p["kills"], "deaths": p["deaths"],
                   "assists": p["assists"], "adr": p["adr"], "kd": p["kd"], "kpr": p["kpr"], "hs": p["hs"],
                   "hs_pct": p["hs_pct"], "kast": p["kast"], "opening": opening.get(sid, 0),
                   "m5": p["multi"][5], "m4": p["multi"][4], "m3": p["multi"][3], "m2": p["multi"][2],
                   "mvps": p["mvps"], "damage": p["damage"], "rounds": p["rounds"]}
    return {
        "key": os.path.splitext(os.path.basename(demo_path))[0],  # identifies the match for re-uploads
        "map": str(header.get("map_name", "")).replace("de_", "").capitalize(),
        "date": m.group(1) if m else datetime.date.fromtimestamp(os.path.getmtime(demo_path)).isoformat(),
        "rounds": len(rounds),
        "teams": [{"name": t["name"], "score": t["score"], "halves": list(t["halves"]),
                   "avg_rating": t["avg_rating"], "players": [s for s in t["players"] if s in pl]} for t in order],
        "players": pl,
        "clips": load_clips(folder) if folder else [],
        "pics": avatars.copy_into(active, folder) if folder else {},
        # MatchZy-style totals per player, so the website can add this match to the 5v5stats ratings
        "counters": counters.counters(demo_path),
        "played_at": played_at(demo_path),
    }


def build(demo_path, out_path=None):
    folder = os.path.dirname(os.path.abspath(out_path)) if out_path else None
    return render(collect(demo_path, folder))


def write(demo_path, out_path):
    folder = os.path.dirname(os.path.abspath(out_path))
    data = collect(demo_path, folder)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(render(data))
    # short summary for the all-matches index (publish.py), full data for the web app (web/)
    with open(os.path.join(folder, "match.json"), "w", encoding="utf-8") as f:
        json.dump(summary(data), f, ensure_ascii=False, indent=1)
    with open(os.path.join(folder, "match_data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return out_path


if __name__ == "__main__":
    demo = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(os.path.basename(demo))[0] + "_scoreboard.html"
    print(write(demo, out))
