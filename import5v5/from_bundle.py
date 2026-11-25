"""Load the stats straight from the MatchZy data pipeline, instead of reading them off the old site's pages.

The old 5v5stats.com (a Next.js site, `cs2-stats-bundle`) was built from the server's MatchZy exports by its
`npm run build:data`, which writes `public/data/*.json`. fetch.py + parse.py used to read that same data back
from the published pages. Since 5v5stats.com now serves this website, those pages are gone, so this script
reads the JSON files directly, writes the same three files parse.py made, and builds web/stats.db from them:
the read-only stats file the website serves (the hosted database keeps only uploads and the admin panel).

    python import5v5\\from_bundle.py <cs2-stats-bundle folder>                 write the JSON and web/stats.db
    python import5v5\\from_bundle.py <cs2-stats-bundle folder> --pull-extras   ... first fetching merges, demo
                                                       duels and highlight links from the hosted database

Then commit import5v5/data/ and web/stats.db and push. Only finished matches are kept (statsdb.finished).
"""
import argparse
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
MAP_NAMES = {"de_dust2": "Dust II", "de_inferno": "Inferno", "de_mirage": "Mirage", "de_nuke": "Nuke",
             "de_anubis": "Anubis", "de_ancient": "Ancient", "de_vertigo": "Vertigo", "de_cache": "Cache",
             "de_train": "Train", "de_overpass": "Overpass", "de_cbble_d": "Cbble d", "de_cbble": "Cobblestone"}


def load(bundle, name):
    with open(os.path.join(bundle, "public", "data", name + ".json"), encoding="utf-8") as f:
        return json.load(f)


def day(ms):
    """The match's day as the old site showed it (in UTC), so match dates do not shift between the two imports."""
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).date().isoformat() if ms else None


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
    matches, skipped = {}, []
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
    return site, players, matches, skipped


def pull_extras():
    """What only the uploads database knows, saved to import5v5/data/extras.json for the stats file:
    account merges made in the admin panel, who-killed-whom from uploaded demos, and which server match has
    a highlight page (its upload's match key). Keyed by the server's match number, so it survives a rebuild."""
    import db
    if not db.IS_PG:
        raise SystemExit("--pull-extras needs DATABASE_URL (the hosted database)")
    con = db.connect()
    extras = {
        "merges": [dict(r) for r in con.execute("SELECT alt, main, created_at, note FROM merges")],
        "links": [dict(r) for r in con.execute("""SELECT ext_id, match_key, played_at FROM games
                                                  WHERE ext_id IS NOT NULL AND match_key IS NOT NULL""")],
        "duels": [dict(r) for r in con.execute("""SELECT g.ext_id, d.killer, d.victim, d.kills, d.hs, d.openings
                                                  FROM duels d JOIN games g ON g.id = d.game_id
                                                  WHERE g.ext_id IS NOT NULL""")],
    }
    con.close()
    with open(EXTRAS, "w", encoding="utf-8") as f:
        json.dump(extras, f, ensure_ascii=False, indent=0)
    print(f"pulled {len(extras['merges'])} merges, {len(extras['links'])} highlight links, "
          f"{len(extras['duels'])} duel rows")


def build_stats_db(site, players, matches, path):
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
    # The pipeline's week starts on Monday; this site's week is the last 7 days. Their 30 days are the same
    # rolling window, so only the weekly totals would be wrong if used (on a day the newest match was played).
    totals = statsdb.get_meta(con, "period_totals", {}) or {}
    totals.pop("7d", None)
    statsdb.set_meta(con, "period_totals", totals)
    extras = json.load(open(EXTRAS, encoding="utf-8")) if os.path.exists(EXTRAS) else {}
    gid = {r["ext_id"]: r["id"] for r in con.execute("SELECT id, ext_id FROM games")}
    con.executemany("INSERT INTO merges(alt, main, created_at, note) VALUES (?,?,?,?)",
                    [(m["alt"], m["main"], m["created_at"], m["note"]) for m in extras.get("merges", [])])
    con.executemany("UPDATE games SET match_key = ?, played_at = ? WHERE id = ?",
                    [(x["match_key"], x["played_at"], gid[x["ext_id"]]) for x in extras.get("links", [])
                     if x["ext_id"] in gid])
    con.executemany("INSERT OR IGNORE INTO duels(game_id, killer, victim, kills, hs, openings) VALUES (?,?,?,?,?,?)",
                    [(gid[d["ext_id"]], d["killer"], d["victim"], d["kills"], d["hs"], d["openings"])
                     for d in extras.get("duels", []) if d["ext_id"] in gid])
    statsdb.bump_rev(con)
    con.commit()
    con.execute("VACUUM")
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("games", "game_players", "merges",
                                                                                 "duels")}
    con.close()
    os.replace(tmp, path)
    print(f"wrote {os.path.relpath(path)} ({os.path.getsize(path) / 1e6:.1f} MB): {counts}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bundle", help="the cs2-stats-bundle folder, after `npm run build:data`")
    ap.add_argument("--pull-extras", action="store_true",
                    help="first save merges, demo duels and highlight links from the hosted database (DATABASE_URL)")
    a = ap.parse_args()
    if a.pull_extras:
        pull_extras()
    site, players, matches, skipped = convert(a.bundle)
    os.makedirs(OUT, exist_ok=True)
    for name, obj in (("site", site), ("players", players), ("matches", matches)):
        with open(os.path.join(OUT, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=0)
    newest = max(matches)
    print(f"{len(players)} players, {len(matches)} finished matches (newest #{newest}, {matches[newest]['date']}), "
          f"{len(skipped)} left out" + (": " + ", ".join(f"#{i} {why}" for i, why in skipped) if skipped else ""))
    import db
    build_stats_db(site, players, matches, db.STATS_DB)
    print("Commit import5v5/data/ and web/stats.db, then push: the site serves the new stats after the deploy.")


if __name__ == "__main__":
    main()
