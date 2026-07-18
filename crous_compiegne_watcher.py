#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crous_compiegne_watcher.py  (cloud / GitHub Actions version)
==============================================================
Checks the CROUS "Trouver un logement" website
(https://trouverunlogement.lescrous.fr) ONCE and sends a Telegram
alert if a new accommodation appears in Compiegne (60200).

This version runs a single check and exits (no infinite loop) because
it is meant to be triggered on a schedule by GitHub Actions, which
handles the "run every X minutes" part for you in the cloud -- your
own computer does not need to stay on or connected.

Config:
    - TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are read from environment
      variables first (recommended: set them as GitHub Actions secrets).
      If not found in the environment, it falls back to the hardcoded
      values below (handy for testing locally).
"""

import json
import os
import re
import sys
import time
import logging

import requests
from bs4 import BeautifulSoup

# ============================ CONFIG ============================

# --- Telegram ---
# Reads from environment variables first (GitHub Actions secrets),
# falls back to hardcoded values for local testing.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8996319223:AAGm5hb_yIaN4GX_iv_S6ZM7cdhpNMEcGdE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "1154764451")

# --- Website ---
# 47 = next academic year (2026-2027) | 42 = current year (2025-2026)
SEARCH_TOOL_IDS = [42, 47]
BASE_URL = "https://trouverunlogement.lescrous.fr/tools/{tool_id}/search"

# --- Filtering on Compiegne ---
CITY_KEYWORDS = ["COMPIEGNE", "COMPIÈGNE"]
POSTAL_CODE = "60200"

# --- State file (committed back to the repo by the GitHub Action) ---
STATE_FILE = "crous_compiegne_seen.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("crous-watcher")


class RateLimited(Exception):
    """Raised when the server tells us to slow down (HTTP 429)."""
    pass


# ============================ HELPERS ============================


def load_seen_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            log.warning("State file is corrupted, starting fresh.")
    return {}


def save_seen_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set!")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(url, data=payload, timeout=15)
        r.raise_for_status()
    except requests.RequestException as e:
        log.error("Failed to send Telegram message: %s", e)


def fetch_page_html(tool_id, page):
    url = BASE_URL.format(tool_id=tool_id)
    resp = requests.get(url, params={"page": page}, headers=HEADERS, timeout=20)
    if resp.status_code == 429:
        raise RateLimited("Server returned 429 Too Many Requests")
    resp.raise_for_status()
    return resp.text


def extract_listings_from_html(html, tool_id):
    soup = BeautifulSoup(html, "html.parser")
    results = {}

    links = soup.find_all("a", href=re.compile(r"/accommodations/\d+"))
    for link in links:
        href = link.get("href", "")
        m = re.search(r"/accommodations/(\d+)", href)
        if not m:
            continue
        acc_id = m.group(1)

        container = link
        block_text = ""
        for _ in range(4):
            if container.parent is None:
                break
            container = container.parent
            block_text = container.get_text(separator=" | ", strip=True)
            if any(kw in block_text.upper() for kw in CITY_KEYWORDS) or POSTAL_CODE in block_text:
                break

        name = link.get_text(strip=True) or f"Accommodation #{acc_id}"
        full_url = href if href.startswith("http") else f"https://trouverunlogement.lescrous.fr{href}"

        key = f"{tool_id}:{acc_id}"
        if key not in results:
            results[key] = {
                "id": acc_id,
                "tool_id": tool_id,
                "name": name,
                "url": full_url,
                "raw_text": block_text[:300],
            }

    return results


def get_total_pages(html):
    m = re.search(r"page\s+(\d+)\s+sur\s+(\d+)", html, re.IGNORECASE)
    if m:
        return int(m.group(2))
    return 1


def fetch_compiegne_listings():
    compiegne_listings = {}

    for tool_id in SEARCH_TOOL_IDS:
        first_page_html = fetch_page_html(tool_id, 1)
        total_pages = get_total_pages(first_page_html)
        all_listings = dict(extract_listings_from_html(first_page_html, tool_id))

        for page in range(2, total_pages + 1):
            html = fetch_page_html(tool_id, page)
            all_listings.update(extract_listings_from_html(html, tool_id))
            time.sleep(1)

        for key, item in all_listings.items():
            text_upper = item["raw_text"].upper()
            if any(kw in text_upper for kw in CITY_KEYWORDS) or POSTAL_CODE in item["raw_text"]:
                compiegne_listings[key] = item

    return compiegne_listings


def format_notification(item):
    return (
        f"🏠 <b>New accommodation in Compiegne!</b>\n\n"
        f"<b>{item['name']}</b>\n"
        f"{item['raw_text'][:200]}\n\n"
        f"🔗 {item['url']}"
    )


# ============================ SINGLE RUN ============================


def run_once():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID. Exiting.")
        sys.exit(1)

    seen = load_seen_state()

    try:
        current = fetch_compiegne_listings()
    except RateLimited:
        log.warning("Rate limited (429) on this run. Will try again on the next scheduled run.")
        return
    except Exception as e:
        log.error("Unexpected error: %s", e)
        return

    log.info("Found %s accommodation(s) in Compiegne on this check.", len(current))

    new_keys = [k for k in current if k not in seen]

    if new_keys:
        log.info("New listing(s) found: %s", len(new_keys))
        for k in new_keys:
            item = current[k]
            send_telegram_message(format_notification(item))
            log.info("Sent alert for: %s", item["name"])
    else:
        log.info("No change since last check.")

    seen.update(current)
    save_seen_state(seen)


if __name__ == "__main__":
    run_once()
