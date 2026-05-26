"""
Bars on Broadway — daily Instagram + Facebook poster.

Runs from GitHub Actions after the 10am Central scrape.
Pulls the current live_lineup from barsonbroadway.com, generates a branded
square image with day-of-week layout + background variation, commits it to
the repo so raw.githubusercontent can serve it, then publishes to Instagram
and (if pages_manage_posts is granted) to the Facebook Page.

Required environment variables:
    META_IG_USER_ID            Instagram Business Account ID
    META_PAGE_ACCESS_TOKEN     Long-lived Facebook Page Access Token

Optional:
    META_FB_PAGE_ID            Facebook Page ID. If set, also posts to FB Page
                               (requires pages_manage_posts scope on the token).

Optional photo backgrounds:
    Drop JPGs into daily_posts/backgrounds/day_0.jpg through day_6.jpg
    (Monday=0, Sunday=6). If present, they override the gradient background
    for that day. Will be auto-resized and darkened for text legibility.
"""

from __future__ import annotations

import datetime as _dt
import html
import math
import os
import random
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

WP_BASE = "https://barsonbroadway.com/wp-json/wp/v2"
GRAPH_API = "https://graph.facebook.com/v25.0"
REPO = "SafetyMan80/bars-on-broadway"
BRANCH = "main"
IMAGES_DIR = Path("daily_posts")
BG_DIR = IMAGES_DIR / "backgrounds"
RAW_BASE = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}"
CANVAS = 1080  # square output

# Brand palette
BG = (11, 10, 13)        # #0B0A0D
PINK = (255, 31, 142)    # #FF1F8E
CREAM = (244, 231, 206)  # #F4E7CE
TEAL = (45, 212, 191)    # #2DD4BF
BODY = (201, 194, 214)   # #C9C2D6
PURPLE = (88, 28, 135)   # accent
ORANGE = (251, 146, 60)  # accent

# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

IG_USER_ID = os.environ["META_IG_USER_ID"]
TOKEN = os.environ["META_PAGE_ACCESS_TOKEN"]
FB_PAGE_ID = os.environ.get("META_FB_PAGE_ID")


# ---------------------------------------------------------------------------
# Lineup fetch
# ---------------------------------------------------------------------------


def fetch_lineup() -> dict[str, list[str]]:
    """Return {venue_name: [band_names]} from the live_lineup CPT."""
    by_venue: dict[str, list[str]] = defaultdict(list)
    page = 1
    while True:
        r = requests.get(
            f"{WP_BASE}/live_lineup",
            params={"per_page": 100, "page": page, "_fields": "id,title"},
            timeout=30,
        )
        if r.status_code != 200:
            print(f"WP fetch failed at page {page}: {r.status_code}")
            break
        batch = r.json()
        if not batch:
            break
        for post in batch:
            # WP REST returns HTML-encoded titles (&#8211; for –, etc.)
            title = html.unescape(post["title"]["rendered"]).strip()
            for sep in (" - ", " – ", " — "):
                if sep in title:
                    band, venue = title.rsplit(sep, 1)
                    band = band.strip()
                    venue = venue.strip()
                    if band and venue and band not in by_venue[venue]:
                        by_venue[venue].append(band)
                    break
        if len(batch) < 100:
            break
        page += 1
    return dict(by_venue)


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------


def _find_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    bold_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ]
    reg_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for path in (bold_paths if bold else reg_paths):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Backgrounds — 7 per week, photo if available, gradient fallback
# ---------------------------------------------------------------------------


def _vertical_gradient(top: tuple, bottom: tuple) -> Image.Image:
    img = Image.new("RGB", (CANVAS, CANVAS), top)
    d = ImageDraw.Draw(img)
    for y in range(CANVAS):
        t = y / CANVAS
        c = tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3))
        d.line([(0, y), (CANVAS, y)], fill=c)
    return img


def _diagonal_gradient(start: tuple, end: tuple) -> Image.Image:
    img = Image.new("RGB", (CANVAS, CANVAS), start)
    d = ImageDraw.Draw(img)
    diag = CANVAS * 2
    for i in range(diag):
        t = i / diag
        c = tuple(int(start[k] * (1 - t) + end[k] * t) for k in range(3))
        d.line([(i, 0), (0, i)], fill=c)
    return img


