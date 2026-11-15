"""Import a match package (zip made by upload.py on the rendering PC) into the website.

A package contains:
  match_data.json         stats and page data (scoreboard_html.collect)
  *.mp4                   the highlight clips
  thumbs/*.jpg            clip thumbnails
  avatars/<steamid>.jpg   profile pictures
Anything else in the zip is ignored. Uploading the same match again (same "key") replaces its files and
stats but keeps its link, title and public/hidden setting.

Files are stored by storage.py (this PC's disk, or Cloudflare R2). With R2, upload.py doesn't send a zip:
it sends the files straight to the bucket and only the page data to the site (app.py /api/upload/begin and
/api/upload/finish), which then calls prepare() and apply() here.
"""
import json
import os
import re
import shutil
import tempfile
import time
import uuid
import zipfile

import db
import statsdb
import storage

HERE = os.path.dirname(os.path.abspath(__file__))
ALLOWED = re.compile(r"^(match_data\.json|[^/\\:]+\.mp4|thumbs/[^/\\:]+\.jpg|avatars/\d{1,20}\.jpg)$")
MAX_UNPACKED = 4 * 1024 ** 3   # refuse zips that would unpack to more than 4 GB
PLAYER_KEYS = ("name", "team", "rating", "swing", "kills", "deaths", "assists", "adr", "kd", "kpr", "hs",
               "hs_pct", "kast", "opening", "m5", "m4", "m3", "m2", "mvps", "damage", "rounds")


class BadPackage(Exception):
    pass


def summary(data):
    """Short version for lists (same as scoreboard_html.summary, kept here so the server needs no PC code)."""
    pl = data["players"]
    best = max(pl, key=lambda s: pl[s]["rating"]) if pl else None
    clips = data["clips"]
    return {"map": data["map"], "date": data["date"], "rounds": data["rounds"],
            "teams": [{"name": t["name"], "score": t["score"]} for t in data["teams"]],
            "mvp": {"name": pl[best]["name"] if best else "", "rating": round(pl[best]["rating"], 2) if best else 0,
                    "avatar": data["pics"].get(best, "") if best else ""},
            "thumb": next((c["thumb2"] or c["thumb"] for c in clips if c.get("thumb2") or c.get("thumb")), ""),
            "clips": len(clips)}


def check_data(data):
    """The page data must have the shape the page expects; file references must stay inside the match folder."""
    try:
        for k in ("key", "map", "date", "rounds", "teams", "players", "clips", "pics"):
            if k not in data:
                raise BadPackage(f"match_data.json has no '{k}'")
        if not re.fullmatch(r"[\w.\-]{1,120}", str(data["key"]), re.UNICODE):
            raise BadPackage("invalid match key")
        if not isinstance(data["players"], dict) or not data["players"]:
            raise BadPackage("no players")
        for sid, p in data["players"].items():
            missing = [k for k in PLAYER_KEYS if k not in p]
            if missing:
                raise BadPackage(f"player {sid} is missing {', '.join(missing)}")
            if p["team"] not in (0, 1):
                raise BadPackage("player team must be 0 or 1")
        if not isinstance(data["teams"], list) or not 1 <= len(data["teams"]) <= 2:
            raise BadPackage("expected two teams")
        for t in data["teams"]:
            if not all(k in t for k in ("name", "score", "halves", "avg_rating", "players")):
                raise BadPackage("team data incomplete")
            t["players"] = [s for s in t["players"] if s in data["players"]]
        for c in data["clips"]:
            for k in ("player", "steamid", "label", "round", "score", "file", "thumb", "thumb2"):
                if k not in c:
                    raise BadPackage(f"clip data is missing '{k}'")
            if not re.fullmatch(r"[^/\\:]+\.mp4", c["file"]):
                raise BadPackage(f"bad clip file name {c['file']!r}")
            for k in ("thumb", "thumb2"):
                if c[k] and not re.fullmatch(r"thumbs/[^/\\:]+\.jpg", c[k]):
                    raise BadPackage(f"bad thumbnail path {c[k]!r}")
        for sid, p in data["pics"].items():
            if not re.fullmatch(r"avatars/\d{1,20}\.jpg", p):
                raise BadPackage(f"bad picture path {p!r}")
        counters = data.get("counters") or {}
        if not isinstance(counters, dict):
            raise BadPackage("counters must be an object")
        for sid, c in counters.items():
            if not re.fullmatch(r"\d{1,20}", str(sid)) or not isinstance(c, dict):
                raise BadPackage("bad player counters")
            for k in statsdb.rating.TOTALS:
                c[k] = int(c.get(k, 0) or 0)  # numbers only
            c["name"] = str(c.get("name", ""))[:64]
        if data.get("duels") is not None:  # older packages have none: the stored duels are kept
            duels = data["duels"]
            if not isinstance(duels, list) or len(duels) > 400:
                raise BadPackage("duels must be a list")
            data["duels"] = [{"k": str(d["k"]), "v": str(d["v"]), "n": int(d.get("n", 0)), "hs": int(d.get("hs", 0)),
                              "op": int(d.get("op", 0))} for d in duels]
            if not all(re.fullmatch(r"\d{1,20}", d["k"]) and re.fullmatch(r"\d{1,20}", d["v"]) for d in data["duels"]):
                raise BadPackage("bad duel steamid")
    except (TypeError, AttributeError, ValueError) as e:
        raise BadPackage(f"match_data.json has an unexpected format ({e})")


