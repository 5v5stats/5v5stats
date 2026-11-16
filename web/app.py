"""cs2render website prototype: public match pages + admin panel, SQLite database, runs on this PC.

The site only stores finished results. Matches are rendered on the gaming PC and uploaded as a package
(upload.py, or render.py --upload); the admin panel can also take a package zip by hand.

    python web\\app.py      -> http://localhost:5000   (admin: http://localhost:5000/admin)

Settings and secrets (admin password, upload token, optional DATABASE_URL and R2 bucket) are in
web\\admin.env, created on first start; on Vercel they are environment variables (see config.py).
"""
import datetime
import functools
import json
import math
import os
import re
import secrets
import shutil
import sys
import tempfile
import time

from markupsafe import Markup  # noqa: E402
from flask import (Flask, abort, flash, jsonify, redirect, render_template, render_template_string, request, send_from_directory, session,
                   url_for)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402
import db  # noqa: E402
import ingest  # noqa: E402
import matchpage  # noqa: E402
import rating  # noqa: E402
import statsdb  # noqa: E402
import storage  # noqa: E402

from itsdangerous import BadSignature, URLSafeTimedSerializer  # noqa: E402
from werkzeug.security import check_password_hash, generate_password_hash  # noqa: E402

SECRETS = config.ensure_secrets()
app = Flask(__name__, template_folder=os.path.join(HERE, "templates"), static_folder=os.path.join(HERE, "static"))
app.secret_key = SECRETS["SECRET_KEY"]
TR_TZ = datetime.timezone(datetime.timedelta(hours=3))  # Turkey (no DST); Vercel runs on UTC
DEFAULT_PERIOD = "30d"  # the site opens on the last 30 days; ?p=all for all time
app.jinja_env.globals.update(map_img=lambda m: map_img(m), map_thumb=lambda m, cls="mthumb": map_thumb(m, cls),
                             default_period=DEFAULT_PERIOD, cos=math.cos, sin=math.sin, media_url=storage.url,
                             level_ring=lambda t, size=28: Markup(matchpage.level_ring(t, size)))
TICKETS = URLSafeTimedSerializer(SECRETS["SECRET_KEY"], salt="upload")
db.init()  # tables (CREATE ... IF NOT EXISTS); cheap, and a fresh hosted database needs it before the first page
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

PERIODS = {"7d": "Bu Hafta", "30d": "30 Gün", "90d": "3 Ay", "all": "Tüm Zamanlar"}


def people(c):
    return {r["steamid"]: dict(r) for r in c.execute("SELECT * FROM people")}


def top_clips(c, period, n=5, since=None):
    """The period's best highlight clips by points (public matches; admins also see hidden ones).
    since: a start date instead of the period's window."""
    cond, args = [], []
    if since is None and period != "all":
        since, _ = statsdb.window(c, period)
    if since:
        cond.append("m.date >= ?")
        args.append(since)
    if not is_admin():
        cond.append("m.published = 1")
    where = ("WHERE " + " AND ".join(cond)) if cond else ""
    return c.execute(f"""SELECT cl.*, m.slug, m.media_rev, m.map AS mmap, m.date AS mdate FROM clips cl
                         JOIN matches m ON m.id = cl.match_id {where} ORDER BY cl.score DESC, m.date DESC LIMIT ?""",
                     args + [n]).fetchall()


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


# map pictures: CS2 screenshots from the Valve Developer Community wiki (web/static/maps/de_<map>.jpg, 960x540)
MAP_IMAGES = {os.path.splitext(f)[0] for f in os.listdir(os.path.join(HERE, "static", "maps")) if f.endswith(".jpg")}     if os.path.isdir(os.path.join(HERE, "static", "maps")) else set()


def map_img(m):
    """URL of a map's picture ("de_dust2", "Dust2", "Dust II" all work), or "" if we have none."""
    k = str(m or "").lower().replace(" ", "").replace("dustii", "dust2")
    k = k if k.startswith("de_") else "de_" + k
    return url_for("static", filename=f"maps/{k}.jpg") if k in MAP_IMAGES else ""


def map_thumb(m, cls="mthumb"):
    u = map_img(m)
    return Markup(f'<img class="{cls}" src="{u}" alt="" loading="lazy">') if u else Markup(f'<span class="{cls} none"></span>')


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


def _n(x):
    return thousands(x or 0)


