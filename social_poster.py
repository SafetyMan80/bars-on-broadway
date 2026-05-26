"""
Bars on Broadway — daily Instagram poster.

Runs from GitHub Actions after the 10am Central scrape.
Pulls the current live_lineup from barsonbroadway.com, generates a branded
square image, commits it to the repo so raw.githubusercontent can serve it,
then publishes a post to Instagram via the Meta Graph API.

Required environment variables:
    META_IG_USER_ID            Instagram Business Account ID
    META_PAGE_ACCESS_TOKEN     Long-lived Facebook Page Access Token

Optional:
    META_FB_PAGE_ID            Facebook Page ID. If set, the same image and
                               caption is also posted to the FB Page (requires
                               pages_manage_posts scope on the token).
"""

from __future__ import annotations

import datetime as _dt
import os
import random
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

WP_BASE = "https://barsonbroadway.com/wp-json/wp/v2"
GRAPH_API = "https://graph.facebook.com/v25.0"
REPO = "SafetyMan80/bars-on-broadway"
BRANCH = "main"
IMAGES_DIR = Path("daily_posts")
RAW_BASE = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}"

# Brand palette (matches the site)
BG = (11, 10, 13)        # #0B0A0D
PINK = (255, 31, 142)    # #FF1F8E
CREAM = (244, 231, 206)  # #F4E7CE
TEAL = (45, 212, 191)    # #2DD4BF
BODY = (201, 194, 214)   # #C9C2D6

# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

IG_USER_ID = os.environ["META_IG_USER_ID"]
TOKEN = os.environ["META_PAGE_ACCESS_TOKEN"]
FB_PAGE_ID = os.environ.get("META_FB_PAGE_ID")  # optional; if set, also posts to FB Page

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
            title = post["title"]["rendered"].strip()
            # Common formats: "Band - Venue" or "Band – Venue"
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
# Image generation
# ---------------------------------------------------------------------------


def _find_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    bold_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
    ]
    reg_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
    ]
    for path in (bold_paths if bold else reg_paths):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def generate_image(by_venue: dict[str, list[str]], out_path: Path) -> Path:
    width, height = 1080, 1080
    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)

    # Pink left accent bar
    draw.rectangle([0, 0, 14, height], fill=PINK)

    # Date (top label)
    today = _dt.date.today()
    date_str = today.strftime("%A, %B %d").upper()
    # Strip a leading zero from day if present (avoid platform issues with %-d)
    date_str = date_str.replace(" 0", " ")
    draw.text((54, 54), date_str, font=_find_font(28), fill=BODY)

    # Headline
    draw.text((54, 96), "LOWER BROADWAY", font=_find_font(76, bold=True), fill=CREAM)
    draw.text((54, 184), "TONIGHT", font=_find_font(76, bold=True), fill=PINK)

    # Featured venues
    populated = [(v, b) for v, b in by_venue.items() if b]
    random.shuffle(populated)
    featured = populated[:6]

    y = 326
    venue_font = _find_font(36, bold=True)
    band_font = _find_font(28)
    for venue, bands in featured:
        if y > height - 180:
            break
        draw.text((54, y), venue.upper(), font=venue_font, fill=TEAL)
        y += 46
        for band in bands[:2]:
            draw.text((76, y), f"+ {band}", font=band_font, fill=CREAM)
            y += 34
        y += 18

    # Footer
    footer_font = _find_font(24)
    draw.text((54, height - 104), "Full schedule and route planner",
              font=footer_font, fill=BODY)
    draw.text((54, height - 72), "barsonbroadway.com",
              font=footer_font, fill=PINK)

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


def build_caption(by_venue: dict[str, list[str]]) -> str:
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
    """Commit the daily image so it's reachable at raw.githubusercontent."""
    subprocess.run(["git", "config", "user.email", "bot@barsonbroadway.com"], check=True)
    subprocess.run(["git", "config", "user.name", "bars-on-broadway-bot"], check=True)
    subprocess.run(["git", "add", str(image_path)], check=True)
    result = subprocess.run(
        ["git", "commit", "-m", f"daily post image {_dt.date.today().isoformat()}"],
        capture_output=True,
        text=True,
    )
    combined = result.stdout + result.stderr
    if result.returncode != 0 and "nothing to commit" not in combined:
        print(combined)
        raise RuntimeError("git commit failed")
    subprocess.run(["git", "push"], check=True)


# ---------------------------------------------------------------------------
# Instagram post
# ---------------------------------------------------------------------------


def post_to_instagram(image_url: str, caption: str) -> str:
    print(f"Creating IG media container from: {image_url}")
    r1 = requests.post(
        f"{GRAPH_API}/{IG_USER_ID}/media",
        data={
            "image_url": image_url,
            "caption": caption,
            "access_token": TOKEN,
        },
        timeout=60,
    )
    j1 = r1.json()
    if "id" not in j1:
        print(f"Container create failed: {j1}")
        sys.exit(1)
    creation_id = j1["id"]
    print(f"Container created: {creation_id}")

    # IG needs a moment to fetch the image
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
    """Post the same image+caption to the FB Page if FB_PAGE_ID is set."""
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
    # Wait for raw.githubusercontent CDN to pick it up
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
