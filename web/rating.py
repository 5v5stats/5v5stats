"""The 5v5stats rating, re-implemented from its published numbers (fits them exactly; see validate()).

Input: per-player raw totals (kills, deaths, damage, shots, clutches, ...). Output: the site's metrics,
PowerScore, AdjustedPowerScore, rank and 1-10 tier. Because everything starts from totals, new matches
(from our own demos) are simply added to a player's totals and the leaderboard is recomputed.

How the site rates players (reverse-engineered):
  metrics      KPR = kills/rounds, DPR, ADR = damage/rounds, KPM = kills/matches, DPM, ADM,
               MK = multi-kill rounds/matches, HS = hs kills/kills, ACC = shots hit/shots fired,
               KAST_PROXY = (KPR + APR + (1 - DPR)) / 3,
               V1 = (wins + 5*prior)/(count + 5), V2 likewise, CLUTCH = (V1 + V2)/2,
               ENTRY = (wins + 20*prior)/(duels + 20),  UTIL = 1/2 z(util dmg/round) + 1/2 z(flashes/round)
  z-scores     (x - mean) / sample std, over the qualified players only (enough matches)
  PowerScore   0.09 KPR - 0.04 DPR + 0.10 ADR + 0.20 KAST + 0.09 KPM - 0.04 DPM + 0.10 ADM + 0.04 MK
               + 0.02 HS + 0.03 ACC + 0.05 CLUTCH + 0.13 ENTRY + 0.07 UTIL   (all as z-scores)
  Adjusted     PowerScore * rounds / (rounds + 200)
  rank, tier   by Adjusted; tier 10..1 from the top percentile (rank - 1) / qualified players
"""
import math

WEIGHTS = {"KPR": 0.09, "DPR": -0.04, "ADR": 0.10, "KAST": 0.20, "KPM": 0.09, "DPM": -0.04, "ADM": 0.10,
           "MK": 0.04, "HS": 0.02, "ACC": 0.03, "CLUTCH": 0.05, "ENTRY": 0.13, "UTIL": 0.07}
# the site's per-match z-scores (KPM/DPM/ADM) equal its per-round ones exactly (a quirk of its code), so
# kills, deaths and damage effectively count twice; reproduced as is
Z_SOURCE = {"KPR": "KPR", "DPR": "DPR", "ADR": "ADR", "KAST": "KAST_PROXY", "KPM": "KPR", "DPM": "DPR", "ADM": "ADR",
            "MK": "MK_per_match", "HS": "HS_PCT", "ACC": "ACC_PCT", "CLUTCH": "CLUTCH_WR", "ENTRY": "ENTRY_WR"}
ADJUST_ROUNDS = 200
V_STRENGTH, ENTRY_STRENGTH = 5, 20
SITE_MIN_MATCHES = {"all": 30, "90d": 20, "30d": 15, "7d": 3}  # 5v5stats' own rules (inferred), for validate()
MIN_MATCHES = {**SITE_MIN_MATCHES, "30d": 9}   # ours: the last 30 days ranks everyone with 9+ matches
# top percentile (rank - 1) / ranked players, below each cut -> tier 10..2, else 1. These are the old site's
# exact rules (percentileToRating in its src/lib/ratings.ts); the earlier inferred cuts disagreed on small boards.
TIER_CUTS = [0.05, 0.15, 0.25, 0.4, 0.55, 0.7, 0.8, 0.9, 0.95]

# Our own display scale (not on 5v5stats): a FACEIT/HLTV-style rating where 1.00 is the server average and
# nobody goes below 0. points = e^(0.5 * adjusted score): same order and tiers, but the bottom is compressed
# and the top stretched, so in team balancing one weak player no longer "cancels" a star.
POINTS_K = 0.5        # below average: the weakest regular players land around 0.47
POINTS_K_UP = 0.45    # above average: a bit flatter, so the very best land around 1.80 (FACEIT's "high impact")
# FACEIT rating bands: 1.80+ high impact, 1.30-1.79 good, 0.90-1.29 okay, 0.89 and below poor
BANDS = [(1.80, "hi", "Yüksek etki"), (1.30, "good", "İyi"), (0.90, "ok", "İdare eder"), (0.0, "poor", "Zayıf")]


