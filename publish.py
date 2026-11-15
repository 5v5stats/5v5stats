"""Publish rendered matches to a website.

Targets (TARGET in publish.env):
  local  copies into a site folder on this PC (default C:\\cs2render\\site), served by serve_site.py at
         http://localhost:8080 to simulate the real website. Nothing leaves this PC.
  r2     uploads to a Cloudflare R2 bucket with public access (the real website).

    python publish.py de_inferno_2026-11-05_02-31-19            publish one match (+ refresh the match list)
    python publish.py de_inferno_2026-11-05_02-31-19 --dry-run  show what would be published, change nothing
    python publish.py --list                                    matches published so far
    python publish.py --unpublish de_inferno_2026-11-05_02-31-19  remove a match from the website
    add --target local|r2 to override publish.env

The website mirrors the output folder: index.html (all published matches) at the top, and one folder per
match with its page, clips, thumbnails and profile pictures. Not published: the joined reel (the page has
"Play all" instead), clips.json (has local paths), scoreboard.txt, raw/temporary files.
"""
import argparse
import hashlib
import html
import json
import os
import shutil
import sys
import urllib.parse

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.abspath(__file__))
OUTPUT = os.path.join(ROOT, "output")
ENV_FILE = os.path.join(ROOT, "publish.env")

TYPES = {".html": "text/html; charset=utf-8", ".json": "application/json", ".mp4": "video/mp4",
         ".jpg": "image/jpeg", ".png": "image/png", ".css": "text/css", ".js": "text/javascript"}
SHORT_CACHE = "public, max-age=60"          # pages and lists change when you publish again
LONG_CACHE = "public, max-age=604800"       # clips, thumbnails, pictures


