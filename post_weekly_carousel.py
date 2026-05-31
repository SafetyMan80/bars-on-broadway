"""
Weekly Broadway carousel poster.

Generates a 5-image IG carousel from the latest schedule data and posts
to Instagram (carousel) and Facebook (single image with photo album link).

Voice rules:
  - Authentic Nashville local, not corporate
  - No em-dashes
  - No "discover", "elevate", "ultimate", "vibrant", "experience the magic"
  - Short, punchy, conversational
  - Reference real venues + bands when possible

Required env:
  WP_USERNAME, WP_APP_PASSWORD (for media upload + schedule fetch)
  META_IG_USER_ID, META_PAGE_ACCESS_TOKEN, META_FB_PAGE_ID

Optional:
  DRY_RUN=1 to skip the actual posting and just save preview images locally.
"""
import os
import re
import sys
import time
import json
import random
import requests
from datetime import datetime
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont

WP_BASE = "https://barsonbroadway.com"
WP_USER = os.environ.get("WP_USERNAME", "")
WP_PASS = os.environ.get("WP_APP_PASSWORD", "")
IG_USER = os.environ.get("META_IG_USER_ID", "")
PAGE_TOKEN = os.environ.get("META_PAGE_ACCESS_TOKEN", "")
FB_PAGE = os.environ.get("META_FB_PAGE_ID", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"

# Brand colors
BG = (11, 10, 13)
CREAM = (244, 231, 206)
PINK = (255, 31, 142)
TEAL = (45, 212, 191)
MUTED = (201, 194, 214)

W, H = 1080, 1080
SAFE = 80  # safe padding

OUTDIR = "pending_carousel"
os.makedirs(OUTDIR, exist_ok=True)


def load_font(size, bold=False):
    """Try to load a system font, fall back to PIL default."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def fetch_schedule():
    """Pull venue/band/time triples from the live /schedule/ page."""
    r = requests.get(f"{WP_BASE}/schedule/", headers={"User-Agent": "BoB-Carousel/1.0"}, timeout=30)
    r.raise_for_status()
    html = r.text
    # Use a tolerant regex-based parser instead of BeautifulSoup to keep deps light
    venue_pattern = re.compile(
        r'<h2 class="bob-schedule__venue">\s*<a href="([^"]+)">([^<]+)</a>.*?</h2>\s*(.*?)(?=<h2 class="bob-schedule__venue">|$)',
        re.S
    )
    show_pattern = re.compile(
        r'<span class="bob-time">([^<]+)</span>.*?<span class="bob-band">([^<]+)</span>',
        re.S
    )
    items = []
    for vurl, vname, block in venue_pattern.findall(html):
        for time_str, band in show_pattern.findall(block):
            band = band.strip()
            time_str = time_str.strip()
            if not band or band.lower() in ("live music", "live country music", "tbd", "tba"):
                continue
            items.append({
                "venue": vname.strip(),
                "venue_url": vurl.strip(),
                "time": time_str,
                "band": band,
            })
    return items


def pick_highlights(items, n=4):
    """Pick n diverse items: max 1 per venue, prefer prime time (6pm-10pm)."""
    by_venue = {}
    for it in items:
        by_venue.setdefault(it["venue"], []).append(it)

    def prime_score(it):
        t = it["time"].upper()
        # 6 PM - 10 PM gets best score
        m = re.match(r"(\d{1,2}):\d{2}\s*(AM|PM)", t)
        if not m:
            return 0
        h = int(m.group(1))
        if m.group(2) == "PM" and h != 12:
            h += 12
        if m.group(2) == "AM" and h == 12:
            h = 0
        if 18 <= h <= 22:
            return 3
        if 15 <= h <= 23:
            return 2
        return 1

    picks = []
    venue_keys = list(by_venue.keys())
    random.shuffle(venue_keys)
    for v in venue_keys:
        if len(picks) >= n:
            break
        sorted_shows = sorted(by_venue[v], key=prime_score, reverse=True)
        picks.append(sorted_shows[0])
    return picks


def wrap_text(draw, text, font, max_width):
    words = text.split()
    lines = []
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] <= max_width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def draw_text_block(draw, text, font, x, y, color, max_width=None, line_height=1.15):
    if max_width:
        lines = wrap_text(draw, text, font, max_width)
    else:
        lines = [text]
    bbox = draw.textbbox((0, 0), "Ay", font=font)
    lh = int((bbox[3] - bbox[1]) * line_height)
    for i, line in enumerate(lines):
        draw.text((x, y + i * lh), line, font=font, fill=color)
    return y + len(lines) * lh


def slide_cover(week_str):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    eyebrow = load_font(28, bold=True)
    headline = load_font(96, bold=True)
    sub = load_font(38)
    foot = load_font(26, bold=True)

    d.text((SAFE, SAFE + 60), "THIS WEEK ON BROADWAY", font=eyebrow, fill=TEAL)
    # Big headline
    headline_y = draw_text_block(d, "Five bands", headline, SAFE, SAFE + 130, PINK, max_width=W - 2 * SAFE, line_height=1.0)
    headline_y = draw_text_block(d, "worth your walk.", headline, SAFE, headline_y + 12, CREAM, max_width=W - 2 * SAFE, line_height=1.0)

    draw_text_block(d, week_str, sub, SAFE, headline_y + 40, MUTED)

    # Footer
    d.text((SAFE, H - SAFE - 40), "BARSONBROADWAY.COM", font=foot, fill=PINK)
    # Accent line
    d.rectangle([SAFE, H - SAFE - 60, SAFE + 160, H - SAFE - 56], fill=TEAL)
    return img


def slide_pick(idx, pick, total):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    eyebrow = load_font(24, bold=True)
    number = load_font(120, bold=True)
    band_font = load_font(72, bold=True)
    venue_font = load_font(36)
    time_font = load_font(32, bold=True)
    foot = load_font(22, bold=True)

    d.text((SAFE, SAFE + 60), f"PICK {idx} OF {total}", font=eyebrow, fill=TEAL)
    d.text((SAFE, SAFE + 100), f"0{idx}", font=number, fill=PINK)

    band_y = SAFE + 280
    band_y = draw_text_block(d, pick["band"], band_font, SAFE, band_y, CREAM, max_width=W - 2 * SAFE, line_height=1.05)
    d.text((SAFE, band_y + 24), pick["time"], font=time_font, fill=TEAL)
    draw_text_block(d, f"at {pick['venue']}", venue_font, SAFE, band_y + 70, MUTED, max_width=W - 2 * SAFE)

    d.text((SAFE, H - SAFE - 40), "FULL LINEUP AT BARSONBROADWAY.COM", font=foot, fill=PINK)
    d.rectangle([SAFE, H - SAFE - 60, SAFE + 160, H - SAFE - 56], fill=TEAL)
    return img


def slide_outro():
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    eyebrow = load_font(24, bold=True)
    headline = load_font(76, bold=True)
    body = load_font(34)
    url = load_font(40, bold=True)
    foot = load_font(22, bold=True)

    d.text((SAFE, SAFE + 60), "PLAN THE NIGHT", font=eyebrow, fill=TEAL)
    y = draw_text_block(d, "Full schedule.", headline, SAFE, SAFE + 130, PINK, max_width=W - 2 * SAFE, line_height=1.0)
    y = draw_text_block(d, "Walking map.", headline, SAFE, y + 8, CREAM, max_width=W - 2 * SAFE, line_height=1.0)
    y = draw_text_block(d, "Bands that are actually worth your time.", body, SAFE, y + 30, MUTED, max_width=W - 2 * SAFE)

    d.text((SAFE, y + 60), "barsonbroadway.com", font=url, fill=TEAL)
    d.text((SAFE, H - SAFE - 40), "NASHVILLE LOWER BROADWAY", font=foot, fill=PINK)
    d.rectangle([SAFE, H - SAFE - 60, SAFE + 160, H - SAFE - 56], fill=TEAL)
    return img


def build_caption(picks):
    """Authentic, Nashville-local voice. No AI tells. No em-dashes."""
    intro_options = [
        "Five bands worth walking Broadway for this week.",
        "If you're hitting Lower Broadway this week, start here.",
        "The lineup that actually matters this week on Broadway.",
        "Skip the tourist trap traps. Here's where to be this week.",
    ]
    outro_options = [
        "Full schedule and a walking map at barsonbroadway.com.",
        "Whole week's lineup plus the walking map: barsonbroadway.com",
        "More at barsonbroadway.com. We track 37 venues so you don't have to.",
    ]
    intro = random.choice(intro_options)
    outro = random.choice(outro_options)
    pick_lines = []
    for i, p in enumerate(picks, 1):
        pick_lines.append(f"{i}. {p['band']} at {p['venue']} ({p['time']})")
    body = "\n".join(pick_lines)
    tags = "#Nashville #NashvilleMusic #LowerBroadway #BroadwayNashville #HonkyTonk #LiveMusic #VisitNashville #NashvilleTN #CountryMusic"
    return f"{intro}\n\n{body}\n\n{outro}\n\n{tags}"


def upload_to_wp(img_path):
    """Upload a local image to WP Media Library, return the public URL."""
    if not WP_USER or not WP_PASS:
        raise RuntimeError("WP credentials missing")
    filename = os.path.basename(img_path)
    with open(img_path, "rb") as f:
        data = f.read()
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": "image/jpeg",
    }
    auth = (WP_USER, WP_PASS)
    r = requests.post(
        f"{WP_BASE}/wp-json/wp/v2/media",
        headers=headers, data=data, auth=auth, timeout=60,
    )
    r.raise_for_status()
    return r.json().get("source_url")


def post_ig_carousel(image_urls, caption):
    """Create IG carousel: child containers then a parent, then publish."""
    if not IG_USER or not PAGE_TOKEN:
        raise RuntimeError("IG credentials missing")
    base = f"https://graph.facebook.com/v21.0/{IG_USER}"
    children = []
    for url in image_urls:
        r = requests.post(f"{base}/media", data={
            "image_url": url,
            "is_carousel_item": "true",
            "access_token": PAGE_TOKEN,
        }, timeout=60)
        r.raise_for_status()
        children.append(r.json()["id"])

    r = requests.post(f"{base}/media", data={
        "media_type": "CAROUSEL",
        "children": ",".join(children),
        "caption": caption,
        "access_token": PAGE_TOKEN,
    }, timeout=60)
    r.raise_for_status()
    parent_id = r.json()["id"]

    # Poll status, then publish
    for _ in range(20):
        s = requests.get(f"https://graph.facebook.com/v21.0/{parent_id}",
                         params={"fields": "status_code", "access_token": PAGE_TOKEN},
                         timeout=30).json()
        if s.get("status_code") == "FINISHED":
            break
        time.sleep(3)

    r = requests.post(f"{base}/media_publish", data={
        "creation_id": parent_id,
        "access_token": PAGE_TOKEN,
    }, timeout=60)
    r.raise_for_status()
    return r.json()


def post_fb(image_url, caption):
    if not FB_PAGE or not PAGE_TOKEN:
        raise RuntimeError("FB credentials missing")
    r = requests.post(
        f"https://graph.facebook.com/v21.0/{FB_PAGE}/photos",
        data={"url": image_url, "caption": caption, "access_token": PAGE_TOKEN},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def main():
    items = fetch_schedule()
    if not items:
        print("No schedule items found. Skipping post.")
        sys.exit(0)

    picks = pick_highlights(items, n=3)
    if len(picks) < 3:
        print(f"Only {len(picks)} picks available. Skipping post (need at least 3).")
        sys.exit(0)

    today = datetime.now()
    week_str = today.strftime("Week of %b %d, %Y")

    images = [slide_cover(week_str)]
    for i, p in enumerate(picks, 1):
        images.append(slide_pick(i, p, len(picks)))
    images.append(slide_outro())

    paths = []
    for i, img in enumerate(images):
        fn = os.path.join(OUTDIR, f"carousel_{today.strftime('%Y%m%d')}_{i}.jpg")
        img.save(fn, "JPEG", quality=92)
        paths.append(fn)
        print(f"Saved {fn}")

    caption = build_caption(picks)
    print("\n--- CAPTION ---")
    print(caption)
    print("---------------\n")

    if DRY_RUN:
        print("DRY_RUN=1 set. Not posting to IG/FB.")
        return

    urls = [upload_to_wp(p) for p in paths]
    print("Uploaded to WP:", urls)

    ig_result = post_ig_carousel(urls, caption)
    print("IG posted:", ig_result)

    fb_result = post_fb(urls[0], caption)
    print("FB posted:", fb_result)


if __name__ == "__main__":
    main()
