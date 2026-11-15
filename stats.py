"""KAST and Swing (win-probability impact) for every player in a CS2 demo.

KAST  = % of rounds played where the player got a Kill, an Assist (incl. flash assist), Survived the
        round, or was Traded (their killer was killed by a teammate within TRADE_WINDOW seconds).

Swing = how much a player changed their team's chance of winning each round, averaged per round played.
        A win-probability model P(T wins | state) is evaluated before and after every kill, plant and
        defuse. The change is credited:
          kill    killer gets KILLER_FLAT of it (less if someone flashed the victim), the rest is split
                  by damage dealt to the victim that round; the flasher gets FLASH_SHARE
          death   the victim loses what their team lost (halved if they were traded)
          plant / defuse   the planter / defuser
        The clock running down is not credited to anyone.

State used by the model: players alive (weighted by HP), average equipment value of the alive players,
bomb planted or not, round/bomb time left, and whether CT can still defuse in time (kit or no kit).
The default coefficients encode typical competitive win rates; `--fit` re-estimates them from your
demos (regularised towards the defaults) and `--evaluate` reports how well each version predicts.

Usage:
  python stats.py <demo.dem>                     scoreboard (K, D, A, ADR, KAST, Swing)
  python stats.py <demo.dem> --rounds            + every player's swing per round
  python stats.py --evaluate a.dem b.dem ...     how well the win model predicts round winners
  python stats.py --fit a.dem b.dem ...          refit the model on these demos (saved to win_model.json)
"""
import argparse
import json
import math
import re
import os
import sys

import numpy as np
from demoparser2 import DemoParser

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.abspath(__file__))
MODEL_FILE = os.path.join(ROOT, "win_model.json")
TICKRATE = 64
T, CT = 2, 3
TRADE_WINDOW = 5.0        # seconds
KILLER_FLAT = 0.4         # share of a kill's swing that always goes to the killer
FLASH_SHARE = 0.1         # share for a flash assist (taken from the killer's flat part)
TRADED_DEATH_FACTOR = 0.5 # a traded death costs half
PLANT_TIME = 3.2          # seconds to plant
PRESSURE_TIME = 20.0      # pre-plant: below this much time left, T's chances fall off

# Default model: logit P(T wins) = sum(coef * feature). Calibrated to typical competitive win rates:
# 5v5 ~47% T, 5v4 ~66% for the side up a player, 1v2 ~20%, 1v3 ~8%, eco vs full buy ~12%,
# post-plant 5v5 ~72% T, bomb timer adds pressure in the last ~15 s.
DEFAULT_COEF = {
    "bias": -0.12,          # slight CT-side advantage
    "diff": 0.55,           # effective player difference (nT - nCT)
    "ratio": 1.03,          # ln(nT / nCT)  (Lanchester-style: matters more when few players are left)
    "equip": 1.2,           # ln(avg equipment T / avg equipment CT)
    "planted": 1.0,         # bomb planted
    "bomb_time": 1.5,       # (planted) pressure as the bomb timer runs out
    "time_pressure": -3.0,  # (not planted) T running out of time to plant
}
FEATURES = list(DEFAULT_COEF)


def load_coef():
    if os.path.isfile(MODEL_FILE):
        with open(MODEL_FILE, encoding="utf-8") as f:
            return {**DEFAULT_COEF, **json.load(f)["coef"]}
    return dict(DEFAULT_COEF)


# ------------------------------------------------------------------------------- win model ----

def features(s):
    """s: dict with nT, nCT (alive counts), eT, eCT (HP-weighted counts), qT, qCT (avg equipment),
    planted, t_left (round seconds left), bomb_left, kit."""
    f = {k: 0.0 for k in FEATURES}
    f["bias"] = 1.0
    f["diff"] = s["eT"] - s["eCT"]
    f["ratio"] = math.log(s["eT"] / s["eCT"])
    f["equip"] = math.log((s["qT"] + 300) / (s["qCT"] + 300))
    if s["planted"]:
        f["planted"] = 1.0
        need = defuse_need(s)
        f["bomb_time"] = max(0.0, 15.0 - (s["bomb_left"] - need)) / 15.0
    else:
        f["time_pressure"] = max(0.0, PRESSURE_TIME - s["t_left"]) / PRESSURE_TIME
    return f


