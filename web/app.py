"""cs2render website prototype: public match pages + admin panel, SQLite database, runs on this PC.

The site only stores finished results. Matches are rendered on the gaming PC and uploaded as a package
(upload.py, or render.py --upload); the admin panel can also take a package zip by hand.

    python web\\app.py      -> http://localhost:5000   (admin: http://localhost:5000/admin)

Secrets (admin password, upload token) are in web\\admin.env, created on first start.
"""
import datetime
import functools
import json
import math
import os
import secrets
import shutil
import sys
import tempfile
import time

from markupsafe import Markup  # noqa: E402
from flask import (Flask, abort, flash, jsonify, redirect, render_template, request, send_from_directory, session,
                   url_for)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import db  # noqa: E402
import ingest  # noqa: E402
import matchpage  # noqa: E402
import rating  # noqa: E402
import statsdb  # noqa: E402

ENV = os.path.join(HERE, "admin.env")


def load_secrets():
    """Admin password, session key and upload token from web/admin.env; random values on first start."""
    vals = {}
    if os.path.isfile(ENV):
        for line in open(ENV, encoding="utf-8"):
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                vals[k] = v
    if not all(vals.get(k) for k in ("ADMIN_PASSWORD", "SECRET_KEY", "UPLOAD_TOKEN")):
        vals.setdefault("ADMIN_PASSWORD", secrets.token_urlsafe(12))
        vals.setdefault("SECRET_KEY", secrets.token_hex(32))
        vals.setdefault("UPLOAD_TOKEN", secrets.token_urlsafe(32))
        with open(ENV, "w", encoding="utf-8") as f:
            f.write("# Secrets for the website. Change ADMIN_PASSWORD if you like, then restart.\n"
                    "# UPLOAD_TOKEN lets upload.py on the rendering PC send matches to the site.\n")
            for k in ("ADMIN_PASSWORD", "SECRET_KEY", "UPLOAD_TOKEN"):
                f.write(f"{k}={vals[k]}\n")
    return vals


SECRETS = load_secrets()
app = Flask(__name__, template_folder=os.path.join(HERE, "templates"), static_folder=os.path.join(HERE, "static"))
app.secret_key = SECRETS["SECRET_KEY"]
app.jinja_env.globals.update(cos=math.cos, sin=math.sin)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 ** 3


def con():
    return db.connect()


def is_admin():
    return session.get("admin") is True


def admin_required(f):
    @functools.wraps(f)
    def wrapper(*a, **kw):
        if not is_admin():
            return redirect(url_for("login", next=request.path))
        if request.method == "POST" and request.form.get("csrf") != session.get("csrf"):
            abort(400, "Form expired, go back and try again.")
        return f(*a, **kw)
    return wrapper


@app.context_processor
def inject():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return {"csrf": session["csrf"], "admin": is_admin()}


@app.template_filter("ago")
def ago(ts):
    if not ts:
        return ""
    d = time.time() - ts
    for n, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if d >= n:
            return f"{int(d // n)} {unit} ago"
    return "just now"


@app.template_filter("mb")
def mb(n):
    return f"{(n or 0) / 1e6:,.0f} MB"


def get_match(slug):
    c = con()
    m = c.execute("SELECT * FROM matches WHERE slug = ?", (slug,)).fetchone()
    c.close()
    if m is None or (not m["published"] and not is_admin()):
        abort(404)
    return m


# ---------------------------------------------------------------- public site ----

PERIODS = {"7d": "Bu Hafta", "30d": "30 Gün", "all": "Tüm Zamanlar"}


def people(c):
    return {r["steamid"]: dict(r) for r in c.execute("SELECT * FROM people")}


def highlight_links(c):
    """match_key -> slug of public highlight pages (admins also see hidden ones)."""
    q = "SELECT match_key, slug FROM matches" + ("" if is_admin() else " WHERE published = 1")
    return {r["match_key"]: r["slug"] for r in c.execute(q)}


@app.template_global()
def rbadge(value, size=""):
    """FACEIT-style coloured rating badge (1.00 = average)."""
    if value is None:
        return Markup('<span class="dim">&ndash;</span>')
    cls, label = rating.band(value)
    width = max(4, min(100, value / 2.2 * 100))
    return Markup(f'<span class="rb {cls} {size}" title="{label}">{value:.2f}'
                  f'<u style="width:calc({width:.0f}% - 10px)"></u></span>')


