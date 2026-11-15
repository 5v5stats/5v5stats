"""Player stats for the website: 5v5stats history + our own demo matches, rated with the 5v5stats formula.

Tables (in web/data/site.db, next to the highlight matches):
  games         every match in the stats: 5v5stats history (source '5v5stats') and our demos ('demo')
  game_players  one row per player per game (K/D/A, ADR, HS%; demo rows also carry full MatchZy-style counters)
  baseline      each player's all-time raw totals from 5v5stats at the last sync (+ per-map stats)
  people        names and Steam avatar URLs
  meta          sync time, the site's smoothing priors per period

All-time stats = baseline totals + counters of demo games that 5v5stats doesn't have yet. When a later sync
includes a match we also have as a demo, the two are merged (the 5v5stats copy counts, linked to our page).
"""
import datetime
import json
import time

import rating

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  ext_id INTEGER UNIQUE,                    -- 5v5stats match number
  match_key TEXT UNIQUE,                    -- our demo's key (matches.match_key)
  map TEXT, date TEXT, played_at TEXT, rounds INTEGER,
  team_a TEXT, score_a INTEGER, team_b TEXT, score_b INTEGER,   -- a = winner
  round_winners TEXT                        -- json list of 'a' / 'b'
);
CREATE INDEX IF NOT EXISTS games_date ON games(date);
CREATE TABLE IF NOT EXISTS game_players (
  game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  steamid TEXT NOT NULL, name TEXT, side TEXT, won INTEGER,
  kills INTEGER, deaths INTEGER, assists INTEGER, adr REAL, hs_pct REAL,
  counters TEXT,                            -- json, demo games only
  PRIMARY KEY (game_id, steamid)
);
CREATE INDEX IF NOT EXISTS game_players_sid ON game_players(steamid);
CREATE TABLE IF NOT EXISTS baseline (steamid TEXT PRIMARY KEY, name TEXT, totals TEXT, map_stats TEXT);
CREATE TABLE IF NOT EXISTS people (steamid TEXT PRIMARY KEY, name TEXT, avatar TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

MAP_NAMES = {"de_dust2": "Dust II", "de_inferno": "Inferno", "de_mirage": "Mirage", "de_nuke": "Nuke",
             "de_anubis": "Anubis", "de_ancient": "Ancient", "de_vertigo": "Vertigo", "de_cache": "Cache",
             "de_train": "Train", "de_overpass": "Overpass", "de_cbble_d": "Cbble d"}


def init(con):
    con.executescript(SCHEMA)


def get_meta(con, key, default=None):
    r = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return json.loads(r[0]) if r else default


def set_meta(con, key, value):
    con.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)))


def map_label(m):
    return MAP_NAMES.get(m, (m or "").replace("de_", "").capitalize())


# ---------------------------------------------------------------- 5v5stats import ----