def defuse_need(s):
    """Seconds CT still needs to defuse: what's left of a defuse in progress, else a full defuse."""
    if s.get("defuse_left") is not None:
        return max(0.0, s["defuse_left"])
    return 5.0 if s["kit"] else 10.0


def terminal(s):
    """Return P(T wins) for decided / near-decided states, else None."""
    if s["planted"]:
        need = defuse_need(s)
        if s["nCT"] == 0 or s["bomb_left"] < need:
            return 1.0
        if s["nT"] == 0:  # CTs only need to reach the bomb and defuse in time
            if s.get("defuse_left") is not None:
                return 0.0  # already defusing, nobody left to stop it, and it finishes in time
            slack = s["bomb_left"] - need
            return 1.0 / (1.0 + math.exp((slack - 3.0) / 1.2))
        return None
    if s["nT"] == 0 or s["t_left"] <= 0:
        return 0.0
    if s["nCT"] == 0:
        return 1.0
    if s["t_left"] < PLANT_TIME:  # can only win by killing everyone in a few seconds
        return 0.02 if s["nCT"] > 1 else 0.1
    return None


def p_t(s, coef):
    t = terminal(s)
    if t is not None:
        return t
    f = features(s)
    z = sum(coef[k] * f[k] for k in FEATURES)
    return 1.0 / (1.0 + math.exp(-z))


# ------------------------------------------------------------------------------- demo parsing ----

def _event(p, name, **kw):
    try:
        return p.parse_event(name, **kw).sort_values("tick")
    except Exception:
        import pandas as pd
        return pd.DataFrame(columns=["tick"])


_CACHE = {}


def analyse(demo_path, coef=None):
    """Full pass over a demo (cached per file, so highlights and both scoreboards share one pass)."""
    if coef is not None:
        return _analyse(demo_path, coef)
    key = (os.path.abspath(demo_path), os.path.getmtime(demo_path), os.path.getsize(demo_path))
    if key not in _CACHE:
        _CACHE[key] = _analyse(demo_path, None)
    return _CACHE[key]


