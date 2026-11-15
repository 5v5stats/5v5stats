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
CREATE TABLE IF NOT EXISTS duels (          -- demo games only: enemy kills between two players
  game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  killer TEXT NOT NULL, victim TEXT NOT NULL, kills INTEGER, hs INTEGER, openings INTEGER,
  PRIMARY KEY (game_id, killer, victim)
);
CREATE INDEX IF NOT EXISTS duels_killer ON duels(killer);
CREATE TABLE IF NOT EXISTS merges (         -- alt accounts counted as one person (undo = delete the row)
  alt TEXT PRIMARY KEY, main TEXT NOT NULL, created_at REAL, note TEXT
);
"""

MAP_NAMES = {"de_dust2": "Dust II", "de_inferno": "Inferno", "de_mirage": "Mirage", "de_nuke": "Nuke",
             "de_anubis": "Anubis", "de_ancient": "Ancient", "de_vertigo": "Vertigo", "de_cache": "Cache",
             "de_train": "Train", "de_overpass": "Overpass", "de_cbble_d": "Cbble d"}

# portable upsert (SQLite and PostgreSQL) for a player's row in a game
GP_UPSERT = """ ON CONFLICT(game_id, steamid) DO UPDATE SET name = excluded.name, side = excluded.side,
    won = excluded.won, kills = excluded.kills, deaths = excluded.deaths, assists = excluded.assists,
    adr = excluded.adr, hs_pct = excluded.hs_pct, counters = excluded.counters"""


def init(con):
    con.executescript(SCHEMA)


# ---------------------------------------------------------------- merged accounts ----
# A merge says "steamid `alt` is the same person as `main`". Nothing in games / game_players / baseline is
# changed: every per-player sum below maps alt -> main as it adds up, so removing the row undoes it fully.

def merge_map(con):
    """{alt steamid: main steamid}."""
    return {r["alt"]: r["main"] for r in con.execute("SELECT alt, main FROM merges")}


def ids_of(con, sid):
    """The accounts that make up one person: [main, *alts]."""
    return [sid] + [r["alt"] for r in con.execute("SELECT alt FROM merges WHERE main = ?", (sid,))]


def merge_accounts(con, main, alt, note=""):
    """Count `alt` as `main` from now on. Returns the main account actually used (if `main` was itself merged
    into someone, that person). Accounts already merged into `alt` move to the new main too."""
    m = merge_map(con)
    main = m.get(main, main)
    if alt == main:
        raise ValueError("bu iki hesap zaten aynı kişi")
    con.execute("UPDATE merges SET main = ? WHERE main = ?", (main, alt))
    con.execute("""INSERT INTO merges(alt, main, created_at, note) VALUES (?,?,?,?) ON CONFLICT(alt) DO UPDATE
                   SET main = excluded.main, created_at = excluded.created_at, note = excluded.note""",
                (alt, main, time.time(), note))
    con.commit()
    return main


def unmerge_account(con, alt):
    con.execute("DELETE FROM merges WHERE alt = ?", (alt,))
    con.commit()


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
    # batched (executemany) so a sync to a hosted database takes seconds, not one round trip per row
    con.execute("DELETE FROM baseline")
    ids = {r["ext_id"]: r["id"] for r in con.execute("SELECT id, ext_id FROM games WHERE source = '5v5stats'")}
    updates, inserts, rosters = [], [], {}
    for mid, m in matches.items():
        if len(m.get("teams", [])) != 2 or not all("name" in t for t in m["teams"]):
            continue
        a, b = sorted(m["teams"], key=lambda t: not t.get("won"))
        rw = None
        if m.get("round_winners"):
            rw = json.dumps(["a" if w == a["name"] else "b" for w in m["round_winners"]])
        vals = (m["map"], m["date"], m["rounds"], a["name"], a["score"], b["name"], b["score"], rw)
        if int(mid) in ids:  # update in place: a match keeps its page address (/g/<id>) across syncs
            updates.append(vals + (ids[int(mid)],))
        else:
            inserts.append((int(mid),) + vals)
        rosters[int(mid)] = [(side, t) for side, t in (("a", a), ("b", b))]
    con.executemany("""UPDATE games SET map=?, date=?, rounds=?, team_a=?, score_a=?, team_b=?, score_b=?,
                       round_winners=? WHERE id=?""", updates)
    con.executemany("""INSERT INTO games(source, ext_id, map, date, rounds, team_a, score_a, team_b,
                       score_b, round_winners) VALUES ('5v5stats',?,?,?,?,?,?,?,?,?)""", inserts)
    if inserts:
        ids = {r["ext_id"]: r["id"] for r in con.execute("SELECT id, ext_id FROM games WHERE source = '5v5stats'")}
    keep = {ids[e] for e in rosters}
    con.executemany("DELETE FROM game_players WHERE game_id = ?", [(ids[e],) for e in rosters])
    con.executemany("""INSERT INTO game_players(game_id, steamid, name, side, won, kills, deaths,
                       assists, adr, hs_pct) VALUES (?,?,?,?,?,?,?,?,?,?)""" + GP_UPSERT,
                    [(ids[e], p["steamid"], p["name"], side, int(bool(t.get("won"))), p["k"], p["d"], p["a"],
                      p["adr"], p["hs_pct"]) for e, teams in rosters.items() for side, t in teams for p in t["players"]])
    # no longer on 5v5stats (e.g. a player opted out of a match listing)
    con.executemany("DELETE FROM games WHERE id = ?", [(g,) for g in ids.values() if g not in keep])
    con.executemany("""INSERT INTO baseline(steamid, name, totals, map_stats) VALUES (?,?,?,?)
                       ON CONFLICT(steamid) DO UPDATE SET name = excluded.name, totals = excluded.totals,
                       map_stats = excluded.map_stats""",
                    [(sid, rec["name"], json.dumps({k: rec.get(k, 0) or 0 for k in rating.TOTALS}),
                      json.dumps(rec.get("mapStats", []))) for sid, rec in players.items()])
    con.executemany("""INSERT INTO people(steamid, name, avatar) VALUES (?,?,?) ON CONFLICT(steamid) DO UPDATE
                       SET avatar = excluded.avatar, name = COALESCE(excluded.name, people.name)""",
                    [(sid, (players.get(sid) or {}).get("name"), av.get("medium") or av.get("full") or av.get("small"))
                     for sid, av in (site.get("avatars") or {}).items()])
    con.executemany("INSERT INTO people(steamid, name) VALUES (?,?) ON CONFLICT(steamid) DO UPDATE SET name = excluded.name",
                    [(sid, rec["name"]) for sid, rec in players.items()])
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
        set_duels(con, existing["id"], data.get("duels"))  # 5v5stats has the totals, but not who killed whom
        return existing["id"]
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
        con.execute("""INSERT INTO game_players(game_id, steamid, name, side, won, kills, deaths, assists,
                       adr, hs_pct, counters) VALUES (?,?,?,?,?,?,?,?,?,?,?)""" + GP_UPSERT,
                    (gid, sid, c["name"], side_of.get(sid, "b"), c["wins"], c["total_kills"], c["total_deaths"],
                     c["total_assists"], p.get("adr"), p.get("hs_pct"), json.dumps(c)))
        con.execute("INSERT INTO people(steamid, name) VALUES (?,?) ON CONFLICT(steamid) DO UPDATE SET name = excluded.name",
                    (sid, c["name"]))
    set_duels(con, gid, data.get("duels"))
    merge_duplicates(con)
    return gid


def set_duels(con, gid, duels):
    """Replace a game's head-to-head kills (None = the package has none: keep what's stored)."""
    if duels is None:
        return
    con.execute("DELETE FROM duels WHERE game_id = ?", (gid,))
    con.executemany("INSERT INTO duels(game_id, killer, victim, kills, hs, openings) VALUES (?,?,?,?,?,?)",
                    [(gid, d["k"], d["v"], d["n"], d["hs"], d["op"]) for d in duels if d["n"] > 0])


def duo_players(con, min_games=5):
    """Players for the duo page's pickers: [(steamid, name, games)], most games first (merged accounts as one)."""
    mm = merge_map(con)
    n = {}
    for r in con.execute("SELECT steamid, COUNT(*) AS c FROM game_players GROUP BY steamid"):
        k = mm.get(r["steamid"], r["steamid"])
        n[k] = n.get(k, 0) + r["c"]
    names = {r["steamid"]: r["name"] for r in con.execute("SELECT steamid, name FROM people")}
    return sorted(((s, names.get(s) or s, c) for s, c in n.items() if c >= min_games), key=lambda x: -x[2])


