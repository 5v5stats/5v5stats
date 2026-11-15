"""Turn the cached 5v5stats pages (fetch.py) into clean JSON.

    python import5v5\\parse.py     -> import5v5\\data\\{site.json, players.json, matches.json}

site.json     leaderboard dataset from the home page (all players, by period), awards, avatars, sync time
players.json  per player: the site's full record (raw totals + its metrics), per-map stats, match history
matches.json  per match: map, date, rounds, teams with score/winner/players (K-D-A, ADR, HS%), round winners
"""
import datetime
import glob
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rsc  # noqa: E402

RAW = os.path.join(HERE, "raw")
OUT = os.path.join(HERE, "data")
MONTHS = {"Oca": 1, "Şub": 2, "Mar": 3, "Nis": 4, "May": 5, "Haz": 6, "Tem": 7, "Ağu": 8, "Eyl": 9, "Eki": 10,
          "Kas": 11, "Ara": 12}
MAPS = {"Dust II": "de_dust2", "Inferno": "de_inferno", "Mirage": "de_mirage", "Nuke": "de_nuke",
        "Anubis": "de_anubis", "Ancient": "de_ancient", "Vertigo": "de_vertigo", "Cache": "de_cache",
        "Train": "de_train", "Overpass": "de_overpass", "Cbble d": "de_cbble_d", "Cobblestone": "de_cbble"}


def tr_date(s):
    m = re.match(r"(\d{1,2}) (\S+) (\d{4})", s.strip())
    if not m or m.group(2) not in MONTHS:
        return None
    return datetime.date(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1))).isoformat()


def find_dict(x, key):
    """First dict (depth-first) that has `key`."""
    if isinstance(x, dict):
        if key in x:
            return x
        for v in x.values():
            r = find_dict(v, key)
            if r is not None:
                return r
    elif isinstance(x, list):
        for y in x:
            r = find_dict(y, key)
            if r is not None:
                return r
    return None


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------- home ----

def parse_home():
    r = rsc.rows(read(os.path.join(RAW, "home.html")))
    d = None
    for v in r.values():
        d = find_dict(v, "byPeriod")
        if d:
            break
    if d is None:
        raise SystemExit("home.html: leaderboard data not found (site changed?)")
    return {"lastSyncMs": d.get("lastSyncMs"), "byPeriod": d["byPeriod"], "awardsByPeriod": d.get("awardsByPeriod"),
            "avatars": d.get("avatars", {}), "activityByMonth": d.get("activityByMonth"), "form": d.get("form")}


# ---------------------------------------------------------------- players ----

def parse_player(path):
    r = rsc.rows(read(path))
    rec = None
    for v in r.values():
        rec = find_dict(v, "steamid64")
        if rec and "total_matches" in rec:
            break
    history, seen = [], set()
    for v in [rsc.main_tree(r)]:
        for tr in rsc.walk(v):
            if tr[1] != "tr":
                continue
            tds = rsc.cells(tr)
            mlinks = rsc.links(tr, "/matches/")
            if len(tds) >= 8 and mlinks and mlinks[0][0] not in seen:
                seen.add(mlinks[0][0])
                t = [rsc.text(c).strip() for c in tds]
                kda = re.findall(r"\d+", t[4])
                history.append({"match": int(mlinks[0][0].split("/")[-1]), "date": tr_date(t[0]), "map": t[2],
                                "team": t[3], "k": int(kda[0]), "d": int(kda[1]), "a": int(kda[2]),
                                "adr": rsc.num(t[5]), "hs_pct": rsc.num(t[6]), "result": t[7]})
    return rec, history


# ---------------------------------------------------------------- matches ----

def split_scores(heads, rounds):
    """Team headers read "<name><score>" with nothing between them, so "unsel4316" may be unsel43 + 16.
    Pick the name/score split where the scores add up to the rounds played (else the shortest score)."""
    options = []
    for h in heads:
        digits = re.search(r"\d+$", h).group(0)
        opts = [(h[:len(h) - k].strip(), int(digits[-k:])) for k in range(1, min(len(digits), 2) + 1)]
        options.append(opts)
    if len(options) == 2 and rounds:
        for a in options[0]:
            for b in options[1]:
                if a[1] + b[1] == rounds:
                    return [a, b]
    return [opts[0] for opts in options]


