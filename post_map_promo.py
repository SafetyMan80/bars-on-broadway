"""
One-shot manual promo post: announces the new interactive walking map
to Instagram (Business) and Facebook (Page) via the Meta Graph API.

Triggered manually via .github/workflows/manual_promo_post.yml
(workflow_dispatch). Reads the same secrets the daily lineup poster uses.

Flow:
1. Generate a 1080x1080 promo PNG with Pillow (no browser screenshot needed).
2. Upload the PNG to WordPress media library (so Meta has a public URL).
3. POST to IG /media (container) + /media_publish (publish) with caption.
4. POST to FB /{page-id}/photos with the same image + message.
"""

import io
import os
import sys
import math
import json
import time
import base64
import textwrap
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

# ---------- Config (from env / secrets) ----------
WP_BASE         = "https://barsonbroadway.com"
WP_USERNAME     = os.environ["WP_USERNAME"]
WP_APP_PASSWORD = os.environ["WP_APP_PASSWORD"]
IG_USER_ID      = os.environ["META_IG_USER_ID"]
PAGE_TOKEN      = os.environ["META_PAGE_ACCESS_TOKEN"]
FB_PAGE_ID      = os.environ.get("META_FB_PAGE_ID", "")

GRAPH_VERSION = "v21.0"

CAPTION = (
    "Finally got the walking map done. 37 bars across Broadway, 2nd Ave, and Printers Alley "
    "— every pin tap shows you the address and a photo of the place. Filter pills if you want "
    "to focus on one street. Plan your crawl in walking order from the river west.\n\n"
    "Live on the homepage now.\n\n"
    "→ barsonbroadway.com\n\n"
    "#nashville #broadwaynashville #honkytonk #bachelorette #nashvillebars "
    "#lowerbroadway #cmafest #musiccity #2ndavenue #printersalley"
)


# ---------- Venue coordinates (for the mockup dots) ----------
# Lifted from the live walking map data (lat, lng, category).
VENUES = [
    ("broadway", 36.160432, -86.777814),
    ("broadway", 36.161864, -86.774319),
    ("broadway", 36.161578, -86.775369),
    ("broadway", 36.16111,  -86.777394),
    ("broadway", 36.160695, -86.777595),
    ("broadway", 36.160877, -86.7768),
    ("broadway", 36.161346, -86.776329),
    ("broadway", 36.161518, -86.776787),
    ("broadway", 36.161447, -86.775776),
    ("broadway", 36.160935, -86.778101),
    ("broadway", 36.1607,   -86.778231),
    ("broadway", 36.16114,  -86.777657),
    ("broadway", 36.161453, -86.776093),
    ("broadway", 36.16138,  -86.777197),
    ("broadway", 36.161623, -86.776588),
    ("broadway", 36.160693, -86.777446),
    ("broadway", 36.161714, -86.776188),
    ("broadway", 36.160366, -86.77814),
    ("broadway", 36.161003, -86.778071),
    ("broadway", 36.16184,  -86.775714),
    ("broadway", 36.16094,  -86.7782),
    ("broadway", 36.16089,  -86.777858),
    ("broadway", 36.160824, -86.778157),
    ("broadway", 36.161654, -86.776579),
    ("second",   36.160941, -86.774824),
    ("second",   36.162208, -86.775528),
    ("second",   36.16254,  -86.77506),
    ("second",   36.162923, -86.775482),
    ("second",   36.163487, -86.775917),
    ("second",   36.162919, -86.77616),
    ("second",   36.162923, -86.775482),
    ("printers", 36.163513, -86.7781),
    ("printers", 36.163074, -86.777902),
    ("printers", 36.164792, -86.778831),
    ("printers", 36.164179, -86.778134),
    ("printers", 36.164494, -86.779036),
    ("printers", 36.164592, -86.778834),
]

CAT_COLOR = {
    "broadway": (255, 31,  142),  # pink
    "second":   (244, 231, 206),  # cream
    "printers": (45,  212, 191),  # teal
}


