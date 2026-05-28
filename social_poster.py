"""
Bars on Broadway — daily Instagram + Facebook poster.

Runs from GitHub Actions after the 10am Central scrape.
Pulls the current live_lineup from barsonbroadway.com, generates a branded
square image with day-of-week layout + background variation, uploads it to
the WordPress Media Library (which gives us a public URL Meta can fetch),
then publishes to Instagram and (if pages_manage_posts is granted) to the
Facebook Page.

Required environment variables:
    META_IG_USER_ID         Instagram Business Account ID
    META_PAGE_ACCESS_TOKEN  Long-lived Facebook Page Access Token
    WP_USERNAME             WordPress user with media upload rights
    WP_APP_PASSWORD         That user's WordPress Application Password

Optional:
    META_FB_PAGE_ID         Facebook Page ID. If set, also posts to FB Page
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
import re
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
BG = (11, 10, 13)          # #0B0A0D
PINK = (255, 31, 142)      # #FF1F8E
CREAM = (244, 231, 206)    # #F4E7CE
TEAL = (45, 212, 191)      # #2DD4BF
BODY = (201, 194, 214)     # #C9C2D6
PURPLE = (88, 28, 135)     # accent
ORANGE = (251, 146, 60)    # accent

# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

IG_USER_ID = os.environ["META_IG_USER_ID"]
TOKEN = os.environ["META_PAGE_ACCESS_TOKEN"]
FB_PAGE_ID = os.environ.get("META_FB_PAGE_ID")

# ---------------------------------------------------------------------------
# Lineup fetch
# ---------------------------------------------------------------------------

_SHOW_DATE_RE = re.compile(r"-(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-")


def _parse_show_datetime(slug: str) -> _dt.datetime | None:
    m = _SHOW_DATE_RE.search(slug or "")
    if not m:
        return None
    try:
        return _dt.datetime(
            int(m.group(1)), int(m.group(2)), int(m.group(3)),
            int(m.group(4)), int(m.group(5)),
        )
    except ValueError:
        return None


def fetch_lineup(target_date: _dt.date) -> dict[str, list[str]]:
    """Return {venue_name: [band_names]} for shows happening on target_date."""
    shows: list[tuple[str, str, _dt.datetime]] = []
    page = 1
    while True:
        r = requests.get(
            f"{WP_BASE}/live_lineup",
            params={"per_page": 100, "page": page, "_fields": "id,title,slug"},
            timeout=30,
        )
        if r.status_code != 200:
            print(f"WP fetch failed at page {page}: {r.status_code}")
            break
        batch = r.json()
        if not batch:
            break
        for post in batch:
            slug = post.get("slug", "")
            show_dt = _parse_show_datetime(slug)
            if not show_dt or show_dt.date() != target_date:
                continue
            title = html.unescape(post["title"]["rendered"]).strip()
            for sep in (" - ", " – ", " — "):
                if sep in title:
                    band, venue = title.rsplit(sep, 1)
                    band = band.strip()
                    venue = venue.strip()
                    if band and venue:
                        shows.append((venue, band, show_dt))
                    break
        if len(batch) < 100:
            break
        page += 1

    by_venue: dict[str, list[str]] = defaultdict(list)
    shows.sort(key=lambda x: x[2])
    for venue, band, _ in shows:
        if band not in by_venue[venue]:
            by_venue[venue].append(band)
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
    img = Image.new("RGB", (CANVAS, CANVAS), BG)
    d = ImageDraw.Draw(img)
    spacing = 80
    for i in range(-CANVAS, CANVAS * 2, spacing):
        d.line([(i, 0), (i + CANVAS, CANVAS)], fill=c1, width=4)
        d.line([(i + 40, 0), (i + 40 + CANVAS, CANVAS)], fill=c2, width=2)
    overlay = Image.new("RGB", (CANVAS, CANVAS), (0, 0, 0))
    img = Image.blend(img, overlay, 0.78)
    return img


def _glow_corner(corner_color: tuple) -> Image.Image:
    img = Image.new("RGB", (CANVAS, CANVAS), BG)
    d = ImageDraw.Draw(img)
    for r in range(800, 0, -10):
        alpha = r / 800
        c = tuple(int(corner_color[i] * (1 - alpha) + BG[i] * alpha) for i in range(3))
        d.ellipse([-r // 2, -r // 2, r, r], fill=c)
    img = img.filter(ImageFilter.GaussianBlur(radius=8))
    return img


def _twin_glow() -> Image.Image:
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
    photo_path = BG_DIR / f"day_{weekday}.jpg"
    if photo_path.exists():
        img = Image.open(photo_path).convert("RGB")
        w, h = img.size
        scale = max(CANVAS / w, CANVAS / h)
        new = (int(w * scale), int(h * scale))
        img = img.resize(new, Image.LANCZOS)
        left = (img.size[0] - CANVAS) // 2
        top = (img.size[1] - CANVAS) // 2
        img = img.crop((left, top, left + CANVAS, top + CANVAS))
        overlay = Image.new("RGB", (CANVAS, CANVAS), (0, 0, 0))
        img = Image.blend(img, overlay, 0.62)
        return img

    gradients = [
        _twin_glow,
        lambda: _vertical_gradient((92, 0, 50), BG),
        lambda: _radial_gradient((15, 60, 55), BG),
        lambda: _bands_pattern(PINK, TEAL),
        lambda: _glow_corner(PINK),
        lambda: _diagonal_gradient(PURPLE, BG),
        lambda: _glow_corner(ORANGE),
    ]
    return gradients[weekday]()


# ---------------------------------------------------------------------------
# Layout functions — 3 styles, rotated by weekday
# ---------------------------------------------------------------------------

def _draw_text_with_shadow(draw, xy, text, font, fill, shadow=(0, 0, 0), offset=2):
    sx, sy = xy[0] + offset, xy[1] + offset
    draw.text((sx, sy), text, font=font, fill=shadow)
    draw.text(xy, text, font=font, fill=fill)


def _layout_poster(bg: Image.Image, by_venue: dict, today: _dt.date) -> Image.Image:
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
    img = bg.copy()
    d = ImageDraw.Draw(img)

    date_str = today.strftime("%A").upper()
    head_font = _find_font(110, bold=True)
    sub_font = _find_font(34)
    pill_font = _find_font(26, bold=True)
    pill_w = 380
    d.rectangle([(CANVAS - pill_w) // 2, 48, (CANVAS + pill_w) // 2, 96], fill=PINK)
    d.text((CANVAS // 2, 72), date_str + " ON BROADWAY",
           font=pill_font, fill=(11, 10, 13), anchor="mm")

    _draw_text_with_shadow(d, (CANVAS // 2, 200), "TONIGHT",
                           head_font, PINK, offset=3)
    bbox = d.textbbox((0, 0), "TONIGHT", font=head_font)
    tw = bbox[2] - bbox[0]
    img_copy = bg.copy()
    d = ImageDraw.Draw(img_copy)
    d.rectangle([0, 0, 14, CANVAS], fill=PINK)
    d.rectangle([(CANVAS - pill_w) // 2, 48, (CANVAS + pill_w) // 2, 96], fill=PINK)
    tx = (CANVAS - tw) // 2
    d.text(((CANVAS - 380) // 2 + 10, 60), date_str + " ON BROADWAY",
           font=pill_font, fill=(11, 10, 13))
    _draw_text_with_shadow(d, (tx, 160), "TONIGHT", head_font, PINK, offset=3)

    sub = today.strftime("%B %d").replace(" 0", " ")
    sbox = d.textbbox((0, 0), sub, font=sub_font)
    sw = sbox[2] - sbox[0]
    _draw_text_with_shadow(d, ((CANVAS - sw) // 2, 300), sub, sub_font, CREAM)

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
    img = Image.new("RGB", (CANVAS, CANVAS), BG)
    top = bg.crop((0, 0, CANVAS, CANVAS // 2))
    img.paste(top, (0, 0))
    d = ImageDraw.Draw(img)

    d.rectangle([0, CANVAS // 2 - 4, CANVAS, CANVAS // 2 + 4], fill=PINK)

    date_str = today.strftime("%A, %B %d").upper().replace(" 0", " ")
    _draw_text_with_shadow(d, (54, 54), date_str, _find_font(28), BODY)
    _draw_text_with_shadow(d, (54, 100), "LOWER", _find_font(110, bold=True), CREAM)
    _draw_text_with_shadow(d, (54, 220), "BROADWAY", _find_font(110, bold=True), PINK)
    _draw_text_with_shadow(d, (54, 380), "TONIGHT", _find_font(60, bold=True), TEAL)

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
    today = _dt.date.today()
    weekday = today.weekday()
    bg = get_background(weekday)
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
# Git commit (still done so images are backed up in repo for audit trail)
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
# WordPress media upload
# ---------------------------------------------------------------------------

def upload_to_wordpress(image_path: Path) -> str:
    """Upload the daily image to WP Media Library and return the public URL.

    Replaces the previous raw.githubusercontent.com URL strategy, which
    failed because the repo is private — Meta's unauthenticated fetcher
    got 404 and returned error 2207052 ("media URI doesn't meet our
    requirements"). Hosting on WordPress gives us a permanent public URL
    on our own domain that Meta can always reach.
    """
    wp_user = os.environ.get("WP_USERNAME")
    wp_pass = os.environ.get("WP_APP_PASSWORD")
    if not wp_user or not wp_pass:
        raise RuntimeError(
            "WP_USERNAME and WP_APP_PASSWORD env vars required. Add them "
            "to the Daily Instagram + FB post step in lineup_sync.yml."
        )

    filename = image_path.name
    with open(image_path, "rb") as f:
        data = f.read()

    print(f"Uploading {filename} ({len(data)} bytes) to WordPress media library...")
    r = requests.post(
        f"{WP_BASE}/media",
        auth=(wp_user, wp_pass),
        headers={
            "Content-Type": "image/jpeg",
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
        data=data,
        timeout=60,
    )
    if r.status_code not in (200, 201):
        print(f"WP upload failed: {r.status_code} {r.text[:500]}")
        sys.exit(1)
    j = r.json()
    url = j.get("source_url") or j.get("guid", {}).get("rendered")
    if not url:
        print(f"WP upload response missing source_url: {j}")
        sys.exit(1)
    print(f"Uploaded to: {url}")
    return url


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
        data={
            "url": image_url,
            "caption": caption,
            "access_token": TOKEN,
            "published": "true",
        },
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
    today = _dt.date.today()
    print(f"Fetching lineup for {today.isoformat()} from barsonbroadway.com ...")
    by_venue = fetch_lineup(today)
    if not by_venue:
        print(f"No lineup data found for {today.isoformat()}; aborting without posting.")
        return
    band_count = sum(len(b) for b in by_venue.values())
    print(f"Loaded {band_count} bands across {len(by_venue)} venues for tonight.")
    for venue, bands in by_venue.items():
        print(f"  {venue}: {', '.join(bands)}")

    today_iso = _dt.date.today().isoformat()
    image_path = IMAGES_DIR / f"{today_iso}.jpg"
    generate_image(by_venue, image_path)
    print(f"Image generated: {image_path}")

    # Commit to repo for backup/audit (images accumulate in daily_posts/).
    commit_and_push(image_path)

    # Upload to WordPress Media Library and use THAT URL for Meta.
    # The old raw.githubusercontent.com URL approach failed because the
    # repo is private and Meta's fetcher gets 404 -> error 2207052.
    image_url = upload_to_wordpress(image_path)

    caption = build_caption(by_venue)
    print("\nCaption preview:\n" + caption + "\n")

    media_id = post_to_instagram(image_url, caption)
    print(f"Posted to Instagram: media id {media_id}")

    fb_id = post_to_facebook_page(image_url, caption)
    if fb_id:
        print(f"Posted to Facebook Page: {fb_id}")


if __name__ == "__main__":
    main()