def parse_match(path):
    html = read(path)
    els = list(rsc.walk(rsc.main_tree(rsc.rows(html))))
    if not els:
        return None
    m = {"id": int(os.path.splitext(os.path.basename(path))[0])}
    # facts: <dt>Tarih</dt><dd>4 Kas 2026</dd> ...
    dts = [e for e in els if e[1] in ("dt", "dd")]
    facts = {}
    for a, b in zip(dts, dts[1:]):
        if a[1] == "dt" and b[1] == "dd":
            facts[rsc.text(a).strip()] = rsc.text(b).strip()
    m["date"] = tr_date(facts.get("Tarih", ""))
    m["rounds"] = rsc.num(facts.get("Round", ""))
    m["total_kills"] = rsc.num(facts.get("Toplam kill", ""))
    h1 = next((rsc.text(e) for e in els if e[1] == "h1"), "")
    map_name = re.sub(r"^.*?\d+\s*", "", h1).strip() or h1.strip()
    for name in sorted(MAPS, key=len, reverse=True):
        if name in h1:
            map_name = name
            break
    m["map_name"] = map_name
    m["map"] = MAPS.get(map_name, "de_" + re.sub(r"\W+", "", map_name.lower()))

    # teams: each scoreboard table sits in a block whose text starts with Kazanan (winner) / Kaybeden (loser)
    teams = []
    for e in els:
        if e[1] != "table":
            continue
        rows_ = [tr for tr in rsc.walk(e) if tr[1] == "tr" and isinstance(tr[2], str) and tr[2].isdigit()]
        players = []
        for tr in rows_:
            tds = rsc.cells(tr)
            t = [rsc.text(c).strip() for c in tds]
            kda = re.findall(r"\d+", t[1])
            players.append({"steamid": tr[2], "name": t[0], "k": int(kda[0]), "d": int(kda[1]), "a": int(kda[2]),
                            "adr": rsc.num(t[2]), "hs_pct": rsc.num(t[3]),
                            "kast": rsc.num(t[4]) if len(t) > 4 else None,
                            "rating": rsc.num(t[5]) if len(t) > 5 else None})
        teams.append({"players": players})
    # winner/loser headers in page order: "Kazanan <name> <score>" / "Kaybeden <name> <score>"
    heads = []
    for e in els:
        if e[1] in ("div", "header", "section") and isinstance(e[3], dict):
            t = rsc.text(e)
            mm = re.match(r"^(Kazanan|Kaybeden|Berabere)(.+?\d+)Oyuncu", t)
            if mm and len(t) < 2000:
                heads.append((mm.group(1), mm.group(2)))  # "<name><score>": split below
    # the innermost matching blocks come last for each table; keep one per table in order
    uniq = []
    for h in heads:
        if not uniq or uniq[-1] != h:
            uniq.append(h)
    seen, ordered = set(), []
    for h in uniq:
        if h not in seen:
            seen.add(h)
            ordered.append(h)
    ordered = ordered[-len(teams):] if len(ordered) >= len(teams) else ordered
    for team, (name, score) in zip(teams, split_scores([h[1] for h in ordered], m["rounds"])):
        team["name"], team["score"] = name, score
    for team, h in zip(teams, ordered):
        team["result"], team["won"] = h[0], h[0] == "Kazanan"
    m["teams"] = teams

    # round winners from the advantage chart: line points = lead of the named team before each round
    m["round_winners"] = None
    svg = next((e for e in els if e[1] == "svg" and "aria-label" in e[3]), None)
    if svg is not None:
        ref = svg[3]["aria-label"].replace(" round başına üstünlük grafiği", "").strip()
        line = next((p for p in rsc.walk(svg) if p[1] == "path" and p[3].get("fill") == "none"), None)
        top = next((rsc.text(t) for t in rsc.walk(svg) if t[1] == "text" and str(t[3].get("y")) == "30"), "")
        n = rsc.num(top.replace("+", ""))
        if line is not None and n:
            ys = [float(y) for y in re.findall(r"[\d.]+,([\d.]+)", line[3]["d"])]
            unit = 104.0 / n
            leads = [round((130.0 - y) / unit) for y in ys]
            winners = [ref if b > a else "other" for a, b in zip(leads, leads[1:])]
            names = [t.get("name") for t in teams]
            other = next((x for x in names if x != ref), None)
            final_winner = next((t["name"] for t in teams if t.get("won")), None)
            winners = [w if w == ref else other for w in winners] + [final_winner]
            m["round_winners"] = winners if len(winners) == m["rounds"] else None
    return m


def main():
    os.makedirs(OUT, exist_ok=True)
    site = parse_home()
    players, problems = {}, []
    for path in sorted(glob.glob(os.path.join(RAW, "players", "*.html"))):
        rec, hist = parse_player(path)
        if rec is None:
            problems.append(f"player {os.path.basename(path)}: no data")
            continue
        rec["history"] = hist
        players[rec["steamid64"]] = rec
    matches = {}
    for path in sorted(glob.glob(os.path.join(RAW, "match", "*.html")), key=lambda p: int(os.path.basename(p)[:-5])):
        m = parse_match(path)
        if not m:
            problems.append(f"match {os.path.basename(path)}: unreadable")
            continue
        if len(m["teams"]) != 2 or any("name" not in t for t in m["teams"]):
            problems.append(f"match {m['id']}: {len(m['teams'])} teams parsed")
        matches[m["id"]] = m
    for name, obj in (("site", site), ("players", players), ("matches", matches)):
        with open(os.path.join(OUT, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=0)
    flows = sum(1 for m in matches.values() if m["round_winners"])
    print(f"{len(players)} players, {len(matches)} matches ({flows} with round-by-round winners), "
          f"leaderboard of {len(site['byPeriod']['all']['players'])} players, synced "
          f"{datetime.datetime.fromtimestamp(site['lastSyncMs'] / 1000):%Y-%m-%d %H:%M}")
    for p in problems[:20]:
        print("  problem:", p)
    if len(problems) > 20:
        print(f"  ... {len(problems) - 20} more problems")


if __name__ == "__main__":
    main()