AWARDS = [  # (slug, title, metric, unit, format, description, detail) - titles as on 5v5stats
    ("hasar-ustasi", "Hasar Ustası", "ADR", "ADR", "{:.0f}", "Round başına en yüksek hasar",
     lambda r: f"{_n(r['total_damage'])} hasar"),
    ("en-cok-frag", "En Çok Frag", "KPR", "KPR", "{:.2f}", "Round başına en çok kill",
     lambda r: f"{_n(r['total_kills'])} kill"),
    ("keskin-nisanci", "Keskin Nişancı", "ACC_PCT", "% isabet", "pct1", "En yüksek isabet oranı",
     lambda r: f"{_n(r['total_shots_on_tgt'])} / {_n(r['total_shots_fired'])} mermi isabet"),
    ("kafa-avcisi", "Kafa Avcısı", "HS_PCT", "% kafa", "pct", "En yüksek kafa atışı oranı",
     lambda r: f"{_n(r['total_hs_kills'])} / {_n(r['total_kills'])} kill kafadan"),
    ("clutch-krali", "Clutch Kralı", "CLUTCH_WR", "% clutch", "pct", "En iyi clutch kazanma oranı",
     lambda r: f"1v1 {_n(r['total_v1_wins'])}/{_n(r['total_v1_count'])} · 1v2 {_n(r['total_v2_wins'])}/{_n(r['total_v2_count'])}"),
    ("giris-krali", "Giriş Kralı", "ENTRY_WR", "% giriş", "pct", "En iyi açılış düellosu oranı",
     lambda r: f"{_n(r['total_entry_wins'])} / {_n(r['total_entry_count'])} açılış düellosu"),
    ("util-ustasi", "Util Ustası", "UDR_PER_ROUND", "util/r", "{:.1f}", "Round başına en çok util hasarı",
     lambda r: f"{_n(r['total_util_damage'])} util hasarı"),
    ("klip-adami", "Klip Adamı", "MK_per_match", "3K+/M", "{:.2f}", "Maç başına en çok multi-kill round",
     lambda r: f"{_n(r['total_multi'])} multi-kill round"),
]


def award_value(fmt, v):
    v = v or 0
    return f"{v * 100:.1f}" if fmt == "pct1" else f"{v * 100:.0f}" if fmt == "pct" else fmt.format(v)


def awards(board):
    q = [r for r in board if r["qualified"]]
    out = []
    for slug, title, key, unit, fmt, desc, _ in AWARDS:
        if not q:
            break
        w = max(q, key=lambda r: r.get(key) or 0)
        out.append({"slug": slug, "title": title, "unit": unit, "value": award_value(fmt, w.get(key)), "desc": desc,
                    "sid": w["steamid64"], "name": w["name"]})
    return out


@app.route("/oduller")
def awards_page():
    """All awards of a period, each with its top 3; a card opens the award's full ranking."""
    period = request.args.get("p", DEFAULT_PERIOD)
    if period not in PERIODS:
        period = DEFAULT_PERIOD
    c = con()
    board = statsdb.leaderboard(c, period)
    q = [r for r in board if r["qualified"]]
    cards = []
    for slug, title, key, unit, fmt, desc, detail in AWARDS:
        top = sorted(q, key=lambda r: r.get(key) or 0, reverse=True)[:3]
        cards.append({"slug": slug, "title": title, "unit": unit, "desc": desc,
                      "top": [{"sid": r["steamid64"], "name": r["name"], "value": award_value(fmt, r.get(key)),
                               "tier": r.get("rating"), "detail": detail(r)} for r in top]})
    ctx = dict(cards=cards, period=period, periods=PERIODS, people=people(c), ranked=len(q),
               minm=rating.MIN_MATCHES.get(period))
    c.close()
    return render_template("awards.html", **ctx)


@app.route("/odul/<slug>")
def award_page(slug):
    """One award's full ranking: every ranked player of the period sorted by the award's stat, top 3 on a podium."""
    found = [a for a in AWARDS if a[0] == slug]
    if not found:
        abort(404)
    slug, title, key, unit, fmt, desc, detail = found[0]
    period = request.args.get("p", DEFAULT_PERIOD)
    if period not in PERIODS:
        period = DEFAULT_PERIOD
    c = con()
    board = statsdb.leaderboard(c, period)
    q = sorted((r for r in board if r["qualified"]), key=lambda r: r.get(key) or 0, reverse=True)
    hi = (q[0].get(key) or 0) if q else 0
    lo = (q[-1].get(key) or 0) if q else 0
    span = hi - lo  # bars run from the lowest ranked value to the highest, so close values still differ visibly
    rows = [{"pos": i + 1, "sid": r["steamid64"], "name": r["name"], "value": award_value(fmt, r.get(key)),
             "frac": (0.12 + 0.88 * ((r.get(key) or 0) - lo) / span) if span else 1, "tier": r.get("rating"), "matches": r["total_matches"],
             "detail": detail(r)} for i, r in enumerate(q)]
    ctx = dict(rows=rows, title=title, unit=unit, desc=desc, slug=slug, period=period, periods=PERIODS,
               people=people(c), others=[(a[0], a[1]) for a in AWARDS], unranked=len(board) - len(q),
               minm=rating.MIN_MATCHES.get(period))
    c.close()
    return render_template("award.html", **ctx)


