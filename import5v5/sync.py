"""Bring the website's player stats up to date with 5v5stats.com.

    python import5v5\\sync.py             download new data (gently), parse it, load it into the website
    python import5v5\\sync.py --offline   only re-parse the cached pages and reload them

5v5stats updates about weekly. Matches we also have as demos are merged automatically: the 5v5stats copy
counts in the stats and links to our highlight page. Nothing here touches CS2.
"""
import argparse
import datetime
import json
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "web"))
import db  # noqa: E402
import statsdb  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="don't download, use the cached pages")
    a = ap.parse_args()
    py = sys.executable
    if not a.offline:
        subprocess.run([py, "-u", os.path.join(HERE, "fetch.py"), "--refresh"], check=True)
    subprocess.run([py, os.path.join(HERE, "parse.py")], check=True)
    data = {n: json.load(open(os.path.join(HERE, "data", n + ".json"), encoding="utf-8"))
            for n in ("site", "players", "matches")}
    db.init()
    con = db.connect()
    merged = statsdb.import_5v5(con, data["site"], data["players"], data["matches"])
    n_games = con.execute("SELECT COUNT(*) FROM games WHERE source = '5v5stats'").fetchone()[0]
    n_demo = con.execute("SELECT COUNT(*) FROM games WHERE source = 'demo'").fetchone()[0]
    con.close()
    synced = datetime.datetime.fromtimestamp(data["site"]["lastSyncMs"] / 1000)
    print(f"Website stats updated: {n_games} matches from 5v5stats (data of {synced:%Y-%m-%d %H:%M}), "
          f"{n_demo} newer demo matches on top" + (f"; {merged} demo match(es) merged with 5v5stats" if merged else ""))


if __name__ == "__main__":
    main()
