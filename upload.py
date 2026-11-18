"""Upload rendered matches (highlights + stats) to the website.

    python upload.py de_inferno_2026-11-05_02-31-19             upload one match from output\\
    python upload.py de_inferno_2026-11-05_02-31-19 --hidden    ... but keep it hidden (public by default)
    python upload.py de_inferno_2026-11-05_02-31-19 --zip-only  only make the package (to upload by hand in Admin)
    python upload.py --all                                       every rendered match in output\\

A package is match_data.json (stats), the clips, thumbnails and profile pictures. The demo is not uploaded;
the website only gets results. Uploading the same match again updates it on the site.

How it's sent depends on the site: one that stores files in a Cloudflare R2 bucket (the hosted site on
Vercel) gives signed links and the files go straight into the bucket, then only the stats go to the site;
one that keeps files itself (the local prototype) takes the package as one zip.

Server and token: SERVER_URL / UPLOAD_TOKEN in upload.env. Defaults: http://localhost:5000 and the token
from web\\admin.env (the local prototype on this PC).
"""
import argparse
import json
import os
import sys
import tempfile
import time
import zipfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.abspath(__file__))
OUTPUT = os.path.join(ROOT, "output")
CONTENT_TYPES = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
CSGO = r"C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive\game\csgo"


def read_env(path):
    vals = {}
    if os.path.isfile(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if v.strip():
                    vals[k.strip()] = v.strip()
    return vals


def settings():
    env = read_env(os.path.join(ROOT, "upload.env"))
    url = env.get("SERVER_URL", "http://localhost:5000").rstrip("/")
    token = env.get("UPLOAD_TOKEN") or read_env(os.path.join(ROOT, "web", "admin.env")).get("UPLOAD_TOKEN", "")
    return url, token


def clips_enabled():
    """UPLOAD_CLIPS=0 in upload.env: send stats and profile pictures only (no clips or thumbnails)."""
    v = read_env(os.path.join(ROOT, "upload.env")).get("UPLOAD_CLIPS", "1")
    return v.strip().lower() not in ("0", "no", "false", "off")


def match_data(folder):
    """match_data.json for a render folder; (re)made from the demo if missing or older than the clips."""
    path = os.path.join(folder, "match_data.json")
    cj = os.path.join(folder, "clips.json")
    fresh = os.path.isfile(path) and (not os.path.isfile(cj) or os.path.getmtime(path) >= os.path.getmtime(cj))
    def complete(d):  # made by the current scoreboard_html (older packages lack duels / walking / flash counters)
        return {"key", "counters", "duels"} <= set(d) and all("bomb_plants" in c for c in d["counters"].values())

    if not fresh or not complete(json.load(open(path, encoding="utf-8"))):
        demo = os.path.join(CSGO, os.path.basename(folder) + ".dem")
        if not os.path.isfile(demo):
            raise SystemExit(f"{folder}: no match_data.json and the demo {demo} is gone, so stats can't be made")
        print("  making stats (match_data.json) ...")
        import scoreboard_html
        scoreboard_html.write(demo, os.path.join(folder, "scoreboard.html"))
    return json.load(open(path, encoding="utf-8"))


def package_files(folder, clips=True):
    """(match data, files of the package that exist, as relative paths with /). clips=False: only the
    players' profile pictures (small), no clips or thumbnails."""
    data = match_data(folder)
    files = []
    if clips:
        for c in data["clips"]:
            files += [c["file"]] + [c[k] for k in ("thumb", "thumb2") if c.get(k)]
    files += list(data["pics"].values())
    return data, [f for f in dict.fromkeys(files) if os.path.isfile(os.path.join(folder, *f.split("/")))]


def package(folder, dst, clips=True):
    """Write the match package zip. Returns (number of files, bytes)."""
    files = ["match_data.json"] + package_files(folder, clips)[1]
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_STORED) as z:  # videos and jpgs are already compressed
        for f in files:
            z.write(os.path.join(folder, *f.split("/")), f)
    return len(files), os.path.getsize(dst)


class Progress:
    """File wrapper that prints upload progress while requests reads it (offset/total: for several files)."""
    def __init__(self, path, offset=0, total=None):
        self.f, self.size, self.done, self.t = open(path, "rb"), os.path.getsize(path), 0, 0
        self.offset, self.total = offset, total or self.size

    def read(self, n=-1):
        b = self.f.read(n if n and n > 0 else 1 << 20)
        self.done += len(b)
        if time.time() - self.t > 0.5 or not b:
            self.t = time.time()
            print(f"\r  uploading {(self.offset + self.done) / 1e6:6.1f} / {self.total / 1e6:.1f} MB", end="",
                  flush=True)
        return b

    def __len__(self):
        return self.size


