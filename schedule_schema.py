#!/usr/bin/env python3
"""schedule_schema.py - daily rolling-week MusicEvent schema for barsonbroadway.com

Runs right after the daily scraper. Reads the live_lineup posts the scraper
maintains and publishes/updates ONE evergreen post carrying MusicEvent JSON-LD
for the next 7 days across all 37 venues, plus a visible listing so the markup
matches on-page content (Google requirement). Stable slug + daily refresh =
accumulates SEO authority while staying current.

Reuses helpers from site_enhancers.py; same WP_* secrets. Adds no risk to the
Elementor /schedule/ page - this is a separate, purpose-built events page.
"""
from __future__ import annotations

import datetime as dt
import sys

from site_enhancers import (
    wp, fetch_shifts, build_jsonld,
    CENTRAL, WP_BASE_URL, WP_USERNAME, WP_APP_PASSWORD,
    F_DATE, F_START, F_PERFORMER, F_VENUE, SITE,
)

SLUG = "live-music-broadway-nashville-this-week"


def week_days(today, n=7):
    return [today + dt.timedelta(days=i) for i in range(n)]


def render(days, by_day):
    sections = []
    for d in days:
        rows, nightly = [], []
        for acf in by_day.get(d.isoformat(), []):
            start = (acf.get(F_START) or "").strip()
            name = acf.get(F_PERFORMER) or "Live music"
            venue = acf.get(F_VENUE, "")
            if start:
                rows.append((start, venue, name))
            else:
                nightly.append(venue)
        rows.sort()
        items = "".join(
            f"<li><strong>"
            f"{dt.datetime.strptime(t, '%H:%M').strftime('%-I:%M %p')}"
            f"</strong> - {n} at {v}</li>"
            for t, v, n in rows)
        extra = ""
        if nightly:
            extra = ("<p><em>Also live nightly: "
                     + ", ".join(sorted(set(nightly))) + "</em></p>")
        if items or extra:
            sections.append(f"<h2>{d.strftime('%A, %B %-d')}</h2>"
                            f"<ul>{items}</ul>{extra}")
    return "".join(sections)


def main():
    if not WP_USERNAME or not WP_APP_PASSWORD:
        print("Missing WP credentials.")
        return 2
    s = wp()
    today = dt.datetime.now(CENTRAL).date()
    days = week_days(today, 7)
    shifts = fetch_shifts(s)
    by_day = {}
    for acf in shifts:
        by_day.setdefault(str(acf.get(F_DATE)), []).append(acf)
    total = sum(len(by_day.get(d.isoformat(), [])) for d in days)
    if total == 0:
        print("No upcoming lineups found; leaving existing post untouched.")
        return 0
    label = f"{days[0].strftime('%B %-d')} - {days[-1].strftime('%B %-d, %Y')}"
    intro = (f"<p>Every live-music set we track on Lower Broadway for the next "
             f"7 days ({label}), across 37 honky-tonks. Updated every morning. "
             f"For just tonight, see the "
             f"<a href='{SITE}/schedule/'>live schedule</a>.</p>")
    html = intro + render(days, by_day) + build_jsonld(days, by_day)
    post = {
        "title": f"Live Music on Broadway This Week ({label})",
        "slug": SLUG,
        "content": html,
        "status": "publish",
    }
    r = s.get(f"{WP_BASE_URL}/wp-json/wp/v2/posts",
              params={"slug": SLUG, "status": "publish,draft"}, timeout=30)
    r.raise_for_status()
    hits = r.json()
    url = f"{WP_BASE_URL}/wp-json/wp/v2/posts"
    if hits:
        url += f"/{hits[0]['id']}"
    r = s.post(url, json=post, timeout=60)
    r.raise_for_status()
    print(f"Updated weekly schema post ({total} listings): {r.json().get('link', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
