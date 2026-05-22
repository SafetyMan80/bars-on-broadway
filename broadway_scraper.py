#!/usr/bin/env python3
"""
barsonbroadway.com - Live Music Data Pipeline (AI extraction edition)
=====================================================================
Cloud-hosted, LLM-powered scraper + WordPress sync for Nashville Lower
Broadway venue lineups. Runs free on GitHub Actions, twice a day.
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
# GLOBAL CONFIGURATION & VENUE TARGETS
# =============================================================================

# The live music calendar web addresses for the major Honky Tonks on Broadway.
# Gemini reads the raw text layout from these pages to extract schedules.
VENUES = {
    # The First Six (Celeb & Major Hubs)
    "tootsies": "https://www.tootsies.net/",
    "olered": "https://olered.com/live-music/",
    "honkytonkcentral": "https://www.honkytonkcentral.com/",
    "whiskeyrow": "https://dierkswhiskeyrow.com/nashville-tn/upcoming-events/",
    "lukes32bridge": "https://www.lukes32bridge.com/",
    "jasonaldeans": "https://jasonaldeansnashville.com/",

    # The Historic & Mega Honky-Tonks
    "thestage": "https://thestageonbroadway.com/",
    "legendscorner": "https://legendscorner.com/",
    "tinroof": "https://tinroofbroadway.com/live-music-events/",
    "robertsoffbroadway": "https://robertswesternworld.com/live-music-calendar/",
    "laylas": "https://laylasnashville.com/calendar/",
    "secondfiddler": "https://thesecondfiddle.com/",

    # The Heavy-Hitting Mainstays
    "casamigos": "https://casamigosnashville.com/",
    "mirandalambert": "https://www.casarosanashville.com/",
    "kidrock": "https://kidrockshonkytonkandrockandrollbar.com/"
}

# Core Environment Configuration
WP_URL = "https://barsonbroadway.com/wp-json/wp/v2"
WP_USER = os.getenv("WP_USERNAME")
WP_PASS = os.getenv("WP_APP_PASSWORD")
GEMINI_API_KEY = os.getenv("AI_API_KEY")

# Set up logging to track the sync progress
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("broadway_pipeline")

# Ensure required credentials exist before booting up
if not WP_USER or not WP_PASS:
    logger.critical("Startup failed: Missing WP_USERNAME / WP_APP_PASSWORD. Set them in .env or CI secrets.")
    sys.exit(2)
if not GEMINI_API_KEY:
    logger.critical("Startup failed: Missing AI_API_KEY. Set it in .env or CI secrets.")
    sys.exit(2)

# =============================================================================
# DATA STRUCTURES & GUARDRAILS
# =============================================================================

@dataclass
class LiveShift:
    venue_id: str
    artist_name: str
    start_time: dt.datetime
    end_time: dt.datetime
    stage: str = "Main Stage"
    raw_scraped_text: str = ""
    
    @property
    def deterministic_slug(self) -> str:
        """Creates a unique ID so the same shift never gets duplicated on WordPress."""
        timestamp = self.start_time.strftime("%Y%m%d-%H%M")
        clean_artist = re.sub(r'[^a-z0-9]', '', self.artist_name.lower())
        clean_stage = re.sub(r'[^a-z0-9]', '', self.stage.lower())
        return f"{self.venue_id}-{clean_stage}-{timestamp}-{clean_artist}"

# =============================================================================
# NETWORK ENGINE & SCRAPER HOOKS
# =============================================================================

def create_http_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    return session

def fetch_venue_raw_text(venue_id: str, url: str, session: requests.Session) -> str:
    """Downloads a bar's webpage and strips away all code layouts, leaving clean text."""
    logger.info(f"Fetching raw calendar layouts for: {venue_id.upper()}")
    try:
        response = session.get(url, timeout=15)
        if response.status_code != 200:
            logger.warning(f"Could not reach {venue_id}, HTTP status: {response.status_code}")
            return ""
            
        if BeautifulSoup:
            soup = BeautifulSoup(response.text, "html.parser")
            # Remove scripts and styling elements
            for script in soup(["script", "style", "meta", "noscript", "header", "footer"]):
                script.extract()
            text = soup.get_text(separator=" \n ")
        else:
            text = response.text
            
        # Clean up excess whitespace layout structures
        cleaned_lines = [line.strip() for line in text.splitlines() if line.strip()]
        return "\n".join(cleaned_lines)[:25000] # Cap text safety boundaries
    except Exception as e:
        logger.error(f"Network error processing text stream for {venue_id}: {e}")
        return ""

# =============================================================================
# GEMINI AI INTELLIGENCE LAYER
# =============================================================================

def extract_shifts_via_gemini(venue_id: str, raw_text: str) -> list[dict]:
    """Uses Google Gemini to parse messy calendar text into clean structured arrays."""
    if not raw_text or len(raw_text.strip()) < 50:
        return []

    current_date_str = dt.datetime.now(ZoneInfo("America/Chicago")).strftime("%A, %B %d, %Y")
    logger.info(f"Sending raw data stream to Gemini AI for {venue_id} parsing...")

    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    
    prompt = (
        f"You are an expert data parsing assistant for barsonbroadway.com.\n"
        f"Today's current date in Nashville (CST) is: {current_date_str}.\n\n"
        f"Analyze the raw web text below from the venue '{venue_id}' and extract the live music calendar schedule.\n"
        f"Return a clean JSON array of objects. Each object MUST look exactly like this:\n"
        f'{{"artist_name": "Band Name", "start_time": "YYYY-MM-DD HH:MM", "end_time": "YYYY-MM-DD HH:MM", "stage": "Main Stage"}}\n\n'
        f"Guidelines:\n"
        f"1. Use military time format for dates (e.g., 2026-05-22 14:00).\n"
        f"2. If the text lists a shift crossing midnight (e.g., 10 PM - 2 AM), make sure the 'end_time' rolls over to the next calendar day.\n"
        f"3. Return ONLY the raw JSON array inside your response text block. Do not add any markdown accents or code wrapping.\n\n"
        f"--- RAW WEB TEXT START ---\n{raw_text}\n--- RAW WEB TEXT END ---"
    )

    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    
    try:
        res = requests.post(endpoint, json=payload, timeout=30)
        if res.status_code != 200:
            logger.error(f"Gemini connection failed with status code: {res.status_code}")
            return []
            
        response_data = res.json()
        raw_ai_text = response_data['contents'][0]['parts'][0]['text'].strip()
        
        # Strip code blocks if Gemini accidentally wraps them
        if raw_ai_text.startswith("```"):
            raw_ai_text = re.sub(r'^
http://googleusercontent.com/immersive_entry_chip/0

---

### 🚀 Trigger the Live Stream

Once you save the file, go back to your **Actions** tab, select your workflow, and hit **Run workflow** again. 

Because the code is now looking at the real URLs, it's going to hit all 15 websites, use Gemini to parse the actual schedules, and push them instantly onto `barsonbroadway.com`. Give it a run and see if it completes smoothly!
