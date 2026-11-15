"""Match summary page design: renders a match's data (scoreboard_html.collect) to HTML.

Shared by the rendering PC (scoreboard_html.py, static pages) and the website (web/app.py). No dependencies
beyond the standard library, so the website can run on a server without the demo/stats code.
"""
import hashlib
import html
import re
import urllib.parse

CSS = """
:root { --bg:#0d0d0d; --panel:#161616; --panel2:#1c1c1c; --line:#262626; --text:#f2f2f2; --muted:#8d8d8d;
        --accent:#ff5500; --win:#32d35a; --lose:#ff4d4d; --gold:#ffc107;
        --rowA:#16302a; --rowA2:#1a3830; --rowB:#1f2238; --rowB2:#252942; }
* { box-sizing:border-box; }
html { -webkit-text-size-adjust:100%; }
body { margin:0; background:var(--bg); color:var(--text);
       font:14px/1.4 "Segoe UI", system-ui, -apple-system, Roboto, sans-serif; }
a { color:inherit; }
.wrap { max-width:1180px; margin:0 auto; padding:20px 16px 48px; }
.num { font-variant-numeric:tabular-nums; }
.pos { color:var(--win); } .neg { color:var(--lose); } .dim { color:var(--muted); }

/* avatars: initials until real profile pictures are added */
.av { display:inline-grid; place-items:center; flex:none; border-radius:6px; font-weight:800; color:#fff;
      width:26px; height:26px; font-size:11px; text-transform:uppercase; letter-spacing:.02em; }
.av.lg { width:150px; height:150px; border-radius:12px; font-size:54px; }
.av.md { width:34px; height:34px; font-size:13px; border-radius:50%; }
.av.tm { width:52px; height:52px; font-size:18px; border-radius:50%; border:2px solid rgba(255,255,255,.25); }
img.av { object-fit:cover; background:#222; }

/* hero */
.hero { position:relative; border-radius:10px; overflow:hidden; min-height:150px; border:1px solid var(--line); margin-bottom:18px;
        background:#1a1a1a center/cover no-repeat; }
.hero::before { content:""; position:absolute; inset:0;
                background:linear-gradient(180deg, rgba(0,0,0,.35), rgba(0,0,0,.75)); }
.hero > * { position:relative; }
.hero .meta { display:flex; justify-content:space-between; gap:10px; padding:12px 16px 0; font-size:12px;
              color:#d0d0d0; font-weight:600; }
.hero .vs { display:grid; grid-template-columns:1fr auto 1fr; align-items:center; gap:18px; padding:22px 16px 34px; }
.hero .team { display:flex; align-items:center; gap:12px; min-width:0; }
.hero .team.l { justify-content:flex-end; text-align:right; }
.hero .tname { font-size:22px; font-weight:800; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.hero .tag { font-size:11px; font-weight:800; color:var(--win); letter-spacing:.06em; }
.hero .score { display:flex; align-items:center; gap:14px; font-size:40px; font-weight:800; }
.hero .score .w { color:var(--win); } .hero .score .l { color:#bdbdbd; }
.hero .score small { font-size:13px; color:var(--muted); font-weight:700; }

.tabs { display:flex; gap:22px; border-bottom:1px solid var(--line); margin-bottom:18px; font-size:12px;
        font-weight:700; text-transform:uppercase; letter-spacing:.04em; }
.tabs span { padding:10px 0; color:var(--muted); }
.tabs span.on { color:var(--accent); border-bottom:2px solid var(--accent); }

/* MVP + leaders */
.top { display:grid; grid-template-columns:minmax(0, 1fr) 300px; gap:14px; margin-bottom:22px; }
/* EDO card, FACEIT style: player card on the left, rating + 2x2 stats, big ★EDO, highlights box */
.top { display:grid; grid-template-columns:minmax(0, 1fr) 436px; gap:22px; margin-bottom:26px; align-items:stretch; }
.mvp { position:relative; display:flex; gap:22px; padding:22px; border-radius:10px; background:#161616;
       border:1px solid #262626; }
.mvp::before { content:""; position:absolute; left:0; top:88px; bottom:88px; width:3px; border-radius:0 3px 3px 0;
               background:var(--team, #8b5cf6); }
.mvp .card { width:196px; flex:none; display:flex; flex-direction:column; align-items:center; justify-content:center;
             gap:14px; padding:26px 12px; border-radius:12px; background:#111; border:1px solid #2c2c2c; }
.mvp .card .av.lg { width:112px; height:112px; border-radius:50%; font-size:40px; }
.mvp .card .nm { font-weight:800; font-size:19px; text-align:center; max-width:100%; overflow:hidden;
                 text-overflow:ellipsis; white-space:nowrap; }
.mvp .lvl { display:inline-flex; align-items:center; gap:7px; padding:3px 10px 3px 3px; border-radius:999px;
            background:#1c1c1c; border:1px solid #2c2c2c; font-weight:700; font-size:12px; }
.mvp .lvl i { display:grid; place-items:center; width:22px; height:22px; border-radius:50%; font-style:normal;
              font-size:11px; font-weight:800; border:2px solid var(--accent); color:#fff; }
.mvp .body { flex:1; min-width:0; display:flex; flex-direction:column; padding-top:4px; padding-right:150px; }
.mvp .badge { display:inline-block; width:max-content; padding:3px 9px 5px; border-radius:4px; background:#262626;
              border:1px solid #3a3a3a; font-weight:800; font-size:21px; position:relative; color:#fff; }
.mvp .badge::after { content:""; position:absolute; left:6px; right:6px; bottom:2px; height:2px; background:#4a4a4a; }
.mvp .badge u { position:absolute; left:6px; bottom:2px; height:2px; background:#fff; text-decoration:none; z-index:1; }
.mvp .rlbl { display:flex; align-items:center; gap:7px; color:#cfcfcf; font-size:14px; margin-top:8px; }
.mvp .rlbl i { width:15px; height:15px; border-radius:50%; border:2px solid var(--accent); display:inline-block; }
.mvp .grid { display:grid; grid-template-columns:repeat(2, minmax(0, 210px)); gap:30px 24px; margin-top:34px; }
.mvp .grid b { display:block; font-size:21px; font-weight:800; }
.mvp .grid span { display:block; color:#cfcfcf; font-size:14px; margin-top:2px; }
.mvp .title { position:absolute; top:18px; right:24px; font-size:36px; font-weight:900; letter-spacing:.01em; }
.mvp .title i { color:var(--gold); font-style:normal; font-size:26px; vertical-align:6px; margin-right:2px; }
.mvp .clip { position:absolute; right:24px; bottom:22px; display:flex; flex-direction:column; align-items:flex-end; gap:10px;
             text-decoration:none; font-weight:700; font-size:15px; }
.mvp .clip .play { display:grid; place-items:center; width:38px; height:38px; border-radius:6px; background:#222;
                   border:1px solid #333; font-size:13px; color:#ddd; }
.mvp .clip.locked { color:#cfcfcf; }
.mvp .clip small { display:block; color:var(--muted); font-weight:500; font-size:12px; }
.leaders { display:grid; gap:10px; grid-auto-rows:1fr; }
.leader { position:relative; display:flex; align-items:center; gap:14px; background:#161616; border:1px solid #262626;
          border-radius:8px; padding:14px 16px 14px 18px; }
.leader::before { content:""; position:absolute; left:0; top:14px; bottom:14px; width:3px; border-radius:0 3px 3px 0;
                  background:var(--team, #8b5cf6); }
.leader .av.md { width:46px; height:46px; }
.leader .who { flex:1; min-width:0; font-weight:700; font-size:15px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.leader .val { text-align:right; } .leader .val b { display:block; font-size:21px; font-weight:800; }
.leader .val span { font-size:14px; color:#cfcfcf; }

/* tables */
.toolbar { display:flex; gap:8px; flex-wrap:wrap; justify-content:flex-end; margin-bottom:6px; }
.toolbar button { background:var(--panel2); color:var(--text); border:1px solid var(--line); border-radius:6px;
                  padding:6px 12px; font:inherit; font-size:12px; font-weight:700; cursor:pointer; }
.toolbar button.on { background:#3a1a0a; border-color:var(--accent); color:#ffb38a; }
.thead { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin:18px 2px 8px; }
.thead .sc { font-weight:800; font-size:16px; } .thead .sc.w { color:var(--win); }
.thead .nm { font-weight:800; }
.thead .meta { margin-left:auto; color:var(--muted); font-size:12px; display:flex; gap:14px; flex-wrap:wrap; }
.thead .meta b { color:var(--text); }
.scroll { overflow-x:auto; border:1px solid var(--line); border-radius:8px; }
table { border-collapse:collapse; width:100%; min-width:1000px; }
th { background:var(--panel); color:var(--muted); font-size:11px; font-weight:700; text-align:left; padding:9px 8px;
     white-space:nowrap; cursor:pointer; user-select:none; border-bottom:1px solid var(--line); }
th:hover { color:var(--text); } th.on { color:var(--text); }
th.on::after { content:" \\25BE"; } th.on.asc::after { content:" \\25B4"; }
td { padding:8px; white-space:nowrap; border-bottom:1px solid rgba(255,255,255,.04); }
tr.A td { background:var(--rowA); } tr.A:nth-child(even) td { background:var(--rowA2); }
tr.B td { background:var(--rowB); } tr.B:nth-child(even) td { background:var(--rowB2); }
td.pl { font-weight:700; } td.pl > span { display:flex; align-items:center; gap:8px; }
.rt { display:inline-flex; flex-direction:column; gap:3px; }
.rt .v { padding:1px 6px; border-radius:3px; font-weight:800; font-size:12px; width:max-content;
         background:#2a2a2a; border:1px solid #3a3a3a; }
.rt .bar { width:70px; height:3px; background:rgba(255,255,255,.12); border-radius:2px; overflow:hidden; }
.rt .bar i { display:block; height:100%; background:#d8d8d8; }
.rt.hi .v { color:#ffb02e; background:#3a2406; border-color:#6b4510; } .rt.hi .bar i { background:linear-gradient(90deg, #ff7a1a, #ffd23f); }
.rt.good .v { color:#6fdc4b; background:#13280f; border-color:#2b6b3d; } .rt.good .bar i { background:#6fdc4b; }
.rt.ok .v { color:#f2f2f2; } .rt.ok .bar i { background:#fff; }
.rt.poor .v { color:#ff4a4a; background:#2c0f0f; border-color:#6b2b2b; } .rt.poor .bar i { background:#ff3b3b; }
.mvp .badge.hi { color:#ffb02e; background:#3a2406; } .mvp .badge.hi u { background:linear-gradient(90deg, #ff7a1a, #ffd23f); }
.mvp .badge.good { color:#6fdc4b; background:#13280f; } .mvp .badge.good u { background:#6fdc4b; }
.mvp .badge.poor { color:#ff4a4a; background:#2c0f0f; } .mvp .badge.poor u { background:#ff3b3b; }
.rt.r .v { color:var(--lose); background:#331717; border-color:#6b2b2b; } .rt.r .bar i { background:var(--lose); }
.star { color:var(--gold); margin-left:6px; }
.hidden { display:none; }

/* highlights */
h2 { font-size:20px; margin:34px 0 12px; }
.clips { display:grid; grid-template-columns:repeat(auto-fill, minmax(min(320px, 100%), 1fr)); gap:14px; }
.clipcard { background:var(--panel); border:1px solid var(--line); border-radius:10px; overflow:hidden; }
.clipcard video { display:block; width:100%; aspect-ratio:16/9; background:#000; }
.clipcard .info { padding:10px 12px; display:flex; gap:10px; align-items:center; }
.clipcard .info .t { flex:1; min-width:0; }
.clipcard .info b { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.clipcard .info span { color:var(--muted); font-size:12px; }
.clipcard .pts { font-weight:800; color:var(--accent); }
.clipcard .vwrap { position:relative; }
.clipcard .big { position:absolute; top:8px; right:8px; width:36px; height:36px; border:0; border-radius:6px; cursor:pointer;
  background:rgba(0,0,0,.6); color:#fff; font-size:18px; display:grid; place-items:center; opacity:.85; transition:opacity .15s, background .15s; }
.clipcard .big:hover { opacity:1; background:var(--accent); }
.clipcard .info { cursor:pointer; }
.clipcard .info:hover b { color:var(--accent); }
/* theater view: one clip large over the page */
.theater { position:fixed; inset:0; z-index:100; background:rgba(5,6,8,.94); display:none; flex-direction:column;
  align-items:center; justify-content:center; padding:16px; box-sizing:border-box; }
.theater.on { display:flex; }
body.theater-open { overflow:hidden; }
.theater .bar { width:min(100%, calc((100vh - 150px) * 16 / 9), 1500px); display:flex; align-items:center; gap:12px; margin-bottom:10px; }
.theater .bar .who { flex:1; min-width:0; display:flex; align-items:center; gap:10px; }
.theater .bar .who b { display:block; font-size:17px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.theater .bar .who span { color:var(--muted); font-size:13px; }
.theater .bar .count { color:var(--muted); font-size:13px; font-variant-numeric:tabular-nums; white-space:nowrap; }
.theater .tbtn { background:#1d2129; color:#fff; border:1px solid #2c323d; border-radius:6px; height:36px; padding:0 12px;
  cursor:pointer; font:600 13px inherit; white-space:nowrap; }
.theater .tbtn:hover { border-color:var(--accent); color:var(--accent); }
.theater .tbtn.x { width:36px; padding:0; font-size:20px; }
.theater .stage { position:relative; width:min(100%, calc((100vh - 150px) * 16 / 9), 1500px); }
.theater video { display:block; width:100%; aspect-ratio:16/9; background:#000; border-radius:8px; }
.theater .nav { position:absolute; top:50%; transform:translateY(-50%); width:48px; height:72px; border:0; border-radius:8px;
  background:rgba(0,0,0,.55); color:#fff; font-size:30px; cursor:pointer; opacity:.7; transition:opacity .15s, background .15s; }
.theater .nav:hover { opacity:1; background:var(--accent); }
.theater .nav.prev { left:-60px; } .theater .nav.next { right:-60px; }
.theater .nav[disabled] { opacity:.15; cursor:default; background:rgba(0,0,0,.55); }
.theater .hint { color:#5d6573; font-size:12px; margin-top:10px; }
@media (max-width: 1100px) { .theater .nav.prev { left:8px; } .theater .nav.next { right:8px; } .theater .nav { width:40px; height:56px; } }
@media (max-width: 640px) { .theater { padding:8px; } .theater .hint, .theater .bar .count { display:none; }
  .theater .tbtn.fs span { display:none; } }
.playall { margin-left:10px; vertical-align:middle; background:var(--accent); color:#fff; border:0; border-radius:6px;
           padding:6px 14px; font:inherit; font-size:13px; font-weight:700; cursor:pointer; }
.back { text-decoration:none; color:#d0d0d0; } .back:hover { color:#fff; }
.foot { color:var(--muted); font-size:12px; margin-top:30px; line-height:1.6; }

@media (max-width: 860px) {
  .top { grid-template-columns:1fr; }
  .hero .tname { font-size:16px; } .hero .score { font-size:30px; gap:8px; }
  .hero .vs { gap:10px; }
}
@media (max-width: 560px) {
  .mvp { flex-direction:column; padding-bottom:90px; } .mvp .card { width:auto; flex-direction:row; padding:14px; }
  .mvp .body { padding-right:0; }
  .mvp .card .av.lg { width:72px; height:72px; font-size:26px; }
  .mvp .title { font-size:24px; } .mvp .grid { margin-top:22px; gap:20px 16px; }
  .hero .tname { font-size:14px; white-space:normal; }
  .av.tm { width:36px; height:36px; font-size:13px; }
}
"""