def upload(folder, public=True, zip_only=False, url=None, token=None, clips=None):
    name = os.path.basename(os.path.normpath(folder))
    if not os.path.isdir(folder):
        raise SystemExit(f"No such render folder: {folder}")
    clips = clips_enabled() if clips is None else clips
    print(f"{name}:" + ("" if clips else "  (stats only: clips switched off, UPLOAD_CLIPS=0 in upload.env)"))
    if zip_only:
        dst = os.path.join(OUTPUT, name + ".zip")
        n, size = package(folder, dst, clips)
        print(f"  package: {dst} ({n} files, {size / 1e6:.1f} MB). Upload it in Admin > Upload a package by hand.")
        return dst
    url, token = (url, token) if url else settings()
    if not token:
        raise SystemExit("No upload token: put UPLOAD_TOKEN in upload.env (it's in the website's admin.env).")
    import requests
    auth = {"Authorization": f"Bearer {token}"}
    params = {"publish": "1"} if public else None
    data, files = package_files(folder, clips)
    sizes = {f: os.path.getsize(os.path.join(folder, *f.split("/"))) for f in files}
    try:
        r = requests.post(f"{url}/api/upload/begin", json={"data": data, "files": sizes}, headers=auth,
                          timeout=(10, 120))
    except requests.ConnectionError:
        raise SystemExit(f"  Can't reach the website at {url}. Is it running? (python web\\app.py)")
    begin = {"mode": "zip"} if r.status_code == 404 else result(r)   # 404: an older site without direct uploads
    if begin["mode"] == "r2":
        res = upload_direct(folder, begin, sizes, auth, params, data, url)
    else:
        res = upload_zip(folder, auth, params, url, clips)
    state = "public" if res.get("published") else "hidden (make it public in Admin)"
    print(f"  {'added' if res['created'] else 'updated'}: {res['url']}  [{state}]")
    for note in res.get("notes", []):
        print(f"  note: {note}")
    return res


def result(r):
    try:
        res = r.json()
    except ValueError:
        raise SystemExit(f"  Upload failed: HTTP {r.status_code}")
    if not res.get("ok"):
        raise SystemExit(f"  Upload rejected: {res.get('error')}")
    return res


def upload_direct(folder, begin, sizes, auth, params, data, url):
    """Files straight into the bucket with the site's signed links, then the stats to the site."""
    import requests
    put = begin["put"]
    total, done = sum(sizes[f] for f in put), 0
    print(f"  {len(put)} files, {total / 1e6:.1f} MB -> storage bucket")
    for rel, link in put.items():
        path = os.path.join(folder, *rel.split("/"))
        ctype = CONTENT_TYPES.get(os.path.splitext(rel)[1].lower(), "application/octet-stream")
        for attempt in range(3):
            body = Progress(path, done, total)
            try:
                r = requests.put(link, data=body, timeout=(10, 600),
                                 headers={"Content-Type": ctype, "Content-Length": str(sizes[rel])})
                if r.status_code < 300:
                    break
                err = f"HTTP {r.status_code} {r.text[:200]}"
            except requests.RequestException as e:
                err = str(e)
            finally:
                body.f.close()
            print(f"\n  {rel}: upload failed ({err}), " + ("retrying" if attempt < 2 else "giving up"))
        else:
            raise SystemExit("  Upload failed; nothing changed on the site. Run upload.py again.")
        done += sizes[rel]
    print()
    r = requests.post(f"{url}/api/upload/finish", params=params, json={"ticket": begin["ticket"], "data": data},
                      headers=auth, timeout=(10, 300))
    return result(r)


def upload_zip(folder, auth, params, url, clips=True):
    import requests
    fd, tmp = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        n, size = package(folder, tmp, clips)
        print(f"  package: {n} files, {size / 1e6:.1f} MB")
        body = Progress(tmp)
        try:
            r = requests.post(f"{url}/api/upload", params=params, data=body,
                              headers={**auth, "Content-Type": "application/zip", "Content-Length": str(size)},
                              timeout=(10, 600))
        finally:
            body.f.close()
            print()
    except requests.ConnectionError:
        raise SystemExit(f"  Can't reach the website at {url}. Is it running? (python web\\app.py)")
    finally:
        os.remove(tmp)
    return result(r)


def stats_only_folder(demo):
    """Output folder for a demo that isn't rendered: the same name render.py would use, so a later render
    (and upload) of the same demo updates the same match. Copies the demo into CS2's folder like render.py."""
    import render
    name, path = render.prepare_demo(demo)
    folder = os.path.join(OUTPUT, name)
    os.makedirs(folder, exist_ok=True)
    print(f"{name}: analysing the demo (stats only, no clips) ...")
    import scoreboard_html
    scoreboard_html.write(path, os.path.join(folder, "scoreboard.html"))
    return folder


def main():
    ap = argparse.ArgumentParser(description="Upload rendered matches (highlights + stats) to the website.")
    ap.add_argument("matches", nargs="*", help="render folder names in output\\ (or paths)")
    ap.add_argument("--all", action="store_true", help="every rendered match in output\\")
    ap.add_argument("--hidden", dest="public", action="store_false", help="keep the match hidden (default: public)")
    ap.add_argument("--public", dest="public", action="store_true", help=argparse.SUPPRESS)  # the default now
    ap.add_argument("--zip-only", action="store_true", help="only write the package zip next to the folder")
    ap.add_argument("--no-clips", dest="clips", action="store_const", const=False, default=None,
                    help="stats and profile pictures only, no clips (default: UPLOAD_CLIPS in upload.env, on if not set)")
    ap.add_argument("--clips", dest="clips", action="store_const", const=True, help="send clips even if UPLOAD_CLIPS=0")
    ap.add_argument("--demo", action="append", metavar="FILE.dem",
                    help="stats only: analyse this demo and upload its stats without rendering clips "
                         "(doesn't use CS2; render it later and the clips join the same match)")
    a = ap.parse_args()
    for path in a.demo or []:
        upload(stats_only_folder(path), a.public, a.zip_only, clips=a.clips)
    if a.demo and not a.matches and not a.all:
        return
    names = a.matches
    if a.all:
        names = [n for n in sorted(os.listdir(OUTPUT)) if os.path.isfile(os.path.join(OUTPUT, n, "clips.json"))]
    if not names:
        ap.error("give a match folder name from output\\ (or --all)")
    for n in names:
        upload(n if os.path.isabs(n) else os.path.join(OUTPUT, n), a.public, a.zip_only, clips=a.clips)


if __name__ == "__main__":
    main()