def import_5v5(con, site, players, matches):
    """Load parsed 5v5stats data (import5v5/parse.py output). Replaces the previous 5v5stats import."""
    con.execute("DELETE FROM baseline")
    keep = set()
    for mid, m in matches.items():
        if len(m.get("teams", [])) != 2 or not all("name" in t for t in m["teams"]):
            continue
        a, b = sorted(m["teams"], key=lambda t: not t.get("won"))
        rw = None
        if m.get("round_winners"):
            rw = json.dumps(["a" if w == a["name"] else "b" for w in m["round_winners"]])
        vals = (m["map"], m["date"], m["rounds"], a["name"], a["score"], b["name"], b["score"], rw)
        old = con.execute("SELECT id FROM games WHERE source = '5v5stats' AND ext_id = ?", (int(mid),)).fetchone()
        if old:  # update in place: a match keeps its page address (/g/<id>) across syncs
            gid = old["id"]
            con.execute("""UPDATE games SET map=?, date=?, rounds=?, team_a=?, score_a=?, team_b=?, score_b=?,
                           round_winners=? WHERE id=?""", vals + (gid,))
            con.execute("DELETE FROM game_players WHERE game_id = ?", (gid,))
        else:
            gid = con.execute("""INSERT INTO games(source, ext_id, map, date, rounds, team_a, score_a, team_b,
                                 score_b, round_winners) VALUES ('5v5stats',?,?,?,?,?,?,?,?,?)""",
                              (int(mid),) + vals).lastrowid
        keep.add(gid)
        for side, t in (("a", a), ("b", b)):
            for p in t["players"]:
                con.execute("""INSERT OR REPLACE INTO game_players(game_id, steamid, name, side, won, kills, deaths,
                               assists, adr, hs_pct) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                            (gid, p["steamid"], p["name"], side, int(bool(t.get("won"))), p["k"], p["d"], p["a"],
                             p["adr"], p["hs_pct"]))
    for (gid,) in con.execute("SELECT id FROM games WHERE source = '5v5stats'").fetchall():
        if gid not in keep:  # no longer on 5v5stats (e.g. a player opted out of a match listing)
            con.execute("DELETE FROM games WHERE id = ?", (gid,))
    for sid, rec in players.items():
        totals = {k: rec.get(k, 0) or 0 for k in rating.TOTALS}
        con.execute("INSERT OR REPLACE INTO baseline(steamid, name, totals, map_stats) VALUES (?,?,?,?)",
                    (sid, rec["name"], json.dumps(totals), json.dumps(rec.get("mapStats", []))))
    for sid, av in (site.get("avatars") or {}).items():
        name = (players.get(sid) or {}).get("name")
        con.execute("""INSERT INTO people(steamid, name, avatar) VALUES (?,?,?) ON CONFLICT(steamid) DO UPDATE
                       SET avatar = excluded.avatar, name = COALESCE(excluded.name, people.name)""",
                    (sid, name, av.get("medium") or av.get("full") or av.get("small")))
    for sid, rec in players.items():
        con.execute("INSERT INTO people(steamid, name) VALUES (?,?) ON CONFLICT(steamid) DO UPDATE SET name = excluded.name",
                    (sid, rec["name"]))
    set_meta(con, "synced_at", site.get("lastSyncMs"))
    # exact 30-day / 7-day totals as published (window ends at the newest 5v5stats match)
    set_meta(con, "sync_date", max((m["date"] for m in matches.values() if m.get("date")), default=None))
    set_meta(con, "period_totals", {per: {p["steamid64"]: {k: p.get(k, 0) or 0 for k in rating.TOTALS}
                                          for p in site["byPeriod"][per]["players"]}
                                    for per in site["byPeriod"] if per != "all"})
    set_meta(con, "priors", {per: rating.implied_priors(site["byPeriod"][per]["players"]) for per in site["byPeriod"]})
    merged = merge_duplicates(con)
    con.commit()
    return merged


def _same_match(con, demo_gid, ext_gid):
    g1 = con.execute("SELECT * FROM games WHERE id = ?", (demo_gid,)).fetchone()
    g2 = con.execute("SELECT * FROM games WHERE id = ?", (ext_gid,)).fetchone()
    if g1["map"] != g2["map"] or {g1["score_a"], g1["score_b"]} != {g2["score_a"], g2["score_b"]}:
        return False
    s1 = {r[0] for r in con.execute("SELECT steamid FROM game_players WHERE game_id = ?", (demo_gid,))}
    s2 = {r[0] for r in con.execute("SELECT steamid FROM game_players WHERE game_id = ?", (ext_gid,))}
    return len(s1 & s2) >= 8


def merge_duplicates(con):
    """Demo games that 5v5stats now also has: keep the 5v5stats copy (it's in the baseline), link our page."""
    merged = 0
    for d in con.execute("SELECT * FROM games WHERE source = 'demo'").fetchall():
        try:
            day = datetime.date.fromisoformat(d["date"])
        except (TypeError, ValueError):
            continue
        cands = con.execute("SELECT id FROM games WHERE source='5v5stats' AND map=? AND date BETWEEN ? AND ?",
                            (d["map"], (day - datetime.timedelta(days=1)).isoformat(),
                             (day + datetime.timedelta(days=1)).isoformat())).fetchall()
        for c in cands:
            if _same_match(con, d["id"], c["id"]):
                con.execute("DELETE FROM games WHERE id = ?", (d["id"],))
                con.execute("UPDATE games SET match_key = ?, played_at = ? WHERE id = ?",
                            (d["match_key"], d["played_at"], c["id"]))
                merged += 1
                break
    return merged


# ---------------------------------------------------------------- demo matches ----

def add_demo(con, match_key, data):
    """Add / update one of our demo matches (from an uploaded package's match_data.json)."""
    counters = data.get("counters") or {}
    if not counters:
        return None
    existing = con.execute("SELECT * FROM games WHERE match_key = ?", (match_key,)).fetchone()
    if existing and existing["source"] == "5v5stats":
        return existing["id"]  # 5v5stats already counts it; nothing to add
    teams = data["teams"]
    a, b = teams[0], teams[1] if len(teams) > 1 else {"name": "", "score": 0, "players": []}
    side_of = {s: "a" for s in a["players"]} | {s: "b" for s in b["players"]}
    mapname = "de_" + data["map"].lower().replace(" ", "") if not data["map"].startswith("de_") else data["map"]
    if existing:
        gid = existing["id"]
        con.execute("DELETE FROM game_players WHERE game_id = ?", (gid,))
        con.execute("""UPDATE games SET map=?, date=?, played_at=?, rounds=?, team_a=?, score_a=?, team_b=?, score_b=?
                       WHERE id=?""", (mapname, data["date"], data.get("played_at"), data["rounds"], a["name"],
                                       a["score"], b["name"], b["score"], gid))
    else:
        gid = con.execute("""INSERT INTO games(source, match_key, map, date, played_at, rounds, team_a, score_a,
                             team_b, score_b) VALUES ('demo',?,?,?,?,?,?,?,?,?)""",
                          (match_key, mapname, data["date"], data.get("played_at"), data["rounds"], a["name"],
                           a["score"], b["name"], b["score"])).lastrowid
    for sid, c in counters.items():
        p = data["players"].get(sid, {})
        con.execute("""INSERT OR REPLACE INTO game_players(game_id, steamid, name, side, won, kills, deaths, assists,
                       adr, hs_pct, counters) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (gid, sid, c["name"], side_of.get(sid, "b"), c["wins"], c["total_kills"], c["total_deaths"],
                     c["total_assists"], p.get("adr"), p.get("hs_pct"), json.dumps(c)))
        con.execute("INSERT INTO people(steamid, name) VALUES (?,?) ON CONFLICT(steamid) DO UPDATE SET name = excluded.name",
                    (sid, c["name"]))
    merge_duplicates(con)
    return gid


def remove_demo(con, match_key):
    con.execute("DELETE FROM games WHERE source = 'demo' AND match_key = ?", (match_key,))
    con.execute("UPDATE games SET match_key = NULL WHERE match_key = ?", (match_key,))


# ---------------------------------------------------------------- leaderboards ----

def _add(t, c):
    for k in rating.TOTALS:
        t[k] = t.get(k, 0) + (c.get(k, 0) or 0)


def _estimate(row, base):
    """Totals for a 5v5stats game row: exact basics from the scoreboard; detail counters (shots, clutches...)
    aren't published per match, so they're estimated from the player's all-time rates over this game's rounds."""
    rounds = row["rounds"] or 0
    t = {"total_matches": 1, "estimated_rounds": rounds, "wins": row["won"], "losses": 1 - row["won"],
         "total_kills": row["kills"], "total_deaths": row["deaths"], "total_assists": row["assists"],
         "total_damage": (row["adr"] or 0) * rounds, "total_hs_kills": (row["hs_pct"] or 0) / 100 * row["kills"]}
    br = base.get("estimated_rounds") or 0
    for k in ("total_multi", "total_shots_fired", "total_shots_on_tgt", "total_v1_count", "total_v1_wins",
              "total_v2_count", "total_v2_wins", "total_entry_count", "total_entry_wins", "total_util_damage",
              "total_flashes", "t_rounds", "ct_rounds"):
        t[k] = (base.get(k, 0) / br * rounds) if br else 0
    return t


def player_totals(con, period="all", now=None):
    """{steamid: totals} for a period: 'all' (exact: 5v5stats totals + newer demos) or '30d' / '7d'."""
    base = {r["steamid"]: json.loads(r["totals"]) for r in con.execute("SELECT * FROM baseline")}
    names = {r["steamid"]: r["name"] for r in con.execute("SELECT steamid, name FROM people")}
    out = {}
    if period == "all":
        for sid, t in base.items():
            out[sid] = dict(t)
        for r in con.execute("""SELECT gp.steamid, gp.counters FROM game_players gp JOIN games g ON g.id = gp.game_id
                                WHERE g.source = 'demo'"""):
            _add(out.setdefault(r["steamid"], {}), json.loads(r["counters"]))
        # players in 5v5stats scoreboards without a 5v5stats profile: their scoreboard numbers only
        for r in con.execute("""SELECT gp.*, g.rounds FROM game_players gp JOIN games g ON g.id = gp.game_id
                                WHERE g.source = '5v5stats' AND gp.steamid NOT IN (SELECT steamid FROM baseline)"""):
            _add(out.setdefault(r["steamid"], {}), _estimate(r, {}))
    else:
        # The same window as 5v5stats (the 7 / 30 days up to its newest match), using its exact published
        # totals, plus our demo matches from that window on (newer ones included, exact).
        since, until = window(con, period)
        snap = (get_meta(con, "period_totals", {}) or {}).get(period)
        if snap:
            for sid, t in snap.items():
                out[sid] = dict(t)
            rows = con.execute("""SELECT gp.* FROM game_players gp JOIN games g ON g.id = gp.game_id
                                  WHERE g.source = 'demo' AND g.date >= ?""", (since,)).fetchall()
            for r in rows:
                _add(out.setdefault(r["steamid"], {}), json.loads(r["counters"]))
        else:  # no 5v5stats data: per-match rows
            rows = con.execute("""SELECT gp.*, g.source, g.rounds FROM game_players gp JOIN games g ON g.id = gp.game_id
                                  WHERE g.date >= ?""", (since,)).fetchall()
            for r in rows:
                c = json.loads(r["counters"]) if r["counters"] else _estimate(r, base.get(r["steamid"], {}))
                _add(out.setdefault(r["steamid"], {}), c)
        out = {sid: t for sid, t in out.items() if t.get("total_matches", 0) >= 1}
    for sid, t in out.items():
        t["steamid64"], t["name"] = sid, names.get(sid, sid)
    return out


def comeback(con, period):
    """The period's biggest comeback: the winner that was furthest behind. Returns (game, deficit, leads)."""
    cond, args = period_where(con, period)
    best = None
    for g in con.execute(f"SELECT * FROM games WHERE {cond} AND round_winners IS NOT NULL", args):
        lead, leads, low = 0, [0], 0
        for w in json.loads(g["round_winners"]):
            lead += 1 if w == "a" else -1
            leads.append(lead)
            low = min(low, lead)
        if best is None or -low > best[1] or (-low == best[1] and g["date"] > best[0]["date"]):
            best = (g, -low, leads)
    return best if best and best[1] > 0 else None


def activity_by_month(con, months=12):
    rows = dict(con.execute("SELECT substr(date, 1, 7) AS m, COUNT(*) FROM games GROUP BY m").fetchall())
    last = latest_date(con)[:7]
    y, mo = int(last[:4]), int(last[5:7])
    out = []
    for _ in range(months):
        key = f"{y:04d}-{mo:02d}"
        out.append({"month": key, "matches": rows.get(key, 0)})
        mo -= 1
        if mo == 0:
            y, mo = y - 1, 12
    return out[::-1]


def weekly_counts(con, weeks=8):
    end = datetime.date.fromisoformat(latest_date(con))
    out = []
    for i in range(weeks - 1, -1, -1):
        a, b = end - datetime.timedelta(days=7 * i + 6), end - datetime.timedelta(days=7 * i)
        out.append(con.execute("SELECT COUNT(*) FROM games WHERE date BETWEEN ? AND ?",
                               (a.isoformat(), b.isoformat())).fetchone()[0])
    return out


def last_seen(con):
    return dict(con.execute("""SELECT gp.steamid, MAX(g.date) FROM game_players gp JOIN games g ON g.id = gp.game_id
                               GROUP BY gp.steamid""").fetchall())


def relations(con, sid, min_games=10):
    """Teammates and opponents: best duo, most played with, nemesis, easiest opponent; and streaks."""
    rows = con.execute("""SELECT g.id, g.date, gp.side, gp.won FROM game_players gp JOIN games g ON g.id = gp.game_id
                          WHERE gp.steamid = ? ORDER BY g.date, COALESCE(g.ext_id, 0), g.id""", (sid,)).fetchall()
    side_of = {r["id"]: (r["side"], r["won"]) for r in rows}
    mates, opps = {}, {}
    for r in con.execute(f"""SELECT game_id, steamid, name, side FROM game_players WHERE game_id IN
                             ({",".join("?" * len(side_of)) or "NULL"}) AND steamid != ?""", (*side_of, sid)):
        mine, won = side_of[r["game_id"]]
        d = (mates if r["side"] == mine else opps).setdefault(r["steamid"], {"sid": r["steamid"], "name": r["name"],
                                                                              "w": 0, "l": 0})
        d["name"] = r["name"]
        d["w" if won else "l"] += 1
    for d in list(mates.values()) + list(opps.values()):
        d["n"] = d["w"] + d["l"]
        d["pct"] = d["w"] / d["n"]
    m_ok = [d for d in mates.values() if d["n"] >= min_games]
    o_ok = [d for d in opps.values() if d["n"] >= min_games]
    results = [r["won"] for r in rows]
    cur, longest_w, longest_l, run, prev = 0, 0, 0, 0, None
    for w in results:
        run = run + 1 if w == prev else 1
        prev = w
        if w:
            longest_w = max(longest_w, run)
        else:
            longest_l = max(longest_l, run)
    if results:
        last = results[-1]
        cur = 0
        for w in reversed(results):
            if w != last:
                break
            cur += 1
        cur = cur if last else -cur
    return {"best_duo": max(m_ok, key=lambda d: (d["pct"], d["n"]), default=None),
            "most_with": max(mates.values(), key=lambda d: d["n"], default=None),
            "nemesis": min(o_ok, key=lambda d: (d["pct"], -d["n"]), default=None),
            "easiest": max(o_ok, key=lambda d: (d["pct"], d["n"]), default=None),
            "streak": cur, "longest_win": longest_w, "longest_loss": longest_l}


def player_maps(con):
    """{steamid: {map: {"matches": n, "adr": x}}} from the 5v5stats per-map stats plus our demo matches."""
    out = {}
    for r in con.execute("SELECT steamid, map_stats FROM baseline"):
        out[r["steamid"]] = {m["map"]: {"matches": m["matches"], "rounds": m["rounds"], "damage": m["damage"]}
                             for m in json.loads(r["map_stats"] or "[]")}
    for r in con.execute("""SELECT gp.steamid, g.map, g.rounds, gp.adr FROM game_players gp
                            JOIN games g ON g.id = gp.game_id WHERE g.source = 'demo'"""):
        m = out.setdefault(r["steamid"], {}).setdefault(r["map"], {"matches": 0, "rounds": 0, "damage": 0})
        m["matches"] += 1
        m["rounds"] += r["rounds"] or 0
        m["damage"] += (r["adr"] or 0) * (r["rounds"] or 0)
    return {sid: {mp: {"matches": v["matches"], "adr": round(v["damage"] / v["rounds"], 1) if v["rounds"] else 0}
                  for mp, v in maps.items()} for sid, maps in out.items()}


def balancer_pool(con, size=60):
    """Players for the team builder, as 5v5stats builds it: the most recent players first, each with
    power = 0.75 * all-time adjusted score + 0.25 * 30-day adjusted score (all-time only if no 30-day games)."""
    allb = {r["steamid64"]: r for r in leaderboard(con, "all")}
    month = {r["steamid64"]: r for r in leaderboard(con, "30d")}
    week = {r["steamid64"]: r for r in leaderboard(con, "7d")}
    seen = last_seen(con)
    maps = player_maps(con)
    avatars = {r["steamid"]: r["avatar"] for r in con.execute("SELECT steamid, avatar FROM people")}
    # most matches in the last 30 days first; players without recent matches by when they last played
    def m30(s):
        return month.get(s, {}).get("total_matches", 0)
    recent = sorted(allb, key=lambda s: (m30(s), seen.get(s, ""), allb[s]["total_matches"]), reverse=True)[:size]
    pool = []
    for s in recent:
        a = allb[s]
        m = month.get(s, {}).get("AdjustedPowerScore")
        raw = 0.75 * a["AdjustedPowerScore"] + 0.25 * m if m is not None else a["AdjustedPowerScore"]
        power = rating.points(raw)  # 5v5stats' güç on our 1.00-average scale (never negative)
        pool.append({"sid": s, "name": a["name"], "power": round(power, 4), "rating": a["rating"],
                     "matches": a["total_matches"], "m30": m30(s), "adr": round(a["ADR"], 1),
                     "avatar": avatars.get(s), "recent": "7d" if s in week else "30d" if s in month else "all",
                     "last": seen.get(s), "maps": maps.get(s, {})})
    return pool


def game_data(con, gid):
    """Page data (the shape matchpage.render takes) for any match in the stats. 5v5stats matches only
    have K/D/A, ADR and HS% per player; the rest is None and left off the page."""
    g = con.execute("SELECT * FROM games WHERE id = ?", (gid,)).fetchone()
    if g is None:
        return None
    rows = con.execute("SELECT * FROM game_players WHERE game_id = ?", (gid,)).fetchall()
    avatars = {r["steamid"]: r["avatar"] for r in con.execute("SELECT steamid, avatar FROM people") if r["avatar"]}
    rounds = g["rounds"] or 0
    players = {}
    for r in rows:
        k, d = r["kills"], r["deaths"]
        c = json.loads(r["counters"]) if r["counters"] else None
        hs = round((r["hs_pct"] or 0) * k / 100) if r["hs_pct"] is not None else None
        players[r["steamid"]] = {
            "name": r["name"], "team": 0 if r["side"] == "a" else 1, "rating": None, "swing": None,
            "kills": k, "deaths": d, "assists": r["assists"], "adr": r["adr"] or 0,
            "kd": k / d if d else float(k), "kpr": k / rounds if rounds else 0,
            "hs": c["total_hs_kills"] if c else hs, "hs_pct": r["hs_pct"] or 0, "kast": None,
            "opening": c["total_entry_wins"] if c else None, "m5": None, "m4": None,
            "m3": None, "m2": None, "mvps": None,
            "damage": c["total_damage"] if c else round((r["adr"] or 0) * rounds), "rounds": rounds}
    rw = json.loads(g["round_winners"]) if g["round_winners"] else None

    def halves(side):
        if not rw:
            return None
        won = [w == side for w in rw]
        return [sum(won[:12]), sum(won[12:24]), sum(won[24:])]
    teams = [{"name": g["team_a"], "score": g["score_a"], "halves": halves("a"), "avg_rating": None,
              "players": [s for s, p in players.items() if p["team"] == 0]},
             {"name": g["team_b"], "score": g["score_b"], "halves": halves("b"), "avg_rating": None,
              "players": [s for s, p in players.items() if p["team"] == 1]}]
    return {"key": g["match_key"] or f"5v5stats-{g['ext_id']}", "map": map_label(g["map"]), "date": g["date"],
            "rounds": rounds, "teams": teams, "players": players, "clips": [],
            "pics": {s: avatars[s] for s in players if s in avatars}}


def latest_date(con):
    r = con.execute("SELECT MAX(date) FROM games").fetchone()[0]
    return r or datetime.date.today().isoformat()


def window(con, period):
    """(first day, last 5v5stats day) of a period, as 5v5stats defines it: the 7 / 30 days ending at the
    newest match of its data. Newer demo matches (after the last day) are counted in the period too."""
    days = {"30d": 30, "7d": 7}[period]
    until = get_meta(con, "sync_date") or latest_date(con)
    since = (datetime.date.fromisoformat(until) - datetime.timedelta(days=days - 1)).isoformat()
    return since, until


def period_where(con, period, alias=""):
    """SQL condition (and args) selecting a period's games."""
    if period == "all":
        return "1", ()
    since, _ = window(con, period)
    return f"{alias}date >= ?", (since,)


def leaderboard(con, period="all"):
    totals = player_totals(con, period)
    priors = (get_meta(con, "priors", {}) or {}).get(period)
    return rating.leaderboard(list(totals.values()), period, prior=priors)


def overview(con, period="all"):
    cond, args = period_where(con, period)
    where = f"WHERE {cond}"
    g = con.execute(f"SELECT COUNT(*), COALESCE(SUM(rounds),0) FROM games {where}", args).fetchone()
    k = con.execute(f"SELECT COALESCE(SUM(kills),0), COUNT(DISTINCT steamid) FROM game_players WHERE game_id IN "
                    f"(SELECT id FROM games {where})", args).fetchone()
    return {"matches": g[0], "rounds": g[1], "kills": k[0], "players": k[1]}


def stamp():
    return time.time()