# ---------- Image generation ----------
def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        # GitHub Actions ubuntu-latest ships DejaVu by default
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def build_promo_image() -> bytes:
    W, H = 1080, 1080
    bg = (11, 10, 13)
    pink = (255, 31, 142)
    cream = (244, 231, 206)
    teal = (45, 212, 191)
    muted = (201, 194, 214)
    dim = (125, 119, 144)

    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)

    # Eyebrow + headline
    eyebrow = _load_font(28, bold=True)
    headline = _load_font(76, bold=True)
    subhead = _load_font(34)
    body = _load_font(28)
    small = _load_font(22, bold=True)

    draw.text((72, 70), "NEW · INTERACTIVE", font=eyebrow, fill=teal)
    draw.text((72, 110), "Walking Map", font=headline, fill=pink)
    draw.text((72, 200), "of every bar on Lower Broadway", font=subhead, fill=cream)
    draw.text((72, 240), "2nd Avenue & Printers Alley", font=subhead, fill=cream)

    # Map mockup region
    map_x, map_y = 80, 320
    map_w, map_h = 920, 560
    draw.rounded_rectangle(
        [map_x, map_y, map_x + map_w, map_y + map_h],
        radius=24, fill=(15, 13, 20), outline=(40, 36, 50), width=2,
    )

    # Stylized street grid in muted gray
    street_color = (32, 28, 42)
    # Horizontal grid lines
    for i in range(1, 8):
        y = map_y + int(map_h * i / 8)
        draw.line([(map_x + 20, y), (map_x + map_w - 20, y)], fill=street_color, width=1)
    # Vertical grid lines
    for i in range(1, 12):
        x = map_x + int(map_w * i / 12)
        draw.line([(x, map_y + 20), (x, map_y + map_h - 20)], fill=street_color, width=1)

    # Broadway highlighted street (horizontal band)
    broadway_y = map_y + int(map_h * 0.62)
    draw.rectangle(
        [map_x + 20, broadway_y - 4, map_x + map_w - 20, broadway_y + 4],
        fill=(45, 40, 60),
    )
    # 2nd Ave highlighted (vertical band)
    second_x = map_x + int(map_w * 0.55)
    draw.rectangle(
        [second_x - 4, map_y + 20, second_x + 4, map_y + map_h - 20],
        fill=(45, 40, 60),
    )

    # Plot the 37 venues, scaled to the map area
    lats = [v[1] for v in VENUES]
    lngs = [v[2] for v in VENUES]
    lat_min, lat_max = min(lats), max(lats)
    lng_min, lng_max = min(lngs), max(lngs)
    pad = 40

    for cat, lat, lng in VENUES:
        # Scale (note: lat -> Y inverted, lng -> X linear)
        x_frac = (lng - lng_min) / (lng_max - lng_min) if lng_max > lng_min else 0.5
        y_frac = 1 - (lat - lat_min) / (lat_max - lat_min) if lat_max > lat_min else 0.5
        x = map_x + pad + int(x_frac * (map_w - 2 * pad))
        y = map_y + pad + int(y_frac * (map_h - 2 * pad))
        color = CAT_COLOR[cat]
        r = 11
        # Ring + fill for a marker look
        draw.ellipse((x - r - 2, y - r - 2, x + r + 2, y + r + 2), fill=(11, 10, 13))
        draw.ellipse((x - r, y - r, x + r, y + r), fill=color)

    # Legend pills at bottom of map
    legend_y = map_y + map_h - 56
    pills = [
        ("Broadway · 24", pink),
        ("2nd Ave · 7", cream),
        ("Printers Alley · 6", teal),
    ]
    x = map_x + 24
    for text, color in pills:
        # measure text
        bbox = draw.textbbox((0, 0), text, font=small)
        tw = bbox[2] - bbox[0]
        pad_x = 14
        dot_r = 6
        pill_w = tw + 36 + pad_x * 2
        draw.rounded_rectangle([x, legend_y, x + pill_w, legend_y + 36], radius=18, fill=(11, 10, 13))
        draw.ellipse((x + pad_x, legend_y + 18 - dot_r, x + pad_x + 2 * dot_r, legend_y + 18 + dot_r), fill=color)
        draw.text((x + pad_x + 18, legend_y + 7), text, font=small, fill=cream)
        x += pill_w + 12

    # Footer call to action
    draw.text((72, H - 120), "Tap any pin → address + photo + venue page", font=body, fill=muted)
    draw.text((72, H - 70), "barsonbroadway.com", font=_load_font(32, bold=True), fill=pink)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ---------- WordPress upload ----------
