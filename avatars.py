"""Steam profile pictures for players, from their public Steam community profile (no API key needed).

Pictures are cached in cache/avatars/<steamid64>.jpg and refreshed after a week. Private profiles,
bots and network errors simply return None (the page then shows initials).
"""
import concurrent.futures
import os
import re
import shutil
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "cache", "avatars")
MAX_AGE = 7 * 24 * 3600
UA = {"User-Agent": "cs2render"}


def _get(url, timeout=8):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def fetch(steamid):
    """Path of the cached picture for this SteamID64, downloading it if needed; None if unavailable."""
    sid = str(steamid)
    if not re.fullmatch(r"7656\d{13}", sid):  # bots / empty slots have no Steam profile
        return None
    path = os.path.join(CACHE, sid + ".jpg")
    if os.path.isfile(path) and time.time() - os.path.getmtime(path) < MAX_AGE:
        return path
    try:
        xml = _get(f"https://steamcommunity.com/profiles/{sid}/?xml=1").decode("utf-8", "replace")
        m = re.search(r"<avatarFull><!\[CDATA\[(https://[^\]]+)\]\]></avatarFull>", xml)  # first one = the player's
        if not m:
            return path if os.path.isfile(path) else None
        data = _get(m.group(1))
        os.makedirs(CACHE, exist_ok=True)
        with open(path + ".tmp", "wb") as f:
            f.write(data)
        os.replace(path + ".tmp", path)
        return path
    except Exception:
        return path if os.path.isfile(path) else None  # offline: an older picture is better than none


def fetch_all(steamids):
    """{steamid: cached path or None}, fetched in parallel."""
    ids = list(dict.fromkeys(str(s) for s in steamids))
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        return dict(zip(ids, ex.map(fetch, ids)))


def copy_into(steamids, folder):
    """Copy the players' pictures into <folder>/avatars/ (so a page there works on its own).
    Returns {steamid: relative path 'avatars/<id>.jpg'} for the players that have one."""
    out = {}
    for sid, src in fetch_all(steamids).items():
        if src:
            os.makedirs(os.path.join(folder, "avatars"), exist_ok=True)
            dst = os.path.join(folder, "avatars", sid + ".jpg")
            if not (os.path.isfile(dst) and os.path.getmtime(dst) >= os.path.getmtime(src)):
                shutil.copyfile(src, dst)
            out[sid] = f"avatars/{sid}.jpg"
    return out


if __name__ == "__main__":
    import sys
    for s, p in fetch_all(sys.argv[1:]).items():
        print(s, p)