def _same(a, b):
    if os.path.getsize(a) != os.path.getsize(b):
        return False
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            x, y = fa.read(1 << 20), fb.read(1 << 20)
            if x != y:
                return False
            if not x:
                return True


def merge_into(src_dir, dst_dir):
    """Make dst_dir hold exactly src_dir's files, touching only what changed. Returns notes."""
    notes, keep = [], set()
    for root, _, files in os.walk(src_dir):
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), src_dir)
            keep.add(os.path.normcase(rel))
            src, dst = os.path.join(src_dir, rel), os.path.join(dst_dir, rel)
            if os.path.isfile(dst) and _same(src, dst):
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                os.replace(src, dst)
            except PermissionError:
                notes.append(f"{rel} is in use (someone is watching it?); the old copy stays until the next upload")
    for root, _, files in os.walk(dst_dir):
        for f in files:
            path = os.path.join(root, f)
            if os.path.normcase(os.path.relpath(path, dst_dir)) not in keep:
                try:
                    os.remove(path)
                except PermissionError:
                    pass  # in use; removed by a later upload
    return notes


def new_rev():
    return uuid.uuid4().hex[:8]


def same_match(con, data):
    """An already uploaded match that is this one, whatever its demo file was called: same map, rounds and
    score, at least 8 of the same players, each with the same kills and deaths. (The key is the demo's file
    name, so a renamed copy of a demo, or the same match uploaded by someone else, would otherwise count twice.)"""
    pl = data["players"]
    score = sorted(t["score"] for t in data["teams"])
    for m in con.execute("SELECT * FROM matches WHERE map = ? AND rounds = ?", (data["map"], data["rounds"])):
        if sorted([m["score1"] or 0, m["score2"] or 0]) != score:
            continue
        try:
            other = json.loads(m["data_json"])["players"]
        except (ValueError, KeyError, TypeError):
            continue
        shared = set(pl) & set(other)
        if len(shared) >= 8 and all(pl[s]["kills"] == other[s].get("kills") and pl[s]["deaths"] == other[s].get("deaths")
                                    for s in shared):
            return m
    return None


def prepare(con, data):
    """Validate page data; returns (match key, existing match row or None, slug). An upload of a match that is
    already on the site (same key, or the same game under another demo name) updates it: never a second copy."""
    check_data(data)
    key = str(data["key"])
    existing = con.execute("SELECT * FROM matches WHERE match_key = ?", (key,)).fetchone() or same_match(con, data)
    if existing:
        key = data["key"] = existing["match_key"]
    return key, existing, (existing["slug"] if existing else uuid.uuid4().hex[:10])