def upload_to_wp(image_bytes: bytes, filename: str) -> str:
    auth = base64.b64encode(f"{WP_USERNAME}:{WP_APP_PASSWORD}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth}",
        "Content-Type": "image/png",
        "Content-Disposition": f'attachment; filename="{filename}"',
    }
    url = f"{WP_BASE}/wp-json/wp/v2/media"
    resp = requests.post(url, headers=headers, data=image_bytes, timeout=60)
    if not resp.ok:
        print(f"[wp] upload failed: {resp.status_code} {resp.text[:400]}", file=sys.stderr)
        resp.raise_for_status()
    src = resp.json().get("source_url")
    print(f"[wp] uploaded → {src}")
    return src


# ---------- Instagram post ----------
def post_instagram(image_url: str, caption: str) -> dict:
    base = f"https://graph.facebook.com/{GRAPH_VERSION}/{IG_USER_ID}"
    create = requests.post(
        f"{base}/media",
        data={
            "image_url": image_url,
            "caption": caption,
            "access_token": PAGE_TOKEN,
        },
        timeout=60,
    )
    cj = create.json()
    if not create.ok or "id" not in cj:
        print(f"[ig] container creation failed: {create.status_code} {cj}", file=sys.stderr)
        create.raise_for_status()
    container_id = cj["id"]
    print(f"[ig] container created id={container_id}")

    # IG sometimes needs a beat to process the container
    time.sleep(4)

    publish = requests.post(
        f"{base}/media_publish",
        data={
            "creation_id": container_id,
            "access_token": PAGE_TOKEN,
        },
        timeout=60,
    )
    pj = publish.json()
    if not publish.ok or "id" not in pj:
        print(f"[ig] publish failed: {publish.status_code} {pj}", file=sys.stderr)
        publish.raise_for_status()
    print(f"[ig] published id={pj['id']}")
    return pj


# ---------- Facebook Page post ----------
def post_facebook(image_url: str, caption: str) -> dict:
    if not FB_PAGE_ID:
        print("[fb] META_FB_PAGE_ID not set — skipping FB post")
        return {"skipped": True}
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{FB_PAGE_ID}/photos"
    resp = requests.post(
        url,
        data={
            "url": image_url,
            "message": caption,
            "access_token": PAGE_TOKEN,
        },
        timeout=60,
    )
    j = resp.json()
    if not resp.ok or "id" not in j:
        print(f"[fb] post failed: {resp.status_code} {j}", file=sys.stderr)
        resp.raise_for_status()
    print(f"[fb] published id={j['id']}")
    return j


# ---------- Main ----------
def main() -> int:
    print("[step] Building promo image…")
    image_bytes = build_promo_image()
    print(f"[step] Image built: {len(image_bytes)} bytes")

    print("[step] Uploading to WordPress media library…")
    filename = f"bob-walking-map-promo-{int(time.time())}.png"
    image_url = upload_to_wp(image_bytes, filename)

    print("[step] Posting to Instagram…")
    try:
        post_instagram(image_url, CAPTION)
    except Exception as e:
        print(f"[ig] error: {e}", file=sys.stderr)

    print("[step] Posting to Facebook…")
    try:
        post_facebook(image_url, CAPTION)
    except Exception as e:
        print(f"[fb] error: {e}", file=sys.stderr)

    print("[done]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