def duo(con, a, b):
    """Two players: games as teammates and as opponents (all games), and head-to-head kills (our demo games)."""
    ia, ib = ids_of(con, a), ids_of(con, b)
    sa, sb = set(ia), set(ib)
    q = ",".join("?" * (len(ia) + len(ib)))
    games = {}
    for r in con.execute(f"""SELECT gp.game_id, gp.steamid, gp.side, gp.won, gp.kills, gp.deaths, gp.adr, g.date, g.map,
                             g.score_a, g.score_b, g.source FROM game_players gp JOIN games g ON g.id = gp.game_id
                             WHERE gp.steamid IN ({q})""", (*ia, *ib)):
        g = games.setdefault(r["game_id"], {"id": r["game_id"], "date": r["date"], "map": r["map"],
                                            "score": f'{r["score_a"]}-{r["score_b"]}', "source": r["source"]})
        g["a" if r["steamid"] in sa else "b"] = r
    both = sorted((g for g in games.values() if "a" in g and "b" in g), key=lambda g: (g["date"] or "", g["id"]),
                  reverse=True)

    def side(rows, who):
        n = len(rows)
        k = sum(g[who]["kills"] or 0 for g in rows)
        d = sum(g[who]["deaths"] or 0 for g in rows)
        adr = [g[who]["adr"] for g in rows if g[who]["adr"] is not None]
        return {"kills": k, "deaths": d, "kd": k / d if d else float(k), "kpm": k / n if n else 0,
                "adr": sum(adr) / len(adr) if adr else None}

    out = {}
    for mode, rows in (("with", [g for g in both if g["a"]["side"] == g["b"]["side"]]),
                       ("vs", [g for g in both if g["a"]["side"] != g["b"]["side"]])):
        aw = sum(1 for g in rows if g["a"]["won"])
        bw = sum(1 for g in rows if g["b"]["won"])
        out[mode] = {"n": len(rows), "a_wins": aw, "b_wins": bw, "draws": len(rows) - (aw if mode == "with" else aw + bw),
                     "a": side(rows, "a"), "b": side(rows, "b")}
    for g in both:
        g["mode"] = "with" if g["a"]["side"] == g["b"]["side"] else "vs"
    # head to head, from the duels table (only our demo games have it)
    h = {"a": {"n": 0, "hs": 0, "op": 0}, "b": {"n": 0, "hs": 0, "op": 0}}
    games_with_duels = set()
    if both:
        gq = ",".join("?" * len(both))
        games_with_duels = {r["game_id"] for r in con.execute(
            f"SELECT DISTINCT game_id FROM duels WHERE game_id IN ({gq})", [g["id"] for g in both])}
        pa, pb = ",".join("?" * len(ia)), ",".join("?" * len(ib))
        for r in con.execute(f"""SELECT killer, SUM(kills) AS n, SUM(hs) AS hs, SUM(openings) AS op FROM duels
                                 WHERE (killer IN ({pa}) AND victim IN ({pb})) OR (killer IN ({pb}) AND victim IN ({pa}))
                                 GROUP BY killer""", (*ia, *ib, *ib, *ia)):
            x = h["a" if r["killer"] in sa else "b"]
            x["n"] += r["n"] or 0
            x["hs"] += r["hs"] or 0
            x["op"] += r["op"] or 0
    h["games"] = sum(1 for g in both if g["mode"] == "vs" and g["id"] in games_with_duels)
    out.update(games=both, h2h=h, total=len(both))
    return out


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
    """{steamid: totals} for a period: 'all' (exact: 5v5stats totals + newer demos), '30d' / '7d' (exact:
    5v5stats' published period totals + demos) or '90d' (5v5stats doesn't publish it: summed per match;
    K/D/A, damage, HS and wins exact, detail counters estimated from each player's all-time rates)."""
    base = {r["steamid"]: json.loads(r["totals"]) for r in con.execute("SELECT * FROM baseline")}
    names = {r["steamid"]: r["name"] for r in con.execute("SELECT steamid, name FROM people")}
    mm = merge_map(con)
    who = lambda sid: mm.get(sid, sid)  # noqa: E731  merged accounts count as their main account
    out = {}
    if period == "all":
        for sid, t in base.items():
            _add(out.setdefault(who(sid), {}), t)
        for r in con.execute("""SELECT gp.steamid, gp.counters FROM game_players gp JOIN games g ON g.id = gp.game_id
                                WHERE g.source = 'demo'"""):
            _add(out.setdefault(who(r["steamid"]), {}), json.loads(r["counters"]))
        # players in 5v5stats scoreboards without a 5v5stats profile: their scoreboard numbers only
        for r in con.execute("""SELECT gp.*, g.rounds FROM game_players gp JOIN games g ON g.id = gp.game_id
                                WHERE g.source = '5v5stats' AND gp.steamid NOT IN (SELECT steamid FROM baseline)"""):
            _add(out.setdefault(who(r["steamid"]), {}), _estimate(r, {}))
    else:
        # The same window as 5v5stats (the 7 / 30 days up to its newest match), using its exact published
        # totals, plus our demo matches from that window on (newer ones included, exact).
        since, until = window(con, period)
        snap = (get_meta(con, "period_totals", {}) or {}).get(period)
        if snap:
            for sid, t in snap.items():
                _add(out.setdefault(who(sid), {}), t)
            rows = con.execute("""SELECT gp.* FROM game_players gp JOIN games g ON g.id = gp.game_id
                                  WHERE g.source = 'demo' AND g.date >= ?""", (since,)).fetchall()
            for r in rows:
                _add(out.setdefault(who(r["steamid"]), {}), json.loads(r["counters"]))
        else:  # no 5v5stats data: per-match rows
            rows = con.execute("""SELECT gp.*, g.source, g.rounds FROM game_players gp JOIN games g ON g.id = gp.game_id
                                  WHERE g.date >= ?""", (since,)).fetchall()
            for r in rows:
                c = json.loads(r["counters"]) if r["counters"] else _estimate(r, base.get(r["steamid"], {}))
                _add(out.setdefault(who(r["steamid"]), {}), c)
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
    mm, out = merge_map(con), {}
    for sid, d in con.execute("""SELECT gp.steamid, MAX(g.date) FROM game_players gp JOIN games g ON g.id = gp.game_id
                                 GROUP BY gp.steamid""").fetchall():
        k = mm.get(sid, sid)
        out[k] = max(out.get(k, ""), d or "")
    return out