@app.route("/")
def stats_home():
    period = request.args.get("p", DEFAULT_PERIOD)
    if period not in PERIODS:
        period = DEFAULT_PERIOD
    c = con()
    board = statsdb.leaderboard(c, period)
    ov = statsdb.overview(c, period)
    synced = statsdb.get_meta(c, "synced_at")
    win = statsdb.window(c, period) if period != "all" else None
    sync_date = statsdb.get_meta(c, "sync_date")
    newer = c.execute("SELECT COUNT(*) FROM games WHERE source = 'demo' AND date > ?",
                      (sync_date or "",)).fetchone()[0]
    clip_title = {"7d": "Haftanın klipleri", "30d": "Bu ayın klipleri", "90d": "Son 3 ayın klipleri"}.get(period)
    if period == "all":  # the front page: this month's clips (the last 30 days), else the best ever
        best = top_clips(c, period, since=(datetime.date.today() - datetime.timedelta(days=30)).isoformat())
        clip_title = "Bu ayın klipleri"
        if not best:
            best, clip_title = top_clips(c, period), "En iyi klipler"
    else:
        best = top_clips(c, period)
    cb = None if best else statsdb.comeback(c, period)  # the comeback story only when there are no clips
    ctx = dict(board=board, period=period, periods=PERIODS, ov=ov, people=people(c), win=win, newer=newer,
               awards=awards(board), min_matches=rating.MIN_MATCHES.get(period, 3), comeback=cb, best_clips=best, clip_title=clip_title, months=statsdb.activity_by_month(c),
               weeks=statsdb.weekly_counts(c), seen=statsdb.last_seen(c), ref=statsdb.latest_date(c),
               month30=statsdb.overview(c, "30d")["matches"],
               sync_date=sync_date,  # newest 5v5stats match we have; later games are our demos only
               synced=(datetime.datetime.fromtimestamp(synced / 1000, TR_TZ).strftime("%Y-%m-%d %H:%M")
                       if synced else None))
    c.close()
    return render_template("stats.html", **ctx)


CLIP_TYPES = [("ace", "Ace (5K)"), ("4k", "4K"), ("3k", "3K"), ("clutch", "Clutch")]
CLIP_MIN_SCORES = [100, 125, 150, 175]


def clip_type_of(label):
    """Kinds a clip counts as, from its label ("4K + 1v2 clutch" -> {"4k", "clutch"})."""
    lab = (label or "").lower()
    kinds = set()
    m = re.match(r"\s*(\d)k", lab)
    if m:
        kinds.add({"5": "ace", "4": "4k", "3": "3k"}.get(m.group(1), ""))
    if "clutch" in lab:
        kinds.add("clutch")
    return kinds - {""}


@app.route("/highlights")
def home():
    """Klipler: every clip, filterable by player, match, map, kind and points; the match cards below."""
    c = con()
    pub = "" if is_admin() else " WHERE m.published = 1"
    matches = c.execute("SELECT * FROM matches m" + pub + " ORDER BY date DESC, id DESC").fetchall()
    clips = c.execute(f"""SELECT cl.*, m.slug, m.media_rev, m.map AS mmap, m.date AS mdate, m.score1, m.score2
                          FROM clips cl JOIN matches m ON m.id = cl.match_id{pub}""").fetchall()
    mm = statsdb.merge_map(c)
    ppl = people(c)
    c.close()
    f = {k: request.args.get(k, "") for k in ("p", "m", "map", "t", "s", "o")}
    try:
        min_score = int(f["s"] or 0)
    except ValueError:
        min_score = 0
    items = [dict(cl, who=mm.get(cl["steamid"], cl["steamid"]), kinds=clip_type_of(cl["label"])) for cl in clips]

    def keep(x, skip=None):
        return ((not f["p"] or x["who"] == f["p"]) and (not f["m"] or x["slug"] == f["m"])
                and (not f["map"] or x["mmap"] == f["map"]) and (x["score"] or 0) >= min_score
                and (skip == "t" or not f["t"] or f["t"] in x["kinds"]))
    type_counts = {k: sum(1 for x in items if keep(x, "t") and k in x["kinds"]) for k, _ in CLIP_TYPES}
    shown = [x for x in items if keep(x)]
    if f["o"] == "new":
        shown.sort(key=lambda x: (x["mdate"] or "", x["match_id"], -(x["idx"] or 0)), reverse=True)
    else:
        shown.sort(key=lambda x: (x["score"] or 0, x["mdate"] or ""), reverse=True)
    pcount = {}
    for x in items:
        pcount[x["who"]] = pcount.get(x["who"], 0) + 1
    players = sorted(((sid, (ppl.get(sid) or {}).get("name") or next(x["player"] for x in items if x["who"] == sid), n)
                      for sid, n in pcount.items()), key=lambda t: (-t[2], t[1].lower()))
    maps = sorted({x["mmap"] for x in items if x["mmap"]})
    return render_template("home.html", matches=matches, clips=shown, total=len(items), f=f, players=players,
                           maps=maps, types=CLIP_TYPES, type_counts=type_counts, min_scores=CLIP_MIN_SCORES,
                           people=ppl, filtered=any(f[k] for k in ("p", "m", "map", "t", "s")))


