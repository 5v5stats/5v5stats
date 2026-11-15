"""Download 5v5stats.com's public pages (the site owner agreed) into a local cache.

    python import5v5\\fetch.py              fetch what isn't cached yet (resumable)
    python import5v5\\fetch.py --refresh    re-fetch home, lists and player pages (new weekly data);
                                           match pages already cached are kept (finished matches don't change)

Polite: one request at a time, ~1 per second, only public pages (robots.txt disallows /api/, so it isn't used).
Cache: C:\\cs2render\\import5v5\\raw\\  (home.html, matches.html, players\\<steamid>.html, match\\<id>.html)
"""
import argparse
import os
import re
import sys
import time

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = "https://www.5v5stats.com"
HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw")
UA = {"User-Agent": "Mozilla/5.0 (cs2render local stats import; friend of the site owner)"}
DELAY = 1.0
session = requests.Session()
_last = [0.0]


def get(path):
    wait = DELAY - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    for attempt in range(4):
        try:
            r = session.get(BASE + path, headers=UA, timeout=30)
            _last[0] = time.time()
            if r.status_code == 200:
                return r.text
            if r.status_code == 404:
                return None
            print(f"  {path}: HTTP {r.status_code}, retrying")
        except requests.RequestException as e:
            print(f"  {path}: {e}, retrying")
        time.sleep(5 * (attempt + 1))
    raise SystemExit(f"Giving up on {path}")


def save(rel, text):
    path = os.path.join(RAW, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(path + ".tmp", path)


def cached(rel):
    return os.path.isfile(os.path.join(RAW, rel))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="re-fetch home, lists and player pages")
    a = ap.parse_args()
    os.makedirs(RAW, exist_ok=True)
    t0 = time.time()

    sitemap = get("/sitemap.xml") or ""
    save("sitemap.xml", sitemap)
    match_ids = {int(m) for m in re.findall(r"/matches/(\d+)<", sitemap)}
    player_ids = set(re.findall(r"/players/(\d{17})<", sitemap))
    for rel, path in (("home.html", "/"), ("matches.html", "/matches")):
        if a.refresh or not cached(rel):
            text = get(path)
            save(rel, text)
            player_ids |= set(re.findall(r'/players/(\d{17})"', text))
            match_ids |= {int(m) for m in re.findall(r'/matches/(\d+)"', text)}
    print(f"sitemap/home: {len(player_ids)} players, {len(match_ids)} matches")

    for i, sid in enumerate(sorted(player_ids), 1):
        rel = os.path.join("players", sid + ".html")
        if a.refresh or not cached(rel):
            text = get(f"/players/{sid}")
            if text:
                save(rel, text)
            print(f"\rplayers {i}/{len(player_ids)}", end="", flush=True)
        if cached(rel):  # player histories list matches the sitemap may miss
            with open(os.path.join(RAW, rel), encoding="utf-8") as f:
                match_ids |= {int(m) for m in re.findall(r'/matches/(\d+)\\?"', f.read())}
    print(f"\nall match numbers found: {len(match_ids)}")

    todo = [m for m in sorted(match_ids) if not cached(os.path.join("match", f"{m}.html"))]
    missing = 0
    for i, mid in enumerate(todo, 1):
        text = get(f"/matches/{mid}")
        if text:
            save(os.path.join("match", f"{mid}.html"), text)
        else:
            missing += 1
        print(f"\rmatches {i}/{len(todo)}", end="", flush=True)
    print(f"\ndone in {(time.time() - t0) / 60:.1f} min; {len(match_ids) - missing} match pages, "
          f"{len(player_ids)} player pages cached in {RAW}" + (f"; {missing} match numbers had no page" if missing else ""))


if __name__ == "__main__":
    main()