def relations(con, sid, min_games=10):
    """Teammates and opponents: best duo, most played with, nemesis, easiest opponent; and streaks."""
    ids = ids_of(con, sid)
    mm = merge_map(con)
    q = ",".join("?" * len(ids))
    rows = con.execute(f"""SELECT g.id, g.date, gp.side, gp.won FROM game_players gp JOIN games g ON g.id = gp.game_id
                          WHERE gp.steamid IN ({q}) ORDER BY g.date, COALESCE(g.ext_id, 0), g.id""", ids).fetchall()
    side_of = {r["id"]: (r["side"], r["won"]) for r in rows}
    mates, opps = {}, {}
    for r in con.execute(f"""SELECT game_id, steamid, name, side FROM game_players WHERE game_id IN
                             ({",".join("?" * len(side_of)) or "NULL"}) AND steamid NOT IN ({q})""", (*side_of, *ids)):
        mine, won = side_of[r["game_id"]]
        other = mm.get(r["steamid"], r["steamid"])
        d = (mates if r["side"] == mine else opps).setdefault(other, {"sid": other, "name": r["name"], "w": 0, "l": 0})
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
    out, mm = {}, merge_map(con)
    for r in con.execute("SELECT steamid, map_stats FROM baseline"):
        mine = out.setdefault(mm.get(r["steamid"], r["steamid"]), {})
        for m in json.loads(r["map_stats"] or "[]"):
            t = mine.setdefault(m["map"], {"matches": 0, "rounds": 0, "damage": 0})
            for k in t:
                t[k] += m.get(k) or 0
    for r in con.execute("""SELECT gp.steamid, g.map, g.rounds, gp.adr FROM game_players gp
                            JOIN games g ON g.id = gp.game_id WHERE g.source = 'demo'"""):
        m = out.setdefault(mm.get(r["steamid"], r["steamid"]), {}).setdefault(r["map"], {"matches": 0, "rounds": 0,
                                                                                         "damage": 0})
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
    days = {"90d": 90, "30d": 30, "7d": 7}[period]
    until = get_meta(con, "sync_date") or latest_date(con)
    since = (datetime.date.fromisoformat(until) - datetime.timedelta(days=days - 1)).isoformat()
    return since, until


def period_where(con, period, alias=""):
    """SQL condition (and args) selecting a period's games."""
    if period == "all":
        return "1=1", ()
    since, _ = window(con, period)
    return f"{alias}date >= ?", (since,)


def leaderboard(con, period="all"):
    totals = player_totals(con, period)
    pri = get_meta(con, "priors", {}) or {}
    priors = pri.get(period) or pri.get("all")
    return rating.leaderboard(list(totals.values()), period, prior=priors)


def overview(con, period="all"):
    cond, args = period_where(con, period)
    where = f"WHERE {cond}"
    g = con.execute(f"SELECT COUNT(*), COALESCE(SUM(rounds),0) FROM games {where}", args).fetchone()
    k = con.execute(f"SELECT COALESCE(SUM(kills),0) FROM game_players WHERE game_id IN (SELECT id FROM games {where})",
                    args).fetchone()
    mm = merge_map(con)
    people = {mm.get(r[0], r[0]) for r in con.execute(
        f"SELECT DISTINCT steamid FROM game_players WHERE game_id IN (SELECT id FROM games {where})", args)}
    return {"matches": g[0], "rounds": g[1], "kills": k[0], "players": len(people)}


def stamp():
    return time.time()
