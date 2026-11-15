"""Find and score highlight moments in a CS2 demo with a points system.

Every (player, round) gets points from four groups, and each point comes with a readable reason:
  impact      kills, multi-kill bonus, won clutches, opening / round-closing kills, bomb plays
  difficulty  headshots, noscopes, through smoke, while flashed, mid-air, wallbangs, long range, flicks,
              speed of the kills, Deagle headshots, knife / Zeus kills
  context     eco vs full buy, match point, overtime, last round of a half, close game
  penalties   kills on nearly dead enemies
All weights are in POINTS below, so the ranking is easy to tune.

Usage: python highlights.py <demo.dem> [--player NAME] [--min-score 100] [--all] [--json]
"""
import argparse
import json
import math
import os
import sys

import pandas as pd
from demoparser2 import DemoParser

TICKRATE = 64
LEAD_IN = 4 * TICKRATE    # shown before the first action of a segment
TAIL = 3 * TICKRATE       # shown after the last action (the last ~2 s carry the end badge and fade-out)
MAX_GAP = 10 * TICKRATE   # quieter stretches longer than this are cut out of the clip
T, CT = 2, 3
BOMB_TIMER = 40 * TICKRATE

POINTS = {
    # impact
    "kill": 10,
    "multi": {2: 5, 3: 20, 4: 45, 5: 80},      # extra on top of per-kill points
    "clutch": {1: 10, 2: 30, 3: 50, 4: 70, 5: 90},
    "clutch_low_hp": 10,                        # clutcher had < 35 HP when it became 1vN
    "opening_kill": 0,                          # first kill of the round: not worth more by itself
    "closing_kill": 5,                          # killed the last enemy alive
    "round_won": 5,
    "defuse_last_second": 40,                   # under 1 s left on the bomb
    "defuse_close": 15,                         # under 3 s left
    "defuse_under_fire": 10,                    # per enemy still alive when the defuse finished
    "he_damage_per_10": 1,                      # HE grenade: per 10 damage to enemies
    "he_multi_hit": 8,                          # per extra enemy hit by the same grenade
    "nade_kill": 15,                            # kill with HE / molotov
    "flash_assist": 6,
    # difficulty
    "headshot": 2,
    "noscope": 15,
    "through_smoke": 8,
    "while_flashed": 10,
    "in_air": 12,
    "wallbang": 8,
    "long_range": 5,                            # >= LONG_RANGE_M meters
    "flick": 6,                                 # crosshair moved >= FLICK_DEG in the last 125 ms
    "deagle_headshot": 4,
    "awp_kill": 2,
    "knife_kill": 30,
    "zeus_kill": 25,
    "fast_3k": 20,                              # 3+ kills within 3 s
    "quick_3k": 10,                             # 3+ kills within 6 s
    # context
    "eco_per_kill": 6,                          # own team equipment < 45% of the enemy's full buy
    "match_point": 10,
    "overtime": 10,
    "last_round_of_half": 5,
    "close_game": 5,                            # late in the game, score difference <= 2
    # penalties
    "weak_victim": -4,                          # victim had <= 20 HP before the killing hit
    # impact on the round, from the win-probability model in stats.py
    "swing_per_pct": 0.5,                       # per 1% the player changed their team's win chance
    "swing_floor": -10,                         # a negative swing costs at most this much
}
LONG_RANGE_M = 30
FLICK_DEG = 40
REGULATION_ROUNDS = 24
WIN_ROUNDS = 13

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _segments(events, round_end):
    """Group action ticks into segments; gaps longer than MAX_GAP are skipped."""
    events = sorted(events)
    segs, cur = [], [events[0], events[0]]
    for t in events[1:]:
        if t - cur[1] > MAX_GAP:
            segs.append(cur)
            cur = [t, t]
        else:
            cur[1] = t
    segs.append(cur)
    return [[max(1, s - LEAD_IN), min(e + TAIL, round_end + TAIL)] for s, e in segs]