JS = """
function sortTable(th) {
  const table = th.closest('table'), body = table.tBodies[0], i = [...th.parentNode.children].indexOf(th);
  const asc = th.classList.contains('on') && !th.classList.contains('asc');
  table.querySelectorAll('th').forEach(h => h.classList.remove('on', 'asc'));
  th.classList.add('on'); if (asc) th.classList.add('asc');
  const key = r => { const c = r.children[i]; const v = c.dataset.v ?? c.textContent; const n = parseFloat(v);
                     return isNaN(n) ? v.toLowerCase() : n; };
  [...body.rows].sort((a, b) => { const x = key(a), y = key(b); return (x > y ? 1 : x < y ? -1 : 0) * (asc ? 1 : -1); })
    .forEach(r => body.appendChild(r));
}
function playAll() { openTheater(0); }

// ---- theater view: a clip large over the page; arrows / keys for the others, plays on to the next one
const T = { i: 0, el: null };
function theaterClips() { return [...document.querySelectorAll('.clipcard')]; }
function openTheater(i, from) {
  const cards = theaterClips();
  if (!cards.length) return;
  if (!T.el) {
    T.el = document.createElement('div');
    T.el.className = 'theater';
    T.el.innerHTML = `<div class="bar"><div class="who"></div><span class="count"></span>
        <button class="tbtn fs" title="Tam ekran (F)">&#x26F6; <span>Tam ekran</span></button>
        <button class="tbtn x" title="Kapat (Esc)">&times;</button></div>
      <div class="stage"><video controls playsinline></video>
        <button class="nav prev" title="Önceki (&larr;)">&#8249;</button><button class="nav next" title="Sonraki (&rarr;)">&#8250;</button></div>
      <div class="hint">&larr; &rarr; klipler &middot; F tam ekran &middot; Esc kapat</div>`;
    document.body.appendChild(T.el);
    const v = T.el.querySelector('video');
    T.el.querySelector('.x').onclick = () => history.state && history.state.theater ? history.back() : closeTheater();
    T.el.querySelector('.fs').onclick = () => (v.requestFullscreen || v.webkitEnterFullscreen || (() => {})).call(v);
    T.el.querySelector('.prev').onclick = () => showClip(T.i - 1);
    T.el.querySelector('.next').onclick = () => showClip(T.i + 1);
    T.el.addEventListener('click', e => { if (e.target === T.el) T.el.querySelector('.x').click(); });
    v.addEventListener('ended', () => { if (T.i < theaterClips().length - 1) showClip(T.i + 1); });
    document.addEventListener('keydown', e => {
      if (!T.el.classList.contains('on') || e.target.closest('input, textarea, select')) return;
      if (e.key === 'Escape' && !document.fullscreenElement) T.el.querySelector('.x').click();
      else if (e.key === 'ArrowRight') { e.preventDefault(); showClip(T.i + 1); }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); showClip(T.i - 1); }
      else if (e.key === 'f' || e.key === 'F') T.el.querySelector('.fs').click();
    });
    window.addEventListener('popstate', () => { if (T.el.classList.contains('on')) closeTheater(); });
  }
  cards.forEach(c => c.querySelector('video').pause());
  if (!T.el.classList.contains('on')) history.pushState({theater: true}, '');
  T.el.classList.add('on');
  document.body.classList.add('theater-open');
  showClip(i, from);
}
function showClip(i, from) {
  const cards = theaterClips();
  if (i < 0 || i >= cards.length) return;
  T.i = i;
  const c = cards[i], small = c.querySelector('video'), v = T.el.querySelector('video');
  v.poster = small.poster || '';
  v.src = small.currentSrc || small.src;
  v.currentTime = from || 0;
  v.play().catch(() => {});
  const av = c.querySelector('.info .av');
  T.el.querySelector('.who').innerHTML = (av ? av.outerHTML : '') + '<div style="min-width:0">' + c.querySelector('.info .t').innerHTML + '</div>';
  T.el.querySelector('.count').textContent = (i + 1) + ' / ' + cards.length;
  T.el.querySelector('.prev').disabled = i === 0;
  T.el.querySelector('.next').disabled = i === cards.length - 1;
}
function closeTheater() {
  if (!T.el) return;
  const v = T.el.querySelector('video');
  v.pause();
  v.removeAttribute('src'); v.load();
  T.el.classList.remove('on');
  document.body.classList.remove('theater-open');
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
}
document.addEventListener('DOMContentLoaded', () => {
  theaterClips().forEach((c, i) => {
    const small = c.querySelector('video');
    c.querySelector('.big').onclick = () => openTheater(i, small.currentTime || 0);
    c.querySelector('.info').onclick = () => openTheater(i, small.currentTime || 0);
  });
  const m = location.hash.match(/^#izle([0-9]+)$/);
  if (m) openTheater(+m[1] - 1);
});
function view(mode) {
  document.querySelectorAll('[data-view]').forEach(e => e.classList.toggle('hidden', e.dataset.view !== mode));
  document.querySelectorAll('.toolbar button').forEach(b => b.classList.toggle('on', b.dataset.mode === mode));
}
"""