@app.route("/players/<steamid>")
def player_page(steamid):
    c = con()
    main = statsdb.merge_map(c).get(steamid)
    if main:  # merged account: show the person
        c.close()
        return redirect(url_for("player_page", steamid=main))
    ids = statsdb.ids_of(c, steamid)
    q = ",".join("?" * len(ids))
    board = statsdb.leaderboard(c, "all")
    me = next((r for r in board if r["steamid64"] == steamid), None)
    if me is None:
        c.close()
        abort(404)
    hist = c.execute(f"""SELECT gp.*, g.id AS gid, g.source, g.ext_id, g.match_key, g.map, g.date, g.rounds, g.team_a,
                        g.score_a, g.team_b, g.score_b FROM game_players gp JOIN games g ON g.id = gp.game_id
                        WHERE gp.steamid IN ({q}) ORDER BY g.date DESC, COALESCE(g.ext_id, 0) DESC, g.id DESC""",
                     ids).fetchall()
    maps = {}
    for b in c.execute(f"SELECT map_stats FROM baseline WHERE steamid IN ({q})", ids):
        for m in json.loads(b["map_stats"] or "[]"):
            t = maps.setdefault(m["map"], {"map": m["map"]})
            for k, v in m.items():
                if k != "map" and isinstance(v, (int, float)):
                    t[k] = t.get(k, 0) + v
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
    mpts = statsdb.map_points(c)
    for m in maps.values():
        m["adr"] = m["damage"] / m["rounds"] if m["rounds"] else 0
        m["kd"] = m["kills"] / m["deaths"] if m["deaths"] else 0
        # Skor on this map: only with a few matches there, else one lucky game would decide it
        pts, n = mpts.get(statsdb.map_label(m["map"]), {}).get(steamid, (None, 0))
        m["points"] = pts if n >= 3 else None
    clips = c.execute("""SELECT clips.*, matches.slug, matches.media_rev, matches.map AS mmap, matches.date AS mdate FROM clips
                         JOIN matches ON matches.id = clips.match_id WHERE clips.steamid IN (""" + q + ")" +
                      ("" if is_admin() else " AND matches.published = 1") + " ORDER BY clips.score DESC",
                      ids).fetchall()
    ppl, links = people(c), highlight_links(c)
    rel = statsdb.relations(c, steamid)
    duo_list = statsdb.duo_players(c)
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
    alts = [{"sid": a, "name": (ppl.get(a) or {}).get("name") or a} for a in ids[1:]]
    return render_template("player.html", p=me, hist=hist, maps=maplist, clips=clips, person=ppl.get(steamid, {}),
                           alts=alts,
                           links=links, n_ranked=len(qualified), rel=rel, radar=radar, ref=ref, best=best,
                           worst=worst, people=ppl, form=list(hist[:10])[::-1], duo_list=duo_list)


@app.route("/ikili")
def duo_page():
    """Two players side by side: as teammates, as opponents, and who killed whom (demo games)."""
    c = con()
    mm = statsdb.merge_map(c)
    a = mm.get(request.args.get("a", ""), request.args.get("a", ""))
    b = mm.get(request.args.get("b", ""), request.args.get("b", ""))
    plist = statsdb.duo_players(c)
    names = {s: n for s, n, _ in plist}
    ppl = people(c)
    tiers = player_tiers(c)
    d = statsdb.duo(c, a, b) if a and b and a != b and a in names and b in names else None
    # who-killed-whom comes only from our demos: say since when there is any
    since, n_demos = c.execute("SELECT MIN(g.date), COUNT(*) FROM games g "
                               "WHERE EXISTS (SELECT 1 FROM duels d WHERE d.game_id = g.id)").fetchone()
    facts = statsdb.duo_facts(c) if d is None else None  # the opening page: the extremes over all pairs
    if facts:
        for s in {r[k] for rows in (facts["lift"], facts["sink"], facts["dom"], facts["rival"]) for r in rows
                  for k in ("a", "b")}:
            names.setdefault(s, (ppl.get(s) or {}).get("name") or s)
    c.close()
    return render_template("duo.html", a=a, b=b, d=d, plist=plist, names=names, people=ppl, tiers=tiers,
                           demo_since=since, n_demos=n_demos, facts=facts)