def _radial_gradient(center: tuple, edge: tuple) -> Image.Image:
    img = Image.new("RGB", (CANVAS, CANVAS), edge)
    d = ImageDraw.Draw(img)
    cx, cy = CANVAS // 2, CANVAS // 2
    max_r = int(math.hypot(cx, cy))
    for r in range(max_r, 0, -2):
        t = r / max_r
        c = tuple(int(center[i] * (1 - t) + edge[i] * t) for i in range(3))
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=c)
    return img


def _bands_pattern(c1: tuple, c2: tuple) -> Image.Image:
    """Diagonal neon-stripe pattern, darkened."""
    img = Image.new("RGB", (CANVAS, CANVAS), BG)
    d = ImageDraw.Draw(img)
    spacing = 80
    for i in range(-CANVAS, CANVAS * 2, spacing):
        d.line([(i, 0), (i + CANVAS, CANVAS)], fill=c1, width=4)
        d.line([(i + 40, 0), (i + 40 + CANVAS, CANVAS)], fill=c2, width=2)
    # darken
    overlay = Image.new("RGB", (CANVAS, CANVAS), (0, 0, 0))
    img = Image.blend(img, overlay, 0.78)
    return img


def _glow_corner(corner_color: tuple) -> Image.Image:
    """Dark background with a soft color glow in the top-left corner."""
    img = Image.new("RGB", (CANVAS, CANVAS), BG)
    d = ImageDraw.Draw(img)
    for r in range(800, 0, -10):
        alpha = r / 800
        c = tuple(int(corner_color[i] * (1 - alpha) + BG[i] * alpha) for i in range(3))
        d.ellipse([-r // 2, -r // 2, r, r], fill=c)
    img = img.filter(ImageFilter.GaussianBlur(radius=8))
    return img


def _twin_glow() -> Image.Image:
    """Two glows in opposite corners — pink top-right, teal bottom-left."""
    img = Image.new("RGB", (CANVAS, CANVAS), BG)
    d = ImageDraw.Draw(img)
    for r in range(700, 0, -10):
        a = r / 700
        c = tuple(int(PINK[i] * (1 - a) + BG[i] * a) for i in range(3))
        d.ellipse([CANVAS - r, -r // 3, CANVAS + r // 2, r], fill=c)
    for r in range(700, 0, -10):
        a = r / 700
        c = tuple(int(TEAL[i] * (1 - a) + BG[i] * a) for i in range(3))
        d.ellipse([-r // 2, CANVAS - r, r, CANVAS + r // 3], fill=c)
    img = img.filter(ImageFilter.GaussianBlur(radius=12))
    return img


def get_background(weekday: int) -> Image.Image:
    """Return a 1080×1080 background image for today.

    If a photo exists at daily_posts/backgrounds/day_{N}.jpg, it's used
    (resized + darkened). Otherwise, falls back to a procedural gradient
    keyed to the day of week.
    """
    photo_path = BG_DIR / f"day_{weekday}.jpg"
    if photo_path.exists():
        img = Image.open(photo_path).convert("RGB")
        # cover-fit to 1080×1080
        w, h = img.size
        scale = max(CANVAS / w, CANVAS / h)
        new = (int(w * scale), int(h * scale))
        img = img.resize(new, Image.LANCZOS)
        # center crop
        left = (img.size[0] - CANVAS) // 2
        top = (img.size[1] - CANVAS) // 2
        img = img.crop((left, top, left + CANVAS, top + CANVAS))
        # darken for text legibility
        overlay = Image.new("RGB", (CANVAS, CANVAS), (0, 0, 0))
        img = Image.blend(img, overlay, 0.62)
        return img

    # Procedural fallback — one per weekday
    gradients = [
        # Monday — twin glow (pink + teal corners)
        _twin_glow,
        # Tuesday — pink vertical gradient
        lambda: _vertical_gradient((92, 0, 50), BG),
        # Wednesday — teal radial
        lambda: _radial_gradient((15, 60, 55), BG),
        # Thursday — diagonal neon stripes
        lambda: _bands_pattern(PINK, TEAL),
        # Friday — bold pink glow corner
        lambda: _glow_corner(PINK),
        # Saturday — purple-to-black diagonal
        lambda: _diagonal_gradient(PURPLE, BG),
        # Sunday — orange glow corner
        lambda: _glow_corner(ORANGE),
    ]
    return gradients[weekday]()


# ---------------------------------------------------------------------------
# Layout functions — 3 styles, rotated by weekday
# ---------------------------------------------------------------------------


def _draw_text_with_shadow(draw, xy, text, font, fill, shadow=(0, 0, 0), offset=2):
    """Draw text with a subtle drop shadow for legibility on any background."""
    sx, sy = xy[0] + offset, xy[1] + offset
    draw.text((sx, sy), text, font=font, fill=shadow)
    draw.text(xy, text, font=font, fill=fill)


def _layout_poster(bg: Image.Image, by_venue: dict, today: _dt.date) -> Image.Image:
    """Layout A — Classic poster. Text-heavy list, full venue showcase."""
    img = bg.copy()
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 14, CANVAS], fill=PINK)
    date_str = today.strftime("%A, %B %d").upper().replace(" 0", " ")
    _draw_text_with_shadow(d, (54, 54), date_str, _find_font(28), BODY)
    _draw_text_with_shadow(d, (54, 96), "LOWER BROADWAY", _find_font(76, bold=True), CREAM)
    _draw_text_with_shadow(d, (54, 184), "TONIGHT", _find_font(76, bold=True), PINK)

    populated = [(v, b) for v, b in by_venue.items() if b]
    random.shuffle(populated)
    y = 326
    venue_font = _find_font(36, bold=True)
    band_font = _find_font(28)
    for venue, bands in populated[:6]:
        if y > CANVAS - 180:
            break
        _draw_text_with_shadow(d, (54, y), venue.upper(), venue_font, TEAL)
        y += 46
        for band in bands[:2]:
            _draw_text_with_shadow(d, (76, y), f"+ {band}", band_font, CREAM)
            y += 34
        y += 18
    footer_font = _find_font(24)
    _draw_text_with_shadow(d, (54, CANVAS - 104), "Full schedule and route planner", footer_font, BODY)
    _draw_text_with_shadow(d, (54, CANVAS - 72), "barsonbroadway.com", footer_font, PINK)
    return img


def _layout_hero(bg: Image.Image, by_venue: dict, today: _dt.date) -> Image.Image:
    """Layout B — Hero. Big top-center headline, 3 featured venues large."""
    img = bg.copy()
    d = ImageDraw.Draw(img)

    # Top half — big headline
    date_str = today.strftime("%A").upper()
    head_font = _find_font(110, bold=True)
    sub_font = _find_font(34)
    # date pill at top
    pill_font = _find_font(26, bold=True)
    pill_w = 380
    d.rectangle([(CANVAS - pill_w) // 2, 48, (CANVAS + pill_w) // 2, 96],
                fill=PINK)
    d.text((CANVAS // 2, 72), date_str + " ON BROADWAY",
           font=pill_font, fill=(11, 10, 13), anchor="mm")

    _draw_text_with_shadow(d, (CANVAS // 2, 200), "TONIGHT",
                           head_font, PINK, offset=3)
    # center-anchor isn't supported in old PIL; recalc manually
    bbox = d.textbbox((0, 0), "TONIGHT", font=head_font)
    tw = bbox[2] - bbox[0]
    img_copy = bg.copy()
    d = ImageDraw.Draw(img_copy)
    d.rectangle([0, 0, 14, CANVAS], fill=PINK)
    d.rectangle([(CANVAS - pill_w) // 2, 48, (CANVAS + pill_w) // 2, 96],
                fill=PINK)
    tx = (CANVAS - tw) // 2
    d.text(((CANVAS - 380) // 2 + 10, 60), date_str + " ON BROADWAY",
           font=pill_font, fill=(11, 10, 13))
    _draw_text_with_shadow(d, (tx, 160), "TONIGHT", head_font, PINK, offset=3)

    sub = today.strftime("%B %d").replace(" 0", " ")
    sbox = d.textbbox((0, 0), sub, font=sub_font)
    sw = sbox[2] - sbox[0]
    _draw_text_with_shadow(d, ((CANVAS - sw) // 2, 300), sub, sub_font, CREAM)

    # 3 featured venues — centered, larger
    populated = [(v, b) for v, b in by_venue.items() if b]
    random.shuffle(populated)
    featured = populated[:3]
    y = 420
    v_font = _find_font(42, bold=True)
    b_font = _find_font(28)
    for venue, bands in featured:
        vbox = d.textbbox((0, 0), venue.upper(), font=v_font)
        vw = vbox[2] - vbox[0]
        _draw_text_with_shadow(d, ((CANVAS - vw) // 2, y), venue.upper(),
                               v_font, TEAL)
        y += 54
        if bands:
            bn = bands[0]
            bbox = d.textbbox((0, 0), bn, font=b_font)
            bw = bbox[2] - bbox[0]
            _draw_text_with_shadow(d, ((CANVAS - bw) // 2, y), bn, b_font, CREAM)
            y += 56

    footer_font = _find_font(24)
    _draw_text_with_shadow(d, (54, CANVAS - 72), "barsonbroadway.com",
                           footer_font, PINK)
    return img_copy


def _layout_split(bg: Image.Image, by_venue: dict, today: _dt.date) -> Image.Image:
    """Layout C — Split. Background image top half, lineup list bottom half on dark."""
    img = Image.new("RGB", (CANVAS, CANVAS), BG)
    # paste bg into top half
    top = bg.crop((0, 0, CANVAS, CANVAS // 2))
    img.paste(top, (0, 0))
    d = ImageDraw.Draw(img)

    # Pink divider between halves
    d.rectangle([0, CANVAS // 2 - 4, CANVAS, CANVAS // 2 + 4], fill=PINK)

    # Top half overlay text
    date_str = today.strftime("%A, %B %d").upper().replace(" 0", " ")
    _draw_text_with_shadow(d, (54, 54), date_str, _find_font(28), BODY)
    _draw_text_with_shadow(d, (54, 100), "LOWER", _find_font(110, bold=True), CREAM)
    _draw_text_with_shadow(d, (54, 220), "BROADWAY", _find_font(110, bold=True), PINK)
    _draw_text_with_shadow(d, (54, 380), "TONIGHT", _find_font(60, bold=True), TEAL)

    # Bottom half — lineup list on dark
    populated = [(v, b) for v, b in by_venue.items() if b]
    random.shuffle(populated)
    y = CANVAS // 2 + 40
    v_font = _find_font(32, bold=True)
    b_font = _find_font(26)
    for venue, bands in populated[:5]:
        if y > CANVAS - 120:
            break
        _draw_text_with_shadow(d, (54, y), venue.upper(), v_font, TEAL)
        y += 40
        if bands:
            _draw_text_with_shadow(d, (76, y), f"+ {bands[0]}", b_font, CREAM)
            y += 36
        y += 8

    footer_font = _find_font(22)
    _draw_text_with_shadow(d, (54, CANVAS - 56), "barsonbroadway.com",
                           footer_font, PINK)
    return img


LAYOUTS = [_layout_poster, _layout_hero, _layout_split]


def generate_image(by_venue: dict, out_path: Path) -> Path:
    """Generate today's post image, rotating layout + background by weekday."""
    today = _dt.date.today()
    weekday = today.weekday()  # Monday=0
    bg = get_background(weekday)
    # Rotate through 3 layouts: weekday mod 3
    layout_fn = LAYOUTS[weekday % 3]
    img = layout_fn(bg, by_venue, today)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "JPEG", quality=92)
    return out_path


# ---------------------------------------------------------------------------
# Caption
# ---------------------------------------------------------------------------


HASHTAGS = [
    "#nashville", "#lowerbroadway", "#broadwaynashville", "#honkytonk",
    "#livemusic", "#nashvillemusic", "#musiccity", "#downtownnashville",
    "#nashvillenightlife", "#nashvilletn", "#visitnashville",
    "#nashvilletennessee", "#countrymusic", "#nashvillebars",
]


def build_caption(by_venue: dict) -> str:
    today = _dt.date.today()
    weekday = today.strftime("%A")
    month_day = today.strftime("%B %d").replace(" 0", " ")

    populated = [(v, b) for v, b in by_venue.items() if b]
    random.shuffle(populated)
    featured = populated[:5]

    lines = [f"Lower Broadway tonight. {weekday}, {month_day}.", ""]
    for venue, bands in featured:
        lines.append(f"{venue}: {bands[0]}")
    lines += [
        "",
        "Full schedule and route planner at barsonbroadway.com",
        "",
        " ".join(HASHTAGS),
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Git commit
# ---------------------------------------------------------------------------


def commit_and_push(image_path: Path) -> None:
    subprocess.run(["git", "config", "user.email", "bot@barsonbroadway.com"], check=True)
    subprocess.run(["git", "config", "user.name", "bars-on-broadway-bot"], check=True)
    subprocess.run(["git", "add", str(image_path)], check=True)
    result = subprocess.run(
        ["git", "commit", "-m", f"daily post image {_dt.date.today().isoformat()}"],
        capture_output=True, text=True,
    )
    combined = result.stdout + result.stderr
    if result.returncode != 0 and "nothing to commit" not in combined:
        print(combined)
        raise RuntimeError("git commit failed")
    subprocess.run(["git", "push"], check=True)


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


def post_to_instagram(image_url: str, caption: str) -> str:
    print(f"Creating IG media container from: {image_url}")
    r1 = requests.post(
        f"{GRAPH_API}/{IG_USER_ID}/media",
        data={"image_url": image_url, "caption": caption, "access_token": TOKEN},
        timeout=60,
    )
    j1 = r1.json()
    if "id" not in j1:
        print(f"Container create failed: {j1}")
        sys.exit(1)
    creation_id = j1["id"]
    print(f"Container created: {creation_id}")
    time.sleep(6)
    r2 = requests.post(
        f"{GRAPH_API}/{IG_USER_ID}/media_publish",
        data={"creation_id": creation_id, "access_token": TOKEN},
        timeout=60,
    )
    j2 = r2.json()
    print(f"Publish response: {j2}")
    if "id" not in j2:
        sys.exit(1)
    return j2["id"]


def post_to_facebook_page(image_url: str, caption: str) -> str | None:
    if not FB_PAGE_ID:
        print("META_FB_PAGE_ID not set; skipping FB Page post.")
        return None
    print(f"Posting to FB Page {FB_PAGE_ID} ...")
    r = requests.post(
        f"{GRAPH_API}/{FB_PAGE_ID}/photos",
        data={"url": image_url, "caption": caption, "access_token": TOKEN},
        timeout=60,
    )
    j = r.json()
    print(f"FB Page response: {j}")
    if "id" in j or "post_id" in j:
        return j.get("post_id") or j.get("id")
    print("FB Page post failed; continuing anyway.")
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    print("Fetching lineup from barsonbroadway.com ...")
    by_venue = fetch_lineup()
    if not by_venue:
        print("No lineup data found; aborting without posting.")
        return
    band_count = sum(len(b) for b in by_venue.values())
    print(f"Loaded {band_count} bands across {len(by_venue)} venues.")

    today_iso = _dt.date.today().isoformat()
    image_path = IMAGES_DIR / f"{today_iso}.jpg"
    generate_image(by_venue, image_path)
    print(f"Image generated: {image_path}")

    commit_and_push(image_path)
    time.sleep(20)

    image_url = f"{RAW_BASE}/{image_path.as_posix()}?t={int(time.time())}"
    caption = build_caption(by_venue)
    print("\nCaption preview:\n" + caption + "\n")

    media_id = post_to_instagram(image_url, caption)
    print(f"Posted to Instagram: media id {media_id}")

    fb_id = post_to_facebook_page(image_url, caption)
    if fb_id:
        print(f"Posted to Facebook Page: {fb_id}")


if __name__ == "__main__":
    main()
