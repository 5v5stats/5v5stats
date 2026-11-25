"""Load the stats straight from the MatchZy data pipeline, instead of reading them off the old site's pages.

The old 5v5stats.com (a Next.js site, `cs2-stats-bundle`) was built from the server's MatchZy exports by its
`npm run build:data`, which writes `public/data/*.json`. fetch.py + parse.py used to read that same data back
from the published pages. Since 5v5stats.com now serves this website, those pages are gone, so this script
reads the JSON files directly, writes the same three files parse.py made, and builds web/stats.db from them:
the read-only stats file the website serves (the hosted database keeps only uploads and the admin panel).

    python import5v5\\from_bundle.py <cs2-stats-bundle folder>                 write the JSON and web/stats.db
    python import5v5\\from_bundle.py <cs2-stats-bundle folder> --pull-extras   ... first fetching account
                                                       merges and highlight links from 5v5stats.com/api/extras

Then commit import5v5/data/ and web/stats.db and push. Only finished matches are kept (statsdb.finished).
"""
import argparse
import csv
import datetime
import json
import math
import os
import re
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "web"))
import statsdb  # noqa: E402

OUT = os.path.join(HERE, "data")
EXTRAS = os.path.join(OUT, "extras.json")
EXTRAS_URL = "https://5v5stats.com/api/extras"
MAP_NAMES = {"de_dust2": "Dust II", "de_inferno": "Inferno", "de_mirage": "Mirage", "de_nuke": "Nuke",
             "de_anubis": "Anubis", "de_ancient": "Ancient", "de_vertigo": "Vertigo", "de_cache": "Cache",
             "de_train": "Train", "de_overpass": "Overpass", "de_cbble_d": "Cbble d", "de_cbble": "Cobblestone"}


def load(bundle, name):
    with open(os.path.join(bundle, "public", "data", name + ".json"), encoding="utf-8") as f:
        return json.load(f)


def day(ms):
    """The match's day in the players' time (US Pacific, statsdb.SITE_TZ): the evening it was played. (The old
    site used the UTC date, which is the next day for nearly every match: they end 19:00-22:00 Pacific.)"""
    return datetime.datetime.fromtimestamp(ms / 1000, statsdb.SITE_TZ).date().isoformat() if ms else None


COUNTER_COLS = {  # rating.TOTALS key: MatchZy CSV column(s), summed exactly as the pipeline's aggregatePlayers
    "total_kills": ["kills"], "total_deaths": ["deaths"], "total_assists": ["assists"], "total_damage": ["damage"],
    "total_hs_kills": ["head_shot_kills"], "total_multi": ["enemy3ks", "enemy4ks", "enemy5ks"],
    "total_shots_fired": ["shots_fired_total"], "total_shots_on_tgt": ["shots_on_target_total"],
    "total_v1_count": ["v1_count"], "total_v1_wins": ["v1_wins"], "total_v2_count": ["v2_count"],
    "total_v2_wins": ["v2_wins"], "total_entry_count": ["entry_count"], "total_entry_wins": ["entry_wins"],
    "total_util_damage": ["utility_damage"], "total_flashes": ["enemies_flashed"],
}


def _num(v):
    try:
        return int(float(v or 0))
    except ValueError:
        return 0


