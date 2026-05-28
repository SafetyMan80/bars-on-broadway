#!/usr/bin/env python3
"""
barsonbroadway.com - Live Music Data Pipeline (AI extraction edition, v2)
==========================================================================
Cloud-hosted, LLM-powered scraper + WordPress sync for Nashville Lower
Broadway venue lineups. Runs free on GitHub Actions, twice a day.

V2 CHANGES (May 2026)
---------------------
1. Added the 16 venues that previously had "no machine-readable lineup."
   They are now scraped against their homepages/event pages with a broader
   prompt that captures specials, DJ nights, themed events, brunch hours,
   cover info -- not just band schedules. Venues that genuinely publish
   nothing still get an explicit "Live music nightly" fallback entry so
   the /schedule/ page shows all 37 bars every night.
2. LLM prompt now extracts ANY scheduled happening (not just bands):
       - band lineups            -> performer_name = "Band Name"
       - DJ sets / themed nights -> performer_name = "DJ Night"
       - special events          -> performer_name = "[Event Title]"
       - brunch / happy hour     -> performer_name = "Brunch", etc.
       - cover charges           -> appended to performer_name
   The stage_floor field carries any extra qualifier (floor, room, "cover $10",
   "industry only", "free entry"). The downstream UI and social poster
   already display performer_name + stage_floor together, so no schema change
   is required.
3. ALWAYS_OPEN venues: 21 of the 37 bars are open nightly with live country
   music regardless of any published calendar. If a scrape finds nothing for
   "tonight" at one of these venues, we synthesize a single fallback entry
   (performer_name="Live music", start="", end="", stage="Main Stage") - times left empty so display shows "check venue for tonight's start time".

HOW IT WORKS
------------
Same as before: fetch -> strip to text -> LLM extract -> validate -> WP upsert.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None  # type: ignore

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# =============================================================================
# CONFIGURATION
# =============================================================================

CENTRAL = ZoneInfo("America/Chicago")

WP_BASE_URL = (os.environ.get("WP_BASE_URL") or "https://barsonbroadway.com").rstrip("/")
WP_USERNAME = os.environ.get("WP_USERNAME", "")
WP_APP_PASSWORD = os.environ.get("WP_APP_PASSWORD", "")
ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "")

AI_API_KEY = os.environ.get("AI_API_KEY", "")
AI_MODEL = os.environ.get("AI_MODEL", "gemini-3.5-flash")
AI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
AI_MAX_PAGE_CHARS = 24000

CPT_REST_BASE = "live_lineup"
WP_FIELD_KEY = "acf"

F_VENUE = "venue_name"
F_DATE = "lineup_date"
F_START = "shift_start_time"
F_END = "shift_end_time"
F_PERFORMER = "performer_name"
F_STAGE = "stage_floor"

DEFAULT_PERFORMER = "Live music"
DEFAULT_STAGE = "Main Stage"
# FALLBACK_START_TIME removed - fallback entries no longer fabricate times.

PURGE_MODE = os.environ.get("PURGE_MODE", "trash").lower()

REQUEST_TIMEOUT = 30
AI_TIMEOUT = 120
VENUE_PAUSE_SECONDS = 5
HTTP_RETRIES = 4
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

NAME_OVERRIDES = {
    "tootsies house band": "Tootsie's House Band",
    "the don kelley band": "The Don Kelley Band",
}

# =============================================================================
# DATA MODEL
# =============================================================================

@dataclass
class PerformanceShift:
    venue_name: str
    lineup_date: str
    shift_start_time: str
    shift_end_time: str
    performer_name: str
    stage_floor: str
    source_url: str = ""

    def slug(self) -> str:
        raw = f"{self.venue_name}-{self.lineup_date}-{self.shift_start_time}-{self.stage_floor}"
        return slugify(raw)[:190]

    def title(self) -> str:
        return f"{self.performer_name} - {self.venue_name}"

    def fields(self) -> dict:
        return {
            F_VENUE: self.venue_name,
            F_DATE: self.lineup_date,
            F_START: self.shift_start_time,
            F_END: self.shift_end_time,
            F_PERFORMER: self.performer_name,
            F_STAGE: self.stage_floor,
        }

    def matches(self, acf: dict) -> bool:
        return all(str(acf.get(k, "")).strip() == str(v).strip()
                   for k, v in self.fields().items())

# =============================================================================
# DATA-INTEGRITY GUARDRAILS
# =============================================================================

log = logging.getLogger("bob")

def clean_text(value):
    if value is None:
        return ""
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value))
    return re.sub(r"\s+", " ", text).strip()

def slugify(text):
    text = str(text).lower().strip()
    text = re.sub(r"[‘’'`]", "", text)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-")

def parse_date(raw, default_year=None):
    if raw is None:
        return None
    s = clean_text(raw)
    if not s:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    if m:
        try:
            return dt.date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        except ValueError:
            return None
    formats = [
        "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y",
        "%A, %B %d, %Y", "%a, %b %d, %Y", "%a %b %d %Y",
        "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y",
        "%B %d", "%b %d", "%A, %B %d", "%a, %b %d", "%m/%d",
    ]
    for fmt in formats:
        try:
            parsed = dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt and "%y" not in fmt:
            parsed = parsed.replace(year=default_year or dt.datetime.now(CENTRAL).year)
        return parsed.date().isoformat()
    return None

def parse_time(raw):
    if raw is None:
        return None
    s = clean_text(raw).lower().replace(".", "")
    if not s:
        return None
    if "noon" in s:
        return "12:00"
    if "midnight" in s:
        return "00:00"
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", s)
    if not m:
        return None
    hour = int(m[1])
    minute = int(m[2]) if m[2] else 0
    meridiem = m[3]
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        if meridiem == "pm" and hour != 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"

def parse_time_range(raw):
    if not raw:
        return None, None
    parts = re.split(r"\s*(?:-|–|—|to|till|until)\s*", clean_text(raw), maxsplit=1, flags=re.I)
    if len(parts) == 2:
        return parse_time(parts[0]), parse_time(parts[1])
    return parse_time(parts[0]), None

def normalize_name(raw):
    name = clean_text(raw)
    if not name:
        return ""
    return NAME_OVERRIDES.get(name.lower(), name)

def build_shift(venue_name, date_raw, start_raw, end_raw, performer_raw, stage_raw,
                source_url="", default_year=None):
    problems = []
    venue = clean_text(venue_name)
    if not venue:
        problems.append("missing venue name")
    date_iso = parse_date(date_raw, default_year)
    if not date_iso:
        problems.append(f"unparseable date: {date_raw!r}")
    start = parse_time(start_raw)
    if start_raw and not start:
        problems.append(f"missing/invalid start time: {start_raw!r}")
    end = parse_time(end_raw)
    if end_raw and not end:
        log.warning("Rejected unparseable end time %r for %s", end_raw, venue)
        end = ""
    performer = normalize_name(performer_raw) or DEFAULT_PERFORMER
    stage = clean_text(stage_raw) or DEFAULT_STAGE
    if problems:
        return None, problems
    return PerformanceShift(
        venue_name=venue, lineup_date=date_iso, shift_start_time=start,
        shift_end_time=end or "", performer_name=performer, stage_floor=stage,
        source_url=source_url,
    ), []

# =============================================================================
# WORDPRESS REST CLIENT (unchanged from v1)
# =============================================================================

class WordPressError(Exception):
    pass

class WordPressClient:
    def __init__(self, base_url, username, app_password):
        if not username or not app_password:
            raise WordPressError("Missing WP_USERNAME / WP_APP_PASSWORD.")
        self.base = base_url.rstrip("/")
        self.api = f"{self.base}/wp-json/wp/v2"
        self.cpt = f"{self.api}/{CPT_REST_BASE}"
        self.session = requests.Session()
        self.session.auth = (username, app_password)
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        retry = Retry(total=HTTP_RETRIES, backoff_factor=1.5,
                      status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=("GET",))
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))

    def verify_connection(self):
        r = self.session.get(f"{self.api}/users/me", timeout=REQUEST_TIMEOUT)
        if r.status_code == 401:
            raise WordPressError("Authentication failed.")
        r.raise_for_status()
        who = r.json().get("name", "?")
        r2 = self.session.get(self.cpt, params={"per_page": 1}, timeout=REQUEST_TIMEOUT)
        if r2.status_code == 404:
            raise WordPressError(f"Post type '{CPT_REST_BASE}' not found.")
        r2.raise_for_status()
        log.info("Connected to %s as '%s'. Post type '%s' is reachable.",
                 self.base, who, CPT_REST_BASE)

    def find_by_slug(self, slug):
        r = self.session.get(self.cpt, params={
            "slug": slug, "status": "publish,draft,pending,future,private", "per_page": 5,
        }, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        items = r.json()
        return items[0] if items else None

    def iter_all_posts(self):
        page = 1
        while page <= 200:
            r = self.session.get(self.cpt, params={
                "per_page": 100, "page": page,
                "status": "publish,draft,pending,future,private",
                "orderby": "id", "order": "asc",
            }, timeout=REQUEST_TIMEOUT)
            if r.status_code == 400:
                break
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            for post in batch:
                yield post
            page += 1

    def _write(self, method, url, **kwargs):
        for attempt in (1, 2):
            try:
                r = self.session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
                r.raise_for_status()
                return r.json() if r.content else {}
            except requests.exceptions.RequestException as exc:
                if attempt == 2:
                    raise WordPressError(f"{method} {url} failed: {exc}") from exc
                log.warning("%s failed (attempt %d), retrying: %s", method, attempt, exc)
                time.sleep(2)

    def create(self, shift):
        return self._write("POST", self.cpt, json={
            "title": shift.title(), "status": "publish",
            "slug": shift.slug(), WP_FIELD_KEY: shift.fields(),
        })

    def update(self, post_id, shift):
        return self._write("POST", f"{self.cpt}/{post_id}", json={
            "title": shift.title(), "status": "publish", WP_FIELD_KEY: shift.fields(),
        })

    def set_draft(self, post_id):
        return self._write("POST", f"{self.cpt}/{post_id}", json={"status": "draft"})

    def remove(self, post_id):
        force = "true" if PURGE_MODE == "delete" else "false"
        return self._write("DELETE", f"{self.cpt}/{post_id}", params={"force": force})

# =============================================================================
# PAGE FETCHING (unchanged from v1)
# =============================================================================

class LayoutShift(Exception):
    pass

def fetch_page(url, render_js=False):
    if render_js:
        return _fetch_rendered(url)
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.text

def _fetch_rendered(url):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("render_js=True needs Playwright.") from exc
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(user_agent=USER_AGENT, ignore_https_errors=True)
            page.goto(url, wait_until="load", timeout=45000)
            page.wait_for_timeout(4500)
            parts = [page.content()]
            for frame in page.frames:
                if frame is page.main_frame:
                    continue
                try:
                    parts.append(frame.content())
                except Exception:
                    pass
            return "\n".join(parts)
        finally:
            browser.close()

def html_to_text(html):
    if BeautifulSoup is None:
        raise RuntimeError("beautifulsoup4 is required.")
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "head", "iframe"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text)[:AI_MAX_PAGE_CHARS]

# =============================================================================
# LLM EXTRACTOR (v2 - broader prompt)
# =============================================================================

class LLMExtractor:
    """V2: extracts ALL scheduled happenings, not just band lineups.
    The model parses; build_shift() re-validates every field."""

    SCHEMA = {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {
                "lineup_date": {"type": "STRING"},
                "shift_start_time": {"type": "STRING"},
                "shift_end_time": {"type": "STRING"},
                "performer_name": {"type": "STRING"},
                "stage_floor": {"type": "STRING"},
            },
            "required": ["lineup_date", "shift_start_time", "performer_name"],
        },
    }

    def __init__(self, api_key, model):
        self.api_key = api_key
        self.model = model

    def _prompt(self, page_text, venue_name, mode="band"):
        year = dt.datetime.now(CENTRAL).year
        # The "band" mode keeps the original behavior for venues that publish
        # actual band calendars. The "events" mode also captures DJ nights,
        # specials, themed events, brunches, etc. -- anything happening at
        # the venue on a specific date.
        if mode == "events":
            specifics = (
                'Extract EVERY scheduled happening, including:\n'
                '- Live band performances\n'
                '- DJ sets / DJ nights (performer_name = "DJ Night" + DJ name if any)\n'
                '- Themed nights ("Industry Monday", "Karaoke Tuesday", "Country Night")\n'
                '- Special events ("Brad Paisley signing", "Bachelorette Brunch")\n'
                '- Brunch / happy hour windows worth highlighting\n'
                '- Watch parties (Predators games, awards shows, etc.)\n\n'
                'For each happening:\n'
                f'- lineup_date: date as YYYY-MM-DD (use year {year} if omitted)\n'
                "- shift_start_time: start as 24-hour HH:MM\n"
                '- shift_end_time: end as 24-hour HH:MM, or "" if not stated\n'
                "- performer_name: band/DJ name OR event title OR theme label.\n"
                '  Examples: "The Don Kelley Band", "DJ Night", "Industry Monday",\n'
                '  "Sunday Brunch", "Karaoke Night", "Predators Watch Party".\n'
                '  If a slot has nothing specific, use "Live music".\n'
                "- stage_floor: stage/floor/room name, or extra qualifier like\n"
                '  "Cover $10", "21+ only", "Free entry", "Rooftop". "" if none.\n'
            )
        else:
            specifics = (
                "Extract EVERY individual live-music performance / shift you can find.\n\n"
                "For each performance:\n"
                f"- lineup_date: date as YYYY-MM-DD (use year {year} if omitted)\n"
                "- shift_start_time: start as 24-hour HH:MM\n"
                '- shift_end_time: end as 24-hour HH:MM, or "" if not stated\n'
                "- performer_name: band/artist name, cleaned up. If no name, "
                '  use "Live Music".\n'
                '- stage_floor: stage/floor/room, or "" if not stated\n'
            )
        return (
            "You are a precise data-extraction engine for a live-music listings "
            f'site. Below is the text of the calendar/events page for "{venue_name}".\n\n'
            + specifics +
            "\nSTRICT RULES:\n"
            "- Extract ONLY things explicitly present in the text. Never guess, "
            "infer, or invent.\n"
            "- If the text contains nothing extractable, return an empty array.\n"
            "- Return only the JSON array, nothing else.\n\n"
            "PAGE TEXT:\n" + page_text
        )

    def extract(self, page_text, venue_name, mode="band"):
        body = {
            "contents": [{"parts": [{"text": self._prompt(page_text, venue_name, mode)}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseSchema": self.SCHEMA,
            },
        }
        resp = None
        for attempt in range(4):
            resp = requests.post(
                AI_ENDPOINT.format(model=self.model),
                params={"key": self.api_key}, json=body, timeout=AI_TIMEOUT,
            )
            if resp.status_code != 429:
                break
            if attempt < 3:
                wait = 30 * (attempt + 1)
                log.warning("Gemini 429 - waiting %ds, retry %d/3.", wait, attempt + 1)
                time.sleep(wait)
        if resp.status_code == 429:
            raise RuntimeError("Gemini rate limit (429) - free-tier quota reached.")
        if resp.status_code in (400, 403):
            raise RuntimeError(f"Gemini rejected request ({resp.status_code}): "
                               f"{resp.text[:300]}")
        resp.raise_for_status()
        data = resp.json()
        try:
            raw = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"Unexpected Gemini response: {exc}")
        try:
            records = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Gemini returned non-JSON: {exc}")
        return records if isinstance(records, list) else []

# =============================================================================
# VENUE ADAPTERS
# =============================================================================

class VenueAdapter(ABC):
    def __init__(self, venue_name, calendar_url):
        self.venue_name = venue_name
        self.calendar_url = calendar_url

    @abstractmethod
    def fetch_shifts(self):
        ...

class LLMVenueAdapter(VenueAdapter):
    """Uses Gemini to extract all scheduled happenings from a venue's page.
    Supports both 'band' (calendar pages) and 'events' (broader) modes."""

    def __init__(self, venue_name, calendar_url, render_js=False, mode="band"):
        super().__init__(venue_name, calendar_url)
        self.render_js = render_js
        self.mode = mode
        self.extractor = LLMExtractor(AI_API_KEY, AI_MODEL)

    def fetch_shifts(self):
        page_text = html_to_text(fetch_page(self.calendar_url, render_js=self.render_js))
        if len(page_text) < 60:
            raise LayoutShift(
                f"{self.venue_name}: page text nearly empty ({len(page_text)} chars). "
                f"Set render_js=True for this venue."
            )
        records = self.extractor.extract(page_text, self.venue_name, mode=self.mode)
        log.info("[%s] LLM extracted %d raw record(s) [mode=%s].",
                 self.venue_name, len(records), self.mode)
        shifts = []
        for rec in records:
            shift, problems = build_shift(
                venue_name=self.venue_name,
                date_raw=rec.get("lineup_date"),
                start_raw=rec.get("shift_start_time"),
                end_raw=rec.get("shift_end_time"),
                performer_raw=rec.get("performer_name"),
                stage_raw=rec.get("stage_floor"),
                source_url=self.calendar_url,
            )
            if shift:
                shifts.append(shift)
            else:
                log.warning("[%s] LLM record failed validation (%s): %r",
                            self.venue_name, "; ".join(problems), rec)
        return shifts

# =============================================================================
# VENUE REGISTRY (V2 - all 37 bars)
# =============================================================================

@dataclass
class VenueConfig:
    venue_name: str
    calendar_url: str = ""
    adapter: str = "llm"
    render_js: bool = False
    mode: str = "band"          # "band" or "events"
    always_open: bool = True    # if True, get a "Live music nightly" fallback when empty

VENUES = [
    # ========================================================================
    # TIER 1: static HTML calendars - dedicated event/calendar pages
    # ========================================================================
    VenueConfig("Acme Feed & Seed",
                "https://www.acmefeedandseed.com/calendar", mode="band"),
    VenueConfig("Miranda Lambert's Casa Rosa",
                "https://casarosanashville.com/music/", mode="band"),
    VenueConfig("Luke's 32 Bridge",
                "https://lukes32bridge.com/live-music/", mode="band"),
    VenueConfig("The Redneck Riviera",
                "https://redneckrivieranashville.com/events/", mode="events"),
    VenueConfig("Ole Red Nashville",
                "https://olered.com/nashville/", mode="events"),
    VenueConfig("Jason Aldean's Kitchen + Rooftop Bar",
                "https://jasonaldeansbar.com/nashville/music/", mode="band"),
    VenueConfig("Margaritaville Nashville",
                "https://www.margaritavillenashville.com/calendar", mode="events"),
    VenueConfig("Bootleggers Inn",
                "https://www.bootleggersnashville.com/live-on-stage", mode="band"),
    VenueConfig("Doc Holliday's Saloon",
                "https://www.dochollidaysnashville.com/live-music", mode="band"),
    VenueConfig("Skull's Rainbow Room",
                "https://www.skullsrainbowroom.com/jazz", mode="band"),
    VenueConfig("Sinatra Bar & Lounge",
                "https://www.sinatranashville.com/entertainment", mode="band"),

    # ========================================================================
    # TIER 2: JavaScript-rendered calendars
    # ========================================================================
    VenueConfig("The Stage on Broadway",
                "https://thestageonbroadway.com/calendar/",
                render_js=True, mode="band"),
    VenueConfig("Legends Corner",
                "https://www.legendscorner.com/viewcalendar",
                render_js=True, mode="band"),
    VenueConfig("Dierks Bentley's Whiskey Row",
                "https://dierkswhiskeyrow.com/nashville-tn/upcoming-events/",
                render_js=True, mode="events"),
    VenueConfig("Friends in Low Places",
                "https://friendsbarnashville.com/events",
                render_js=True, mode="events"),
    VenueConfig("Lucky Bastard Saloon",
                "https://www.luckybastardsaloon.com/live-bands",
                render_js=True, mode="band"),
    VenueConfig("Kane Brown's On Broadway",
                "https://kanebrownsonbroadway.com/live-music/",
                render_js=True, mode="band"),
    VenueConfig("Whiskey Bent Saloon",
                "https://www.whiskeybentsaloon.com/live-on-stage",
                render_js=True, mode="band"),
    VenueConfig("Barstool Nashville",
                "https://www.barstoolnashville.com/events",
                render_js=True, mode="events"),
    VenueConfig("The Lounge @2nd",
                "https://theloungeat2nd.com/nashville-downtown-the-lounge-at-2nd-music-calendar",
                render_js=True, mode="band"),
    VenueConfig("Bourbon Street Blues and Boogie Bar",
                "https://www.bourbonstreetbluesandboogiebar.com/schedule",
                render_js=True, mode="band"),

    # ========================================================================
    # TIER 3 (NEW): venues that don't publish dedicated calendars.
    # We scrape their homepage in "events" mode to catch any specials,
    # themed nights, watch parties, etc. If nothing is found, the
    # always_open=True flag triggers a "Live music nightly" fallback
    # so the venue still appears on /schedule/.
    # ========================================================================
    VenueConfig("Tootsie's Orchid Lounge",
                "https://tootsies.net/", mode="events"),
    VenueConfig("Robert's Western World",
                "https://robertswesternworld.com/", mode="events"),
    VenueConfig("Honky Tonk Central",
                "https://honkytonkcentral.com/", mode="events"),
    VenueConfig("Kid Rock's Big Honky Tonk",
                "https://kidrocksbighonkytonk.com/", mode="events"),
    VenueConfig("AJ's Good Time Bar",
                "https://ajsgoodtimebar.com/", mode="events"),
    VenueConfig("Layla's Honky Tonk",
                "https://www.laylasnashville.com/", mode="events"),
    VenueConfig("The Second Fiddle",
                "https://thesecondfiddle.com/", mode="events"),
    VenueConfig("Nudie's Honky Tonk",
                "https://nudieshonkytonk.com/", mode="events"),
    VenueConfig("Rippy's Bar & Grill",
                "https://rippysbarandgrill.com/", mode="events"),
    VenueConfig("Pete's Dueling Piano Bar",
                "https://www.petesnashville.com/", mode="events"),
    VenueConfig("PBR Nashville",
                "https://www.pbrnashville.com/", mode="events"),
    VenueConfig("The Spot by Dre and Snoop",
                "https://thespotbydreandsnoop.com/", mode="events"),
    VenueConfig("Big Jimmy's",
                "https://www.bigjimmysnashville.com/", mode="events"),
    VenueConfig("Alley Taps",
                "https://www.alleytaps.com/", mode="events"),
    VenueConfig("Lonnie's Western Room",
                "https://www.lonniesnashville.com/", mode="events"),
    VenueConfig("Blueprint Underground",
                "https://blueprintunderground.com/", mode="events"),
]

# CLOSED venues - excluded entirely:
#   FGL House, Crazytown, The George Jones, Wildhorse Saloon,
#   B.B. King's Blues Club, Famous Saloon.

def adapter_for(cfg):
    if cfg.adapter == "llm":
        return LLMVenueAdapter(cfg.venue_name, cfg.calendar_url, cfg.render_js, cfg.mode)
    raise ValueError(f"Unknown adapter: {cfg.adapter}")

# =============================================================================
# SCRAPE ORCHESTRATION
# =============================================================================

@dataclass
class ScrapeResult:
    shifts: list = field(default_factory=list)
    errors: int = 0
    layout_warnings: int = 0
    successful_venues: set = field(default_factory=set)

def make_fallback_shift(venue_name, target_date, source_url=""):
    """Synthesize a 'Live music nightly' entry for a venue that
    scraped empty but is known to be open every night."""
    shift, _ = build_shift(
        venue_name=venue_name,
        date_raw=target_date,
        start_raw="",  # no fabricated start time
        end_raw="",  # no fabricated end time
        performer_raw=DEFAULT_PERFORMER,
        stage_raw=DEFAULT_STAGE,
        source_url=source_url,
    )
    return shift

def scrape_all_venues():
    result = ScrapeResult()
    today = dt.datetime.now(CENTRAL).date().isoformat()

    for cfg in VENUES:
        if not cfg.calendar_url:
            log.warning("[%s] no calendar_url - skipping.", cfg.venue_name)
            continue
        venue_had_shifts = False
        try:
            shifts = adapter_for(cfg).fetch_shifts()
            if shifts:
                log.info("[%s] %d valid shift(s).", cfg.venue_name, len(shifts))
                result.successful_venues.add(cfg.venue_name)
                result.shifts.extend(shifts)
                venue_had_shifts = any(s.lineup_date == today for s in shifts)
            else:
                log.warning("[%s] returned 0 shifts.", cfg.venue_name)
                result.layout_warnings += 1
        except LayoutShift as exc:
            log.error("LAYOUT_SHIFT %s", exc)
            result.layout_warnings += 1
            result.errors += 1
        except Exception as exc:
            log.error("[%s] scrape failed: %s", cfg.venue_name, exc)
            result.errors += 1

        # Fallback: if this venue is always open and we didn't get a shift
        # for TODAY, add a generic "Live music nightly" entry so the
        # venue still appears on /schedule/.
        if cfg.always_open and not venue_had_shifts:
            fallback = make_fallback_shift(cfg.venue_name, today, cfg.calendar_url)
            if fallback:
                log.info("[%s] adding nightly fallback entry.", cfg.venue_name)
                result.shifts.append(fallback)
                result.successful_venues.add(cfg.venue_name)

        time.sleep(VENUE_PAUSE_SECONDS)
    return result

# =============================================================================
# TWO-RUN LIFECYCLE (unchanged from v1)
# =============================================================================

def today_iso():
    return dt.datetime.now(CENTRAL).date().isoformat()

def run_pipeline(client, mode, dry_run=False):
    started = time.time()
    today = today_iso()
    log.info("=== %s run starting (%s) %s===",
             mode.upper(), today, "[DRY RUN] " if dry_run else "")

    created = updated = bypassed = drafted = purged = 0
    scrape = scrape_all_venues()
    errors = scrape.errors

    if mode == "morning":
        for post in client.iter_all_posts():
            acf = post.get(WP_FIELD_KEY) or {}
            post_date = parse_date(acf.get(F_DATE))
            if post_date and post_date < today:
                if dry_run:
                    log.info("[dry-run] would purge post %s", post["id"])
                else:
                    try:
                        client.remove(post["id"])
                    except WordPressError as exc:
                        log.error("Purge failed: %s", exc)
                        errors += 1
                        continue
                purged += 1
        log.info("Purged %d past-dated post(s).", purged)

    scraped_slugs = set()
    for shift in scrape.shifts:
        scraped_slugs.add(shift.slug())
        try:
            existing = client.find_by_slug(shift.slug())
            if existing is None:
                if dry_run:
                    log.info("[dry-run] would CREATE %s", shift.title())
                else:
                    client.create(shift)
                    created += 1
            elif shift.matches(existing.get(WP_FIELD_KEY) or {}) and \
                 existing.get("status") == "publish":
                bypassed += 1
            else:
                if dry_run:
                    log.info("[dry-run] would UPDATE %s", shift.title())
                else:
                    client.update(existing["id"], shift)
                    updated += 1
        except WordPressError as exc:
            log.error("Upsert failed for '%s': %s", shift.title(), exc)
            errors += 1

    if mode == "evening":
        for post in client.iter_all_posts():
            acf = post.get(WP_FIELD_KEY) or {}
            if parse_date(acf.get(F_DATE)) != today or post.get("status") != "publish":
                continue
            post_venue = acf.get(F_VENUE, "")
            if post_venue not in scrape.successful_venues:
                continue
            if post.get("slug") not in scraped_slugs:
                if dry_run:
                    log.info("[dry-run] would DRAFT post %s", post["id"])
                else:
                    try:
                        client.set_draft(post["id"])
                    except WordPressError as exc:
                        log.error("Draft failed: %s", exc)
                        errors += 1
                        continue
                drafted += 1
        log.info("Drafted %d vanished shift(s).", drafted)

    elapsed = round(time.time() - started, 1)
    if errors == 0:
        summary = (f"barsonbroadway.com Lineups Updated Successfully - "
                   f"{created} Created / {updated} Updated - 0 Errors.")
        log.info(summary)
        if drafted or bypassed or purged:
            log.info("(also: %d drafted, %d unchanged, %d purged, %ss)",
                     drafted, bypassed, purged, elapsed)
        print(summary)
        return 0

    summary = (f"barsonbroadway.com Lineup Sync Completed WITH ISSUES - "
               f"{created} Created / {updated} Updated / {drafted} Drafted - "
               f"{errors} Error(s).")
    log.error(summary)
    print(summary)
    alert(summary)
    return 1

def alert(message):
    if not ALERT_WEBHOOK_URL:
        return
    try:
        requests.post(ALERT_WEBHOOK_URL, json={"text": message}, timeout=15)
    except requests.exceptions.RequestException as exc:
        log.error("Alert webhook failed: %s", exc)

# =============================================================================
# ENTRY POINT
# =============================================================================

def resolve_mode(requested):
    if requested in ("morning", "evening"):
        return requested
    return "morning" if dt.datetime.now(CENTRAL).hour < 12 else "evening"

def main():
    parser = argparse.ArgumentParser(description="barsonbroadway.com lineup sync v2")
    parser.add_argument("--mode", choices=["morning", "evening", "auto"], default="auto")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S", stream=sys.stdout,
    )

    try:
        client = WordPressClient(WP_BASE_URL, WP_USERNAME, WP_APP_PASSWORD)
        client.verify_connection()
    except (WordPressError, requests.exceptions.RequestException) as exc:
        log.critical("Startup failed: %s", exc)
        alert(f"barsonbroadway.com sync could not start: {exc}")
        return 2

    if any(v.adapter == "llm" for v in VENUES) and not AI_API_KEY:
        log.critical("AI_API_KEY missing.")
        return 2

    mode = resolve_mode(args.mode)
    try:
        return run_pipeline(client, mode, dry_run=args.dry_run)
    except Exception as exc:
        log.critical("Unhandled error in %s run: %s", mode, exc, exc_info=True)
        alert(f"barsonbroadway.com {mode} sync crashed: {exc}")
        return 2

if __name__ == "__main__":
    sys.exit(main())
