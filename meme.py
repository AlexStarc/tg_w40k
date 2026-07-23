"""Crisiswoman meme generator: literary quote + punchline over a stock photo.

Style system shuffles fonts, colours, layout and text arrangement on each run.
No author attribution is ever rendered.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import random
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).parent
BANK_PATH = HERE / "bank.json"
SEED_PATH = HERE / "bank.seed.json"
FONTS_DIR = HERE / "fonts"
# small rotating on-disk cache of generated memes (history/inspection only,
# gitignored; publishing is file_id-based and does not depend on it)
CACHE_DIR = HERE / "memes_cache"
CACHE_KEEP = 50

CANVAS_W, CANVAS_H = 1080, 1350
MARGIN = 80

# --- bundled OFL fonts (Cyrillic). spec = (path, variation-name-or-None) ---
def _fp(name: str) -> str:
    return str(FONTS_DIR / name)


SERIFS = [
    (_fp("PlayfairDisplay-Italic.ttf"), "Bold Italic"),
    (_fp("PTSerif-Italic.ttf"), None),
    (_fp("PTSerif-BoldItalic.ttf"), None),
]
SANS = [
    (_fp("PTSans-Bold.ttf"), None),
    (_fp("Oswald.ttf"), "Bold"),
    (_fp("Montserrat.ttf"), "ExtraBold"),
]
HAND = [
    (_fp("Caveat.ttf"), "Bold"),
    (_fp("BadScript.ttf"), None),
]

CREAM = (242, 230, 216)
WHITE = (255, 255, 255)
DIM = (200, 188, 170)
BLACK = (0, 0, 0)

# moody search tags — real photos only, never AI
BG_TAGS = [
    "fog", "rain window", "empty room", "lonely street", "night city",
    "grey sky", "empty bed", "kitchen night", "subway", "clouds",
    "abandoned", "dark forest", "ocean grey", "desert road", "empty cafe",
    "neon night", "snow", "old building", "tunnel", "wet asphalt",
]

LAYOUTS = ["bottom", "bars", "poster", "split", "minimal", "hand"]


@dataclass
class Entry:
    id: str
    source: str
    quote: str
    punchline: str
    tone: str = "light"


@dataclass
class Style:
    layout: str
    quote_font: str
    punch_font: str
    quote_color: tuple
    punch_color: tuple
    quote_stroke: int = 0
    punch_stroke: int = 0
    quote_size: int = 40
    punch_size: int = 70
    accent: tuple = field(default_factory=lambda: (0, 0, 0))
    effect: str | None = None


# ----------------------------------------------------------------- helpers
def _font(spec, size: int) -> ImageFont.FreeTypeFont:
    path, variation = spec
    f = ImageFont.truetype(path, size)
    if variation:
        try:
            f.set_variation_by_name(variation)
        except Exception:
            pass
    return f


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if font.getlength(trial) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def _fit(text, spec, max_w, max_lines, start, stop, step=2):
    for size in range(start, stop - 1, -step):
        f = _font(spec, size)
        lines = _wrap(text, f, max_w)
        if len(lines) <= max_lines:
            return f, lines, size
    f = _font(spec, stop)
    return f, _wrap(text, f, max_w), stop


def _cover(im: Image.Image) -> Image.Image:
    im = im.convert("RGB")
    s = im.width / im.height
    tw, th = CANVAS_W, CANVAS_H
    t = tw / th
    if s > t:
        new_h, new_w = th, int(th * s)
    else:
        new_w, new_h = tw, int(tw / s)
    im = im.resize((new_w, new_h), Image.LANCZOS)
    left = (im.width - tw) // 2
    t2 = (im.height - th) // 2
    return im.crop((left, t2, left + tw, t2 + th))


def _vgrad(top: float, strength: int, power=1.4, reverse=False) -> Image.Image:
    """Vertical alpha gradient overlay (black). top = fraction where ramp starts."""
    ov = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    rng = range(CANVAS_H - 1, -1, -1) if reverse else range(CANVAS_H)
    for y in rng:
        t = max(0.0, (y - CANVAS_H * top) / (CANVAS_H * (1 - top)))
        if reverse:
            t = max(0.0, ((CANVAS_H * (1 - top)) - y) / (CANVAS_H * (1 - top)))
        a = int(strength * (t ** power))
        d.line([(0, y), (CANVAS_W, y)], fill=(0, 0, 0, a))
    return ov


def _solid_bar(y0, y1) -> Image.Image:
    return Image.new("RGBA", (CANVAS_W, y1 - y0), (0, 0, 0, 235))


def _block(draw, lines, font, top_y, line_h, fill, stroke=0, sfill=BLACK,
           align="center", left_x=MARGIN):
    y = top_y
    for ln in lines:
        x = (CANVAS_W - font.getlength(ln)) / 2 if align == "center" else left_x
        draw.text((x, y), ln, font=font, fill=fill,
                  stroke_width=stroke, stroke_fill=sfill)
        y += line_h


def _block_h(lines, font, line_h):
    return line_h * len(lines)


# ----------------------------------------------------------------- styles
def _bias_pick(items, key_fn, avg_map, explore=0.2, default=3.0,
               low_cutoff=2.5, min_keep=6):
    """Weighted random pick favouring higher-rated keys. Low-rated keys are
    excluded once enough alternatives exist; `explore` fraction is pure random
    so the pool never collapses to a single winner."""
    excluded = {k for k, v in avg_map.items() if v and v < low_cutoff}
    pool = [it for it in items if key_fn(it) not in excluded] or list(items)
    if len(pool) < min_keep:
        pool = list(items)
    if not avg_map or random.random() < explore:
        return random.choice(pool)
    weights = [max(0.1, avg_map.get(key_fn(it), default)) for it in pool]
    return random.choices(pool, weights=weights, k=1)[0]


def pick_style(force_layout: str | None = None, layout_avg: dict | None = None) -> Style:
    if force_layout:
        layout = force_layout
    elif layout_avg:
        layout = _bias_pick(LAYOUTS, lambda x: x, layout_avg, explore=0.2)
    else:
        layout = random.choice(LAYOUTS)
    if layout == "hand":
        return Style("hand", random.choice(HAND), random.choice(HAND),
                     WHITE, CREAM, quote_stroke=2, punch_stroke=3,
                     quote_size=46, punch_size=66, accent=(60, 30, 40))
    if layout == "bars":
        return Style("bars", random.choice(SERIFS), random.choice(SANS),
                     WHITE, WHITE, quote_stroke=0, punch_stroke=2,
                     quote_size=50, punch_size=72)
    if layout == "poster":
        return Style("poster", random.choice(SERIFS), random.choice(SANS),
                     CREAM, WHITE, quote_stroke=1, punch_stroke=3,
                     quote_size=56, punch_size=80)
    if layout == "split":
        return Style("split", random.choice(SERIFS), random.choice(SANS),
                     CREAM, WHITE, quote_stroke=1, punch_stroke=2,
                     quote_size=52, punch_size=68)
    if layout == "minimal":
        return Style("minimal", random.choice(SERIFS), random.choice(SANS),
                     CREAM, WHITE, quote_stroke=2, punch_stroke=3,
                     quote_size=52, punch_size=74)
    # bottom (default)
    return Style("bottom", random.choice(SERIFS), random.choice(SANS),
                 CREAM, WHITE, quote_stroke=1, punch_stroke=2,
                 quote_size=56, punch_size=72)


# ----------------------------------------------------------------- layouts
def _place_bottom(base, e, s):
    base = Image.alpha_composite(base, _vgrad(0.48, 205))
    d = ImageDraw.Draw(base, "RGBA")
    mw = CANVAS_W - 2 * MARGIN
    pf, pl, _ = _fit(e.punchline, s.punch_font, mw, 3, s.punch_size, 44)
    qf, ql, _ = _fit(f"«{e.quote}»", s.quote_font, mw, 3, s.quote_size, 36)
    plh = pf.getbbox("Ag")[3] + 14
    qlh = qf.getbbox("Ag")[3] + 14
    p_bottom = CANVAS_H - 96
    p_top = p_bottom - _block_h(pl, pf, plh)
    rule_y = p_top - 42
    q_bottom = rule_y - 18
    q_top = q_bottom - _block_h(ql, qf, qlh)
    _block(d, ql, qf, q_top, qlh, s.quote_color, s.quote_stroke)
    d.line([CANVAS_W / 2 - 110, rule_y, CANVAS_W / 2 + 110, rule_y],
           fill=(242, 230, 216, 90), width=2)
    _block(d, pl, pf, p_top, plh, s.punch_color, s.punch_stroke)
    return base.convert("RGB")


def _place_bars(base, e, s):
    top_bar = int(CANVAS_H * 0.20)
    bot_bar = int(CANVAS_H * 0.28)
    d = ImageDraw.Draw(base, "RGBA")
    d.rectangle([0, 0, CANVAS_W, top_bar], fill=(0, 0, 0, 238))
    d.rectangle([0, CANVAS_H - bot_bar, CANVAS_W, CANVAS_H], fill=(0, 0, 0, 245))
    mw = CANVAS_W - 2 * MARGIN
    qf, ql, _ = _fit(f"«{e.quote}»", s.quote_font, mw, 2, s.quote_size, 36)
    pf, pl, _ = _fit(e.punchline, s.punch_font, mw, 2, s.punch_size + 4, 48)
    qlh = qf.getbbox("Ag")[3] + 12
    plh = pf.getbbox("Ag")[3] + 14
    _block(d, ql, qf, (top_bar - _block_h(ql, qf, qlh)) / 2, qlh, s.quote_color)
    _block(d, pl, pf, CANVAS_H - bot_bar + (bot_bar - _block_h(pl, pf, plh)) / 2,
           plh, s.punch_color, s.punch_stroke)
    return base.convert("RGB")


def _place_poster(base, e, s):
    base = Image.alpha_composite(base, _vgrad(0.0, 165))
    d = ImageDraw.Draw(base, "RGBA")
    mw = CANVAS_W - 2 * MARGIN
    qf, ql, _ = _fit(f"«{e.quote}»", s.quote_font, mw, 3, s.quote_size, 36)
    pf, pl, _ = _fit(e.punchline, s.punch_font, mw, 3, s.punch_size, 52)
    qlh = qf.getbbox("Ag")[3] + 14
    plh = pf.getbbox("Ag")[3] + 16
    q_top = CANVAS_H * 0.26
    p_top = CANVAS_H * 0.52
    _block(d, ql, qf, q_top, qlh, s.quote_color, s.quote_stroke)
    _block(d, pl, pf, p_top, plh, s.punch_color, s.punch_stroke)
    return base.convert("RGB")


def _place_split(base, e, s):
    base = Image.alpha_composite(base, _vgrad(0.0, 200, reverse=True))   # top dark
    base = Image.alpha_composite(base, _vgrad(0.62, 200))                # bottom dark
    d = ImageDraw.Draw(base, "RGBA")
    mw = CANVAS_W - 2 * MARGIN
    qf, ql, _ = _fit(f"«{e.quote}»", s.quote_font, mw, 2, s.quote_size, 36)
    pf, pl, _ = _fit(e.punchline, s.punch_font, mw, 2, s.punch_size, 46)
    qlh = qf.getbbox("Ag")[3] + 12
    plh = pf.getbbox("Ag")[3] + 14
    _block(d, ql, qf, 70, qlh, s.quote_color, s.quote_stroke)
    p_bottom = CANVAS_H - 70
    _block(d, pl, pf, p_bottom - _block_h(pl, pf, plh), plh,
           s.punch_color, s.punch_stroke)
    return base.convert("RGB")


def _place_minimal(base, e, s):
    blur = base.filter(ImageFilter.GaussianBlur(2))
    dark = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 70))
    base = Image.alpha_composite(blur, dark)
    d = ImageDraw.Draw(base, "RGBA")
    mw = CANVAS_W - 2 * MARGIN
    qf, ql, _ = _fit(f"«{e.quote}»", s.quote_font, mw, 2, s.quote_size, 36)
    pf, pl, _ = _fit(e.punchline, s.punch_font, mw, 3, s.punch_size, 48)
    qlh = qf.getbbox("Ag")[3] + 12
    plh = pf.getbbox("Ag")[3] + 16
    _block(d, ql, qf, 80, qlh, s.quote_color, s.quote_stroke)
    p_bottom = CANVAS_H - 90
    _block(d, pl, pf, p_bottom - _block_h(pl, pf, plh), plh,
           s.punch_color, s.punch_stroke)
    return base.convert("RGB")


def _place_hand(base, e, s):
    # intentionally scrappy: translucent sticker rectangle, off-centre text
    d = ImageDraw.Draw(base, "RGBA")
    d.rectangle([0, 0, CANVAS_W, CANVAS_H], fill=(0, 0, 0, 70))
    mw = CANVAS_W - 2 * MARGIN
    qf, ql, _ = _fit(e.quote, s.quote_font, mw, 2, s.quote_size, 34)
    pf, pl, _ = _fit(e.punchline, s.punch_font, mw, 3, s.punch_size, 44)
    qlh = qf.getbbox("Ag")[3] + 14
    plh = pf.getbbox("Ag")[3] + 16
    sticker = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sticker)
    bh = _block_h(pl, pf, plh) + _block_h(ql, qf, qlh) + 90
    sd.rectangle([80, CANVAS_H - bh - 60, CANVAS_W - 60, CANVAS_H - 30],
                 fill=(45, 22, 33, 205))
    base = Image.alpha_composite(base, sticker)
    d = ImageDraw.Draw(base, "RGBA")
    q_top = CANVAS_H - bh - 30
    _block(d, ql, qf, q_top, qlh, s.quote_color, s.quote_stroke,
           align="center")
    _block(d, pl, pf, q_top + _block_h(ql, qf, qlh) + 30, plh,
           s.punch_color, s.punch_stroke)
    return base.convert("RGB")


LAYOUT_FN = {
    "bottom": _place_bottom, "bars": _place_bars, "poster": _place_poster,
    "split": _place_split, "minimal": _place_minimal, "hand": _place_hand,
}


# ----------------------------------------------------------------- niche effects
# Applied occasionally (~1 in 5 memes) to the photo before text. All fast in Pillow.
def _fx_gray(im):
    return im.convert("L").convert("RGBA")


def _fx_cold(im):
    g = im.convert("L")
    r = g.point(lambda x: int(x * 0.72))
    gg = g.point(lambda x: int(x * 0.88))
    b = g.point(lambda x: min(255, int(x * 1.18)))
    return Image.merge("RGBA", (r, gg, b, im.getchannel("A")))


def _fx_sepia(im):
    g = im.convert("L")
    r = g.point(lambda x: min(255, int(x * 1.08)))
    gg = g.point(lambda x: int(x * 0.9))
    b = g.point(lambda x: int(x * 0.68))
    return Image.merge("RGBA", (r, gg, b, im.getchannel("A")))


def _fx_blur(im):
    return im.filter(ImageFilter.GaussianBlur(4))


def _fx_grain(im):
    noise = Image.effect_noise(im.size, 28)
    layer = Image.merge("RGBA", (noise, noise, noise, Image.new("L", im.size, 45)))
    return Image.alpha_composite(im, layer)


def _fx_noir(im):
    g = im.convert("L").point(lambda x: 255 if x > 150 else (0 if x < 70 else x))
    return g.convert("RGBA")


EFFECTS = {"gray": _fx_gray, "cold": _fx_cold, "sepia": _fx_sepia,
           "blur": _fx_blur, "grain": _fx_grain, "noir": _fx_noir}


def _apply_effect(im, name: str | None) -> Image.Image:
    fn = EFFECTS.get(name or "")
    return fn(im) if fn else im


def render_meme(image_bytes: bytes, entry: Entry, style: Style | None = None) -> bytes:
    style = style or pick_style()
    im = _cover(Image.open(io.BytesIO(image_bytes))).convert("RGBA")
    if style.effect:
        im = _apply_effect(im, style.effect)
    out = LAYOUT_FN[style.layout](im, entry, style)
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=88, optimize=True)   # JPEG: smaller TG payload
    return buf.getvalue()


def cache_meme(res: dict, keep: int = CACHE_KEEP) -> Path | None:
    """Persist a generated meme to disk for history/inspection (gitignored,
    rotating to the last `keep` files). Purely additive — publishing does not
    read from here, so any failure is non-fatal."""
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        suffix = hashlib.md5(res["image"][:64]).hexdigest()[:4]
        p = CACHE_DIR / f"{ts}_{res['layout']}_{res['entry_id']}_{suffix}.jpg"
        p.write_bytes(res["image"])
        for old in sorted(CACHE_DIR.glob("*.jpg"), key=lambda f: f.stat().st_mtime)[:-keep]:
            try:
                old.unlink()
            except OSError:
                pass
        return p
    except OSError:
        return None


# ----------------------------------------------------------------- sources
# Real photos only. Each keyed source returns None on any failure so the chain
# falls through; Picsum (no key, always last) is the guaranteed floor.
async def _fetch_img(client: httpx.AsyncClient, url: str, **kw) -> bytes | None:
    r = await client.get(url, **kw)
    r.raise_for_status()
    ct = r.headers.get("content-type", "")
    if not ct.startswith("image"):
        return None
    return r.content


async def _pexels(client, key):
    tag = random.choice(BG_TAGS)
    r = await client.get(
        "https://api.pexels.com/v1/search",
        params={"query": tag, "per_page": 15, "page": random.randint(1, 8),
                "orientation": "portrait"},
        headers={"Authorization": key}, timeout=15,
    )
    r.raise_for_status()
    photos = r.json().get("photos") or []
    if not photos:
        return None
    img = await _fetch_img(client, random.choice(photos)["src"]["large2x"])
    return (img, "pexels") if img else None


async def _unsplash(client, key):
    tag = random.choice(BG_TAGS)
    r = await client.get(
        "https://api.unsplash.com/search/photos",
        params={"query": tag, "per_page": 15, "orientation": "portrait"},
        headers={"Authorization": f"Client-ID {key}"}, timeout=15,
    )
    r.raise_for_status()
    res = r.json().get("results") or []
    if not res:
        return None
    img = await _fetch_img(client, random.choice(res)["urls"]["regular"])
    return (img, "unsplash") if img else None


async def _pixabay(client, key):
    tag = random.choice(BG_TAGS)
    r = await client.get(
        "https://pixabay.com/api/",
        params={"key": key, "q": tag, "image_type": "photo",
                "orientation": "vertical", "per_page": 15}, timeout=15,
    )
    r.raise_for_status()
    hits = r.json().get("hits") or []
    if not hits:
        return None
    img = await _fetch_img(client, random.choice(hits)["largeImageURL"])
    return (img, "pixabay") if img else None


async def _openverse(client):
    tag = random.choice(BG_TAGS)
    r = await client.get(
        "https://api.openverse.org/v1/images/",
        params={"q": tag, "page_size": 15, "license_type": "all"}, timeout=15,
    )
    r.raise_for_status()
    res = r.json().get("results") or []
    if not res:
        return None
    img = await _fetch_img(client, random.choice(res)["url"])
    return (img, "openverse") if img else None


async def _picsum(client) -> tuple:
    seed = random.randint(0, 99999)
    r = await client.get(
        f"https://picsum.photos/seed/{seed}/{CANVAS_W}/{CANVAS_H}",
        timeout=25, follow_redirects=True,
    )
    r.raise_for_status()
    return (r.content, "picsum")


async def fetch_background(*, pexels_key=None, unsplash_key=None,
                           pixabay_key=None) -> tuple:
    """Multi-source fallback chain → (image_bytes, source_name). Keyed sources
    (if configured) + Openverse are shuffled for variety; Picsum is always the
    guaranteed last resort, so generation works with zero API keys."""
    async with httpx.AsyncClient(
        timeout=25, follow_redirects=True,
        headers={"User-Agent": "tg_w40k/1.0"},
    ) as client:
        sources = []
        if pexels_key:
            sources.append(lambda: _pexels(client, pexels_key))
        if unsplash_key:
            sources.append(lambda: _unsplash(client, unsplash_key))
        if pixabay_key:
            sources.append(lambda: _pixabay(client, pixabay_key))
        sources.append(lambda: _openverse(client))
        random.shuffle(sources)
        for src in sources:
            try:
                got = await src()
            except Exception:
                got = None
            if got:
                return got
        return await _picsum(client)


# ----------------------------------------------------------------- bank
def ensure_bank():
    """Bootstrap: if the live bank is missing, seed it from the tracked
    curated baseline (bank.seed.json). No-op once bank.json exists."""
    if not BANK_PATH.exists() and SEED_PATH.exists():
        BANK_PATH.write_bytes(SEED_PATH.read_bytes())


def load_bank() -> list[Entry]:
    ensure_bank()
    raw = json.loads(BANK_PATH.read_text(encoding="utf-8"))
    keep = Entry.__dataclass_fields__
    return [Entry(**{k: v for k, v in e.items() if k in keep}) for e in raw]


def append_entry(quote: str, punchline: str, tone: str = "light",
                 source: str = "—") -> str:
    raw = json.loads(BANK_PATH.read_text(encoding="utf-8"))
    eid = "u" + hashlib.md5((quote + punchline).encode("utf-8")).hexdigest()[:8]
    if any(e.get("id") == eid for e in raw):
        return eid
    raw.append({"id": eid, "source": source, "quote": quote,
                "punchline": punchline, "tone": tone, "themes": []})
    BANK_PATH.write_text(json.dumps(raw, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    return eid


def cap_bank(auto_cap: int = 120) -> int:
    """Bound growth: drop the oldest auto-added entries (id starts with 'u'),
    keeping all hand-authored ones. Returns number removed."""
    raw = json.loads(BANK_PATH.read_text(encoding="utf-8"))
    auto_idx = [i for i, e in enumerate(raw) if e.get("id", "").startswith("u")]
    if len(auto_idx) <= auto_cap:
        return 0
    drop_count = len(auto_idx) - auto_cap
    drop = set(auto_idx[:drop_count])
    kept = [e for i, e in enumerate(raw) if i not in drop]
    BANK_PATH.write_text(json.dumps(kept, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    return drop_count


async def generate_meme(bank: list[Entry] | None = None,
                        entry_id: str | None = None,
                        style: Style | None = None,
                        pexels_key: str | None = None,
                        unsplash_key: str | None = None,
                        pixabay_key: str | None = None,
                        bias: dict | None = None) -> dict:
    bank = bank or load_bank()
    bias = bias or {}
    entry_avg = bias.get("entry", {})
    layout_avg = bias.get("layout", {})
    if entry_id:
        entry = next((e for e in bank if e.id == entry_id), random.choice(bank))
    else:
        entry = _bias_pick(bank, lambda e: e.id, entry_avg)
    style = style or pick_style(layout_avg=layout_avg)
    if not style.effect and random.random() < 0.22:
        style.effect = random.choice(list(EFFECTS))
    img, img_source = await fetch_background(pexels_key=pexels_key, unsplash_key=unsplash_key,
                                             pixabay_key=pixabay_key)
    png = render_meme(img, entry, style)
    return {
        "image": png, "entry_id": entry.id, "quote": entry.quote,
        "punchline": entry.punchline, "source": entry.source,
        "layout": style.layout, "tone": entry.tone, "img_source": img_source,
        "effect": style.effect,
    }


if __name__ == "__main__":
    async def _demo():
        bank = load_bank()
        layouts = ["bottom", "bars", "poster", "split", "minimal", "hand"]
        e = next(x for x in bank if x.id == "woolf-room")
        for ly in layouts:
            st = pick_style(ly)
            img, _src = await fetch_background()
            data = render_meme(img, e, st)
            p = Path("/tmp") / f"meme-demo-{ly}.jpg"
            p.write_bytes(data)
            print("rendered", ly, p)
    asyncio.run(_demo())