COLS = [  # (header, tooltip, key)
    ("Oyuncu", "", "name"), ("Puan", "HLTV Rating 2.0 formülü (topluluk yaklaşımı); 1.00 = ortalama", "rating"),
    ("Swing", "Oyuncunun takımının round kazanma şansını round başına ortalama ne kadar değiştirdiği", "swing"),
    ("K", "Kill", "kills"), ("Ö", "Ölüm", "deaths"), ("A", "Asist", "assists"),
    ("ADR", "Round başına ortalama hasar", "adr"), ("K/Ö", "Kill / ölüm", "kd"), ("K/R", "Round başına kill", "kpr"),
    ("Kafa", "Kafa atışıyla kill", "hs"), ("Kafa%", "Kafa atışı oranı", "hs_pct"),
    ("KAST", "Kill, asist, hayatta kalma veya trade olan roundlar", "kast"),
    ("Açılış K", "Roundun ilk killi", "opening"),
    ("5K", "Ace", "m5"), ("4K", "4 killi roundlar", "m4"), ("3K", "3 killi roundlar", "m3"),
    ("2K", "2 killi roundlar", "m2"), ("EDO", "Round MVP'leri", "mvps"),
]

TR_MONTHS = ["Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"]


def tr_date(iso):
    """2026-11-04 -> 4 Kas 2026"""
    try:
        y, m, d = (int(x) for x in str(iso)[:10].split("-"))
        return f"{d} {TR_MONTHS[m - 1]} {y}"
    except (ValueError, IndexError):
        return str(iso)