@app.route("/balancer")
def balancer():
    """Takım Kur: pick 10 players, get the most even 5v5 split (all 252 splits are checked in the browser)."""
    c = con()
    pool = statsdb.balancer_pool(c)
    maps = [r["map"] for r in c.execute("SELECT map, COUNT(*) n FROM games GROUP BY map HAVING COUNT(*) >= 5 ORDER BY n DESC")]
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
    mm = statsdb.merge_map(c)
    pid, wid = mm.get(pid, pid), mm.get(wid, wid)
    if pid:
        pids = statsdb.ids_of(c, pid)
        join = f"JOIN game_players me ON me.game_id = g.id AND me.steamid IN ({','.join('?' * len(pids))})"
        args[0:0] = pids
    if wid:
        wids = statsdb.ids_of(c, wid)
        join += f" JOIN game_players other ON other.game_id = g.id AND other.steamid IN ({','.join('?' * len(wids))})"
        args[len(pids):len(pids)] = wids
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
    counts, names = {}, {}
    for r in c.execute("""SELECT p.steamid, p.name, COUNT(*) AS n FROM game_players gp JOIN people p ON p.steamid = gp.steamid
                          GROUP BY p.steamid, p.name"""):
        k = mm.get(r["steamid"], r["steamid"])
        counts[k] = counts.get(k, 0) + r["n"]
        if r["steamid"] == k:
            names[k] = r["name"]
    players = sorted(({"steamid": k, "name": names.get(k, k), "n": n} for k, n in counts.items()), key=lambda p: -p["n"])
    links = highlight_links(c)
    c.close()
    return render_template("games.html", games=rows, maps=maps, mapf=mapf, page=page, pid=pid, wid=wid,
                           pname=names.get(pid), wname=names.get(wid), players=players, summary=summary,
                           pages=max(1, (total + per - 1) // per), total=total, links=links)


def player_tiers(c):
    """{steamid: {"tier": 1-10 or None, "points": 1.00-scale score}} for the EDO card's level badge."""
    out = {r["steamid64"]: {"tier": r["rating"], "points": r["points"]} for r in statsdb.leaderboard(c, "all")}
    for alt, main in statsdb.merge_map(c).items():
        if main in out:
            out[alt] = out[main]
    return out


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
    page = matchpage.render(data, back=url_for("games_page"), back_label="Tüm maçlar", extra_head=note,
                            map_bg=map_img(g["map"]),
                            site_nav=site_nav())
    return page.replace("<title>", f"<title>{label} - ", 1)


def site_nav():
    """The site's header for match pages (they're drawn by matchpage.py with their own stylesheet)."""
    return render_template_string('{% from "_header.html" import standalone_header with context %}{{ standalone_header() }}')


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
    page = matchpage.render(data, base=storage.base(slug, m["media_rev"]), back="/highlights", extra_head=banner,
                            map_bg=map_img(m["map"]),
                            site_nav=site_nav(),
                            back_label="Klipler")
    if m["title"]:
        page = page.replace("<title>", f"<title>{matchpage.esc(m['title'])} - ", 1)
    return page


@app.route("/media/<slug>/<path:path>")
def media(slug, path):
    m = get_match(slug)  # files of hidden matches are hidden too
    if storage.R2 and m["media_rev"]:
        return redirect(storage.url(slug, m["media_rev"], path))
    return send_from_directory(storage.local_dir(slug), path, conditional=True, max_age=3600)


# ---------------------------------------------------------------- merged accounts ----
# /birlestir: count a player's second account as the same person. Admins only (the admin login);
# every merge is listed there and can be undone ("Ayır").

def merge_allowed():
    return is_admin()


@app.route("/birlestir", methods=["GET", "POST"])
def merge_page():
    if not merge_allowed():
        return redirect(url_for("login", next=request.path))
    c = con()
    try:
        if request.method == "POST":
            if request.form.get("csrf") != session.get("csrf"):
                abort(400, "Form expired, go back and try again.")
            action = request.form.get("action")
            names = {r["steamid"]: r["name"] for r in c.execute("SELECT steamid, name FROM people")}
            if action == "merge":
                main, alt = request.form.get("main", ""), request.form.get("alt", "")
                if main not in names or alt not in names:
                    flash("İki hesabı da listeden seç.")
                else:
                    try:
                        used = statsdb.merge_accounts(c, main, alt, request.form.get("note", "").strip()[:120])
                        flash(f"{names[alt]} artık {names.get(used, used)} olarak sayılıyor.")
                    except ValueError as e:
                        flash(f"Birleştirilemedi: {e}.")
            elif action == "unmerge":
                alt = request.form.get("alt", "")
                statsdb.unmerge_account(c, alt)
                flash(f"{names.get(alt, alt)} ayrıldı; istatistikleri yine kendi hesabında.")
            return redirect(url_for("merge_page"))
        ctx = {"allowed": merge_allowed(), "accounts": [], "merges": []}
        if ctx["allowed"]:
            mm = statsdb.merge_map(c)
            n = {r[0]: r[1] for r in c.execute("SELECT steamid, COUNT(*) FROM game_players GROUP BY steamid")}
            rows = list(c.execute("SELECT steamid, name, avatar FROM people"))
            ppl = {r["steamid"]: r["name"] for r in rows}
            av = {r["steamid"]: r["avatar"] for r in rows if r["avatar"]}
            last = {r[0]: r[1] for r in c.execute("""SELECT gp.steamid, MAX(g.date) FROM game_players gp
                                                    JOIN games g ON g.id = gp.game_id GROUP BY gp.steamid""")}
            ctx["accounts"] = sorted(({"sid": s, "name": nm or s, "n": n.get(s, 0), "alt_of": mm.get(s),
                                       "avatar": av.get(s), "last": day(last[s]) if last.get(s) else None}
                                      for s, nm in ppl.items()), key=lambda a: (-a["n"], (a["name"] or "").lower()))
            ctx["avatars"] = av
            ctx["merges"] = [{"alt": r["alt"], "main": r["main"], "alt_name": ppl.get(r["alt"], r["alt"]),
                              "main_name": ppl.get(r["main"], r["main"]), "n": n.get(r["alt"], 0), "note": r["note"],
                              "alt_av": av.get(r["alt"]), "main_av": av.get(r["main"]),
                              "when": day(time.strftime("%Y-%m-%d", time.localtime(r["created_at"] or 0)))}
                             for r in c.execute("SELECT * FROM merges ORDER BY main, created_at")]
        return render_template("merge.html", **ctx)
    finally:
        c.close()


# ---------------------------------------------------------------- uploads ----

def receive(stream_or_file, via):
    """Save an uploaded package to a temp file and import it. Returns (match, created, notes)."""
    fd, tmp = tempfile.mkstemp(suffix=".zip")
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


def upload_auth():
    auth = request.headers.get("Authorization", "")
    return auth.startswith("Bearer ") and secrets.compare_digest(auth[7:], SECRETS["UPLOAD_TOKEN"])


def upload_result(m, created, notes):
    publish = request.args.get("publish") == "1"
    if publish and not m["published"]:
        c = con()
        c.execute("UPDATE matches SET published = 1 WHERE id = ?", (m["id"],))
        c.commit()
        c.close()
    base = request.host_url.rstrip("/")
    return jsonify(ok=True, created=created, notes=notes, match=m["map"], key=m["match_key"],
                   published=bool(m["published"]) or publish,
                   url=f"{base}/m/{m['slug']}", admin_url=f"{base}/admin/match/{m['id']}")


@app.route("/api/levels")
def api_levels():
    """{steamid: {"tier": 1-10 or None, "points": rating}} for the title cards render.py puts in clips
    (the same levels and ratings the site shows; merged accounts get their person's)."""
    c = con()
    try:
        return jsonify(player_tiers(c))
    finally:
        c.close()


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """For upload.py: body = the package zip, header Authorization: Bearer <UPLOAD_TOKEN>.
    (A site on Vercel can't take big bodies; there upload.py uses /api/upload/begin + /finish.)"""
    if not upload_auth():
        return jsonify(ok=False, error="wrong or missing upload token"), 401
    try:
        m, created, notes = receive(request.stream, "upload.py")
    except ingest.BadPackage as e:
        return jsonify(ok=False, error=str(e)), 400
    return upload_result(m, created, notes)


@app.route("/api/upload/begin", methods=["POST"])
def api_upload_begin():
    """upload.py step 1. Body: {"data": match_data.json, "files": {path: size}}.
    Answers mode "r2" with a signed link per file (PUT the file there) and a ticket for step 2, or
    mode "zip" when this site keeps files on its own disk (then send the package to /api/upload)."""
    if not upload_auth():
        return jsonify(ok=False, error="wrong or missing upload token"), 401
    if not storage.R2:
        return jsonify(ok=True, mode="zip")
    body = request.get_json(silent=True) or {}
    files = body.get("files") if isinstance(body.get("files"), dict) else {}
    c = con()
    try:
        key, existing, slug = ingest.prepare(c, body.get("data"))
    except ingest.BadPackage as e:
        return jsonify(ok=False, error=str(e)), 400
    finally:
        c.close()
    paths = [p for p in files if isinstance(p, str) and p != "match_data.json" and ingest.ALLOWED.match(p)]
    rev = ingest.new_rev()
    return jsonify(ok=True, mode="r2", slug=slug, rev=rev,
                   ticket=TICKETS.dumps({"slug": slug, "rev": rev, "key": key}),
                   put={p: storage.presign("PUT", storage.key(slug, rev, p), expires=3 * 3600) for p in paths},
                   ignored=[p for p in files if p not in paths and p != "match_data.json"])


@app.route("/api/upload/finish", methods=["POST"])
def api_upload_finish():
    """upload.py step 2, after the files are in the bucket. Body: {"ticket": ..., "data": match_data.json}."""
    if not upload_auth():
        return jsonify(ok=False, error="wrong or missing upload token"), 401
    body = request.get_json(silent=True) or {}
    try:
        t = TICKETS.loads(body.get("ticket") or "", max_age=12 * 3600)
    except BadSignature:
        return jsonify(ok=False, error="upload ticket invalid or expired; run upload.py again"), 400
    c = con()
    try:
        key, existing, _ = ingest.prepare(c, body.get("data"))
        if key != t["key"] or (existing and existing["slug"] != t["slug"]):
            raise ingest.BadPackage("the ticket is for another match; run upload.py again")
        files = storage.files_of(t["slug"], t["rev"])
        m, created, notes = ingest.apply(c, body["data"], key, existing, t["slug"], t["rev"], files, "upload.py",
                                         sum(files.values()))
    except ingest.BadPackage as e:
        ingest.log_failure(c, "upload.py", str(e), 0)
        return jsonify(ok=False, error=str(e)), 400
    finally:
        c.close()
    storage.drop_other_revs(t["slug"], t["rev"])
    return upload_result(m, created, notes)


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

OWNER = "admin"  # the main admin: logs in with ADMIN_PASSWORD and can add or remove other admins
ADMIN_NAME = re.compile(r"[\w.\-]{2,32}", re.UNICODE)


def check_login(name, password):
    """The admin's name if the password is right, else None."""
    name = name.strip()
    if name.lower() in ("", OWNER):
        return OWNER if secrets.compare_digest(password, SECRETS["ADMIN_PASSWORD"]) else None
    c = con()
    row = c.execute("SELECT name, pw_hash FROM admins WHERE lower(name) = lower(?)", (name,)).fetchone()
    c.close()
    return row["name"] if row and check_password_hash(row["pw_hash"], password) else None


def is_owner():
    return is_admin() and session.get("admin_name", OWNER) == OWNER


@app.before_request
def drop_removed_admins():
    """An admin whose account was removed is logged out on their next request."""
    name = session.get("admin_name")
    if session.get("admin") and name and name != OWNER:
        c = con()
        alive = c.execute("SELECT 1 FROM admins WHERE name = ?", (name,)).fetchone()
        c.close()
        if not alive:
            session.clear()


@app.route("/admin/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        who = check_login(request.form.get("name", ""), request.form.get("password", ""))
        if who:
            session.clear()
            session["admin"] = True
            session["admin_name"] = who
            session["csrf"] = secrets.token_hex(16)
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("admin"))
        time.sleep(1)  # slows down password guessing
        flash("Kullanıcı adı veya şifre yanlış.")
    return render_template("login.html")


@app.route("/admin/admins/add", methods=["POST"])
@admin_required
def admin_add():
    if not is_owner():
        abort(403)
    name, pw = request.form.get("name", "").strip(), request.form.get("password", "")
    if not ADMIN_NAME.fullmatch(name) or name.lower() == OWNER:
        flash("Kullanıcı adı 2-32 karakter olmalı (harf, rakam, . _ -) ve 'admin' olamaz.")
    elif len(pw) < 8:
        flash("Şifre en az 8 karakter olmalı.")
    else:
        c = con()
        if c.execute("SELECT 1 FROM admins WHERE lower(name) = lower(?)", (name,)).fetchone():
            flash(f"{name} zaten yönetici.")
        else:
            c.execute("INSERT INTO admins(name, pw_hash, created_at) VALUES (?,?,?)",
                      (name, generate_password_hash(pw), time.time()))
            c.commit()
            flash(f"{name} yönetici olarak eklendi. Kullanıcı adı ve şifreyi kendisine ilet.")
        c.close()
    return redirect(url_for("admin") + "#admins")


@app.route("/admin/admins/remove", methods=["POST"])
@admin_required
def admin_remove():
    if not is_owner():
        abort(403)
    name = request.form.get("name", "")
    c = con()
    c.execute("DELETE FROM admins WHERE name = ?", (name,))
    c.commit()
    c.close()
    flash(f"{name} artık yönetici değil.")
    return redirect(url_for("admin") + "#admins")


# ---------------------------------------------------------------- clips: save / delete ----
# Saved clips are kept when unsaved ones are cleaned up. A deleted clip's video and thumbnails are removed
# from storage (R2) and it stays deleted when the match is uploaded again (clip_marks).

def _mark(c, match_key, rnd, sid, who, **cols):
    sets = ", ".join(f"{k} = excluded.{k}" for k in cols)
    c.execute(f"""INSERT INTO clip_marks(match_key, round, steamid, {', '.join(cols)}, by_admin, updated_at)
                  VALUES (?, ?, ?, {', '.join('?' for _ in cols)}, ?, ?)
                  ON CONFLICT(match_key, round, steamid) DO UPDATE SET {sets}, by_admin = excluded.by_admin,
                  updated_at = excluded.updated_at""",
              (match_key, rnd, sid, *cols.values(), who, time.time()))


def saved_clips(c):
    """{(match_key, round, steamid)} of saved clips."""
    return {(r["match_key"], r["round"], r["steamid"])
            for r in c.execute("SELECT match_key, round, steamid FROM clip_marks WHERE saved = 1 AND removed = 0")}


def remove_clips(c, m, plays, who, sizes=None):
    """Delete clips of match row m: plays = {(round, steamid)}. Updates the page, remembers the deletion,
    then removes the files. Returns (clips deleted, bytes freed)."""
    data = json.loads(m["data_json"])
    dropped = [x for x in data["clips"] if (x["round"], x["steamid"]) in plays]
    if not dropped:
        return 0, 0
    data["clips"] = [x for x in data["clips"] if (x["round"], x["steamid"]) not in plays]
    unused = ingest.clip_files(dropped) - ingest.clip_files(data["clips"]) - set(data["pics"].values())
    sizes = sizes if sizes is not None else storage.file_sizes(m["slug"], m["media_rev"])
    freed = sum(sizes.get(p, 0) for p in unused)
    if not data["clips"]:
        data["note"] = "Bu maçın klipleri silindi."
    db.save_match_data(c, m["id"], data, ingest.summary(data))
    c.execute("UPDATE matches SET size_bytes = CASE WHEN size_bytes > ? THEN size_bytes - ? ELSE 0 END WHERE id = ?",
              (freed, freed, m["id"]))
    for x in dropped:
        _mark(c, m["match_key"], x["round"], x["steamid"], who, saved=0, removed=1)
    c.commit()
    storage.delete_files(m["slug"], m["media_rev"], unused)
    return len(dropped), freed


def _back(default):
    ref = request.form.get("back", "")
    return redirect(ref if ref.startswith("/") and not ref.startswith("//") else default)


def _clip_row(c, cid):
    row = c.execute("SELECT cl.*, m.match_key FROM clips cl JOIN matches m ON m.id = cl.match_id WHERE cl.id = ?",
                    (cid,)).fetchone()
    if row is None:
        c.close()
        abort(404)
    return row


@app.route("/admin/clip/<int:cid>/save", methods=["POST"])
@admin_required
def clip_save(cid):
    c = con()
    cl = _clip_row(c, cid)
    keep = request.form.get("saved") == "1"
    _mark(c, cl["match_key"], cl["round"], cl["steamid"], session.get("admin_name", OWNER), saved=int(keep))
    c.commit()
    c.close()
    return _back(url_for("admin_clips"))


@app.route("/admin/clip/<int:cid>/delete", methods=["POST"])
@admin_required
def clip_delete(cid):
    c = con()
    cl = _clip_row(c, cid)
    m = c.execute("SELECT * FROM matches WHERE id = ?", (cl["match_id"],)).fetchone()
    n, freed = remove_clips(c, m, {(cl["round"], cl["steamid"])}, session.get("admin_name", OWNER))
    c.close()
    flash(f"Klip silindi ({cl['player']}, {cl['label']}, round {cl['round']}); {freed / 1e6:.1f} MB yer açıldı.")
    return _back(url_for("admin_clips"))


def _unsaved(c):
    """[(match row, [clip rows not saved])] for every match with unsaved clips."""
    saved = saved_clips(c)
    out = []
    for m in c.execute("SELECT * FROM matches ORDER BY date DESC, id DESC").fetchall():
        rows = [cl for cl in c.execute("SELECT * FROM clips WHERE match_id = ? ORDER BY idx", (m["id"],))
                if (m["match_key"], cl["round"], cl["steamid"]) not in saved]
        if rows:
            out.append((m, rows))
    return out


@app.route("/admin/klipler")
@admin_required
def admin_clips():
    c = con()
    saved = saved_clips(c)
    matches = c.execute("SELECT * FROM matches ORDER BY date DESC, id DESC").fetchall()
    groups, n_saved, n_all = [], 0, 0
    for m in matches:
        rows = []
        for cl in c.execute("SELECT * FROM clips WHERE match_id = ? ORDER BY score DESC", (m["id"],)):
            s = (m["match_key"], cl["round"], cl["steamid"]) in saved
            n_saved += s
            rows.append(dict(cl, saved=s))
        n_all += len(rows)
        if rows:
            groups.append((m, rows))
    removed = c.execute("SELECT COUNT(*) FROM clip_marks WHERE removed = 1").fetchone()[0]
    c.close()
    return render_template("admin_clips.html", groups=groups, n_saved=n_saved, n_all=n_all, removed=removed)


@app.route("/admin/klipler/temizle", methods=["GET", "POST"])
@admin_required
def admin_clean():
    """Delete every clip nobody saved: GET shows what would go (count and size), POST does it."""
    c = con()
    todo = _unsaved(c)
    sizes = {m["id"]: storage.file_sizes(m["slug"], m["media_rev"]) for m, _ in todo}
    if request.method == "POST":
        n = freed = 0
        for m, rows in todo:
            k, b = remove_clips(c, m, {(cl["round"], cl["steamid"]) for cl in rows},
                                session.get("admin_name", OWNER), sizes[m["id"]])
            n, freed = n + k, freed + b
        c.close()
        flash(f"{n} kaydedilmemiş klip silindi; {freed / 1e6:.1f} MB yer açıldı.")
        return redirect(url_for("admin_clips"))
    plan = []
    for m, rows in todo:
        data = json.loads(m["data_json"])
        plays = {(cl["round"], cl["steamid"]) for cl in rows}
        drop = [x for x in data["clips"] if (x["round"], x["steamid"]) in plays]
        keep = [x for x in data["clips"] if (x["round"], x["steamid"]) not in plays]
        unused = ingest.clip_files(drop) - ingest.clip_files(keep) - set(data["pics"].values())
        plan.append({"m": m, "n": len(rows), "keep": len(keep),
                     "bytes": sum(sizes[m["id"]].get(p, 0) for p in unused)})
    c.close()
    return render_template("admin_clean.html", plan=plan, n=sum(p["n"] for p in plan),
                           total=sum(p["bytes"] for p in plan))


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
    admins = c.execute("SELECT name, created_at FROM admins ORDER BY name").fetchall()
    c.close()
    return render_template("admin.html", matches=matches, uploads=uploads, total=total, admins=admins,
                           owner=is_owner(), me=session.get("admin_name", OWNER),
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
    saved = saved_clips(c)
    clips = [dict(cl, saved=(m["match_key"], cl["round"], cl["steamid"]) in saved)
             for cl in c.execute("SELECT * FROM clips WHERE match_id = ? ORDER BY idx", (mid,))]
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
    storage.delete_match(m["slug"])
    flash("Match deleted from the site. Your files on the rendering PC are untouched; upload it again to bring it back.")
    return redirect(url_for("admin"))


def main():
    print("Website: http://localhost:5000   Admin: http://localhost:5000/admin  (secrets in web\\admin.env)")
    sys.stdout.flush()
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
