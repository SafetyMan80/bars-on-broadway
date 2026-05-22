#!/usr/bin/env python3
"""
barsonbroadway.com - Live Music Data Pipeline (AI extraction edition)
=====================================================================
Cloud-hosted, LLM-powered scraper + WordPress sync for Nashville Lower
Broadway venue lineups. Runs free on GitHub Actions, twice a day.

HOW IT WORKS
------------
For each venue:
  1. Fetch the calendar page (static HTTP, or headless-rendered for JS sites).
  2. Strip it to plain text.
  3. Send that text to Google Gemini, which extracts every performance/shift
     as structured JSON (layout-resilient -- no CSS selectors to break).
  4. EVERY value the model returns is re-validated by the deterministic
     guardrails (build_shift). The model only PARSES; it is never trusted
     to be correct. Malformed or hallucinated values are caught and dropped.
  5. Each valid shift is upserted into the `live_lineup` custom post type via
     the WordPress REST API, writing the Secure Custom Fields (SCF) values.

Two-run lifecycle: 'morning' (full sweep + purge past days) and
'evening' (delta re-scrape + draft vanished shifts). Idempotent: the
deterministic post slug is the composite key, so re-running never duplicates.

WHAT YOU MUST COMPLETE
----------------------
Only the venue calendar URLs (the `VENUES` list). With LLM extraction there
are NO selectors to write -- just the URL. Confirm you are permitted to
scrape each venue (robots.txt + Terms of Use) before enabling it; prefer an
official feed or events API where one exists. Unconfigured venues are skipped.

Python 3.9+.  Dependencies: see requirements.txt.
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
    pass  # In CI, env vars are injected directly; .env is a local convenience.


# =============================================================================
# CONFIGURATION
# =============================================================================

CENTRAL = ZoneInfo("America/Chicago")  # Handles CST/CDT automatically.

WP_BASE_URL = (os.environ.get("WP_BASE_URL") or "https://barsonbroadway.com").rstrip("/")
WP_USERNAME = os.environ.get("WP_USERNAME", "")
WP_APP_PASSWORD = os.environ.get("WP_APP_PASSWORD", "")
ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "")  # optional Slack/Discord

# --- AI extraction (Google Gemini) ---
AI_API_KEY = os.environ.get("AI_API_KEY", "")
# Free-tier model that supports structured JSON output. If Google renames the
# model, set AI_MODEL in the environment -- no code change needed.
AI_MODEL = os.environ.get("AI_MODEL", "gemini-3.5-flash")
AI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
AI_MAX_PAGE_CHARS = 24000  # cap the page text sent to the model

# Custom post type REST base -- set in CPT UI under "REST API base slug".
CPT_REST_BASE = "live_lineup"

# Secure Custom Fields accepts/returns field values under this REST key.
# SCF is a fork of ACF and keeps the "acf" key. The --selftest run verifies it.
WP_FIELD_KEY = "acf"

# SCF field names -- the immutable contract with the WordPress field group.
F_VENUE = "venue_name"
F_DATE = "lineup_date"
F_START = "shift_start_time"
F_END = "shift_end_time"
F_PERFORMER = "performer_name"
F_STAGE = "stage_floor"

DEFAULT_PERFORMER = "Live Music"
DEFAULT_STAGE = "Main Stage"

PURGE_MODE = os.environ.get("PURGE_MODE", "trash").lower()  # "trash" or "delete"

REQUEST_TIMEOUT = 30
HTTP_RETRIES = 4
USER_AGENT = "BarsOnBroadwayBot/2.0 (+https://barsonbroadway.com)"

# Band-name corrections, matched case-insensitively against the cleaned name.
# Seed this as you spot recurring mistakes in the logs.
NAME_OVERRIDES = {
    "tootsies house band": "Tootsie's House Band",
    "the don kelley band": "The Don Kelley Band",
}


# =============================================================================
# DATA MODEL
# =============================================================================

@dataclass
class PerformanceShift:
    """One individual performance/shift. One of these -> one WordPress post."""
    venue_name: str
    lineup_date: str        # canonical YYYY-MM-DD
    shift_start_time: str   # canonical 24h HH:MM
    shift_end_time: str     # canonical 24h HH:MM, or "" if unknown
    performer_name: str
    stage_floor: str
    source_url: str = ""

    def slug(self) -> str:
        """Deterministic slug = composite key. Same shift -> same slug, which
        is how de-duplication and idempotent upserts work."""
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
# DATA-INTEGRITY GUARDRAILS  (strict parsing -- the zero-QA protection)
# Applied to EVERY record, including everything the LLM returns.
# =============================================================================

log = logging.getLogger("bob")


def clean_text(value: Optional[str]) -> str:
    if value is None:
        return ""
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value))
    return re.sub(r"\s+", " ", text).strip()


def slugify(text: str) -> str:
    text = str(text).lower().strip()
    text = re.sub(r"[‘’'`]", "", text)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-")


def parse_date(raw: Optional[str], default_year: Optional[int] = None) -> Optional[str]:
    """Return canonical YYYY-MM-DD, or None if not a valid recognized date.
    Intentionally STRICT -- this re-validates the LLM's output."""
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