def esc(s):
    return html.escape(str(s))


def initials(name):
    words = re.findall(r"\w+", name)
    if not words:
        return esc(name.strip()[:1] or "?")
    return esc((words[0][0] + (words[1][0] if len(words) > 1 else words[0][1:2])).upper())


def band(r):
    """FACEIT rating bands: 1.80+ high impact, 1.30-1.79 good, 0.90-1.29 okay, below poor."""
    return "hi" if r >= 1.8 else "good" if r >= 1.3 else "ok" if r >= 0.9 else "poor"


def rating_html(r, best=False):
    if r is None:
        return '<span class="dim">&ndash;</span>'
    cls = band(r)
    star = '<span class="star" title="Maçın en yüksek puanı">&#9733;</span>' if best else ""
    return (f'<span class="rt {cls}"><span class="v">{r:.2f}</span><span class="bar">'
            f'<i style="width:{max(4, min(100, r / 2 * 100)):.0f}%"></i></span></span>{star}')


def summary(data):
    """Short version for match lists (match.json / database columns)."""
    pl = data["players"]
    best = max(pl, key=lambda s: pl[s]["rating"]) if pl else None
    clips = data["clips"]
    return {"map": data["map"], "date": data["date"], "rounds": data["rounds"],
            "teams": [{"name": t["name"], "score": t["score"]} for t in data["teams"]],
            "mvp": {"name": pl[best]["name"] if best else "", "rating": round(pl[best]["rating"], 2) if best else 0,
                    "avatar": data["pics"].get(best, "") if best else ""},
            "thumb": next((c["thumb2"] or c["thumb"] for c in clips if c.get("thumb2") or c.get("thumb")), ""),
            "clips": len(clips), "page": "scoreboard.html"}