def points(adjusted):
    if adjusted is None:
        return None
    return math.exp((POINTS_K_UP if adjusted > 0 else POINTS_K) * adjusted)


def band(value):
    """FACEIT colour band of a 1.00-scale rating: (css class, Turkish label)."""
    for low, cls, label in BANDS:
        if value is not None and value >= low:
            return cls, label
    return "poor", "Zayıf"

# raw totals a player record needs (names as the site uses them)
TOTALS = ["total_matches", "estimated_rounds", "wins", "losses", "total_kills", "total_deaths", "total_assists",
          "total_damage", "total_hs_kills", "total_multi", "total_shots_fired", "total_shots_on_tgt",
          "total_v1_count", "total_v1_wins", "total_v2_count", "total_v2_wins", "total_entry_count",
          "total_entry_wins", "total_util_damage", "total_flashes", "t_rounds", "ct_rounds",
          # demo-only counters (5v5stats has none: 0 there), not used by the rating: walking with shift, flashes
          "demo_matches", "demo_rounds", "alive_ticks", "walk_ticks", "walk_presses",
          "enemy_blinds", "enemy_blind_ms", "team_blind_ms", "demo_kills", "awp_kills", "pistol_kills",
          "deagle_kills", "revolver_kills",
          "flashes_thrown", "flashes_wasted", "flash_assists", "knife_kills", "zeus_kills", "team_damage",
          "team_kills", "trade_kills", "blind_kills", "bomb_plants"]


def _div(a, b):
    return a / b if b else 0.0


def priors(players):
    """Prior rates for the smoothed win rates: the pooled rate over everyone in the period."""
    s = lambda k: sum(p.get(k, 0) for p in players)  # noqa: E731
    return {"v1": _div(s("total_v1_wins"), s("total_v1_count")) or 0.3558,
            "v2": _div(s("total_v2_wins"), s("total_v2_count")) or 0.2020,
            "entry": _div(s("total_entry_wins"), s("total_entry_count")) or 0.4764}


def metrics(t, pri):
    """Per-player metrics from raw totals (the site's formulas)."""
    r, n = t.get("estimated_rounds", 0), t.get("total_matches", 0)
    k, d, a = t.get("total_kills", 0), t.get("total_deaths", 0), t.get("total_assists", 0)
    m = {"KPR": _div(k, r), "DPR": _div(d, r), "APR": _div(a, r), "ADR": _div(t.get("total_damage", 0), r),
         "KPM": _div(k, n), "DPM": _div(d, n), "ADM": _div(t.get("total_damage", 0), n),
         "MK_per_match": _div(t.get("total_multi", 0), n), "HS_PCT": _div(t.get("total_hs_kills", 0), k),
         "ACC_PCT": _div(t.get("total_shots_on_tgt", 0), t.get("total_shots_fired", 0)),
         "WIN_PCT": _div(t.get("wins", 0), n), "UDR_PER_ROUND": _div(t.get("total_util_damage", 0), r),
         "FPR_PER_ROUND": _div(t.get("total_flashes", 0), r)}
    m["KAST_PROXY"] = (m["KPR"] + m["APR"] + (1 - m["DPR"])) / 3
    m["V1_WR"] = (t.get("total_v1_wins", 0) + V_STRENGTH * pri["v1"]) / (t.get("total_v1_count", 0) + V_STRENGTH)
    m["V2_WR"] = (t.get("total_v2_wins", 0) + V_STRENGTH * pri["v2"]) / (t.get("total_v2_count", 0) + V_STRENGTH)
    m["CLUTCH_WR"] = (m["V1_WR"] + m["V2_WR"]) / 2
    m["ENTRY_WR"] = ((t.get("total_entry_wins", 0) + ENTRY_STRENGTH * pri["entry"])
                     / (t.get("total_entry_count", 0) + ENTRY_STRENGTH))
    m["KD"] = _div(k, d)
    return m


def _z(values):
    n = len(values)
    if n < 2:
        return [0.0] * n
    mean = sum(values) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))
    return [(v - mean) / sd if sd else 0.0 for v in values]


def tier_for(rank, n):
    pct = (rank - 1) / n
    for i, cut in enumerate(TIER_CUTS):
        if pct < cut:
            return 10 - i
    return 1