def load_env():
    env = {"TARGET": "local", "LOCAL_SITE": os.path.join(ROOT, "site")}
    if os.path.isfile(ENV_FILE):
        for line in open(ENV_FILE, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if v.strip():
                    env[k.strip()] = v.strip().strip('"')
    return env


def sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class LocalTarget:
    """A folder on this PC that serve_site.py serves as the website."""
    name = "local"

    def __init__(self, env):
        self.root = env["LOCAL_SITE"]
        self.url = env.get("LOCAL_URL", "http://localhost:8080")
        self.record = os.path.join(OUTPUT, "published_local.json")

    def put(self, path, key, ctype, cache, digest):
        dst = os.path.join(self.root, *key.split("/"))
        if os.path.isfile(dst) and os.path.getsize(dst) == os.path.getsize(path) and sha1(dst) == digest:
            return False
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(path, dst + ".tmp")
        os.replace(dst + ".tmp", dst)
        return True

    def delete_prefix(self, prefix):
        d = os.path.join(self.root, prefix.rstrip("/"))
        n = sum(len(fs) for _, _, fs in os.walk(d)) if os.path.isdir(d) else 0
        if os.path.isdir(d):
            shutil.rmtree(d)
        return n

    def check(self):
        return []


class R2Target:
    """Cloudflare R2 bucket with public access (the real website)."""
    name = "r2"
    NEEDED = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")

    def __init__(self, env):
        self.env = env
        self.url = env.get("PUBLIC_URL", "")
        self.bucket = env.get("R2_BUCKET", "")
        self.record = os.path.join(OUTPUT, "published_r2.json")
        self._s3 = None

    def check(self):
        return [k for k in self.NEEDED if not self.env.get(k)]

    @property
    def s3(self):
        if self._s3 is None:
            import boto3
            from botocore.config import Config
            e = self.env
            self._s3 = boto3.client("s3", endpoint_url=f"https://{e['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
                                    aws_access_key_id=e["R2_ACCESS_KEY_ID"],
                                    aws_secret_access_key=e["R2_SECRET_ACCESS_KEY"],
                                    region_name="auto", config=Config(retries={"max_attempts": 5}))
        return self._s3

    def put(self, path, key, ctype, cache, digest):
        try:
            if self.s3.head_object(Bucket=self.bucket, Key=key).get("Metadata", {}).get("sha1") == digest:
                return False
        except Exception:
            pass
        self.s3.upload_file(path, self.bucket, key, ExtraArgs={"ContentType": ctype, "CacheControl": cache,
                                                                 "Metadata": {"sha1": digest}})
        return True

    def delete_prefix(self, prefix):
        keys = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            keys += [{"Key": o["Key"]} for o in page.get("Contents", [])]
        for i in range(0, len(keys), 1000):
            self.s3.delete_objects(Bucket=self.bucket, Delete={"Objects": keys[i:i + 1000]})
        return len(keys)


def publish_file(target, path, key):
    ext = os.path.splitext(path)[1].lower()
    ctype = TYPES.get(ext, "application/octet-stream")
    cache = SHORT_CACHE if ext in (".html", ".json") else LONG_CACHE
    return target.put(path, key, ctype, cache, sha1(path))


def match_files(folder):
    """(local path, key relative to the site root) for everything a match page needs."""
    name = os.path.basename(folder)
    files = [os.path.join(folder, "scoreboard.html"), os.path.join(folder, "match.json")]
    cj = os.path.join(folder, "clips.json")
    if os.path.isfile(cj):
        for c in filter(None, json.load(open(cj, encoding="utf-8"))):
            p = os.path.join(folder, os.path.basename(c.get("file", "")))
            if c.get("file") and os.path.isfile(p):
                files.append(p)
    for sub in ("thumbs", "avatars"):
        d = os.path.join(folder, sub)
        if os.path.isdir(d):
            files += [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith(".jpg")]
    return [(f, f"{name}/" + os.path.relpath(f, folder).replace("\\", "/")) for f in files if os.path.isfile(f)]


INDEX_CSS = """
:root { --bg:#0d0d0d; --panel:#161616; --line:#262626; --text:#f2f2f2; --muted:#8d8d8d; --accent:#ff5500; --win:#32d35a; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:14px/1.4 "Segoe UI", system-ui, -apple-system, Roboto, sans-serif; }
.wrap { max-width:1180px; margin:0 auto; padding:24px 16px 48px; }
h1 { font-size:24px; margin:0 0 4px; } .sub { color:var(--muted); margin-bottom:22px; }
.grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(min(340px, 100%), 1fr)); gap:14px; }
.m { display:block; text-decoration:none; color:inherit; border:1px solid var(--line); border-radius:10px; overflow:hidden;
     background:var(--panel); transition:border-color .15s; }
.m:hover { border-color:var(--accent); }
.m .img { position:relative; aspect-ratio:16/7; background:#1d1d1d center/cover; }
.m .img::after { content:""; position:absolute; inset:0; background:linear-gradient(180deg, rgba(0,0,0,.1), rgba(0,0,0,.75)); }
.m .img .t { position:absolute; left:12px; right:12px; bottom:10px; z-index:1; display:flex; justify-content:space-between;
             align-items:flex-end; gap:8px; }
.m .map { font-size:20px; font-weight:800; } .m .date { color:#cfcfcf; font-size:12px; }
.m .body { padding:12px; display:flex; align-items:center; gap:10px; }
.m .score { font-weight:800; font-size:16px; font-variant-numeric:tabular-nums; flex:1; min-width:0; }
.m .score .w { color:var(--win); }
.m .mvp { display:flex; align-items:center; gap:8px; color:var(--muted); font-size:12px; white-space:nowrap; }
.m .mvp b { color:var(--text); }
.m .mvp img, .m .mvp .av { width:28px; height:28px; border-radius:50%; object-fit:cover; background:#333; }
.empty { color:var(--muted); }
"""


def build_index(names):
    metas = []
    for n in names:
        mj = os.path.join(OUTPUT, n, "match.json")
        if os.path.isfile(mj):
            metas.append((n, json.load(open(mj, encoding="utf-8"))))
    metas.sort(key=lambda x: (x[1].get("date", ""), x[0]), reverse=True)
    cards = []
    for n, m in metas:
        base = urllib.parse.quote(n)
        thumb = f' style="background-image:url(\'{base}/{m["thumb"]}\')"' if m.get("thumb") else ""
        t = m.get("teams", [])
        score = ""
        if len(t) == 2:
            score = (f'<span class="w">{t[0]["score"]}</span> : {t[1]["score"]} &nbsp;'
                     f'<span style="font-weight:600;color:#bdbdbd">{html.escape(t[0]["name"])} vs {html.escape(t[1]["name"])}</span>')
        mv = m.get("mvp", {})
        pic = f'<img src="{base}/{mv["avatar"]}" alt="">' if mv.get("avatar") else '<span class="av"></span>'
        cards.append(f"""<a class="m" href="{base}/{m.get("page", "scoreboard.html")}">
  <div class="img"{thumb}><div class="t"><span class="map">{html.escape(m.get("map", ""))}</span>
    <span class="date">{html.escape(m.get("date", ""))} &middot; {m.get("clips", 0)} highlights</span></div></div>
  <div class="body"><span class="score">{score}</span>
    <span class="mvp">{pic}<span>MVP <b>{html.escape(mv.get("name", ""))}</b> {mv.get("rating", 0):.2f}</span></span></div>
</a>""")
    grid = f'<div class="grid">{"".join(cards)}</div>' if cards else '<p class="empty">No matches published yet.</p>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>CS2 highlights</title><style>{INDEX_CSS}</style></head>
<body><div class="wrap"><h1>CS2 highlights</h1><div class="sub">{len(cards)} match{"es" if len(cards) != 1 else ""}</div>
{grid}</div></body></html>"""


def main():
    ap = argparse.ArgumentParser(description="Publish rendered matches to the website.")
    ap.add_argument("matches", nargs="*", help="match folder names in output/ (or paths to them)")
    ap.add_argument("--target", choices=["local", "r2"], help="override TARGET from publish.env")
    ap.add_argument("--dry-run", action="store_true", help="show what would be published; change nothing")
    ap.add_argument("--unpublish", action="store_true", help="remove the given matches from the website")
    ap.add_argument("--list", action="store_true", help="list published matches")
    a = ap.parse_args()

    env = load_env()
    target = (R2Target if (a.target or env["TARGET"]) == "r2" else LocalTarget)(env)
    published = json.load(open(target.record, encoding="utf-8")) if os.path.isfile(target.record) else []
    url = target.url.rstrip("/")

    if a.list:
        print(f"target: {target.name}" + (f" ({target.root})" if target.name == "local" else ""))
        for n in published:
            print(f"  {n}   {url + '/' + urllib.parse.quote(n) + '/scoreboard.html' if url else ''}")
        return
    if not a.matches:
        ap.error("give at least one match folder (see output/)")
    names = [os.path.basename(os.path.normpath(m)) for m in a.matches]
    for n in names:
        if not a.unpublish and not os.path.isfile(os.path.join(OUTPUT, n, "match.json")):
            sys.exit(f"{n}: no match.json in output/{n} (render it first, or run scoreboard_html.py for it)")
    missing = target.check()
    if missing and not a.dry_run:
        sys.exit(f"publish.env is missing: {', '.join(missing)}. Fill it in (see the comments in that file).")
    print(f"target: {target.name}" + (f" ({target.root})" if target.name == "local" else f" (bucket {target.bucket})"))

    if a.unpublish:
        for n in names:
            if a.dry_run:
                print(f"would remove everything under {n}/")
            else:
                print(f"removed {n} ({target.delete_prefix(n + '/')} files)")
            published = [p for p in published if p != n]
    else:
        total = 0
        for n in names:
            files = match_files(os.path.join(OUTPUT, n))
            print(f"{n}: {len(files)} files")
            for path, key in files:
                size = os.path.getsize(path)
                if a.dry_run:
                    state = "would publish"
                else:
                    state = "published" if publish_file(target, path, key) else "unchanged"
                if state != "unchanged":
                    total += size
                print(f"  {state:13} {size / 1e6:7.1f} MB  {key}")
            if n not in published:
                published.append(n)
        print(f"{'would publish' if a.dry_run else 'published'} {total / 1e6:.1f} MB")

    if a.dry_run:
        return
    index = os.path.join(OUTPUT, "index.html")  # also a local preview of the match list
    with open(index, "w", encoding="utf-8") as f:
        f.write(build_index(published))
    publish_file(target, index, "index.html")
    os.makedirs(os.path.dirname(target.record), exist_ok=True)
    with open(target.record, "w", encoding="utf-8") as f:
        json.dump(published, f, indent=1)
    if url:
        print(f"match list: {url}/index.html")
        for n in names:
            if not a.unpublish:
                print(f"match page: {url}/{urllib.parse.quote(n)}/scoreboard.html")


if __name__ == "__main__":
    main()