def apply(con, data, key, existing, slug, rev, files, via, size, notes=None):
    """Store a match whose files are in place. files = {relative path: size} that were actually stored.
    Returns (match row, created, notes)."""
    notes = list(notes or [])
    files = {p: n for p, n in files.items() if ALLOWED.match(p) and p != "match_data.json"}
    have = files.__contains__
    before = len(data["clips"])
    data["clips"] = [c for c in data["clips"] if have(c["file"])]
    if len(data["clips"]) < before:
        notes.append(f"{before - len(data['clips'])} clip(s) listed but not in the package")
    for c in data["clips"]:
        for k in ("thumb", "thumb2"):
            if c[k] and not have(c[k]):
                c[k] = ""
    data["pics"] = {s: p for s, p in data["pics"].items() if have(p)}
    if not data["clips"]:
        data["note"] = "No highlight clips were uploaded for this match."
    stored = sum(files.values())

    now = time.time()
    if existing:
        mid = existing["id"]
        con.execute("UPDATE matches SET size_bytes=?, uploads=uploads+1, media_rev=? WHERE id=?", (stored, rev, mid))
    else:
        mid = con.execute("INSERT INTO matches(slug, match_key, data_json, size_bytes, media_rev, created_at, "
                          "updated_at) VALUES (?,?,?,?,?,?,?)", (slug, key, "{}", stored, rev, now, now)).lastrowid
    db.save_match_data(con, mid, data, summary(data))
    if data.get("counters"):  # player stats: add this match to the leaderboard
        statsdb.add_demo(con, key, data)
    con.execute("INSERT INTO uploads(match_id, via, ok, message, size_bytes, created_at) VALUES (?,?,?,?,?,?)",
                (mid, via, 1, ("updated" if existing else "new match") + (f"; {'; '.join(notes)}" if notes else ""),
                 size, now))
    con.commit()
    return con.execute("SELECT * FROM matches WHERE id = ?", (mid,)).fetchone(), existing is None, notes


def _walk(folder):
    out = {}
    for root, _, fs in os.walk(folder):
        for f in fs:
            p = os.path.join(root, f)
            out[os.path.relpath(p, folder).replace(os.sep, "/")] = os.path.getsize(p)
    return out


def ingest(con, zip_path, via="admin"):
    """Import a package zip (admin form, or upload.py talking to a site that stores files itself).
    Returns (match row, created: bool, notes: list of str)."""
    size = os.path.getsize(zip_path)
    try:
        z = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise BadPackage("not a zip file")
    with z:
        names = set(z.namelist())
        if "match_data.json" not in names:
            raise BadPackage("the zip has no match_data.json (make packages with upload.py)")
        if sum(i.file_size for i in z.infolist()) > MAX_UNPACKED:
            raise BadPackage("package too large")
        try:
            data = json.loads(z.read("match_data.json").decode("utf-8"))
        except ValueError:
            raise BadPackage("match_data.json is not valid JSON")
        key, existing, slug = prepare(con, data)

        # unpack only the allowed files into a fresh folder
        incoming = (tempfile.mkdtemp(prefix=f"{slug}-") if storage.R2 else storage.local_dir(slug) + ".incoming")
        shutil.rmtree(incoming, ignore_errors=True)
        os.makedirs(incoming)
        notes = []
        for info in z.infolist():
            if info.is_dir() or info.filename == "match_data.json":
                continue
            if not ALLOWED.match(info.filename):
                notes.append(f"ignored {info.filename}")
                continue
            dst = os.path.join(incoming, *info.filename.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with z.open(info) as src, open(dst, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
    files = _walk(incoming)

    if storage.R2:  # to the bucket, in a new folder; older uploads' files are removed once the page points here
        rev = new_rev()
        try:
            for rel in files:
                storage.put_file(storage.key(slug, rev, rel), os.path.join(incoming, *rel.split("/")))
        finally:
            shutil.rmtree(incoming, ignore_errors=True)
        result = apply(con, data, key, existing, slug, rev, files, via, size, notes)
        storage.drop_other_revs(slug, rev)
        return result

    final = storage.local_dir(slug)
    if os.path.isdir(final):
        # update in place: files someone is watching right now are locked on Windows, so identical files are
        # kept as they are and only changed or new files are swapped in; files no longer used are removed
        # (a locked one stays until next time)
        notes += merge_into(incoming, final)
        shutil.rmtree(incoming, ignore_errors=True)
    else:
        os.makedirs(os.path.dirname(final), exist_ok=True)
        os.replace(incoming, final)
    return apply(con, data, key, existing, slug, "", files, via, size, notes)


def log_failure(con, via, message, size):
    con.execute("INSERT INTO uploads(match_id, via, ok, message, size_bytes, created_at) VALUES (NULL,?,0,?,?,?)",
                (via, message[:300], size, time.time()))
    con.commit()