@app.template_filter("pct")
def pct(x, digits=0):
    return "-" if x is None else f"{x * 100:.{digits}f}%"


@app.template_filter("f2")
def f2(x):
    return "-" if x is None else f"{x:.2f}"


@app.template_global()
def map_label(m):
    return statsdb.map_label(m)


TR_MONTHS = ["Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"]


@app.template_filter("day")
def day(iso):
    """2026-11-04 -> 4 Kas 2026 (Turkish month names, like 5v5stats)"""
    try:
        d = datetime.date.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or ""
    return f"{d.day} {TR_MONTHS[d.month - 1]} {d.year}"


@app.template_filter("month_tr")
def month_tr(ym):
    """2026-10 -> Eki"""
    try:
        return TR_MONTHS[int(ym[5:7]) - 1]
    except (TypeError, ValueError, IndexError):
        return ym


@app.template_filter("ago_day")
def ago_day(iso, ref):
    """days before ref, short Turkish: bugün / 4g / 2ay"""
    try:
        n = (datetime.date.fromisoformat(ref) - datetime.date.fromisoformat(iso)).days
    except (TypeError, ValueError):
        return ""
    return "bugün" if n <= 0 else f"{n}g" if n < 30 else f"{n // 30}ay"


@app.template_filter("bin")
def thousands(n):
    """852175 -> 852.175 (Turkish thousands separator)"""
    try:
        return f"{int(round(n)):,}".replace(",", ".")
    except (TypeError, ValueError):
        return n


@app.template_filter("iyelik")
def possessive(name):
    """Turkish genitive suffix by vowel harmony: abraXas -> ABRAXAS'IN, NERO -> NERO'NUN."""
    word = (name or "").strip()
    letters = [ch for ch in word.lower() if ch in "abcçdefgğhıijklmnoöpqrsştuüvwxyz"]  # Latin/Turkish only
    vowels = [ch for ch in letters if ch in "aıoueiöü"]
    v = vowels[-1] if vowels else "e"
    suffix = {"a": "ın", "ı": "ın", "o": "un", "u": "un", "e": "in", "i": "in", "ö": "ün", "ü": "ün"}[v]
    if letters and letters[-1] in "aıoueiöü":
        suffix = "n" + suffix
    return f"{word}'{suffix}"


AWARDS = [  # (title, metric, unit, format, description) - titles as on 5v5stats
    ("Hasar Ustası", "ADR", "ADR", "{:.0f}", "Round başına en yüksek hasar"),
    ("En Çok Frag", "KPR", "KPR", "{:.2f}", "Round başına en çok kill"),
    ("Keskin Nişancı", "ACC_PCT", "% isabet", "pct1", "En yüksek isabet oranı"),
    ("Kafa Avcısı", "HS_PCT", "% kafa", "pct", "En yüksek kafa atışı oranı"),
    ("Clutch Kralı", "CLUTCH_WR", "% clutch", "pct", "En iyi clutch kazanma oranı"),
    ("Giriş Kralı", "ENTRY_WR", "% giriş", "pct", "En iyi açılış düellosu oranı"),
    ("Util Ustası", "UDR_PER_ROUND", "util/r", "{:.1f}", "Round başına en çok util hasarı"),
    ("Klip Adamı", "MK_per_match", "3K+/M", "{:.2f}", "Maç başına en çok multi-kill round"),
]


def awards(board):
    q = [r for r in board if r["qualified"]]
    out = []
    for title, key, unit, fmt, desc in AWARDS:
        if not q:
            break
        w = max(q, key=lambda r: r.get(key) or 0)
        v = w.get(key) or 0
        shown = f"{v * 100:.1f}" if fmt == "pct1" else f"{v * 100:.0f}" if fmt == "pct" else fmt.format(v)
        out.append({"title": title, "unit": unit, "value": shown, "desc": desc, "sid": w["steamid64"],
                    "name": w["name"]})
    return out


