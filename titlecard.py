"""FACEIT-style title card for a highlight clip: Steam avatar, level badge, name, the play, round / map.

render.py draws one PNG per clip with make_card() and FFmpeg slides it in at the start of the clip
(animate_filter()). Needs Pillow (pip install pillow); without it render.py falls back to the plain text title.

Levels (1-10, the site's tiers) and ratings come from the website's /api/levels (SERVER_URL in upload.env,
default the local site); if it can't be reached the card is drawn without a level.
"""
import json
import os
import re
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
FONTS = r"C:\Windows\Fonts"
ACCENT = (255, 85, 0)
# FACEIT's level colours (same as the website's level rings, web/matchpage.py LEVEL_COLORS)
LEVEL_COLORS = {10: "#fe1f00", 9: "#ff6309", 8: "#ff6309", 7: "#ffc800", 6: "#ffc800", 5: "#ffc800", 4: "#ffc800",
                3: "#1ce400", 2: "#1ce400", 1: "#eeeeee"}
MAP_NAMES = {"de_dust2": "Dust II", "de_inferno": "Inferno", "de_mirage": "Mirage", "de_nuke": "Nuke",
             "de_anubis": "Anubis", "de_ancient": "Ancient", "de_vertigo": "Vertigo", "de_overpass": "Overpass",
             "de_train": "Train", "de_cache": "Cache"}

# card timing in the clip (seconds)
SHOW_AT, SLIDE, HOLD_UNTIL, FADE = 0.25, 0.45, 3.6, 0.5

_levels = None


def available():
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


def map_label(demo_name):
    m = re.search(r"de_[a-z0-9]+", demo_name or "")
    if not m:
        return ""
    return MAP_NAMES.get(m.group(0), m.group(0)[3:].capitalize())


def levels():
    """{steamid: {"tier": 1-10 or None, "points": rating}} from the website; {} if it can't be reached."""
    global _levels
    if _levels is None:
        url = "http://localhost:5000"
        env = os.path.join(ROOT, "upload.env")
        if os.path.isfile(env):
            for line in open(env, encoding="utf-8"):
                if line.strip().startswith("SERVER_URL=") and line.split("=", 1)[1].strip():
                    url = line.split("=", 1)[1].strip().rstrip("/")
        try:
            with urllib.request.urlopen(urllib.request.Request(f"{url}/api/levels", headers={"User-Agent": "cs2render"}),
                                        timeout=6) as r:
                _levels = json.loads(r.read().decode("utf-8"))
        except Exception:
            _levels = {}
    return _levels


def _font(text, size, weight="Bold"):
    from PIL import ImageFont
    if any(ord(ch) > 0x24F for ch in text):  # Japanese / Chinese / Korean names: Bahnschrift has none
        return ImageFont.truetype(os.path.join(FONTS, "YuGothB.ttc"), int(size * 0.92))
    f = ImageFont.truetype(os.path.join(FONTS, "bahnschrift.ttf"), size)
    try:
        f.set_variation_by_name(weight)
    except Exception:
        pass
    return f


_MISSING = {}


def _has_glyph(font, ch):
    """False for characters the font would draw as an empty box (e.g. ✪ in Bahnschrift)."""
    if ch.isspace():
        return True
    key = id(font)
    if key not in _MISSING:
        _MISSING[key] = bytes(font.getmask(""))  # a private-use character no font has
    return bytes(font.getmask(ch)) != _MISSING[key]


def _runs(text, font, size):
    """Split text into (piece, font) runs: symbols the main font lacks use Segoe UI Symbol."""
    from PIL import ImageFont
    sym = None
    runs = []
    for ch in text:
        f = font
        if not _has_glyph(font, ch):
            sym = sym or ImageFont.truetype(os.path.join(FONTS, "seguisym.ttf"), int(size * 0.9))
            f = sym
        if runs and runs[-1][1] is f:
            runs[-1][0] += ch
        else:
            runs.append([ch, f])
    return runs


def _text_len(draw, text, font, size):
    return sum(draw.textlength(t, font=f) for t, f in _runs(text, font, size))


def _draw_text(draw, xy, text, font, size, fill):
    x, y = xy
    asc = font.getmetrics()[0]
    for t, f in _runs(text, font, size):
        dy = asc - f.getmetrics()[0] if f is not font else 0  # line up the baselines
        draw.text((x, y + dy), t, font=f, fill=fill)
        x += draw.textlength(t, font=f)


def _hex(c, a=255):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4)) + (a,)