def render(data, base="", back="../index.html", extra_head="", back_label="Tüm maçlar"):
    """HTML page for collect()'s data. `base` is prepended to media paths (clips, thumbnails, pictures).
    Stats a match doesn't have (None, e.g. rating/swing/KAST for 5v5stats matches) are left out."""
    pl, teams, clips, pics = data["players"], data["teams"], data["clips"], data["pics"]
    rank_key = "rating" if all(pl[s].get("rating") is not None for s in pl) else "adr"
    rank = lambda s: pl[s].get(rank_key) or 0  # noqa: E731
    best_sid = max(pl, key=rank)
    map_name, date = data["map"], data["date"]
    cols = [c for c in COLS if c[2] == "name" or any(pl[s].get(c[2]) is not None for s in pl)]

    def avatar(sid, name, size=""):
        if str(sid) in pics:
            src = pics[str(sid)]
            src = src if src.startswith(("http://", "https://")) else base + src
            return f'<img class="av {size}" src="{esc(src)}" alt="" loading="lazy">'
        hue = int(hashlib.md5(str(sid).encode()).hexdigest()[:4], 16) % 360
        return (f'<span class="av {size}" style="background:linear-gradient(135deg,hsl({hue} 55% 42%),'
                f'hsl({(hue + 40) % 360} 60% 28%))">{initials(name)}</span>')

    def media(rel):
        return esc(base + urllib.parse.quote(rel))

    def row(sid):
        v = pl[sid]
        fmts = {"kills": "{}", "deaths": "{}", "assists": "{}", "adr": "{:.1f}", "kd": "{:.2f}", "kpr": "{:.2f}",
                "hs": "{}", "hs_pct": "{:.1f}%", "kast": "{:.1f}%", "opening": "{}", "m5": "{}", "m4": "{}",
                "m3": "{}", "m2": "{}", "mvps": "{}"}
        cells = []
        for _, _, key in cols:
            x = v.get(key)
            if key == "name":
                cells.append(f'<td class="pl" data-v="{esc(v["name"].lower())}"><span>{avatar(sid, v["name"])}'
                             f'{esc(v["name"])}</span></td>')
            elif x is None:
                cells.append('<td data-v="-1" class="dim">&ndash;</td>')
            elif key == "rating":
                cells.append(f'<td data-v="{x:.3f}">{rating_html(x, sid == best_sid)}</td>')
            elif key == "swing":
                cells.append(f'<td data-v="{x:.3f}" class="{"pos" if x >= 0 else "neg"}">{x:+.2f}%</td>')
            else:
                dim = ' class="dim"' if key in ("m5", "m4", "m3", "m2", "mvps", "opening") and not x else ""
                cells.append(f'<td data-v="{x}"{dim}>{fmts[key].format(x)}</td>')
        return f'<tr class="{"AB"[v["team"]]}">' + "".join(cells) + "</tr>"

    head = "".join(f'<th title="{esc(t)}" class="{"on" if k == rank_key else ""}" onclick="sortTable(this)">{h}</th>'
                   for h, t, k in cols)

    def table(sids):
        sids = sorted(sids, key=lambda s: -rank(s))
        return (f'<div class="scroll"><table class="num"><thead><tr>{head}</tr></thead>'
                f'<tbody>{"".join(row(s) for s in sids)}</tbody></table></div>')

    team_blocks = []
    for k, t in enumerate(teams):
        h1, h2, ot = t.get("halves") or (None, None, None)
        halves = (f"<span>İlk yarı <b>{h1}</b></span><span>İkinci yarı <b>{h2}</b></span>"
                  + (f"<span>Uzatma <b>{ot}</b></span>" if ot else "")) if h1 is not None else ""
        avg = (f"<span>Takım ortalaması <b>{t['avg_rating']:.2f}</b></span>"
               if t.get("avg_rating") is not None else "")
        team_blocks.append(f"""
<div class="thead"><span class="sc {"w" if k == 0 else ""}">{t["score"]}</span><span class="nm">{esc(t["name"])}</span>
  <span class="meta">{avg}{halves}</span></div>
{table(t["players"])}""")

    # hero: a frame from the match as background, teams and score with each team's top player
    bg = next((c["thumb2"] or c["thumb"] for c in clips if c.get("thumb2") or c.get("thumb")), "")
    bg_style = f' style="background-image:url(\'{media(bg)}\')"' if bg else ""
    vs = ""
    if len(teams) == 2 and all(t["players"] for t in teams):
        a, b = teams
        sa = max(a["players"], key=rank)
        sb = max(b["players"], key=rank)
        vs = f"""<div class="vs">
  <div class="team l"><div><div class="tag">GALİP</div><div class="tname">{esc(a["name"])}</div></div>{avatar(sa, pl[sa]["name"], "tm")}</div>
  <div class="score num"><span class="w">{a["score"]}</span><small>VS</small><span class="l">{b["score"]}</span></div>
  <div class="team">{avatar(sb, pl[sb]["name"], "tm")}<div><div class="tag" style="visibility:hidden">-</div><div class="tname">{esc(b["name"])}</div></div></div>
</div>"""

    # MVP card (best rating) with their best rendered clip
    team_color = {0: "#22d3c5", 1: "#8b5cf6"}  # winners teal, losers purple (FACEIT-like)
    tiers = data.get("tiers") or {}            # website only: {steamid: {"tier": 1-10, "points": 1.00-scale}}
    mvp = pl[best_sid]
    mclip = max((c for c in clips if c.get("steamid") == best_sid), key=lambda c: c["score"], default=None)
    if mclip:
        clip_link = (f'<a class="clip" href="#clip{clips.index(mclip) + 1}" '
                     f'onclick="openTheater({clips.index(mclip)}); return false"><span class="play">&#9654;</span>'
                     f'<span>Öne çıkan anlar<small>{esc(mclip["label"])} &middot; {mclip["round"]}. round</small></span></a>')
    else:
        clip_link = '<span class="clip locked"><span class="play">&#128274;</span><span>Öne çıkan an yok</span></span>'
    sw = mvp.get("swing")
    if mvp.get("rating") is not None:
        bar = max(4, min(100, mvp["rating"] / 2 * 100))
        badge = (f'<span class="badge num {band(mvp["rating"])}">{mvp["rating"]:.2f}<u style="width:calc({bar:.0f}% - 12px)"></u></span>'
                 f'<span class="rlbl"><i></i>Maç puanı</span>')
        first = (f'<div><b>{sw:+.2f}%</b><span>Değişim</span></div>' if sw is not None else
                 f'<div><b>{mvp["kd"]:.2f}</b><span>K/Ö</span></div>')
    else:  # no rating for this match: the best ADR is the MVP
        bar = max(4, min(100, mvp["adr"] / 150 * 100))
        badge = (f'<span class="badge num">{mvp["adr"]:.0f}<u style="width:calc({bar:.0f}% - 12px)"></u></span>'
                 f'<span class="rlbl"><i></i>ADR, maçın en iyisi</span>')
        first = f'<div><b>{mvp["kd"]:.2f}</b><span>K/Ö</span></div>' if mvp.get("kd") is not None else ""
    t = tiers.get(best_sid) or {}
    lvl = (f'<span class="lvl num"><i>{t["tier"] or "?"}</i>{t["points"]:.2f}</span>' if t.get("points") is not None else "")
    mvp_html = f"""<div class="mvp" style="--team:{team_color[mvp['team']]}">
  <div class="card">{avatar(best_sid, mvp["name"], "lg")}<div class="nm">{esc(mvp["name"])}</div>{lvl}</div>
  <div class="body">
    <div>{badge}</div>
    <div class="grid num">
      {first}
      <div><b>{mvp["adr"]:.1f}</b><span>ADR</span></div>
      <div><b>{mvp["kills"]}/{mvp["deaths"]}/{mvp["assists"]}</b><span>K/Ö/A</span></div>
      <div><b>{mvp["hs_pct"]:.1f}%</b><span>Headshot yüzdesi</span></div>
    </div>
  </div>
  {clip_link}
  <div class="title"><i>&#9733;</i>EDO</div>
</div>"""

    def leader(label, fmt, key):
        if all(pl[s].get(key) in (None, 0) for s in pl):
            return ""  # not known for this match
        sid = max(pl, key=lambda s: pl[s].get(key) or 0)
        return (f'<div class="leader" style="--team:{team_color[pl[sid]["team"]]}">{avatar(sid, pl[sid]["name"], "md")}'
                f'<span class="who">{esc(pl[sid]["name"])}</span>'
                f'<span class="val num"><b>{fmt.format(pl[sid][key])}</b><span>{label}</span></span></div>')

    cards = [c for c in (leader("En yüksek kill", "{}", "kills"), leader("En fazla hasar", "{:.0f}", "damage"),
                         leader("KAST", "{:.1f}%", "kast"), leader("İlk öldürmeler", "{}", "opening")) if c]
    for extra in (leader("En yüksek ADR", "{:.1f}", "adr"), leader("En çok asist", "{}", "assists")):
        if len(cards) < 4 and extra:  # matches without KAST / opening kills (5v5stats) still show four cards
            cards.append(extra)
    leaders = "".join(cards)

    clip_cards = ""
    if clips:
        cards = []
        for i, c in enumerate(clips, 1):
            poster = f' poster="{media(c["thumb"])}"' if c.get("thumb") else ""
            cards.append(f"""<div class="clipcard" id="clip{i}">
  <div class="vwrap"><video controls preload="none"{poster} src="{media(c["file"])}"></video>
    <button class="big" type="button" title="Büyük izle">&#x26F6;</button></div>
  <div class="info">{avatar(c.get("steamid", ""), c["player"], "md")}<div class="t"><b>{esc(c["player"])} &middot; {esc(c["label"])}</b>
    <span>{c["round"]}. round</span></div><span class="pts num" title="Klip puanı">{c["score"]} puan</span></div>
</div>""")
        play_all = '<button class="playall" onclick="playAll()">&#9654; Tümünü oynat</button>' if len(clips) > 1 else ""
        clip_cards = f'<h2>Maç özetleri {play_all}</h2><div class="clips">{"".join(cards)}</div>'
    elif data.get("note"):
        clip_cards = f'<h2>Maç özetleri</h2><p class="dim">{esc(data["note"])}</p>'

    back_link = f'<a class="back" href="{esc(back)}">&larr; {esc(back_label)}</a> &nbsp;' if back else ""
    return f"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(map_name)} maç özeti</title><style>{CSS}</style>{extra_head}</head>