def exact_counters(bundle, m, sides):
    """{steamid: totals} for one match from its raw MatchZy CSV rows: every detail counter exact (clutches,
    opening duels, utility, accuracy...), so a week or a month is summed exactly instead of estimated from
    all-time rates. Same rules as the pipeline: spectators (team "Spectator", or no kills, deaths, damage,
    assists and shots) are left out, the winner's team gets the win, the round backup gives T/CT rounds."""
    folder = os.path.join(bundle, "data", "matches", str(m["matchid"]))
    out = {}
    for f in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if not f.endswith(".csv"):
            continue
        with open(os.path.join(folder, f), encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                team = (r.get("team") or "").strip()
                if team.lower() == "spectator" or not team or not any(
                        _num(r.get(c)) for c in ("kills", "deaths", "damage", "assists", "shots_fired_total")):
                    continue
                t = out.setdefault(r["steamid64"], {k: 0 for k in ("total_matches", "estimated_rounds", "wins", "losses",
                                                                   "t_rounds", "ct_rounds", *COUNTER_COLS)})
                for key, cols in COUNTER_COLS.items():
                    t[key] += sum(_num(r.get(c)) for c in cols)
                t["total_matches"] += 1
                t["estimated_rounds"] += m["rounds"]
                if m.get("winner"):
                    t["wins" if m["winner"] == team else "losses"] += 1
                side = (sides or {}).get(team)
                if side:
                    t["t_rounds"] += side["tPlayed"]
                    t["ct_rounds"] += side["ctPlayed"]
    return out


def team_name(team):
    """MatchZy's `team_NERO__Kaya` -> `NERO  Kaya`, as the old site showed it (its src/lib/teams.ts)."""
    spaced = re.sub(r"^team_", "", team, flags=re.I).replace("_", " ").strip()
    return spaced or team


def round_winners(m, winner, loser):
    """`path` is the winner's lead before each round; a round is the winner's when the lead goes up."""
    path = m.get("path")
    if not path or len(path) != m["rounds"]:
        return None
    rw = [winner if b > a else loser for a, b in zip(path, path[1:])] + [winner]
    return rw


def convert(bundle):
    meta, board, awards, avatars = (load(bundle, n) for n in ("meta", "leaderboard", "awards", "avatars"))
    site = {"lastSyncMs": meta["lastSyncMs"], "byPeriod": board["byPeriod"], "awardsByPeriod": awards,
            "avatars": avatars, "activityByMonth": meta.get("activityByMonth"), "form": None}
    players = {}
    for p in load(bundle, "players"):
        rec = dict(p["scored"])
        rec["history"] = [{"match": h["matchid"], "date": day(h.get("realDateMs") or h.get("mtimeMs")),
                           "map": h.get("map"), "team": h.get("team"), "k": h.get("kills"), "d": h.get("deaths"),
                           "a": h.get("assists")} for h in p.get("matches", [])]
        players[rec["steamid64"]] = rec
    matches, skipped, counters = {}, [], {}
    with open(os.path.join(bundle, "data", "match-results.json"), encoding="utf-8") as f:
        results = json.load(f)["matches"]
    for m in load(bundle, "matches"):
        if len(m.get("teams") or []) != 2:
            skipped.append((m["matchid"], "not two teams"))
            continue
        t1, t2 = m["teams"]
        if not statsdb.finished(t1["score"], t2["score"]):
            skipped.append((m["matchid"], f"unfinished {t1['score']}-{t2['score']}"))
            continue
        win, lose = (t1, t2) if t1.get("isWinner") else (t2, t1)
        teams = [{"name": team_name(t["name"]), "score": t["score"], "result": "Kazanan" if t is win else "Kaybeden",
                  "won": t is win,
                  "players": [{"steamid": p["steamid64"], "name": p["name"], "k": p["kills"], "d": p["deaths"],
                               "a": p["assists"], "adr": round(p["adr"], 1) if p.get("adr") is not None else None,
                               # rounded half up like the old site (JavaScript), not Python's half-to-even
                               "hs_pct": (math.floor(100 * p["hsKills"] / p["kills"] + 0.5) if p.get("kills") else 0),
                               "kast": p.get("kast"), "rating": p.get("rating")} for p in t["players"]]}
                 for t in (t1, t2)]
        matches[m["matchid"]] = {"id": m["matchid"], "date": day(m.get("realDateMs") or m.get("mtimeMs")),
                                 "rounds": m["rounds"], "total_kills": m.get("totalKills"),
                                 "map_name": MAP_NAMES.get(m["map"], m["map"]), "map": m["map"], "teams": teams,
                                 "round_winners": round_winners(m, team_name(win["name"]), team_name(lose["name"]))}
        counters[m["matchid"]] = exact_counters(bundle, m, (results.get(str(m["matchid"])) or {}).get("sides"))
    return site, players, matches, skipped, counters


def pull_extras(url):
    """What only the uploads database knows, from the site's /api/extras, saved to import5v5/data/extras.json:
    account merges made in the admin panel and which matches have a published highlight page. No demo data
    (the stats come only from MatchZy). No database password is needed: the site reads its own database."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "5v5stats-stats-update"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            extras = json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{url} answered {e.code}" + (": the uploads database is still paused" if e.code == 503 else ""))
    if not isinstance(extras, dict) or "merges" not in extras or "games" not in extras:
        raise SystemExit(f"{url} did not answer with the expected data")
    with open(EXTRAS, "w", encoding="utf-8") as f:
        json.dump(extras, f, ensure_ascii=False, indent=0)
    print(f"pulled {len(extras['merges'])} merges and {len(extras['games'])} highlight links")


def _match_game(con, gid, g):
    """The stats file's game for an uploads-database game: by match number, else (a demo the uploads database
    never matched) the same map, a date within a day, the same score and at least 8 of the same players."""
    if g.get("ext_id") in gid:
        return gid[g["ext_id"]]
    try:
        day = datetime.date.fromisoformat(g["date"])
    except (TypeError, ValueError):
        return None
    for c in con.execute("SELECT id, score_a, score_b FROM games WHERE map = ? AND date BETWEEN ? AND ?",
                         (g["map"], (day - datetime.timedelta(days=1)).isoformat(),
                          (day + datetime.timedelta(days=1)).isoformat())).fetchall():
        if sorted([c["score_a"], c["score_b"]]) != sorted(g["score"]):
            continue
        mine = {r[0] for r in con.execute("SELECT steamid FROM game_players WHERE game_id = ?", (c["id"],))}
        if len(mine & set(g["players"])) >= 8:
            return c["id"]
    return None


def check_counters(con):
    """Every player's exact per-match numbers must add up to the pipeline's all-time totals. Returns the
    mismatches (empty = the week / month sums built from them are exact)."""
    base = statsdb.baseline_totals(con)
    sums = {}
    for r in con.execute("SELECT steamid, counters FROM game_players WHERE counters IS NOT NULL"):
        statsdb._add(sums.setdefault(r["steamid"], {}), json.loads(r["counters"]))
    keys = ("total_matches", "estimated_rounds", "wins", "losses", "t_rounds", "ct_rounds", *COUNTER_COLS)
    bad = [(sid, k, round(sums.get(sid, {}).get(k, 0), 3), round(t.get(k, 0), 3)) for sid, t in base.items()
           for k in keys if abs(sums.get(sid, {}).get(k, 0) - (t.get(k, 0) or 0)) > 1e-6]
    bad += [(sid, "no all-time totals", 0, 0) for sid in sums if sid not in base]
    return bad


def build_stats_db(site, players, matches, path, counters):
    """The read-only stats file the website serves (web/stats.db), built fresh, then swapped in."""
    tmp = path + ".new"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    statsdb.init(con)
    # inserted in match-number order, so a match's page address (/g/<id>) stays the same as before
    statsdb.import_5v5(con, site, players, dict(sorted(matches.items())))
    # Every match row carries its exact numbers from the MatchZy CSVs, so a week, a month or a map is summed
    # exactly. The pipeline's own period totals are not used: its windows (a UTC Monday, now minus 30 days)
    # are not this site's (the last 7 / 30 / 90 days by date, in the players' time).
    statsdb.set_meta(con, "period_totals", {})
    gid = {r["ext_id"]: r["id"] for r in con.execute("SELECT id, ext_id FROM games")}
    con.executemany("UPDATE game_players SET counters = ? WHERE game_id = ? AND steamid = ?",
                    [(json.dumps(t), gid[mid], sid) for mid, per in counters.items() if mid in gid
                     for sid, t in per.items()])
    bad = check_counters(con)
    if bad:
        con.close()
        os.remove(tmp)
        raise SystemExit(f"per-match numbers do not add up to the all-time totals ({len(bad)} differences, e.g. "
                         f"{bad[:5]}); stats file NOT written")
    print("per-match numbers add up exactly to every player's all-time totals")
    extras = json.load(open(EXTRAS, encoding="utf-8")) if os.path.exists(EXTRAS) else {}
    con.executemany("INSERT INTO merges(alt, main, created_at, note) VALUES (?,?,?,'')",
                    [(m["alt"], m["main"], m["created_at"]) for m in extras.get("merges", [])])
    matched, unmatched = 0, []
    for g in extras.get("games", []):
        target = _match_game(con, gid, g)
        if target is None:
            unmatched.append(f"{g.get('map')} {g.get('date')} {'-'.join(map(str, g.get('score', [])))}")
            continue
        matched += 1
        if g.get("link"):
            con.execute("UPDATE games SET match_key = ?, played_at = ? WHERE id = ?", (g["link"], g["played_at"], target))
    if extras.get("games"):
        print(f"highlight pages matched to server matches: {matched} of {len(extras['games'])}"
              + (f"; not in the MatchZy data (yet): {', '.join(unmatched[:10])}" if unmatched else ""))
    statsdb.bump_rev(con)
    con.commit()
    con.execute("VACUUM")
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("games", "game_players", "merges")}
    con.close()
    os.replace(tmp, path)
    print(f"wrote {os.path.relpath(path)} ({os.path.getsize(path) / 1e6:.1f} MB): {counts}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bundle", help="the cs2-stats-bundle folder, after `npm run build:data`")
    ap.add_argument("--pull-extras", nargs="?", const=EXTRAS_URL, metavar="URL",
                    help=f"first save account merges and highlight links from the site (default {EXTRAS_URL})")
    a = ap.parse_args()
    if a.pull_extras:
        pull_extras(a.pull_extras)
    site, players, matches, skipped, counters = convert(a.bundle)
    os.makedirs(OUT, exist_ok=True)
    for name, obj in (("site", site), ("players", players), ("matches", matches)):
        with open(os.path.join(OUT, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=0)
    newest = max(matches)
    print(f"{len(players)} players, {len(matches)} finished matches (newest #{newest}, {matches[newest]['date']}), "
          f"{len(skipped)} left out" + (": " + ", ".join(f"#{i} {why}" for i, why in skipped) if skipped else ""))
    import db
    build_stats_db(site, players, matches, db.STATS_DB, counters)
    print("Commit import5v5/data/ and web/stats.db, then push: the site serves the new stats after the deploy.")


if __name__ == "__main__":
    main()
