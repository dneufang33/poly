"""
Polymarket Module
=================
Fetches markets from Polymarket's public Gamma API.
No API key required.
"""

import requests
import json
import os
import time
from datetime import datetime, timezone

GAMMA_API   = "https://gamma-api.polymarket.com"
TRADES_FILE = "trades.json"
HEADERS     = {"User-Agent": "Mozilla/5.0 (compatible; PredictionBot/1.0)"}

PROP_SUFFIXES = [
    "more markets", "halftime", "half time", "half-time", "corners",
    "cards", "anytime", "first goal", "last goal", "both teams", "btts",
    "clean sheet", "over/under", "player props", "correct score",
    "asian", "handicap", "total goals", "double chance", "draw no bet",
]

SKIP_MARKET_KEYWORDS = [
    "spread", "total", "over", "under", "handicap", "both teams",
    "btts", "corner", "card", "goal", "score", "half", "clean sheet", "anytime",
]


# ── Fetchers ──────────────────────────────────────────────────────────────────

def fetch_events(tag_slug, limit=200):
    """Fetch all active events for a given tag slug."""
    all_events = []
    offset = 0
    while True:
        params = {
            "tag_slug":  tag_slug,
            "active":    "true",
            "closed":    "false",
            "limit":     min(limit, 100),
            "offset":    offset,
            "order":     "endDate",
            "ascending": "true",
        }
        try:
            r = requests.get(f"{GAMMA_API}/events", params=params,
                             headers=HEADERS, timeout=15)
            r.raise_for_status()
            events = r.json()
        except Exception as e:
            print(f"  Polymarket fetch error ({tag_slug}): {e}")
            break
        if not events or not isinstance(events, list):
            break
        all_events.extend(events)
        if len(events) < 100:
            break
        offset += 100
        if len(all_events) >= limit:
            break
        time.sleep(0.3)
    return all_events


def fetch_soccer_events(limit=300):
    return fetch_events("soccer", limit)


def fetch_weather_events(limit=200):
    return fetch_events("weather", limit)


# ── Soccer market parser ──────────────────────────────────────────────────────

def parse_soccer_markets(events):
    """
    Extract home/away win markets from soccer events.
    Skips props, spreads, totals, halftime markets.
    Returns list of dicts with home, away, p_home, p_away, volume etc.
    """
    parsed = []
    for event in events:
        title = event.get("title", "") or ""

        if any(s in title.lower() for s in PROP_SUFFIXES):
            continue

        title_clean = title.replace("vs.", "vs")
        if " - " in title_clean:
            title_clean = title_clean.split(" - ")[0].strip()

        if " vs " not in title_clean:
            continue

        parts = title_clean.split(" vs ")
        if len(parts) != 2:
            continue

        home_name = parts[0].strip()
        away_name = parts[1].strip().rstrip("?").strip()
        if " - " in away_name:
            away_name = away_name.split(" - ")[0].strip()

        markets = event.get("markets", [])
        if not markets:
            continue

        moneyline = None
        for m in markets:
            q          = (m.get("question") or m.get("groupItemTitle") or "").lower()
            sport_type = (m.get("sportsMarketType") or "").lower()

            if any(kw in q for kw in SKIP_MARKET_KEYWORDS):
                continue
            if sport_type in ("winner", "moneyline", "h2h", "match_winner"):
                moneyline = m
                break
            try:
                outcomes_raw = m.get("outcomes", "[]")
                outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                if len(outcomes) == 2:
                    o0, o1 = outcomes[0].lower(), outcomes[1].lower()
                    h, a   = home_name.lower()[:6], away_name.lower()[:6]
                    if (h in o0 or h in o1) and (a in o0 or a in o1):
                        moneyline = m
                        break
            except (json.JSONDecodeError, ValueError):
                continue

        if not moneyline:
            continue

        try:
            outcomes_raw = moneyline.get("outcomes", "[]")
            prices_raw   = moneyline.get("outcomePrices", "[]")
            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
            prices   = [float(p) for p in (
                json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            )]
            if len(outcomes) < 2 or len(prices) < 2:
                continue

            p_home = p_away = None
            for i, o in enumerate(outcomes):
                if home_name.lower()[:6] in o.lower():
                    p_home = prices[i]
                elif away_name.lower()[:6] in o.lower():
                    p_away = prices[i]
            if p_home is None:
                p_home, p_away = prices[0], prices[1]
            if not p_home or not p_away:
                continue

            parsed.append({
                "type":         "soccer",
                "home":         home_name,
                "away":         away_name,
                "p_home":       round(p_home, 4),
                "p_draw":       None,
                "p_away":       round(p_away, 4),
                "condition_id": moneyline.get("conditionId", moneyline.get("id", "")),
                "volume":       float(event.get("volume", 0) or 0),
                "url":          f"https://polymarket.com/event/{event.get('slug','')}",
                "end_date":     event.get("endDate", ""),
                "event_title":  event.get("title", ""),
            })
        except (ValueError, TypeError, json.JSONDecodeError):
            continue

    return parsed