<body><div class="wrap">
  <div class="hero"{bg_style}>
    <div class="meta"><span>{back_link}{esc(map_name)}</span><span>{esc(tr_date(date))} &middot; {data["rounds"]} round</span></div>
    {vs}
  </div>
  <div class="tabs"><span class="on">Özet</span></div>
  <div class="top">{mvp_html}<div class="leaders">{leaders}</div></div>
  <div class="toolbar"><button class="on" data-mode="team" onclick="view('team')">Takıma göre sırala</button>
    <button data-mode="all" onclick="view('all')">Oyunculara göre sırala</button></div>
  <div data-view="team">{"".join(team_blocks)}</div>
  <div data-view="all" class="hidden" style="margin-top:12px">{table(list(pl))}</div>
  {clip_cards}
  <div class="foot">
    <b>Puan</b>: HLTV Rating 2.0 formülü (topluluk yaklaşımı): KAST, kill, ölüm, asist ve ADR'den; 1.00 ortalamadır.<br>
    <b>Swing</b>: oyuncunun takımının round kazanma şansını round başına ortalama ne kadar değiştirdiği (cs2render kazanma olasılığı modeli).<br>
    <b>KAST</b>: kill, asist, hayatta kalma veya 5 sn içinde trade edilme olan roundlar. &nbsp; <b>ADR</b>: round başına gerçekten verilen hasar.
    &nbsp; <b>Açılış K</b>: roundun ilk killi. &nbsp; Sıralamak için bir sütuna tıkla.
  </div>
</div><script>{JS}</script></body></html>"""