def implied_priors(site_players):
    """The exact priors the site used, read back from its published rates: (rate*(n+m) - wins)/m."""
    out = {}
    for key, rate, w, c, m in (("v1", "V1_WR", "total_v1_wins", "total_v1_count", V_STRENGTH),
                               ("v2", "V2_WR", "total_v2_wins", "total_v2_count", V_STRENGTH),
                               ("entry", "ENTRY_WR", "total_entry_wins", "total_entry_count", ENTRY_STRENGTH)):
        vals = [(p[rate] * (p[c] + m) - p[w]) / m for p in site_players if p.get(rate) is not None]
        if vals:
            out[key] = sum(vals) / len(vals)
    return out


def leaderboard(players, period="all", min_matches=None, prior=None, guests=None):
    """players: list of dicts with 'steamid64', 'name' and raw TOTALS. Returns new dicts with metrics,
    z-scores, PowerScore, AdjustedPowerScore, qualified, rank and rating (tier), best first.
    prior: smoothing priors (implied_priors() of the site's data); default: pooled rates.
    guests: extra totals (e.g. one player's games with a friend) scored on the same scale without
    changing it or being ranked; then returns (leaderboard, scored guests)."""
    need = MIN_MATCHES.get(period, 3) if min_matches is None else min_matches
    pri = {**priors(players), **(prior or {})}
    rows = []
    for p in players:
        row = dict(p)
        row.update(metrics(p, pri))
        row["qualified"] = p.get("total_matches", 0) >= need
        rows.append(row)
    extra = []
    for p in guests or ():
        row = dict(p)
        row.update(metrics(p, pri))
        row["qualified"] = False
        extra.append(row)
    q = [r for r in rows if r["qualified"]]
    # z-scores use the ranked (qualified) players' average and spread; unranked players get a score on the
    # same scale (5v5stats uses it, e.g. in the team builder) but no rank or tier
    srcs = dict(Z_SOURCE, UDR="UDR_PER_ROUND", FPR="FPR_PER_ROUND")
    norm = {}
    for key, src in srcs.items():
        vals = [r[src] for r in q]
        n = len(vals)
        mean = sum(vals) / n if n else 0.0
        sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1)) if n > 1 else 0.0
        norm[key] = (src, mean, sd)
    for r in rows + extra:
        z = {key: ((r[src] - mean) / sd if sd else 0.0) for key, (src, mean, sd) in norm.items()}
        for key in Z_SOURCE:
            r["z_" + key] = z[key]
        r["z_UTIL"] = (z["UDR"] + z["FPR"]) / 2
        r["PowerScore"] = sum(w * r["z_" + key] for key, w in WEIGHTS.items())
        rr = r.get("estimated_rounds", 0)
        r["AdjustedPowerScore"] = r["PowerScore"] * rr / (rr + ADJUST_ROUNDS)
        r["points"] = points(r["AdjustedPowerScore"])
        r["rank"] = r["rating"] = None
    q.sort(key=lambda r: -r["AdjustedPowerScore"])
    for i, r in enumerate(q, 1):
        r["rank"], r["rating"] = i, tier_for(i, len(q))
    board = q + sorted((r for r in rows if not r["qualified"]), key=lambda r: -r.get("total_matches", 0))
    return (board, extra) if guests is not None else board


def validate(site_period_players, period):
    """Recompute a period's leaderboard from the site's raw totals; return the largest differences."""
    mine = {r["steamid64"]: r for r in leaderboard(site_period_players, period, min_matches=SITE_MIN_MATCHES.get(period, 3),
                                                    prior=implied_priors(site_period_players))}
    worst = {}
    for p in site_period_players:
        m = mine[p["steamid64"]]
        for key in ("KAST_PROXY", "V1_WR", "V2_WR", "ENTRY_WR", "CLUTCH_WR", "PowerScore", "AdjustedPowerScore"):
            if p.get(key) is not None and m.get(key) is not None:
                worst[key] = max(worst.get(key, 0), abs(p[key] - m[key]))
        for key in ("qualified", "rank", "rating"):
            if p.get(key) != m.get(key):
                worst[key] = worst.get(key, 0) + 1
    return worst
