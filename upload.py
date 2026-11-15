"""Upload rendered matches (highlights + stats) to the website.

    python upload.py de_inferno_2026-11-05_02-31-19             upload one match from output\\
    python upload.py de_inferno_2026-11-05_02-31-19 --public    ... and make it public right away
    python upload.py de_inferno_2026-11-05_02-31-19 --zip-only  only make the package (to upload by hand in Admin)
    python upload.py --all                                       every rendered match in output\\

A package is one zip: match_data.json (stats), the clips, thumbnails and profile pictures. The demo is not
uploaded; the website only gets results. Uploading the same match again updates it on the site.

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


def match_data(folder):
    """match_data.json for a render folder; (re)made from the demo if missing or older than the clips."""
    path = os.path.join(folder, "match_data.json")
    cj = os.path.join(folder, "clips.json")
    fresh = os.path.isfile(path) and (not os.path.isfile(cj) or os.path.getmtime(path) >= os.path.getmtime(cj))
    if not fresh or not {"key", "counters"} <= set(json.load(open(path, encoding="utf-8"))):
        demo = os.path.join(CSGO, os.path.basename(folder) + ".dem")
        if not os.path.isfile(demo):
            raise SystemExit(f"{folder}: no match_data.json and the demo {demo} is gone, so stats can't be made")
        print("  making stats (match_data.json) ...")
        import scoreboard_html
        scoreboard_html.write(demo, os.path.join(folder, "scoreboard.html"))
    return json.load(open(path, encoding="utf-8"))


def package(folder, dst):
    """Write the match package zip. Returns (number of files, bytes)."""
    data = match_data(folder)
    files = ["match_data.json"]
    for c in data["clips"]:
        files += [c["file"]] + [c[k] for k in ("thumb", "thumb2") if c.get(k)]
    files += list(data["pics"].values())
    files = [f for f in dict.fromkeys(files) if os.path.isfile(os.path.join(folder, *f.split("/")))]
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_STORED) as z:  # videos and jpgs are already compressed
        for f in files:
            z.write(os.path.join(folder, *f.split("/")), f)
    return len(files), os.path.getsize(dst)


class Progress:
    """File wrapper that prints upload progress while requests reads it."""
    def __init__(self, path):
        self.f, self.size, self.done, self.t = open(path, "rb"), os.path.getsize(path), 0, 0

    def read(self, n=-1):
        b = self.f.read(n if n and n > 0 else 1 << 20)
        self.done += len(b)
        if time.time() - self.t > 0.5 or not b:
            self.t = time.time()
            print(f"\r  uploading {self.done / 1e6:6.1f} / {self.size / 1e6:.1f} MB", end="", flush=True)
        return b

    def __len__(self):
        return self.size


def upload(folder, public=False, zip_only=False, url=None, token=None):
    name = os.path.basename(os.path.normpath(folder))
    if not os.path.isdir(folder):
        raise SystemExit(f"No such render folder: {folder}")
    print(f"{name}:")
    if zip_only:
        dst = os.path.join(OUTPUT, name + ".zip")
        n, size = package(folder, dst)
        print(f"  package: {dst} ({n} files, {size / 1e6:.1f} MB). Upload it in Admin > Upload a package by hand.")
        return dst
    url, token = (url, token) if url else settings()
    if not token:
        raise SystemExit("No upload token: put UPLOAD_TOKEN in upload.env (it's in the website's admin.env).")
    import requests
    fd, tmp = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        n, size = package(folder, tmp)
        print(f"  package: {n} files, {size / 1e6:.1f} MB")
        body = Progress(tmp)
        try:
            r = requests.post(f"{url}/api/upload", params={"publish": "1"} if public else None, data=body,
                              headers={"Authorization": f"Bearer {token}", "Content-Type": "application/zip",
                                       "Content-Length": str(size)}, timeout=(10, 600))
        finally:
            body.f.close()
            print()
    except requests.ConnectionError:
        raise SystemExit(f"  Can't reach the website at {url}. Is it running? (python web\\app.py)")
    finally:
        os.remove(tmp)
    try:
        res = r.json()
    except ValueError:
        raise SystemExit(f"  Upload failed: HTTP {r.status_code}")
    if not res.get("ok"):
        raise SystemExit(f"  Upload rejected: {res.get('error')}")
    state = "public" if res.get("published") else "hidden (make it public in Admin, or use --public)"
    print(f"  {'added' if res['created'] else 'updated'}: {res['url']}  [{state}]")
    for note in res.get("notes", []):
        print(f"  note: {note}")
    return res


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
    ap.add_argument("--public", action="store_true", help="make the match public right away")
    ap.add_argument("--zip-only", action="store_true", help="only write the package zip next to the folder")
    ap.add_argument("--demo", action="append", metavar="FILE.dem",
                    help="stats only: analyse this demo and upload its stats without rendering clips "
                         "(doesn't use CS2; render it later and the clips join the same match)")
    a = ap.parse_args()
    for path in a.demo or []:
        upload(stats_only_folder(path), a.public, a.zip_only)
    if a.demo and not a.matches and not a.all:
        return
    names = a.matches
    if a.all:
        names = [n for n in sorted(os.listdir(OUTPUT)) if os.path.isfile(os.path.join(OUTPUT, n, "clips.json"))]
    if not names:
        ap.error("give a match folder name from output\\ (or --all)")
    for n in names:
        upload(n if os.path.isabs(n) else os.path.join(OUTPUT, n), a.public, a.zip_only)


if __name__ == "__main__":
    main()
