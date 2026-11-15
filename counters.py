"""Per-player match totals from a demo, in the same form 5v5stats uses (MatchZy-style counters), so a match
we analysed can be added to a player's 5v5stats totals and rated with the same formula (web/rating.py).

    counters(demo_path) -> {steamid: {total_matches, estimated_rounds, wins, losses, total_kills, ...}}
"""
import collections

import stats

GUN_EXCLUDE = ("knife", "bayonet", "grenade", "flashbang", "smokegrenade", "molotov", "incgrenade", "decoy", "c4",
               "inferno", "hegrenade", "world")
UTIL_WEAPONS = ("hegrenade", "inferno", "molotov", "incgrenade")


def _is_gun(weapon):
    w = str(weapon or "").lower()
    return bool(w) and not any(x in w for x in GUN_EXCLUDE)


def counters(demo_path):
    from demoparser2 import DemoParser
    res = stats.analyse(demo_path)
    players, rounds = res["players"], res["rounds"]
    p = DemoParser(demo_path)
    deaths = p.parse_event("player_death")
    hurts = p.parse_event("player_hurt").sort_values("tick")
    fires = p.parse_event("weapon_fire")
    for df in (deaths, hurts, fires):
        for col in ("user_steamid", "attacker_steamid"):
            if col in df.columns:
                df[col] = df[col].astype(str)

    c = collections.defaultdict(lambda: collections.Counter())
    active = [s for s in players if players[s]["rounds"]]
    team_score = {}  # final result per player: did their team win the match?
    for t in res["teams"].values():
        for s in t["players"]:
            team_score[s] = t["score"]
    best = max(team_score.values()) if team_score else 0
    tied = list(team_score.values()).count(best) == len(team_score) if team_score else False

    for r in rounds:
        side_of = {s: players[s]["side_by_round"].get(r["no"]) for s in active}
        side_of = {s: v for s, v in side_of.items() if v}
        in_round = lambda tick: r["start"] < tick <= r["end"]  # noqa: E731
        rd = deaths[deaths["tick"].map(in_round)].sort_values("tick")
        alive = {s for s in side_of}
        clutch = {}  # side -> (player, enemies) once that side is down to one player
        first = True
        for _, d in rd.iterrows():
            v, a = d["user_steamid"], d.get("attacker_steamid")
            if v not in side_of:
                continue
            if first:
                first = False
                if a in side_of and side_of[a] != side_of[v]:  # opening duel between enemies
                    c[a]["total_entry_count"] += 1
                    c[a]["total_entry_wins"] += 1
                    c[v]["total_entry_count"] += 1
            alive.discard(v)
            for side in set(side_of.values()):
                mine = [s for s in alive if side_of[s] == side]
                enemies = [s for s in alive if side_of[s] != side]
                if len(mine) == 1 and side not in clutch and enemies:
                    clutch[side] = (mine[0], len(enemies))
        for side, (s, n) in clutch.items():
            if n in (1, 2):
                c[s][f"total_v{n}_count"] += 1
                if side == r["winner"]:
                    c[s][f"total_v{n}_wins"] += 1
        for s, side in side_of.items():
            c[s]["t_rounds" if side == stats.T else "ct_rounds"] += 1

    for _, h in hurts.iterrows():
        a, v = h.get("attacker_steamid"), h["user_steamid"]
        if a in players and a != v:
            w = str(h.get("weapon", "")).lower()
            if _is_gun(w):
                c[a]["total_shots_on_tgt"] += 1
            if any(x in w for x in UTIL_WEAPONS):
                c[a]["total_util_damage"] += int(h.get("dmg_health") or 0)
    for _, f in fires.iterrows():
        s, w = f["user_steamid"], str(f.get("weapon", "")).lower()
        if s in players:
            if _is_gun(w):
                c[s]["total_shots_fired"] += 1
            elif "flashbang" in w:
                c[s]["total_flashes"] += 1

    out = {}
    for s in active:
        pl, x = players[s], c[s]
        won = (not tied) and team_score.get(s) == best
        out[s] = {"name": pl["name"], "total_matches": 1, "estimated_rounds": pl["rounds"],
                  "wins": int(won), "losses": int(not won and not tied),
                  "total_kills": pl["kills"], "total_deaths": pl["deaths"], "total_assists": pl["assists"],
                  "total_damage": int(round(pl["damage"])), "total_hs_kills": pl["hs"],
                  "total_multi": sum(pl["multi"].get(k, 0) for k in (3, 4, 5)),
                  **{k: int(x.get(k, 0)) for k in ("total_shots_fired", "total_shots_on_tgt", "total_v1_count",
                                                    "total_v1_wins", "total_v2_count", "total_v2_wins",
                                                    "total_entry_count", "total_entry_wins", "total_util_damage",
                                                    "total_flashes", "t_rounds", "ct_rounds")}}
    return out


if __name__ == "__main__":
    import json
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for sid, v in counters(sys.argv[1]).items():
        print(sid, json.dumps(v, ensure_ascii=False))
