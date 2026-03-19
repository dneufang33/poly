"""
Market Watcher — New Polymarket Event Detection
=================================================
Polls Polymarket's Gamma API for newly opened soccer/sports events.
Compares createdAt timestamps against a stored state file to find
markets that opened since the last check.

New markets = soft prices = most exploitable edge.

State file: market_state.json
  {
    "last_seen_ts": "2026-03-15T10:00:00+00:00",
    "seen_ids": ["event_id_1", "event_id_2", ...]
  }

This module is called by run_sharp.py every 2 hours.
"""

import json
import os
import requests
import time
from datetime import datetime, timezone

GAMMA_API    = "https://gamma-api.polymarket.com"
STATE_FILE   = "market_state.json"
HEADERS      = {"User-Agent": "Mozilla/5.0 (compatible; SportsBot/1.0)"}

# Minimum volume threshold — skip markets above this (already efficient)
# $50,000 in volume = likely already priced efficiently
MAX_VOLUME_FOR_EDGE = 50_000

# Tags to watch for
WATCH_TAGS = ["soccer", "basketball", "nba"]


# ── State management ──────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_seen_ts": None, "seen_ids": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Event fetcher ─────────────────────────────────────────────────────────────

def fetch_recent_events(tag_slug, limit=100):
    """Fetch most recently created events for a tag, newest first."""
    params = {
        "tag_slug":  tag_slug,
        "active":    "true",
        "closed":    "false",
        "limit":     limit,
        "order":     "createdAt",
        "ascending": "false",   # newest first
    }
    try:
        r = requests.get(
            f"{GAMMA_API}/events",
            params=params,
            headers=HEADERS,
            timeout=15
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  Market watcher fetch error ({tag_slug}): {e}")
        return []


def fetch_low_volume_events(tag_slug, limit=100):
    """
    Fetch open events sorted by volume ascending (lowest volume first).
    These are the most inefficiently priced markets.
    """
    params = {
        "tag_slug":  tag_slug,
        "active":    "true",
        "closed":    "false",
        "limit":     limit,
        "order":     "volume",
        "ascending": "true",   # lowest volume first
    }
    try:
        r = requests.get(
            f"{GAMMA_API}/events",
            params=params,
            headers=HEADERS,
            timeout=15
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  Low-volume fetch error ({tag_slug}): {e}")
        return []


# ── New market detector ───────────────────────────────────────────────────────

def find_new_markets():
    """
    Returns events that opened since the last check.
    Updates the state file with new last_seen_ts.

    This is your "early market" signal — these are the softest prices.
    """
    state    = load_state()
    last_ts  = state.get("last_seen_ts")
    seen_ids = set(state.get("seen_ids", []))

    new_events = []
    now_ts     = datetime.now(timezone.utc).isoformat()

    for tag in WATCH_TAGS:
        events = fetch_recent_events(tag, limit=50)
        for e in events:
            event_id   = e.get("id", "")
            created_at = e.get("createdAt", "")

            if event_id in seen_ids:
                continue

            # If we have a last_seen_ts, only include newer events
            if last_ts and created_at <= last_ts:
                continue

            # Must be a match event (contains " vs ")
            title = (e.get("title") or "").replace("vs.", "vs")
            if " vs " not in title:
                continue

            new_events.append(e)
            seen_ids.add(event_id)

        time.sleep(0.5)

    # Update state
    state["last_seen_ts"] = now_ts
    state["seen_ids"]     = list(seen_ids)[-500:]  # keep last 500 to avoid file bloat
    save_state(state)

    return new_events


def find_low_volume_markets(max_volume=MAX_VOLUME_FOR_EDGE):
    """
    Returns currently open match markets with volume below threshold.
    These are candidates for mispricing even if not newly opened.
    """
    low_vol = []
    for tag in WATCH_TAGS:
        events = fetch_low_volume_events(tag, limit=50)
        for e in events:
            vol = float(e.get("volume", 0) or e.get("liquidity", 0) or 0)
            if vol > max_volume:
                break   # sorted ascending, so once we exceed threshold we're done

            title = (e.get("title") or "").replace("vs.", "vs")
            if " vs " not in title:
                continue

            low_vol.append(e)
        time.sleep(0.5)

    return low_vol


def get_candidate_events():
    """
    Returns two lists:
      1. new_events   — just opened (softest prices)
      2. low_vol      — low volume open markets (less efficient)

    Deduplicates between them.
    """
    print("  Checking for new markets...")
    new_events = find_new_markets()
    print(f"  {len(new_events)} new markets since last check")

    print("  Fetching low-volume markets...")
    low_vol = find_low_volume_markets()
    print(f"  {len(low_vol)} low-volume markets (under ${MAX_VOLUME_FOR_EDGE:,})")

    # Deduplicate
    seen = {e["id"] for e in new_events}
    for e in low_vol:
        if e["id"] not in seen:
            new_events.append(e)
            seen.add(e["id"])

    print(f"  {len(new_events)} total candidate markets")
    return new_events