def _analyse(demo_path, coef=None):
    """Full pass over a demo. Returns dict with players, per-round swing/KAST, model states."""
    coef = coef or load_coef()
    p = DemoParser(demo_path)
    cvars = {}
    sc = _event(p, "server_cvar")
    for _, r in sc.iterrows():
        cvars[r.get("name")] = r.get("value")
    round_time = float(cvars.get("mp_roundtime") or 1.92) * 60
    c4_time = float(cvars.get("mp_c4timer") or 40)

    deaths = p.parse_event("player_death", player=["team_num"], other=["is_warmup_period"])
    deaths = deaths[deaths["is_warmup_period"] == False].sort_values("tick")
    hurts = p.parse_event("player_hurt", player=["team_num"]).sort_values("tick")
    freeze = _event(p, "round_freeze_end", other=["total_rounds_played"])
    ends = _event(p, "round_end")
    plants = _event(p, "bomb_planted")
    defuses = _event(p, "bomb_defused")
    begins = _event(p, "bomb_begindefuse")
    for df in (deaths, hurts, plants, defuses, begins):
        for col in ("user_steamid", "attacker_steamid", "assister_steamid"):
            if col in df.columns:
                df[col] = df[col].astype(str)

    rounds = []
    for _, f in freeze.groupby("total_rounds_played").tail(1).iterrows():
        start = int(f["tick"])
        after = ends[ends["tick"] > start]
        if after.empty:
            continue
        e = after.iloc[0]
        rounds.append({"no": int(f["total_rounds_played"]) + 1, "start": start, "end": int(e["tick"]),
                       "winner": {"CT": CT, "T": T}.get(str(e["winner"])), "reason": str(e["reason"])})
    rounds = [r for r in rounds if r["winner"]]

    # events in each round -> ticks we need player state for (tick before each event)
    ev_ticks = set()
    for r in rounds:
        ev_ticks.add(r["start"])
        for df in (deaths, plants, defuses):
            for t in df[(df["tick"] > r["start"]) & (df["tick"] <= r["end"])]["tick"]:
                ev_ticks.add(int(t) - 1)
    st = p.parse_ticks(["team_num", "is_alive", "health", "current_equip_value", "has_defuser", "name"],
                       ticks=sorted(ev_ticks))
    st["steamid"] = st["steamid"].astype(str)
    # empty or disconnected player slots come back with no team; drop them, fill gaps in the rest
    st = st[st["team_num"].notna()].copy()
    st["team_num"] = st["team_num"].astype(int)
    st["health"] = st["health"].fillna(0)
    st["is_alive"] = st["is_alive"].fillna(False)
    st["current_equip_value"] = st["current_equip_value"].fillna(0)
    st["has_defuser"] = st["has_defuser"].fillna(False)
    by_tick = {t: g.set_index("steamid") for t, g in st.groupby("tick")}

    players = {}   # sid -> stats
    model_rows = []  # (features dict or terminal, label) for evaluation/fit

    def P(sid, name):
        if sid not in players:
            players[sid] = {"name": name, "rounds": 0, "kills": 0, "deaths": 0, "assists": 0, "damage": 0,
                            "kast_rounds": 0, "swing": 0.0, "per_round": {}, "team_key": None, "hs": 0,
                            "multi": {2: 0, 3: 0, 4: 0, 5: 0}, "mvps": 0, "side_by_round": {}}
        return players[sid]

    for r in rounds:
        g0 = by_tick.get(r["start"])
        if g0 is None:
            continue
        team = {sid: int(row["team_num"]) for sid, row in g0.iterrows() if int(row["team_num"]) in (T, CT)}
        alive = {sid for sid, row in g0.iterrows() if sid in team and bool(row["is_alive"])}
        names = {sid: row["name"] for sid, row in g0.iterrows()}
        for sid in team:
            pl = P(sid, names[sid])
            pl["rounds"] += 1
            pl["name"] = names[sid]
            pl["per_round"][r["no"]] = {"swing": 0.0, "k": 0, "a": 0, "s": True, "t": False, "events": []}

        rd = deaths[(deaths["tick"] > r["start"]) & (deaths["tick"] <= r["end"])]
        rh = hurts[(hurts["tick"] > r["start"]) & (hurts["tick"] <= r["end"])].copy()
        hp = {sid: max(0, int(row["health"])) for sid, row in g0.iterrows()}
        real = []
        for u, h_after, raw in zip(rh["user_steamid"], rh["health"], rh["dmg_health"]):
            before = hp.get(u, 100)
            real.append(max(0, min(int(raw), before)))
            hp[u] = max(0, int(h_after))
        rh["dmg_real"] = real
        rp = plants[(plants["tick"] > r["start"]) & (plants["tick"] <= r["end"])]
        rdef = defuses[(defuses["tick"] > r["start"]) & (defuses["tick"] <= r["end"])]
        rbeg = begins[(begins["tick"] > r["start"]) & (begins["tick"] <= r["end"])]
        events = [(int(t), "kill", d) for t, d in zip(rd["tick"], rd.to_dict("records"))]
        events += [(int(t), "plant", d) for t, d in zip(rp["tick"], rp.to_dict("records"))]
        events += [(int(t), "defuse", d) for t, d in zip(rdef["tick"], rdef.to_dict("records"))]
        events += [(int(t), "begindefuse", d) for t, d in zip(rbeg["tick"], rbeg.to_dict("records"))]
        events.sort(key=lambda e: (e[0], {"kill": 0, "plant": 1, "begindefuse": 2, "defuse": 3}[e[1]]))

        planted_tick = None
        defusing = None  # {"sid", "start", "need"} while a defuse is in progress

        def state(tick):
            g = by_tick.get(tick - 1, g0)
            s = {"planted": planted_tick is not None,
                 "t_left": round_time - (tick - r["start"]) / TICKRATE,
                 "bomb_left": c4_time - (tick - planted_tick) / TICKRATE if planted_tick else c4_time,
                 "kit": False, "defuse_left": None}
            if planted_tick and defusing and defusing["sid"] in alive:
                left = defusing["need"] - (tick - defusing["start"]) / TICKRATE
                if left > -0.5:  # past that, the defuse was abandoned (no explicit event for it)
                    s["defuse_left"] = left
            for side, key in ((T, "T"), (CT, "CT")):
                ids = [sid for sid in alive if team.get(sid) == side]
                hps = [max(1, int(g.loc[sid, "health"])) if sid in g.index and int(g.loc[sid, "health"]) > 0
                       else 100 for sid in ids]
                eq = [int(g.loc[sid, "current_equip_value"]) if sid in g.index else 0 for sid in ids]
                s["n" + key] = len(ids)
                s["e" + key] = sum(0.4 + 0.6 * h / 100 for h in hps)
                s["q" + key] = (sum(eq) / len(eq)) if eq else 0
                if side == CT:
                    s["kit"] = any(sid in g.index and bool(g.loc[sid, "has_defuser"]) for sid in ids)
            return s

        label = 1.0 if r["winner"] == T else 0.0
        s0 = state(r["start"] + 1)
        model_rows.append((s0, label))
        r["trace"] = [(r["start"], "round start", None, p_t(s0, coef), s0)]
        kill_log = []  # (tick, killer, victim, victim_team) for trades
        p_last = p_t(s0, coef)  # T's chance right after the latest credited event (clock drift excluded)

        for tick, kind, d in events:
            before = state(tick)
            pb = p_t(before, coef)
            if kind == "begindefuse":
                # T's chance right before this defuse started is what the defuse takes away
                defusing = {"sid": d["user_steamid"], "start": tick, "need": 5.0 if d.get("haskit") else 10.0,
                            "p_before": pb}
                continue
            if kind == "kill":
                victim, killer = d["user_steamid"], d["attacker_steamid"]
                if victim not in alive:
                    continue
                alive.discard(victim)
                if defusing and defusing["sid"] == victim:
                    defusing = None
                vteam = team.get(victim)
                after = state(tick)
                pa = p_t(after, coef)
                d_t = pa - pb                       # change in T's win probability
                v_loss = d_t if vteam == T else -d_t  # change for the victim's team (negative)
                vp = players[victim]["per_round"][r["no"]]
                vp["s"] = False
                vp["events"].append(("death", tick, v_loss))
                players[victim]["deaths"] += 1
                enemy_kill = killer in team and team[killer] != vteam
                if enemy_kill:
                    gain = -v_loss
                    shares = {killer: KILLER_FLAT}
                    flasher = d.get("assister_steamid") if d.get("assistedflash") else None
                    if flasher and flasher in team and team[flasher] != vteam:
                        shares[killer] -= FLASH_SHARE
                        shares[flasher] = shares.get(flasher, 0) + FLASH_SHARE
                    dmg = rh[(rh["user_steamid"] == victim) & (rh["tick"] <= tick)]
                    dmg = dmg[dmg["attacker_steamid"].map(lambda a: a in team and team[a] != vteam)]
                    by = dmg.groupby("attacker_steamid")["dmg_real"].sum()
                    total = float(by.sum())
                    rest = 1.0 - KILLER_FLAT
                    if total > 0:
                        for a, v in by.items():
                            shares[a] = shares.get(a, 0) + rest * float(v) / total
                    else:
                        shares[killer] += rest
                    for a, sh in shares.items():
                        players[a]["per_round"][r["no"]]["events"].append(("kill" if a == killer else "help",
                                                                            tick, gain * sh))
                    players[killer]["kills"] += 1
                    players[killer]["per_round"][r["no"]]["k"] += 1
                    if d.get("headshot"):
                        players[killer]["hs"] += 1
                    ast = d.get("assister_steamid")
                    if ast and ast in team and team[ast] != vteam and ast != killer:
                        players[ast]["assists"] += 1
                        players[ast]["per_round"][r["no"]]["a"] += 1
                    kill_log.append((tick, killer, victim, vteam))
                p_last = pa
                desc = f"{players[killer]['name'] if killer in players else 'world'} killed {players[victim]['name']}"
                r["trace"].append((tick, desc, pb, pa, after))
                model_rows.append((after, label))
            elif kind == "plant":
                planted_tick = tick
                after = state(tick)
                pa = p_t(after, coef)
                sid = d["user_steamid"]
                p_last = pa
                if sid in players:
                    players[sid]["per_round"][r["no"]]["events"].append(("plant", tick, pa - pb))
                r["trace"].append((tick, f"{d.get('user_name')} planted", pb, pa, after))
                model_rows.append((after, label))
            elif kind == "defuse":
                sid = d["user_steamid"]
                if sid in players:
                    # credit what T's chance was after the last real event; the clock is nobody's doing
                    gain = min(defusing["p_before"] if defusing and defusing["sid"] == sid else pb, p_last)
                    players[sid]["per_round"][r["no"]]["events"].append(("defuse", tick, gain))  # P_T -> 0
                r["trace"].append((tick, f"{d.get('user_name')} defused", pb, 0.0, before))

        # trades: victim V (killed by K at t) is traded if K is killed by V's teammate within the window
        for t0, k, v, vteam in kill_log:
            for t1, k2, v2, _ in kill_log:
                if v2 == k and t0 <= t1 <= t0 + TRADE_WINDOW * TICKRATE and team.get(k2) == vteam:
                    pr = players[v]["per_round"][r["no"]]
                    pr["t"] = True
                    pr["events"] = [(kind, tk, val * TRADED_DEATH_FACTOR if kind == "death" else val)
                                    for kind, tk, val in pr["events"]]
                    break

        # damage (ADR), capped at 100 per victim per round
        enemy = rh[rh["attacker_steamid"].map(lambda a: a in team) & rh["user_steamid"].map(lambda u: u in team)]
        enemy = enemy[[team[a] != team[u] for a, u in zip(enemy["attacker_steamid"], enemy["user_steamid"])]]
        for a, dm in enemy.groupby("attacker_steamid")["dmg_real"].sum().items():
            players[a]["damage"] += int(dm)

        for sid in team:
            players[sid]["side_by_round"][r["no"]] = team[sid]
            pr = players[sid]["per_round"][r["no"]]
            if pr["k"] >= 2:
                players[sid]["multi"][min(5, pr["k"])] += 1
            pr["swing"] = sum(v for _, _, v in pr["events"])
            players[sid]["swing"] += pr["swing"]
            if pr["k"] or pr["a"] or pr["s"] or pr["t"]:
                players[sid]["kast_rounds"] += 1

    # team identity: the side each player started on
    first = rounds[0] if rounds else None
    for sid, pl in players.items():
        pl["team_key"] = "A"
    if first is not None and first["start"] in by_tick:
        g = by_tick[first["start"]]
        for sid, pl in players.items():
            if sid in g.index:
                pl["team_key"] = "started CT" if int(g.loc[sid, "team_num"]) == CT else "started T"
    # players who joined later: same team as whoever was on their side in their first round
    for sid, pl in players.items():
        if pl["team_key"] != "A" or not pl["side_by_round"]:
            continue
        r0 = min(pl["side_by_round"])
        side = pl["side_by_round"][r0]
        mate = next((o for o in players.values() if o["team_key"] != "A"
                     and o["side_by_round"].get(r0) == side), None)
        if mate:
            pl["team_key"] = mate["team_key"]
    for pl in players.values():
        n = max(1, pl["rounds"])
        pl["kast"] = 100.0 * pl["kast_rounds"] / n
        pl["swing_per_round"] = 100.0 * pl["swing"] / n
        pl["adr"] = pl["damage"] / n
        pl["kpr"], pl["dpr"], pl["apr"] = pl["kills"] / n, pl["deaths"] / n, pl["assists"] / n
        pl["kd"] = pl["kills"] / max(1, pl["deaths"])
        pl["hs_pct"] = 100.0 * pl["hs"] / max(1, pl["kills"])
        pl["rating"] = rating2(pl)

    # MVPs and clan names from the game's own counters; team scores per half
    teams = {}
    if rounds:
        last_tick = rounds[-1]["end"]
        try:
            fin = p.parse_ticks(["mvps", "team_clan_name"], ticks=[rounds[0]["start"], last_tick])
            fin["steamid"] = fin["steamid"].astype(str)
            for sid, row in fin[fin["tick"] == fin["tick"].max()].set_index("steamid").iterrows():
                if sid in players:
                    players[sid]["mvps"] = int(row["mvps"]) if row["mvps"] == row["mvps"] and row["mvps"] else 0
            clan = fin[fin["tick"] == fin["tick"].min()].set_index("steamid")["team_clan_name"].to_dict()
        except Exception:
            clan = {}
        for sid, pl in players.items():
            t = teams.setdefault(pl["team_key"], {"key": pl["team_key"], "name": "", "players": [],
                                                  "score": 0, "halves": [0, 0, 0]})
            t["players"].append(sid)
            if clan.get(sid) and not t["name"]:
                t["name"] = str(clan[sid])
        for r in rounds:
            for t in teams.values():
                side = next((players[s]["side_by_round"].get(r["no"]) for s in t["players"]
                             if r["no"] in players[s]["side_by_round"]), None)
                if side == r["winner"]:
                    t["score"] += 1
                    t["halves"][0 if r["no"] <= 12 else 1 if r["no"] <= 24 else 2] += 1
        for t in teams.values():
            ps = [players[s] for s in t["players"] if players[s]["rounds"]]
            t["avg_rating"] = sum(p_["rating"] for p_ in ps) / max(1, len(ps))
            # auto-names ("team_<nickname>") aren't real team names: name the team after its top fragger
            if not t["name"] or re.match(r"(?i)team_", t["name"]):
                top = max(ps, key=lambda p_: (p_["kills"], p_["adr"]), default=None)
                t["name"] = top["name"] if top else ("Team 1" if t["key"] == "started CT" else "Team 2")
    return {"players": players, "rounds": rounds, "model_rows": model_rows, "teams": teams}