def _angle_delta(y1, p1, y2, p2):
    dy = (y2 - y1 + 180) % 360 - 180
    return math.hypot(dy, p2 - p1)


class Cand:
    """Points collector for one player in one round."""

    def __init__(self, sid, name, rnd):
        self.sid, self.name, self.round = sid, name, rnd
        self.points, self.reasons, self.events = 0, [], []
        self.kills, self.headshots, self.weapons = 0, 0, set()
        self.clutch_vs, self.tags = 0, []

    def add(self, key, reason, n=1, value=None):
        pts = (POINTS[key] if value is None else value) * n
        if pts:
            self.points += pts
            self.reasons.append((reason, pts))

    def reason_text(self):
        """Group repeated reasons: 4x 'kill' -> '4 kills (+40)'."""
        grouped = {}
        for reason, pts in self.reasons:
            g = grouped.setdefault(reason, [0, 0])
            g[0] += 1
            g[1] += pts
        out = []
        for reason, (n, pts) in sorted(grouped.items(), key=lambda kv: -abs(kv[1][1])):
            label = reason if n == 1 else f"{n}x {reason}"
            out.append(f"{label} ({'+' if pts > 0 else ''}{pts})")
        return out


def _label(c):
    parts = []
    if c.kills >= 5:
        parts.append("ACE")
    elif c.kills >= 2:
        parts.append(f"{c.kills}K")
    if c.clutch_vs:
        parts.append(f"1v{c.clutch_vs} clutch")
    parts += c.tags
    if not parts:
        parts.append(f"{c.kills} kill" if c.kills == 1 else "play")
    return " + ".join(parts[:3])


