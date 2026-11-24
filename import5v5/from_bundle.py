"""Load the stats straight from the MatchZy data pipeline, instead of reading them off the old site's pages.

The old 5v5stats.com (a Next.js site, `cs2-stats-bundle`) was built from the server's MatchZy exports by its
`npm run build:data`, which writes `public/data/*.json`. fetch.py + parse.py used to read that same data back
from the published pages. Since 5v5stats.com now serves this website, those pages are gone, so this script
reads the JSON files directly and writes the same three files parse.py made, then loads them.

    python import5v5\\from_bundle.py <cs2-stats-bundle folder>            convert and load into the website
    python import5v5\\from_bundle.py <cs2-stats-bundle folder> --dry-run  only write import5v5\\data\\*.json

Only finished matches are kept (statsdb.finished), whatever the pipeline produced.
"""
import argparse
import datetime
import json
import math
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "web"))
import statsdb  # noqa: E402

OUT = os.path.join(HERE, "data")
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bundle", help="the cs2-stats-bundle folder, after `npm run build:data`")
    ap.add_argument("--dry-run", action="store_true", help="only write import5v5/data/*.json")
    a = ap.parse_args()
    site, players, matches, skipped = convert(a.bundle)
    os.makedirs(OUT, exist_ok=True)
    for name, obj in (("site", site), ("players", players), ("matches", matches)):
        with open(os.path.join(OUT, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=0)
    newest = max(matches)
    print(f"{len(players)} players, {len(matches)} finished matches (newest #{newest}, {matches[newest]['date']}), "
          f"{len(skipped)} left out" + (": " + ", ".join(f"#{i} {why}" for i, why in skipped) if skipped else ""))
    if a.dry_run:
        return
    import db
    db.init()
    con = db.connect()
    merged = statsdb.import_5v5(con, site, players, matches)
    # The pipeline's week starts on Monday; this site's week is the last 7 days. Their 30 days are the same
    # rolling window, so only the weekly totals would be wrong if used (on a day the newest match was played).
    totals = statsdb.get_meta(con, "period_totals", {}) or {}
    totals.pop("7d", None)
    statsdb.set_meta(con, "period_totals", totals)
    con.commit()
    n_games = con.execute("SELECT COUNT(*) FROM games WHERE source = '5v5stats'").fetchone()[0]
    n_demo = con.execute("SELECT COUNT(*) FROM games WHERE source = 'demo'").fetchone()[0]
    con.close()
    print(f"Website stats updated: {n_games} server matches, {n_demo} newer demo matches on top"
          + (f"; {merged} demo match(es) merged with the server's copy" if merged else ""))


if __name__ == "__main__":
    main()