def rating2(pl):
    """HLTV Rating 2.0, community approximation (1.00 = average): built from KAST, kills, deaths,
    assists and ADR per round. HLTV's exact formula is unpublished."""
    impact = 2.13 * pl["kpr"] + 0.42 * pl["apr"] - 0.41
    return (0.0073 * pl["kast"] + 0.3591 * pl["kpr"] - 0.5329 * pl["dpr"] + 0.2372 * impact
            + 0.0032 * pl["adr"] + 0.1587)


# ------------------------------------------------------------------------------- model fit/eval ----

def _xy(rows, coef):
    """Split rows into model-decided (features) and terminal ones."""
    X, y, term = [], [], []
    for s, label in rows:
        t = terminal(s)
        if t is None:
            f = features(s)
            X.append([f[k] for k in FEATURES])
            y.append(label)
        else:
            term.append((t, label))
    return np.array(X), np.array(y), term


def scores(rows, coef):
    X, y, term = _xy(rows, coef)
    w = np.array([coef[k] for k in FEATURES])
    pr = 1 / (1 + np.exp(-X @ w)) if len(X) else np.array([])
    allp = np.r_[pr, [t for t, _ in term]]
    ally = np.r_[y, [l for _, l in term]]
    allp = np.clip(allp, 1e-4, 1 - 1e-4)
    logloss = float(-np.mean(ally * np.log(allp) + (1 - ally) * np.log(1 - allp)))
    brier = float(np.mean((allp - ally) ** 2))
    acc = float(np.mean((allp > 0.5) == (ally > 0.5)))
    return {"states": len(ally), "logloss": logloss, "brier": brier, "accuracy": acc}