@app.route("/")
def stats_home():
    period = request.args.get("p", "all")
    if period not in PERIODS:
        period = "all"
    c = con()
    board = statsdb.leaderboard(c, period)
    ov = statsdb.overview(c, period)
    synced = statsdb.get_meta(c, "synced_at")
    win = statsdb.window(c, period) if period != "all" else None
    sync_date = statsdb.get_meta(c, "sync_date")
    newer = c.execute("SELECT COUNT(*) FROM games WHERE source = 'demo' AND date > ?",
                      (sync_date or "",)).fetchone()[0]
    cb = statsdb.comeback(c, period)
    ctx = dict(board=board, period=period, periods=PERIODS, ov=ov, people=people(c), win=win, newer=newer,
               awards=awards(board), comeback=cb, months=statsdb.activity_by_month(c),
               weeks=statsdb.weekly_counts(c), seen=statsdb.last_seen(c), ref=statsdb.latest_date(c),
               month30=statsdb.overview(c, "30d")["matches"],
               synced=(day(time.strftime("%Y-%m-%d", time.localtime(synced / 1000))) + " "
                       + time.strftime("%H:%M", time.localtime(synced / 1000))) if synced else None)
    c.close()
    return render_template("stats.html", **ctx)


@app.route("/highlights")
def home():
    c = con()
    rows = c.execute("SELECT * FROM matches WHERE published = 1 ORDER BY date DESC, id DESC").fetchall()
    c.close()
    return render_template("home.html", matches=rows)


@app.route("/players/<steamid>")
def player_page(steamid):
    c = con()
    board = statsdb.leaderboard(c, "all")
    me = next((r for r in board if r["steamid64"] == steamid), None)
    if me is None:
        c.close()
        abort(404)
    hist = c.execute("""SELECT gp.*, g.id AS gid, g.source, g.ext_id, g.match_key, g.map, g.date, g.rounds, g.team_a,
                        g.score_a, g.team_b, g.score_b FROM game_players gp JOIN games g ON g.id = gp.game_id
                        WHERE gp.steamid = ? ORDER BY g.date DESC, COALESCE(g.ext_id, 0) DESC, g.id DESC""",
                     (steamid,)).fetchall()
    base = c.execute("SELECT map_stats FROM baseline WHERE steamid = ?", (steamid,)).fetchone()
    maps = {m["map"]: dict(m) for m in (json.loads(base["map_stats"]) if base else [])}
    for h in hist:  # demo games aren't in the 5v5stats map stats yet
        if h["source"] == "demo":
            m = maps.setdefault(h["map"], {"map": h["map"], "matches": 0, "rounds": 0, "kills": 0, "deaths": 0,
                                           "damage": 0, "wins": 0, "losses": 0})
            m["matches"] += 1
            m["rounds"] += h["rounds"] or 0
            m["kills"] += h["kills"]
            m["deaths"] += h["deaths"]
            m["damage"] += (h["adr"] or 0) * (h["rounds"] or 0)
            m["wins"] += h["won"]
            m["losses"] += 1 - h["won"]
    for m in maps.values():
        m["adr"] = m["damage"] / m["rounds"] if m["rounds"] else 0
        m["kd"] = m["kills"] / m["deaths"] if m["deaths"] else 0
    clips = c.execute("""SELECT clips.*, matches.slug, matches.map AS mmap, matches.date AS mdate FROM clips
                         JOIN matches ON matches.id = clips.match_id WHERE clips.steamid = ?""" +
                      ("" if is_admin() else " AND matches.published = 1") + " ORDER BY clips.score DESC",
                      (steamid,)).fetchall()
    ppl, links = people(c), highlight_links(c)
    rel = statsdb.relations(c, steamid)
    ref = statsdb.latest_date(c)
    c.close()
    qualified = [r for r in board if r["qualified"]]

    def pctl(key, invert=False):  # percentile among ranked players, 0..1
        vals = sorted(r[key] for r in qualified)
        if not vals:
            return 0.5
        below = sum(1 for v in vals if v < me[key])
        p = below / max(1, len(vals) - 1)
        return 1 - p if invert else p
    radar = [("Fragging", pctl("KPR")), ("Survival", pctl("DPR", invert=True)), ("Damage", pctl("ADR")),
             ("Aim", pctl("ACC_PCT")), ("Clutch", pctl("CLUTCH_WR")), ("Entry", pctl("ENTRY_WR"))]
    maplist = sorted(maps.values(), key=lambda m: -m["matches"])
    enough = [m for m in maplist if m["matches"] >= 5]
    best = max(enough, key=lambda m: m["wins"] / m["matches"], default=None)
    worst = min(enough, key=lambda m: m["wins"] / m["matches"], default=None)
    return render_template("player.html", p=me, hist=hist, maps=maplist, clips=clips, person=ppl.get(steamid, {}),
                           links=links, n_ranked=len(qualified), rel=rel, radar=radar, ref=ref, best=best,
                           worst=worst, people=ppl, form=list(hist[:10])[::-1])


