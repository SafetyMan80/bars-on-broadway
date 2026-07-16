#!/usr/bin/env python3
"""site_enhancers.py - weekly SEO + monetization layer for barsonbroadway.com

Runs standalone on GitHub Actions (.github/workflows/weekly_post.yml).
Reads the live_lineup posts broadway_scraper.py already maintains in
WordPress, then publishes ONE weekly post ('This Weekend on Broadway')
containing: Fri/Sat/Sun lineups, MusicEvent JSON-LD for Google event
rich results, and per-venue affiliate links.

Affiliate links stay hidden until repo secrets AFF_EXPEDIA_AFFCID /
AFF_VIATOR_PID are set. Zero changes to broadway_scraper.py; uses the
same WP_* secrets.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from zoneinfo import ZoneInfo

import requests

CENTRAL = ZoneInfo("America/Chicago")
WP_BASE_URL = (os.environ.get("WP_BASE_URL") or "https://barsonbroadway.com").rstrip("/")
WP_USERNAME = os.environ.get("WP_USERNAME", "")
WP_APP_PASSWORD = os.environ.get("WP_APP_PASSWORD", "")
AFF_EXPEDIA = os.environ.get("AFF_EXPEDIA_AFFCID", "").strip()
AFF_VIATOR = os.environ.get("AFF_VIATOR_PID", "").strip()

SITE = "https://barsonbroadway.com"
# CJ Deep Link Automation (website 101764714). Placed in post HTML, it
# auto-converts any plain link to a joined CJ advertiser (Expedia, Hotels.com,
# Booking, etc.) into a tracked, commission-earning link at click time.
CJ_SCRIPT = ('<script src="https://www.anrdoezrs.net/am/101764714'
             '/include/allCj/impressions/page/am.js"></' + 'script>')
CPT = "live_lineup"
F_VENUE, F_DATE = "venue_name", "lineup_date"
F_START, F_PERFORMER, F_STAGE = "shift_start_time", "performer_name", "stage_floor"

VENUE_GEO = {
    "Acme Feed & Seed": (36.161864, -86.774319),
    "Bootleggers Inn": (36.161578, -86.775369),
    "The Redneck Riviera": (36.161840, -86.775714),
    "Kid Rock's Big Honky Tonk": (36.161447, -86.775776),
    "Ole Red Nashville": (36.161714, -86.776188),
    "Luke's 32 Bridge": (36.161453, -86.776093),
    "Whiskey Bent Saloon": (36.161654, -86.776579),
    "Jason Aldean's Kitchen + Rooftop Bar": (36.161346, -86.776329),
    "Miranda Lambert's Casa Rosa": (36.161623, -86.776588),
    "Kane Brown's On Broadway": (36.161518, -86.776787),
    "Margaritaville Nashville": (36.161380, -86.777197),
    "Honky Tonk Central": (36.160877, -86.776800),
    "Dierks Bentley's Whiskey Row": (36.161110, -86.777394),
    "Lucky Bastard Saloon": (36.161140, -86.777657),
    "Nudie's Honky Tonk": (36.160693, -86.777446),
    "Friends in Low Places": (36.160695, -86.777595),
    "The Stage on Broadway": (36.160890, -86.777858),
    "Robert's Western World": (36.161003, -86.778071),
    "Layla's Honky Tonk": (36.160935, -86.778101),
    "The Second Fiddle": (36.160940, -86.778200),
    "AJ's Good Time Bar": (36.160432, -86.777814),
    "Tootsie's Orchid Lounge": (36.160824, -86.778157),
    "Legends Corner": (36.160700, -86.778231),
    "Rippy's Bar & Grill": (36.160366, -86.778140),
    "Big Jimmy's": (36.162208, -86.775528),
    "Doc Holliday's Saloon": (36.162540, -86.775060),
    "Barstool Nashville": (36.160941, -86.774824),
    "PBR Nashville": (36.162923, -86.775482),
    "The Spot by Dre and Snoop": (36.162923, -86.775482),
    "The Lounge @2nd": (36.162919, -86.776160),
    "Pete's Dueling Piano Bar": (36.163487, -86.775917),
    "Blueprint Underground": (36.163074, -86.777902),
    "Alley Taps": (36.163513, -86.778100),
    "Bourbon Street Blues and Boogie Bar": (36.164792, -86.778831),
    "Skull's Rainbow Room": (36.164592, -86.778834),
    "Sinatra Bar & Lounge": (36.164494, -86.779036),
    "Lonnie's Western Room": (36.164179, -86.778134),
}


def wp():
    s = requests.Session()
    s.auth = (WP_USERNAME, WP_APP_PASSWORD)
    s.headers.update({"Accept": "application/json"})
    return s


def fetch_shifts(s):
    out, page = [], 1
    while page <= 40:
        r = s.get(f"{WP_BASE_URL}/wp-json/wp/v2/{CPT}",
                  params={"per_page": 100, "page": page, "status": "publish"},
                  timeout=30)
        if r.status_code == 400:
            break
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for p in batch:
            acf = p.get("acf") or {}
            if acf.get(F_DATE):
                out.append(acf)
        page += 1
    return out


def tz_offset(d):
    y = d.year
    m = dt.date(y, 3, 8)
    dst_a = m + dt.timedelta(days=(6 - m.weekday()) % 7)
    n = dt.date(y, 11, 1)
    dst_b = n + dt.timedelta(days=(6 - n.weekday()) % 7)
    return "-05:00" if dst_a <= d < dst_b else "-06:00"


def weekend(today):
    fri = today + dt.timedelta(days=(4 - today.weekday()) % 7)
    return [fri, fri + dt.timedelta(days=1), fri + dt.timedelta(days=2)]


def build_jsonld(days, by_day):
    graph = []
    for d in days:
        for acf in by_day.get(d.isoformat(), []):
            start = (acf.get(F_START) or "").strip()
            venue = acf.get(F_VENUE, "")
            if not start or venue not in VENUE_GEO:
                continue
            lat, lng = VENUE_GEO[venue]
            graph.append({
                "@type": "MusicEvent",
                "name": f"{acf.get(F_PERFORMER, 'Live music')} at {venue}",
                "startDate": f"{d.isoformat()}T{start}:00{tz_offset(d)}",
                "performer": {"@type": "MusicGroup",
                              "name": acf.get(F_PERFORMER, "Live music")},
                "location": {
                    "@type": "MusicVenue", "name": venue,
                    "address": {"@type": "PostalAddress",
                                "addressLocality": "Nashville",
                                "addressRegion": "TN",
                                "addressCountry": "US"},
                    "geo": {"@type": "GeoCoordinates",
                            "latitude": lat, "longitude": lng}},
                "eventStatus": "https://schema.org/EventScheduled",
                "eventAttendanceMode":
                    "https://schema.org/OfflineEventAttendanceMode",
                "isAccessibleForFree": True,
                "url": f"{SITE}/schedule/"})
    if not graph:
        return ""
    payload = {"@context": "https://schema.org", "@graph": graph}
    return ('<script type="application/ld+json">'
            + json.dumps(payload, separators=(",", ":")) + "</" + "script>")


def aff_links(venue):
    # Plain Expedia (a joined CJ advertiser) Nashville hotel search near the
    # venue. CJ_SCRIPT on the page converts it to a tracked link on click, so
    # no per-link IDs or secrets are needed.
    lat, lng = VENUE_GEO.get(venue, (36.1612, -86.7775))
    url = ("https://www.expedia.com/Hotel-Search?destination=Nashville%2C%20TN"
           f"&latLong={lat}%2C{lng}")
    return (f' &middot; <a href="{url}" target="_blank" '
            f'rel="nofollow sponsored">hotels nearby</a>')

def build_post(days, by_day):
    label = f"{days[0].strftime('%B %-d')}-{days[-1].strftime('%-d, %Y')}"
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
            f"</strong> - {n} at {v}{aff_links(v)}</li>"
            for t, v, n in rows)
        extra = ""
        if nightly:
            extra = ("<p><em>Also open with live music nightly: "
                     + ", ".join(sorted(set(nightly))) + "</em></p>")
        if items or extra:
            sections.append(f"<h2>{d.strftime('%A')} on Broadway</h2>"
                            f"<ul>{items}</ul>{extra}")
    total = sum(len(by_day.get(d.isoformat(), [])) for d in days)
    intro = (f"<p>Planning a night on Lower Broadway this weekend? Here is "
             f"every set we track across 37 honky-tonks for {label}. "
             f"Lineups shift daily - the <a href='{SITE}/schedule/'>live "
             f"schedule</a> always has tonight's latest.</p>")
    html = intro + "".join(sections) + build_jsonld(days, by_day) + CJ_SCRIPT
    return {
        "title": f"Who's Playing on Broadway This Weekend ({label})",
        "slug": f"broadway-this-weekend-{days[0].isoformat()}",
        "content": html,
        "status": "publish",
    }, total


def upsert(s, post):
    r = s.get(f"{WP_BASE_URL}/wp-json/wp/v2/posts",
              params={"slug": post["slug"], "status": "publish,draft"},
              timeout=30)
    r.raise_for_status()
    hits = r.json()
    url = f"{WP_BASE_URL}/wp-json/wp/v2/posts"
    if hits:
        url += f"/{hits[0]['id']}"
    r = s.post(url, json=post, timeout=60)
    r.raise_for_status()
    return r.json().get("link", "")


def main():
    if not WP_USERNAME or not WP_APP_PASSWORD:
        print("Missing WP credentials.")
        return 2
    s = wp()
    today = dt.datetime.now(CENTRAL).date()
    days = weekend(today)
    shifts = fetch_shifts(s)
    by_day = {}
    for acf in shifts:
        by_day.setdefault(str(acf.get(F_DATE)), []).append(acf)
    post, total = build_post(days, by_day)
    if total == 0:
        print("No weekend lineups found; skipping post.")
        return 0
    link = upsert(s, post)
    print(f"Published weekend post ({total} listings): {link}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