def find_highlights(demo_path, player=None, min_kills=1, min_score=100):
    p = DemoParser(demo_path)
    deaths = p.parse_event("player_death", player=["team_num"],
                           other=["total_rounds_played", "is_warmup_period"])
    deaths = deaths[deaths["is_warmup_period"] == False].sort_values("tick")
    freeze = p.parse_event("round_freeze_end", other=["total_rounds_played"]).sort_values("tick")
    ends = p.parse_event("round_end").sort_values("tick")
    hurts = p.parse_event("player_hurt", player=["team_num"]).sort_values("tick")
    planted = _safe_event(p, "bomb_planted")
    defused = _safe_event(p, "bomb_defused")

    # ---- live rounds: last freeze end for each round index, then the next round end
    rounds = {}
    for _, f in freeze.groupby("total_rounds_played").tail(1).iterrows():
        rnd, start = int(f["total_rounds_played"]), int(f["tick"])
        after = ends[ends["tick"] > start]
        if after.empty:
            continue
        e = after.iloc[0]
        rounds[rnd] = {"start": start, "end": int(e["tick"]), "reason": str(e["reason"]),
                       "winner": {"CT": CT, "T": T}.get(str(e["winner"]))}
    if not rounds:
        return []

    # ---- player state at each round start: team, alive, equipment value
    st = p.parse_ticks(["team_num", "is_alive", "name", "current_equip_value"],
                       ticks=[r["start"] for r in rounds.values()])
    st["steamid"] = st["steamid"].astype(str)
    wins = {}  # roster key -> rounds won (teams swap sides, so identify a team by its players)
    for rnd in sorted(rounds):
        r = rounds[rnd]
        at = st[st["tick"] == r["start"]]
        alive = at[at["is_alive"] == True]
        r["alive"] = {tm: set(alive[alive["team_num"] == tm]["steamid"]) for tm in (T, CT)}
        r["names"] = dict(zip(at["steamid"], at["name"]))
        r["equip"] = {tm: float(at[at["team_num"] == tm]["current_equip_value"].mean() or 0) for tm in (T, CT)}
        keys = {tm: min(at[at["team_num"] == tm]["steamid"], default="?") for tm in (T, CT)}
        r["score_before"] = {tm: wins.get(keys[tm], 0) for tm in (T, CT)}
        if r["winner"]:
            wins[keys[r["winner"]]] = wins.get(keys[r["winner"]], 0) + 1

    cands = {}

    def cand(sid, rnd):
        key = (sid, rnd)
        if key not in cands:
            cands[key] = Cand(sid, rounds[rnd]["names"].get(sid, sid), rnd)
        return cands[key]

    flick_requests = []  # (cand, killer sid, kill tick)
    clutch_hp_requests = []

    for rnd, r in rounds.items():
        rd = deaths[(deaths["tick"] >= r["start"]) & (deaths["tick"] <= r["end"])]
        rh = hurts[(hurts["tick"] >= r["start"]) & (hurts["tick"] <= r["end"])]
        alive = {k: set(v) for k, v in r["alive"].items()}
        clutch = None
        first_kill_done = False
        for _, d in rd.iterrows():
            victim, killer = str(d["user_steamid"]), str(d["attacker_steamid"])
            vteam, kteam = d["user_team_num"], d["attacker_team_num"]
            for tm in (T, CT):
                alive[tm].discard(victim)
            if killer not in ("nan", "None", victim) and kteam != vteam:
                c = cand(killer, rnd)
                tick, w = int(d["tick"]), str(d["weapon"])
                c.kills += 1
                c.weapons.add(w)
                c.events.append(tick)
                c.add("kill", "kill")
                if not first_kill_done:
                    c.add("opening_kill", "opening kill of the round")
                if len(alive[vteam]) == 0:
                    c.add("closing_kill", "killed the last enemy")
                if bool(d["headshot"]):
                    c.headshots += 1
                    c.add("headshot", "headshot")
                    if w in ("deagle", "revolver"):
                        c.add("deagle_headshot", f"{w} headshot")
                if bool(d["noscope"]):
                    c.add("noscope", "noscope"); c.tags.append("noscope")
                if bool(d["thrusmoke"]):
                    c.add("through_smoke", "kill through smoke")
                if bool(d["attackerblind"]):
                    c.add("while_flashed", "kill while flashed")
                if bool(d["attackerinair"]):
                    c.add("in_air", "mid-air kill"); c.tags.append("mid-air")
                if int(d["penetrated"] or 0) > 0:
                    c.add("wallbang", "wallbang")
                if float(d["distance"] or 0) >= LONG_RANGE_M:
                    c.add("long_range", f"long-range kill (30+ m)")
                if w == "awp":
                    c.add("awp_kill", "AWP kill")
                if w.startswith("knife") or w == "bayonet":
                    c.add("knife_kill", "knife kill"); c.tags.append("knife kill")
                if w == "taser":
                    c.add("zeus_kill", "Zeus kill"); c.tags.append("Zeus kill")
                if w in ("hegrenade", "inferno", "molotov", "incgrenade"):
                    c.add("nade_kill", f"{'HE' if w == 'hegrenade' else 'molotov'} kill")
                # economy: own team far cheaper than a full-buying enemy
                me, them = r["equip"][kteam], r["equip"][vteam]
                if them >= 3500 and me < 0.45 * them:
                    c.add("eco_per_kill", "eco kill vs full buy")
                # nearly dead victim: damage of the killing hit = health they had left
                hit = rh[(rh["tick"] == tick) & (rh["user_steamid"].astype(str) == victim) & (rh["health"] == 0)]
                if not hit.empty and int(hit["dmg_health"].iloc[0]) <= 20:
                    c.add("weak_victim", "victim was almost dead")
                flick_requests.append((c, killer, tick))
                # flash assist
                if bool(d["assistedflash"]) and str(d["assister_steamid"]) not in ("nan", "None"):
                    a = cand(str(d["assister_steamid"]), rnd)
                    a.add("flash_assist", "flash assist"); a.events.append(tick)
                first_kill_done = True
            for tm, other in ((T, CT), (CT, T)):
                if clutch is None and len(alive[tm]) == 1 and len(alive[other]) >= 1 and len(r["alive"][tm]) > 1:
                    clutch = (next(iter(alive[tm])), len(alive[other]), int(d["tick"]), tm)
        if clutch and r["winner"] == clutch[3]:
            c = cand(clutch[0], rnd)
            c.clutch_vs = clutch[1]
            c.add("clutch", f"won a 1v{clutch[1]} clutch", value=POINTS["clutch"].get(clutch[1], 90))
            c.events += [clutch[2], r["end"]]
            clutch_hp_requests.append((c, clutch[0], clutch[2]))
        for c in [x for (s, rn), x in cands.items() if rn == rnd]:
            if r["winner"] and r["equip"]:
                team = st[(st["tick"] == r["start"]) & (st["steamid"] == c.sid)]["team_num"]
                if not team.empty and int(team.iloc[0]) == r["winner"]:
                    c.add("round_won", "round won")

        # HE grenades: group damage by thrower + tick
        he = rh[(rh["weapon"] == "hegrenade") & (rh["attacker_team_num"] != rh["user_team_num"])]
        for (att, tick), g in he.groupby([he["attacker_steamid"].astype(str), "tick"]):
            if att in ("nan", "None"):
                continue
            dmg, n = int(g["dmg_health"].sum()), g["user_steamid"].nunique()
            if dmg >= 60 or n >= 2:
                c = cand(att, rnd)
                c.add("he_damage_per_10", f"HE grenade: {dmg} damage", n=dmg // 10)
                if n >= 2:
                    c.add("he_multi_hit", f"HE hit {n} enemies", n=n - 1)
                    c.tags.append(f"HE hit {n}")
                c.events.append(int(tick))

        # bomb defuse: time left and enemies still alive
        if not defused.empty:
            for _, dd in defused[(defused["tick"] >= r["start"]) & (defused["tick"] <= r["end"] + 64)].iterrows():
                pl = planted[(planted["tick"] >= r["start"]) & (planted["tick"] <= dd["tick"])]
                if pl.empty:
                    continue
                left = (int(pl["tick"].iloc[-1]) + BOMB_TIMER - int(dd["tick"])) / TICKRATE
                c = cand(str(dd["user_steamid"]), rnd)
                c.events.append(int(dd["tick"]))
                if left < 1:
                    c.add("defuse_last_second", f"defused with {left:.1f} s left"); c.tags.append("last-second defuse")
                elif left < 3:
                    c.add("defuse_close", f"defused with {left:.1f} s left"); c.tags.append("close defuse")
                enemies = len(alive[T])
                if enemies:
                    c.add("defuse_under_fire", f"defused with {enemies} enemy alive", n=enemies)
                    c.tags.append("ninja defuse" if enemies >= 2 else "defuse under fire")

    # ---- per-candidate extras that need tick data: flicks, clutch HP
    _apply_flicks(p, flick_requests)
    _apply_clutch_hp(p, clutch_hp_requests)

    # ---- round impact (swing) from the win-probability model
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import stats
        sw = {(sid, no): pr["swing"] for sid, pl in stats.analyse(demo_path)["players"].items()
              for no, pr in pl["per_round"].items()}
    except Exception as e:  # never let the stats pass break highlight detection
        print(f"(swing unavailable: {e})")
        sw = {}

    # ---- multi-kill, speed, and match situation
    out = []
    for (sid, rnd), c in cands.items():
        r = rounds[rnd]
        if c.kills >= 2:
            c.add("multi", f"{_label_kills(c.kills)}", value=POINTS["multi"].get(c.kills, 80))
        kt = sorted(t for t in c.events)
        if c.kills >= 3:
            ks = sorted(kt)[:]
            span = (max(ks) - min(ks)) / TICKRATE
            if span <= 3:
                c.add("fast_3k", f"{c.kills} kills in {span:.1f} s")
            elif span <= 6:
                c.add("quick_3k", f"{c.kills} kills in {span:.1f} s")
        swing = sw.get((sid, rnd + 1))
        if swing is not None:
            pts = max(POINTS["swing_floor"], round(POINTS["swing_per_pct"] * swing * 100))
            if pts:
                c.add("swing_per_pct", f"swing {swing * 100:+.0f}% win chance", value=pts)
        if c.points >= 25:  # context only boosts plays that already matter
            s = r["score_before"]
            no = rnd + 1
            if no > REGULATION_ROUNDS:
                c.add("overtime", "overtime")
            elif max(s.values()) == WIN_ROUNDS - 1:
                c.add("match_point", "match point")
            if no == REGULATION_ROUNDS // 2:
                c.add("last_round_of_half", "last round of the half")
            if no >= 18 and abs(s[T] - s[CT]) <= 2:
                c.add("close_game", f"close game ({s[T]}-{s[CT]})")
        if c.kills < min_kills and not (c.clutch_vs or c.tags):
            continue
        if not c.events:
            continue
        out.append({
            "player": c.name, "steamid": sid, "round": rnd + 1,
            "kills": c.kills, "clutch_vs": c.clutch_vs, "headshots": c.headshots,
            "weapons": sorted(c.weapons), "label": _label(c), "score": c.points,
            "reasons": c.reason_text(),
            "start_tick": max(1, min(c.events) - LEAD_IN),
            "end_tick": min(max(c.events) + TAIL, r["end"] + TAIL),
            "segments": _segments(c.events, r["end"]),
        })

    clips = [c for c in out if (player is None or c["player"] == player) and c["score"] >= min_score]
    clips.sort(key=lambda c: (-c["score"], c["start_tick"]))
    return clips


def _label_kills(k):
    return "ace" if k >= 5 else f"{k}K"


def _safe_event(p, name):
    try:
        return p.parse_event(name).sort_values("tick")
    except Exception:
        return pd.DataFrame(columns=["tick", "user_steamid"])


def _apply_flicks(p, reqs):
    """Crosshair movement in the last 125 ms before each kill."""
    if not reqs:
        return
    ticks = sorted({t - o for _, _, t in reqs for o in range(0, 9)})
    df = p.parse_ticks(["yaw", "pitch"], ticks=ticks)
    df["steamid"] = df["steamid"].astype(str)
    idx = df.set_index(["steamid", "tick"])
    for c, sid, t in reqs:
        try:
            rows = [idx.loc[(sid, t - o)] for o in range(8, -1, -1)]
        except KeyError:
            continue
        best = max(_angle_delta(rows[0]["yaw"], rows[0]["pitch"], rr["yaw"], rr["pitch"]) for rr in rows[1:])
        if best >= FLICK_DEG:
            c.add("flick", f"{best:.0f}° flick")


def _apply_clutch_hp(p, reqs):
    if not reqs:
        return
    df = p.parse_ticks(["health"], ticks=[t for _, _, t in reqs])
    df["steamid"] = df["steamid"].astype(str)
    for c, sid, t in reqs:
        row = df[(df["steamid"] == sid) & (df["tick"] == t)]
        if not row.empty and int(row["health"].iloc[0]) < 35:
            c.add("clutch_low_hp", f"clutched on {int(row['health'].iloc[0])} HP")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("demo")
    ap.add_argument("--player")
    ap.add_argument("--min-score", type=int, default=100)
    ap.add_argument("--all", action="store_true", help="show every scored play (min score 0)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    clips = find_highlights(a.demo, a.player, min_score=0 if a.all else a.min_score)
    if a.json:
        print(json.dumps(clips, indent=1))
    else:
        for c in clips:
            secs = sum(e - s for s, e in c["segments"]) / TICKRATE
            print(f"{c['score']:>4}  R{c['round']:<3} {c['player']:<14} {c['label']:<28} {secs:5.1f}s")
            print("        " + ", ".join(c["reasons"]))