# ── Weather market parser ─────────────────────────────────────────────────────

def parse_weather_markets(events):
    """
    Extract weather/temperature markets.
    Returns list of dicts with question, p_yes, location hints, threshold etc.
    """
    parsed = []
    for event in events:
        title = event.get("title", "") or ""
        markets = event.get("markets", [])
        if not markets:
            continue

        for m in markets:
            question = m.get("question", "") or title
            try:
                outcomes_raw = m.get("outcomes", "[]")
                prices_raw   = m.get("outcomePrices", "[]")
                outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                prices   = [float(p) for p in (
                    json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
                )]
                if len(outcomes) < 2 or len(prices) < 2:
                    continue

                # For binary YES/NO markets take YES price
                yes_price = None
                for i, o in enumerate(outcomes):
                    if o.lower() in ("yes", "over", "above", "higher"):
                        yes_price = prices[i]
                        break
                if yes_price is None:
                    yes_price = prices[0]

                parsed.append({
                    "type":         "weather",
                    "question":     question,
                    "event_title":  title,
                    "p_yes":        round(yes_price, 4),
                    "condition_id": m.get("conditionId", m.get("id", "")),
                    "volume":       float(event.get("volume", 0) or 0),
                    "url":          f"https://polymarket.com/event/{event.get('slug','')}",
                    "end_date":     event.get("endDate", ""),
                    "slug":         event.get("slug", ""),
                    "raw_market":   m,
                })
            except (ValueError, TypeError, json.JSONDecodeError):
                continue

    return parsed


# ── Trade helpers ─────────────────────────────────────────────────────────────

def load_trades():
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE) as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return []
    return []


def save_trades(trades):
    with open(TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=2)


def log_trade(question, event_title, outcome, edge, market_prob,
              model_prob, bet_size, source, condition_id, url,
              extra=None):
    """Universal trade logger for all strategies."""
    trades = load_trades()
    today  = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for t in trades:
        if (t.get("question") == question
                and t.get("outcome") == outcome
                and t.get("timestamp", "").startswith(today)):
            print(f"  Skipping duplicate: {question[:50]}")
            return

    trade = {
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "paper":       True,
        "source":      source,
        "market_id":   condition_id,
        "question":    question,
        "event_title": event_title,
        "outcome":     outcome,
        "edge":        round(edge, 4),
        "market_prob": round(market_prob, 4),
        "my_estimate": round(model_prob, 4),
        "bet_size":    bet_size,
        "prob_before": round(market_prob, 4),
        "prob_after":  None,
        "url":         url,
        "resolved":    False,
        "result":      None,
        "profit":      None,
    }
    if extra:
        trade.update(extra)

    trades.append(trade)
    save_trades(trades)
    print(f"  📝 [{source}] {outcome} — {question[:55]}")
    print(f"     Edge {edge*100:.1f}% | Market {market_prob*100:.1f}% "
          f"Model {model_prob*100:.1f}% | Bet €{bet_size:.2f}")