def parse_time(raw: Optional[str]) -> Optional[str]:
    """Return canonical 24-hour HH:MM, or None if not a valid time."""
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


def parse_time_range(raw: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if not raw:
        return None, None
    parts = re.split(r"\s*(?:-|–|—|to|till|until)\s*", clean_text(raw), maxsplit=1, flags=re.I)
    if len(parts) == 2:
        return parse_time(parts[0]), parse_time(parts[1])
    return parse_time(parts[0]), None


def normalize_name(raw: Optional[str]) -> str:
    name = clean_text(raw)
    if not name:
        return ""
    return NAME_OVERRIDES.get(name.lower(), name)


def build_shift(venue_name, date_raw, start_raw, end_raw, performer_raw, stage_raw,
                source_url="", default_year=None) -> tuple[Optional[PerformanceShift], list[str]]:
    """Single chokepoint for all data integrity. Returns (shift, problems).
    A non-empty `problems` list means the record is unsafe and is dropped."""
    problems: list[str] = []

    venue = clean_text(venue_name)
    if not venue:
        problems.append("missing venue name")

    date_iso = parse_date(date_raw, default_year)
    if not date_iso:
        problems.append(f"unparseable date: {date_raw!r}")

    start = parse_time(start_raw)
    if not start:
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
# WORDPRESS REST CLIENT
# =============================================================================

class WordPressError(Exception):
    pass


class WordPressClient:
    """Talks to the WordPress REST API for the live_lineup custom post type."""

    def __init__(self, base_url: str, username: str, app_password: str):
        if not username or not app_password:
            raise WordPressError(
                "Missing WP_USERNAME / WP_APP_PASSWORD. Set them in .env or CI secrets."
            )
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

    def verify_connection(self) -> None:
        r = self.session.get(f"{self.api}/users/me", timeout=REQUEST_TIMEOUT)
        if r.status_code == 401:
            raise WordPressError("Authentication failed - check WP_USERNAME / WP_APP_PASSWORD.")
        r.raise_for_status()
        who = r.json().get("name", "?")
        r2 = self.session.get(self.cpt, params={"per_page": 1}, timeout=REQUEST_TIMEOUT)
        if r2.status_code == 404:
            raise WordPressError(
                f"Post type '{CPT_REST_BASE}' not found at the REST API. "
                "Check the CPT UI 'Show in REST API' setting and 'REST API base slug'."
            )
        r2.raise_for_status()
        log.info("Connected to %s as '%s'. Post type '%s' is reachable.",
                 self.base, who, CPT_REST_BASE)

    def self_test_fields(self) -> None:
        """Round-trip a draft post to prove SCF fields read/write cleanly."""
        log.info("Field self-test: creating a temporary draft...")
        probe = PerformanceShift("__selftest__", "2000-01-01", "00:00", "01:00",
                                 "Self Test", "Test Stage")
        r = self.session.post(self.cpt, json={
            "title": "SELFTEST - delete me", "status": "draft",
            "slug": probe.slug(), WP_FIELD_KEY: probe.fields(),
        }, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        post = r.json()
        try:
            acf = post.get(WP_FIELD_KEY) or {}
            if not acf:
                raise WordPressError(
                    f"REST response had no '{WP_FIELD_KEY}' object - confirm the SCF "
                    f"field group has 'Show in REST API' enabled."
                )
            bad = [k for k, v in probe.fields().items()
                   if str(acf.get(k, "")).strip() != str(v).strip()]
            if bad:
                raise WordPressError(
                    "SCF field round-trip mismatch on: " + ", ".join(bad) +
                    ". Likely a date/time PICKER reformatting the value. Fix: set "
                    "lineup_date, shift_start_time, shift_end_time to 'Text' type in SCF."
                )
            log.info("Field self-test PASSED - all 6 SCF fields round-trip cleanly.")
        finally:
            self.session.delete(f"{self.cpt}/{post['id']}", params={"force": "true"},
                                timeout=REQUEST_TIMEOUT)
            log.info("Field self-test: temporary draft removed.")

    def find_by_slug(self, slug: str) -> Optional[dict]:
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

    def _write(self, method: str, url: str, **kwargs):
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

    def create(self, shift: PerformanceShift) -> dict:
        return self._write("POST", self.cpt, json={
            "title": shift.title(), "status": "publish",
            "slug": shift.slug(), WP_FIELD_KEY: shift.fields(),
        })

    def update(self, post_id: int, shift: PerformanceShift) -> dict:
        return self._write("POST", f"{self.cpt}/{post_id}", json={
            "title": shift.title(), "status": "publish", WP_FIELD_KEY: shift.fields(),
        })

    def set_draft(self, post_id: int) -> dict:
        return self._write("POST", f"{self.cpt}/{post_id}", json={"status": "draft"})

    def remove(self, post_id: int) -> dict:
        force = "true" if PURGE_MODE == "delete" else "false"
        return self._write("DELETE", f"{self.cpt}/{post_id}", params={"force": force})


# =============================================================================
# PAGE FETCHING
# =============================================================================

class LayoutShift(Exception):
    """Raised when a page no longer yields usable content."""


def fetch_page(url: str, render_js: bool = False) -> str:
    """Return the HTML of a page. render_js=True renders JavaScript with a
    headless browser (needed for calendars built client-side)."""
    if render_js:
        return _fetch_rendered(url)
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.text


def _fetch_rendered(url: str) -> str:
    """Render a JavaScript page with Playwright.
    Requires:  pip install playwright  &&  playwright install chromium"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "render_js=True needs Playwright. Run: pip install playwright "
            "&& playwright install chromium  (and add those steps to the workflow)."
        ) from exc
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(url, wait_until="networkidle", timeout=45000)
            return page.content()
        finally:
            browser.close()


def html_to_text(html: str) -> str:
    """Strip a page to clean plain text for the LLM (scripts/styles removed)."""
    if BeautifulSoup is None:
        raise RuntimeError("beautifulsoup4 is required. pip install -r requirements.txt")
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "head", "iframe"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text)[:AI_MAX_PAGE_CHARS]


# =============================================================================
# LLM EXTRACTOR  (Google Gemini)
# =============================================================================

class LLMExtractor:
    """Sends raw venue-page text to Gemini and gets structured performance
    records back. The model only PARSES -- every value it returns is then
    re-validated by build_shift(), so it is never trusted blindly."""

    # Structured-output schema. venue_name is NOT requested from the model:
    # we already know which venue's page we are parsing.
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

    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

    def _prompt(self, page_text: str, venue_name: str) -> str:
        year = dt.datetime.now(CENTRAL).year
        return (
            "You are a precise data-extraction engine for a live-music listings "
            f'site. Below is the text of the live-music calendar page for "{venue_name}". '
            "Extract EVERY individual live-music performance / shift you can find.\n\n"
            "For each performance, return these fields:\n"
            f"- lineup_date: the date as YYYY-MM-DD (use the year {year} if the page "
            "omits the year)\n"
            "- shift_start_time: the start time as 24-hour HH:MM\n"
            '- shift_end_time: the end time as 24-hour HH:MM, or "" if not stated\n'
            "- performer_name: the band/artist name, with obvious typos and casing "
            'cleaned up. If a slot names no specific act, use "Live Music".\n'
            '- stage_floor: the stage, floor or room, or "" if not stated\n\n'
            "STRICT RULES:\n"
            "- Extract ONLY performances explicitly present in the text below. "
            "Never guess, infer, or invent a performance, date, time, or name.\n"
            "- If the text contains no performances, return an empty array.\n"
            "- Return only the JSON array, nothing else.\n\n"
            "PAGE TEXT:\n" + page_text
        )

    def extract(self, page_text: str, venue_name: str) -> list[dict]:
        body = {
            "contents": [{"parts": [{"text": self._prompt(page_text, venue_name)}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseSchema": self.SCHEMA,
            },
        }
        resp = requests.post(
            AI_ENDPOINT.format(model=self.model),
            params={"key": self.api_key}, json=body, timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 429:
            raise RuntimeError("Gemini rate limit (429) - free-tier quota reached.")
        if resp.status_code in (400, 403):
            raise RuntimeError(f"Gemini rejected the request ({resp.status_code}): "
                               f"{resp.text[:300]}")
        resp.raise_for_status()
        data = resp.json()
        try:
            raw = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"Unexpected Gemini response shape: {exc}")
        try:
            records = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Gemini returned non-JSON output: {exc}")
        return records if isinstance(records, list) else []


# =============================================================================
# VENUE ADAPTERS
# =============================================================================

class VenueAdapter(ABC):
    def __init__(self, venue_name: str, calendar_url: str):
        self.venue_name = venue_name
        self.calendar_url = calendar_url

    @abstractmethod
    def fetch_shifts(self) -> list[PerformanceShift]:
        ...


class LLMVenueAdapter(VenueAdapter):
    """Recommended adapter. Fetches the calendar page, converts it to text,
    and uses Gemini to extract performances. No CSS selectors -- only a URL."""

    def __init__(self, venue_name: str, calendar_url: str, render_js: bool = False):
        super().__init__(venue_name, calendar_url)
        self.render_js = render_js
        self.extractor = LLMExtractor(AI_API_KEY, AI_MODEL)

    def fetch_shifts(self) -> list[PerformanceShift]:
        page_text = html_to_text(fetch_page(self.calendar_url, render_js=self.render_js))
        if len(page_text) < 60:
            raise LayoutShift(
                f"{self.venue_name}: page text was nearly empty ({len(page_text)} "
                f"chars). The calendar is probably JavaScript-rendered - set "
                f"render_js=True for this venue in the VENUES list."
            )
        records = self.extractor.extract(page_text, self.venue_name)
        log.info("[%s] LLM extracted %d raw record(s).", self.venue_name, len(records))
        shifts: list[PerformanceShift] = []
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


class GenericJsonLdAdapter(VenueAdapter):
    """Fallback: extract schema.org Event objects from a page's JSON-LD."""

    def fetch_shifts(self) -> list[PerformanceShift]:
        if BeautifulSoup is None:
            raise RuntimeError("beautifulsoup4 is required.")
        soup = BeautifulSoup(fetch_page(self.calendar_url), "html.parser")
        events: list[dict] = []
        for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                events.extend(self._collect(json.loads(tag.string or "{}")))
            except (json.JSONDecodeError, TypeError):
                continue
        shifts: list[PerformanceShift] = []
        for ev in events:
            start = _iso_to_dt(ev.get("startDate"))
            end = _iso_to_dt(ev.get("endDate"))
            if not start:
                continue
            shift, _ = build_shift(
                venue_name=self.venue_name,
                date_raw=start.date().isoformat(),
                start_raw=start.strftime("%H:%M"),
                end_raw=end.strftime("%H:%M") if end else None,
                performer_raw=ev.get("name"),
                stage_raw=_event_stage(ev),
                source_url=self.calendar_url,
            )
            if shift:
                shifts.append(shift)
        return shifts

    @staticmethod
    def _collect(node) -> list[dict]:
        found: list[dict] = []
        if isinstance(node, list):
            for item in node:
                found.extend(GenericJsonLdAdapter._collect(item))
        elif isinstance(node, dict):
            if "@graph" in node:
                found.extend(GenericJsonLdAdapter._collect(node["@graph"]))
            types = node.get("@type", "")
            types = types if isinstance(types, list) else [types]
            if any("Event" in str(t) for t in types):
                found.append(node)
        return found


def _iso_to_dt(value) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _event_stage(ev: dict) -> Optional[str]:
    loc = ev.get("location")
    if isinstance(loc, dict):
        return loc.get("name")
    if isinstance(loc, list) and loc and isinstance(loc[0], dict):
        return loc[0].get("name")
    return None


# -----------------------------------------------------------------------------
# VENUE REGISTRY
# Fill in `calendar_url` for each venue. With the "llm" adapter that is ALL you
# need -- no selectors. Set render_js=True if a venue's calendar is built by
# JavaScript (the script tells you in the log if it is). Venues with an empty
# calendar_url are skipped with a warning, so the script runs safely meanwhile.
# -----------------------------------------------------------------------------

@dataclass
class VenueConfig:
    venue_name: str
    calendar_url: str = ""        # TODO: the venue's live-music calendar URL
    adapter: str = "llm"          # "llm" (recommended) or "jsonld"
    render_js: bool = False       # True if the calendar is JavaScript-rendered


VENUES: list[VenueConfig] = [
    VenueConfig("Tootsie's Orchid Lounge"),            # TODO calendar_url
    VenueConfig("Ole Red Nashville"),                  # TODO calendar_url
    VenueConfig("Jason Aldean's Kitchen + Rooftop"),   # TODO calendar_url
    VenueConfig("Luke's 32 Bridge"),                   # TODO calendar_url
    VenueConfig("Honky Tonk Central"),                 # TODO calendar_url
    VenueConfig("Dierks Bentley's Whiskey Row"),       # TODO calendar_url
]


def adapter_for(cfg: VenueConfig) -> VenueAdapter:
    if cfg.adapter == "llm":
        return LLMVenueAdapter(cfg.venue_name, cfg.calendar_url, cfg.render_js)
    if cfg.adapter == "jsonld":
        return GenericJsonLdAdapter(cfg.venue_name, cfg.calendar_url)
    raise ValueError(f"Unknown adapter type: {cfg.adapter}")


# =============================================================================
# SCRAPE ORCHESTRATION
# =============================================================================

@dataclass
class ScrapeResult:
    shifts: list[PerformanceShift] = field(default_factory=list)
    errors: int = 0
    layout_warnings: int = 0


def scrape_all_venues() -> ScrapeResult:
    """Scrape every configured venue. One venue failing never stops the run."""
    result = ScrapeResult()
    for cfg in VENUES:
        if not cfg.calendar_url:
            log.warning("[%s] no calendar_url configured - skipping.", cfg.venue_name)
            continue
        try:
            shifts = adapter_for(cfg).fetch_shifts()
            if not shifts:
                log.warning("[%s] returned 0 shifts - possible layout change "
                            "or genuinely no events.", cfg.venue_name)
                result.layout_warnings += 1
            else:
                log.info("[%s] %d valid shift(s).", cfg.venue_name, len(shifts))
            result.shifts.extend(shifts)
        except LayoutShift as exc:
            log.error("LAYOUT_SHIFT %s", exc)
            result.layout_warnings += 1
            result.errors += 1
        except Exception as exc:  # network / LLM / parse -- isolate it
            log.error("[%s] scrape failed: %s", cfg.venue_name, exc)
            result.errors += 1
    return result


# =============================================================================
# TWO-RUN LIFECYCLE
# =============================================================================

def today_iso() -> str:
    return dt.datetime.now(CENTRAL).date().isoformat()


def run_pipeline(client: WordPressClient, mode: str, dry_run: bool = False) -> int:
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
                    log.info("[dry-run] would purge past post %s (%s)", post["id"], post_date)
                else:
                    try:
                        client.remove(post["id"])
                    except WordPressError as exc:
                        log.error("Purge failed for post %s: %s", post["id"], exc)
                        errors += 1
                        continue
                purged += 1
        log.info("Purged %d past-dated post(s).", purged)

    scraped_slugs: set[str] = set()
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
                    log.info("[dry-run] would UPDATE post %s -> %s",
                             existing["id"], shift.title())
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
            if post.get("slug") not in scraped_slugs:
                if dry_run:
                    log.info("[dry-run] would DRAFT vanished post %s", post["id"])
                else:
                    try:
                        client.set_draft(post["id"])
                    except WordPressError as exc:
                        log.error("Draft failed for post %s: %s", post["id"], exc)
                        errors += 1
                        continue
                drafted += 1
        log.info("Drafted %d vanished shift(s).", drafted)

    elapsed = round(time.time() - started, 1)
    if errors == 0:
        summary = (f"barsonbroadway.com Lineups Updated Successfully - "
                   f"{created} Shifts Created / {updated} Shifts Updated - 0 Errors.")
        log.info(summary)
        if drafted or bypassed or purged:
            log.info("(also: %d drafted, %d unchanged, %d purged, %ss)",
                     drafted, bypassed, purged, elapsed)
        print(summary)
        return 0

    summary = (f"barsonbroadway.com Lineup Sync Completed WITH ISSUES - "
               f"{created} Created / {updated} Updated / {drafted} Drafted - "
               f"{errors} Error(s). See log.")
    log.error(summary)
    print(summary)
    alert(summary)
    return 1


def alert(message: str) -> None:
    if not ALERT_WEBHOOK_URL:
        log.info("No ALERT_WEBHOOK_URL set - skipping alert.")
        return
    try:
        requests.post(ALERT_WEBHOOK_URL, json={"text": message, "content": message},
                      timeout=15)
    except requests.exceptions.RequestException as exc:
        log.error("Alert webhook failed: %s", exc)


# =============================================================================
# ENTRY POINT
# =============================================================================

def resolve_mode(requested: str) -> str:
    if requested in ("morning", "evening"):
        return requested
    return "morning" if dt.datetime.now(CENTRAL).hour < 12 else "evening"


def main() -> int:
    parser = argparse.ArgumentParser(description="barsonbroadway.com lineup sync (AI)")
    parser.add_argument("--mode", choices=["morning", "evening", "auto"], default="auto")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scrape and report, but make no changes in WordPress.")
    parser.add_argument("--selftest", action="store_true",
                        help="Verify the WordPress connection and SCF fields, then exit.")
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

    if args.selftest:
        try:
            client.self_test_fields()
            return 0
        except (WordPressError, requests.exceptions.RequestException) as exc:
            log.critical("Self-test failed: %s", exc)
            return 2

    if any(v.adapter == "llm" for v in VENUES) and not AI_API_KEY:
        log.critical("AI_API_KEY is not set, but venues use LLM extraction. "
                     "Set AI_API_KEY in .env or CI secrets.")
        alert("barsonbroadway.com sync could not start: AI_API_KEY missing.")
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