def _spaced(draw, xy, text, font, fill, spacing):
    """Text with extra letter spacing (FACEIT's small caps labels)."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + spacing
    return x


def _spaced_len(draw, text, font, spacing):
    return sum(draw.textlength(ch, font=font) + spacing for ch in text) - spacing if text else 0


def make_card(path, name, steamid, label, round_no, demo_name="", height=1080):
    """Write the card PNG (transparent around it). Returns (path, width, height) in pixels."""
    from PIL import Image, ImageDraw, ImageFilter

    import avatars
    s = height / 1080
    S = lambda v: int(round(v * s))  # noqa: E731
    lv = levels().get(str(steamid)) or {}
    tier, points = lv.get("tier"), lv.get("points")

    f_name = _font(name, S(46), "Bold")
    f_play = _font(label, S(27), "Bold SemiCondensed")
    f_meta = _font("x", S(17), "SemiBold")
    meta = "  ·  ".join(x for x in [f"ROUND {round_no}", map_label(demo_name).upper(),
                                     f"{points:.2f} RATING" if points else ""] if x)

    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    av, pad, gap = S(112), S(18), S(22)
    badge = S(25) if tier else 0
    name_w = _text_len(probe, name, f_name, S(46))
    text_w = max(name_w + (S(16) + 2 * badge + S(8) if tier else 0), _spaced_len(probe, label.upper(), f_play, S(1.5)),
                 _spaced_len(probe, meta, f_meta, S(2.2)))
    w = S(8) + pad + av + gap + int(text_w) + S(34)
    h = av + 2 * pad
    shadow = S(26)

    img = Image.new("RGBA", (w + 2 * shadow, h + 2 * shadow), (0, 0, 0, 0))
    # soft shadow
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle((shadow, shadow + S(6), shadow + w, shadow + h + S(6)), S(12), fill=(0, 0, 0, 150))
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(S(14))))
    # panel: dark glass with a faint orange glow from the left
    panel = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    grad = Image.new("RGBA", (w, h))
    gd = ImageDraw.Draw(grad)
    for x in range(w):
        t = x / max(1, w - 1)
        glow = max(0.0, 1 - t * 2.2)
        gd.line([(x, 0), (x, h)], fill=(int(14 + 40 * glow), int(17 + 12 * glow), int(23 + 2 * glow), 235))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), S(12), fill=255)
    panel.paste(grad, (0, 0), mask)
    pd = ImageDraw.Draw(panel)
    pd.rounded_rectangle((0, 0, w - 1, h - 1), S(12), outline=(255, 255, 255, 28), width=max(1, S(1)))
    # orange accent bar on the left edge
    bar = Image.new("L", (w, h), 0)
    ImageDraw.Draw(bar).rounded_rectangle((0, 0, S(24), h - 1), S(12), fill=255)
    ImageDraw.Draw(bar).rectangle((S(8), 0, S(24), h), fill=0)
    panel.paste(Image.new("RGBA", (w, h), ACCENT + (255,)), (0, 0), bar)

    # avatar (rounded square)
    ax, ay = S(8) + pad, pad
    src = avatars.fetch(steamid)
    if src:
        pic = Image.open(src).convert("RGBA").resize((av, av), Image.LANCZOS)
    else:
        pic = Image.new("RGBA", (av, av), (38, 48, 62, 255))
        ini = _font(name[:1].upper() or "?", S(54), "Bold")
        ImageDraw.Draw(pic).text((av / 2, av / 2), name[:1].upper() or "?", font=ini, fill=(201, 209, 220, 255), anchor="mm")
    am = Image.new("L", (av, av), 0)
    ImageDraw.Draw(am).rounded_rectangle((0, 0, av - 1, av - 1), S(10), fill=255)
    panel.paste(pic, (ax, ay), am)
    pd.rounded_rectangle((ax, ay, ax + av - 1, ay + av - 1), S(10), outline=(255, 255, 255, 40), width=max(1, S(2)))
    tx = ax + av + gap
    if tier:  # FACEIT-style level ring next to the name
        r = badge
        cx, cy = tx + int(name_w) + S(16) + r, pad + S(27)
        col = _hex(LEVEL_COLORS.get(tier, "#8a94a3"))
        pd.ellipse((cx - r - S(4), cy - r - S(4), cx + r + S(4), cy + r + S(4)), fill=(10, 13, 18, 255))
        pd.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(22, 27, 36, 255))
        # FACEIT-style ring: filled share = level / 10
        pd.arc((cx - r, cy - r, cx + r, cy + r), 0, 360, fill=(255, 255, 255, 30), width=S(5))
        pd.arc((cx - r, cy - r, cx + r, cy + r), 135, 135 + 270 * tier / 10, fill=col, width=S(5))
        pd.text((cx, cy + S(1)), str(tier), font=_font("1", S(25), "Bold"), fill=col, anchor="mm")

    # text
    _draw_text(pd, (tx, pad + S(2)), name, f_name, S(46), (255, 255, 255, 255))
    _spaced(pd, (tx, pad + S(58)), label.upper(), f_play, ACCENT + (255,), S(1.5))
    _spaced(pd, (tx, pad + S(92)), meta, f_meta, (150, 160, 175, 255), S(2.2))

    img.alpha_composite(panel, (shadow, shadow))
    img.save(path)
    return path, img.width, img.height


def overlay_filter(card_w, video_h=1080):
    """FFmpeg overlay x/y for the card: slides in from the left, then (with the fade on the card input) fades.
    Position: left side, a bit above the middle (clear of the radar, kill feed and the bottom HUD)."""
    s = video_h / 1080
    x0, y = int(40 * s) - int(26 * s), int(video_h * 0.60)
    a, b = SHOW_AT, SHOW_AT + SLIDE
    x = (f"if(lt(t,{a}),-w,if(lt(t,{b}),{x0}-({x0}+w)*pow(1-(t-{a})/{SLIDE},3),{x0}))")
    return f"overlay=x='{x}':y={y}:shortest=1:format=auto"


def fade_filter():
    return f"format=rgba,fade=t=out:st={HOLD_UNTIL}:d={FADE}:alpha=1"


# ending: the last OUTRO seconds of a clip. The footage keeps playing while a small server badge slides in
# (same place and motion as the title card) and picture and sound fade out over the final END_FADE seconds.
OUTRO, OUTRO_SLIDE, END_FADE = 1.8, 0.4, 0.8


def make_outro(path, height=1080):
    """Write the end badge PNG: '5V5 TURKISH SERVER' with the orange accent bar, like a small title card."""
    from PIL import Image, ImageDraw, ImageFilter
    s = height / 1080
    S = lambda v: int(round(v * s))  # noqa: E731
    f_big, f_small = _font("x", S(34), "Bold"), _font("x", S(15), "SemiBold")
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    a_w = probe.textlength("5V5 ", font=f_big)
    b_w = probe.textlength("TURKISH SERVER", font=f_big)
    sub = "HIGHLIGHTS  ·  5V5STATS.COM"
    pad = S(18)
    w = S(8) + pad + int(max(a_w + b_w, _spaced_len(probe, sub, f_small, S(2.4)))) + S(30)
    h = S(84)
    shadow = S(26)
    img = Image.new("RGBA", (w + 2 * shadow, h + 2 * shadow), (0, 0, 0, 0))
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle((shadow, shadow + S(6), shadow + w, shadow + h + S(6)), S(12), fill=(0, 0, 0, 150))
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(S(14))))
    panel = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), S(12), fill=255)
    panel.paste(Image.new("RGBA", (w, h), (14, 17, 23, 235)), (0, 0), mask)
    pd = ImageDraw.Draw(panel)
    pd.rounded_rectangle((0, 0, w - 1, h - 1), S(12), outline=(255, 255, 255, 28), width=max(1, S(1)))
    bar = Image.new("L", (w, h), 0)
    ImageDraw.Draw(bar).rounded_rectangle((0, 0, S(24), h - 1), S(12), fill=255)
    ImageDraw.Draw(bar).rectangle((S(8), 0, S(24), h), fill=0)
    panel.paste(Image.new("RGBA", (w, h), ACCENT + (255,)), (0, 0), bar)
    tx = S(8) + pad
    pd.text((tx, S(10)), "5V5 ", font=f_big, fill=(255, 255, 255, 255))
    pd.text((tx + a_w, S(10)), "TURKISH SERVER", font=f_big, fill=ACCENT + (255,))
    _spaced(pd, (tx, S(54)), sub, f_small, (150, 160, 175, 255), S(2.4))
    img.alpha_composite(panel, (shadow, shadow))
    img.save(path)
    return path


def outro_filter(duration, video_h=1080):
    """FFmpeg overlay for the end badge: slides in from the left OUTRO seconds before the end."""
    s = video_h / 1080
    x0, y = int(40 * s) - int(26 * s), int(video_h * 0.60)
    a = max(0.0, duration - OUTRO)
    x = f"if(lt(t,{a:.3f}),-w,if(lt(t,{a + OUTRO_SLIDE:.3f}),{x0}-({x0}+w)*pow(1-(t-{a:.3f})/{OUTRO_SLIDE},3),{x0}))"
    return f"overlay=x='{x}':y={y}:shortest=1:format=auto"