def fit(rows, prior=None, l2=4.0, iters=4000, lr=0.05):
    """Logistic regression on model-decided states, regularised towards the prior coefficients."""
    prior = prior or dict(DEFAULT_COEF)
    X, y, _ = _xy(rows, prior)
    w0 = np.array([prior[k] for k in FEATURES])
    w = w0.copy()
    n = max(1, len(X))
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ w))
        grad = X.T @ (p - y) / n + l2 * (w - w0) / n
        w -= lr * grad
    return dict(zip(FEATURES, map(float, w)))


# ------------------------------------------------------------------------------- output ----

def print_scoreboard(res, show_rounds=False):
    players = res["players"]
    teams = {}
    for sid, pl in players.items():
        teams.setdefault(pl["team_key"], []).append(pl)
    print(f"{'Player':<16}{'K':>4}{'D':>4}{'A':>4}{'ADR':>7}{'KAST':>8}{'Swing':>9}")
    for tk in sorted(teams):
        print(f"--- team that {tk} ---")
        for pl in sorted(teams[tk], key=lambda x: -x["swing_per_round"]):
            print(f"{pl['name'][:15]:<16}{pl['kills']:>4}{pl['deaths']:>4}{pl['assists']:>4}{pl['adr']:>7.1f}"
                  f"{pl['kast']:>7.0f}%{pl['swing_per_round']:>+8.2f}%")
    if show_rounds:
        print("\nSwing per round (%, + helped / - hurt their team):")
        nos = [r["no"] for r in res["rounds"]]
        print(f"{'':16}" + "".join(f"{n:>6}" for n in nos))
        for pl in sorted(players.values(), key=lambda x: (x["team_key"], -x["swing_per_round"])):
            print(f"{pl['name'][:15]:<16}" + "".join(
                f"{100 * pl['per_round'][n]['swing']:>+6.0f}" if n in pl["per_round"] else f"{'':>6}" for n in nos))