@app.route("/balancer")
def balancer():
    """Takım Kur: pick 10 players, get the most even 5v5 split (all 252 splits are checked in the browser)."""
    c = con()
    pool = statsdb.balancer_pool(c)
    maps = [r["map"] for r in c.execute("SELECT map, COUNT(*) n FROM games GROUP BY map HAVING n >= 5 ORDER BY n DESC")]
    c.close()
    return render_template("balancer.html", pool=pool, maps=[(m, statsdb.map_label(m)) for m in maps])


@app.template_global()
def games_url(map=None, player=None, other=None, page=1):
    """Link to the matches list with these filters (empty ones left out)."""
    args = {k: v for k, v in (("map", map), ("player", player), ("with", other)) if v}
    if page and page > 1:
        args["page"] = page
    return url_for("games_page", **args)


@app.route("/games")
def games_page():
    """All matches; filter by map and/or player (?player=<steamid>), and optionally a second player
    (?with=<steamid>) for the matches they played together or against each other."""
    mapf = request.args.get("map", "")
    pid = request.args.get("player", "").strip()
    wid = request.args.get("with", "").strip() if pid else ""
    page = max(1, int(request.args.get("page", 1) or 1))
    per = 40
    c = con()
    conds, args = [], []
    if mapf:
        conds.append("g.map = ?")
        args.append(mapf)
    join = ""
    if pid:
        join = "JOIN game_players me ON me.game_id = g.id AND me.steamid = ?"
        args.insert(0, pid)
    if wid:
        join += " JOIN game_players other ON other.game_id = g.id AND other.steamid = ?"
        args.insert(1, wid)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    cols = "g.*" + (", me.side AS my_side, me.won AS my_won, me.kills AS my_k, me.deaths AS my_d, me.assists AS my_a, "
                    "me.adr AS my_adr, me.hs_pct AS my_hs" if pid else "") + (", other.side AS w_side" if wid else "")
    base = f"FROM games g {join} {where}"
    total = c.execute(f"SELECT COUNT(*) {base}", args).fetchone()[0]
    rows = c.execute(f"""SELECT {cols} {base} ORDER BY g.date DESC, COALESCE(g.ext_id, 0) DESC, g.id DESC
                         LIMIT ? OFFSET ?""", args + [per, (page - 1) * per]).fetchall()
    summary = None
    if pid:
        s = c.execute(f"SELECT COUNT(*), SUM(me.won), SUM(me.kills), SUM(me.deaths) {base}", args).fetchone()
        summary = {"n": s[0], "w": s[1] or 0, "k": s[2] or 0, "d": s[3] or 0}
        if wid:
            summary["same"] = c.execute(f"SELECT COUNT(*), SUM(me.won) {base} AND me.side = other.side"
                                        if where else f"SELECT COUNT(*), SUM(me.won) {base} WHERE me.side = other.side",
                                        args).fetchone()
    maps = c.execute("SELECT map, COUNT(*) AS n FROM games GROUP BY map ORDER BY n DESC").fetchall()
    players = c.execute("""SELECT p.steamid, p.name, COUNT(*) AS n FROM game_players gp JOIN people p ON p.steamid = gp.steamid
                           GROUP BY gp.steamid ORDER BY n DESC""").fetchall()
    names = {r["steamid"]: r["name"] for r in players}
    links = highlight_links(c)
    c.close()
    return render_template("games.html", games=rows, maps=maps, mapf=mapf, page=page, pid=pid, wid=wid,
                           pname=names.get(pid), wname=names.get(wid), players=players, summary=summary,
                           pages=max(1, (total + per - 1) // per), total=total, links=links)


def player_tiers(c):
    """{steamid: {"tier": 1-10 or None, "points": 1.00-scale score}} for the EDO card's level badge."""
    return {r["steamid64"]: {"tier": r["rating"], "points": r["points"]} for r in statsdb.leaderboard(c, "all")}


@app.route("/g/<int:gid>")
def game_page(gid):
    """Any match: the highlight page if we have one, otherwise the same FACEIT-style page from its stats."""
    c = con()
    g = c.execute("SELECT * FROM games WHERE id = ?", (gid,)).fetchone()
    if g is None:
        c.close()
        abort(404)
    slug = highlight_links(c).get(g["match_key"]) if g["match_key"] else None
    if slug:
        c.close()
        return redirect(url_for("match_page", slug=slug))
    data = statsdb.game_data(c, gid)
    data["tiers"] = player_tiers(c)
    c.close()
    label = f"Maç #{g['ext_id']}" if g["ext_id"] else "Demo maçı"
    note = (f'<style>.srcnote{{color:#8d8d8d;font-size:12px;margin-top:6px}}</style>'
            f'<script>document.addEventListener("DOMContentLoaded",()=>{{const m=document.querySelector(".wrap");'
            f'm.insertAdjacentHTML("beforeend",`<div class="srcnote">{label}. 5v5stats maç başına sadece K/Ö/A, '
            f'ADR ve kafa% verir; puan, swing ve KAST için demo gerekir.</div>`)}})</script>') if g["ext_id"] else ""
    page = matchpage.render(data, back=url_for("games_page"), back_label="Tüm maçlar", extra_head=note)
    return page.replace("<title>", f"<title>{label} - ", 1)


@app.route("/m/<slug>")
def match_page(slug):
    m = get_match(slug)
    data = json.loads(m["data_json"])
    c = con()
    data["tiers"] = player_tiers(c)
    c.close()
    banner = ""
    if is_admin():
        state = "Yayında" if m["published"] else "Yayında değil - bu sayfayı sadece sen (yönetici) görüyorsun"
        banner = (f'<style>.adminbar{{background:#3a1a0a;border-bottom:1px solid #ff5500;color:#ffb38a;'
                  f'padding:8px 16px;font:13px "Segoe UI",sans-serif}}.adminbar a{{color:#fff}}</style>'
                  f'<script>document.addEventListener("DOMContentLoaded",()=>document.body.insertAdjacentHTML('
                  f'"afterbegin",`<div class="adminbar">{state} &middot; <a href="/admin/match/{m["id"]}">'
                  f'Bu maçı yönet</a></div>`))</script>')
    page = matchpage.render(data, base=f"/media/{slug}/", back="/highlights", extra_head=banner, back_label="Klipler")
    if m["title"]:
        page = page.replace("<title>", f"<title>{matchpage.esc(m['title'])} - ", 1)
    return page


@app.route("/media/<slug>/<path:path>")
def media(slug, path):
    get_match(slug)  # files of hidden matches are hidden too
    return send_from_directory(ingest.media_dir(slug), path, conditional=True, max_age=3600)


# ---------------------------------------------------------------- uploads ----

def receive(stream_or_file, via):
    """Save an uploaded package to a temp file and import it. Returns (match, created, notes)."""
    fd, tmp = tempfile.mkstemp(suffix=".zip", dir=db.DATA)
    try:
        with os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(stream_or_file, out, 1 << 20)
        size = os.path.getsize(tmp)
        c = con()
        try:
            return ingest.ingest(c, tmp, via)
        except ingest.BadPackage as e:
            ingest.log_failure(c, via, str(e), size)
            raise
        finally:
            c.close()
    finally:
        os.remove(tmp)


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """For upload.py: body = the package zip, header Authorization: Bearer <UPLOAD_TOKEN>."""
    auth = request.headers.get("Authorization", "")
    if not (auth.startswith("Bearer ") and secrets.compare_digest(auth[7:], SECRETS["UPLOAD_TOKEN"])):
        return jsonify(ok=False, error="wrong or missing upload token"), 401
    try:
        m, created, notes = receive(request.stream, "upload.py")
    except ingest.BadPackage as e:
        return jsonify(ok=False, error=str(e)), 400
    if request.args.get("publish") == "1" and not m["published"]:
        c = con()
        c.execute("UPDATE matches SET published = 1 WHERE id = ?", (m["id"],))
        c.commit()
        c.close()
    base = request.host_url.rstrip("/")
    return jsonify(ok=True, created=created, notes=notes, match=m["map"], key=m["match_key"],
                   published=bool(m["published"]) or request.args.get("publish") == "1",
                   url=f"{base}/m/{m['slug']}", admin_url=f"{base}/admin/match/{m['id']}")


@app.route("/admin/upload", methods=["POST"])
@admin_required
def admin_upload():
    f = request.files.get("package")
    if not f or not f.filename:
        flash("Choose a match package (.zip) first.")
        return redirect(url_for("admin"))
    try:
        m, created, notes = receive(f.stream, "admin")
    except ingest.BadPackage as e:
        flash(f"Upload rejected: {e}")
        return redirect(url_for("admin"))
    flash(("New match added" if created else "Match updated") + f": {m['map']} {m['score1']}:{m['score2']}, "
          f"{m['clip_count']} clips." + ("" if m["published"] else " It's hidden until you make it public.")
          + (f" ({'; '.join(notes)})" if notes else ""))
    return redirect(url_for("admin_match", mid=m["id"]))


# ---------------------------------------------------------------- admin ----

@app.route("/admin/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if secrets.compare_digest(request.form.get("password", ""), SECRETS["ADMIN_PASSWORD"]):
            session.clear()
            session["admin"] = True
            session["csrf"] = secrets.token_hex(16)
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("admin"))
        flash("Wrong password.")
    return render_template("login.html")


@app.route("/admin/logout", methods=["POST"])
@admin_required
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/admin")
@admin_required
def admin():
    c = con()
    matches = c.execute("SELECT * FROM matches ORDER BY updated_at DESC").fetchall()
    uploads = c.execute("SELECT uploads.*, matches.map, matches.score1, matches.score2, matches.id AS mid "
                        "FROM uploads LEFT JOIN matches ON matches.id = uploads.match_id "
                        "ORDER BY uploads.id DESC LIMIT 12").fetchall()
    total = c.execute("SELECT COALESCE(SUM(size_bytes), 0) FROM matches").fetchone()[0]
    c.close()
    return render_template("admin.html", matches=matches, uploads=uploads, total=total,
                           server=request.host_url.rstrip("/"))


@app.route("/admin/match/<int:mid>")
@admin_required
def admin_match(mid):
    c = con()
    m = c.execute("SELECT * FROM matches WHERE id = ?", (mid,)).fetchone()
    if m is None:
        c.close()
        abort(404)
    players = c.execute("SELECT * FROM players WHERE match_id = ? ORDER BY team, rating DESC", (mid,)).fetchall()
    clips = c.execute("SELECT * FROM clips WHERE match_id = ? ORDER BY idx", (mid,)).fetchall()
    c.close()
    return render_template("admin_match.html", m=m, players=players, clips=clips)


@app.route("/admin/match/<int:mid>/edit", methods=["POST"])
@admin_required
def edit_match(mid):
    c = con()
    title = request.form.get("title", "").strip()[:120] or None
    published = 1 if request.form.get("published") == "1" else 0
    c.execute("UPDATE matches SET title = ?, published = ? WHERE id = ?", (title, published, mid))
    c.commit()
    c.close()
    flash("Saved." + (" The match is public on the site." if published else " The match is hidden."))
    return redirect(url_for("admin_match", mid=mid))


@app.route("/admin/match/<int:mid>/delete", methods=["POST"])
@admin_required
def delete_match(mid):
    c = con()
    m = c.execute("SELECT * FROM matches WHERE id = ?", (mid,)).fetchone()
    if m is None:
        c.close()
        abort(404)
    c.execute("DELETE FROM matches WHERE id = ?", (mid,))
    c.commit()
    c.close()
    shutil.rmtree(ingest.media_dir(m["slug"]), ignore_errors=True)
    flash("Match deleted from the site. Your files on the rendering PC are untouched; upload it again to bring it back.")
    return redirect(url_for("admin"))


def main():
    db.init()
    print("Website: http://localhost:5000   Admin: http://localhost:5000/admin  (secrets in web\\admin.env)")
    sys.stdout.flush()
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