def print_trace(res, no):
    r = next((r for r in res["rounds"] if r["no"] == no), None)
    if not r:
        print("no such round")
        return
    side = {T: "T", CT: "CT"}
    print(f"Round {no}: won by {side[r['winner']]} ({r['reason']})")
    print(f"{'time':>6}  {'event':<34}{'alive T-CT':>11}{'P(T wins)':>11}")
    for tick, desc, pb, pa, s in r["trace"]:
        secs = (tick - r["start"]) / TICKRATE
        bomb = " (bomb)" if s["planted"] else ""
        print(f"{secs:>5.0f}s  {desc[:33]:<34}{s['nT']:>5}-{s['nCT']:<5}{pa:>10.0%}{bomb}")
    print()
    print("Swing this round:")
    for pl in sorted(res["players"].values(), key=lambda x: -x["per_round"].get(no, {"swing": 0})["swing"]):
        if no in pl["per_round"] and abs(pl["per_round"][no]["swing"]) >= 0.005:
            print(f"  {pl['name']:<16}{100 * pl['per_round'][no]['swing']:+.0f}%")


def main():
    ap = argparse.ArgumentParser(description="KAST and Swing for CS2 demos")
    ap.add_argument("demos", nargs="+")
    ap.add_argument("--rounds", action="store_true", help="show swing per round")
    ap.add_argument("--json", help="write full results to this file")
    ap.add_argument("--trace", type=int, help="show the win probability through this round")
    ap.add_argument("--evaluate", action="store_true", help="test the win model on these demos")
    ap.add_argument("--fit", action="store_true", help="refit the win model on these demos and save it")
    a = ap.parse_args()

    if a.evaluate or a.fit:
        per_demo = {d: analyse(d, DEFAULT_COEF)["model_rows"] for d in a.demos}
        allrows = [r for rows in per_demo.values() for r in rows]
        base = sum(l for _, l in allrows) / len(allrows)
        const = {k: 0.0 for k in FEATURES}
        const["bias"] = math.log(base / (1 - base))
        print(f"{len(allrows)} game states from {len(per_demo)} demos")
        print(f"  coin-flip baseline  : {scores(allrows, const)}")
        print(f"  default model       : {scores(allrows, DEFAULT_COEF)}")
        if len(per_demo) > 1:  # leave-one-demo-out: fit on the others, test on the held-out demo
            held = []
            for d in per_demo:
                train = [r for dd, rows in per_demo.items() if dd != d for r in rows]
                c = fit(train)
                held += [(r, c) for r in per_demo[d]]
            ll = [scores([r], c)["logloss"] for r, c in held]
            br = [scores([r], c)["brier"] for r, c in held]
            print(f"  refit (held-out)    : logloss {np.mean(ll):.4f}, brier {np.mean(br):.4f}")
        if a.fit:
            c = fit(allrows)
            with open(MODEL_FILE, "w", encoding="utf-8") as f:
                json.dump({"coef": c, "demos": [os.path.basename(d) for d in a.demos],
                           "states": len(allrows)}, f, indent=1)
            print("  saved refit coefficients to", MODEL_FILE)
            print("  " + ", ".join(f"{k} {DEFAULT_COEF[k]:+.2f} -> {c[k]:+.2f}" for k in FEATURES))
        return

    for d in a.demos:
        res = analyse(d)
        print(f"\n=== {os.path.basename(d)} ({len(res['rounds'])} rounds) ===")
        if a.trace:
            print_trace(res, a.trace)
            continue
        print_scoreboard(res, a.rounds)
        if a.json:
            out = {sid: {k: v for k, v in pl.items() if k != "per_round"} | {
                "per_round": {n: {"swing": v["swing"], "kast": bool(v["k"] or v["a"] or v["s"] or v["t"])}
                              for n, v in pl["per_round"].items()}} for sid, pl in res["players"].items()}
            with open(a.json, "w", encoding="utf-8") as f:
                json.dump(out, f, indent=1)


if __name__ == "__main__":
    main()
