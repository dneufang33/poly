"""
Prediction Market Bot — Main Runner
=====================================
Combines two strategies:

  1. SOCCER: Pinnacle sharp lines vs Polymarket soccer markets
     - Uses The Odds API (free 500 req/month)
     - Only calls Odds API when low-volume markets found (quota efficient)
     - Filters: volume < $8k, edge 5-40%, >30min before kickoff

  2. WEATHER: Open-Meteo ECMWF forecasts vs Polymarket weather markets
     - Completely free, no API key needed
     - Converts weather forecast to probability, compares to market price
     - Filters: volume < $50k, edge > 6%, forecast within 15 days

All trades logged to trades.json as paper trades (nothing executed).
Half-Kelly sizing on €100 bankroll.

Run:
    python run_bot.py            # live paper trading
    python run_bot.py --dry-run  # show opportunities only
"""

import argparse
import json
import os
import time
import requests
import math
from datetime import datetime, timezone, timedelta

from polymarket import (
    fetch_soccer_events, fetch_weather_events,
    parse_soccer_markets, parse_weather_markets,
    load_trades, save_trades, log_trade, TRADES_FILE,
)
from odds_api import (
    fetch_pinnacle_odds, match_to_pinnacle,
    ODDS_API_KEY, SPORTS,
)
from weather_scanner import scan_weather_markets

GAMMA_API  = "https://gamma-api.polymarket.com"
HEADERS    = {"User-Agent": "Mozilla/5.0"}

# ── Config ────────────────────────────────────────────────────────────────────

BANKROLL              = 100.0   # €100 starting bankroll
KELLY_FRACTION        = 0.5     # half Kelly
MAX_BET               = 20.0    # hard cap per bet €
MAX_TRADES_PER_RUN    = 5       # max new trades logged per run

# Soccer filters
SOCCER_MAX_VOLUME     = 8_000   # only inefficient markets
SOCCER_MIN_EDGE       = 0.05    # 5%
SOCCER_MAX_EDGE       = 0.40    # above = likely matching error
SOCCER_MIN_POLY_PROB  = 0.02
SOCCER_MIN_KICKOFF    = 30      # minutes before kickoff

# Weather filters
WEATHER_MAX_VOLUME    = 50_000
WEATHER_MIN_EDGE      = 0.06    # 6% — slightly higher bar given model uncertainty


def kelly_bet(edge, market_prob):
    """Half-Kelly bet sizing."""
    if market_prob <= 0 or market_prob >= 1:
        return 1.0
    our_prob = market_prob + edge
    b        = (1 / market_prob) - 1
    q        = 1 - our_prob
    if b <= 0:
        return 1.0
    kelly_f = max(0, (b * our_prob - q) / b)
    return round(min(kelly_f * KELLY_FRACTION * BANKROLL, MAX_BET), 2)


# ── Resolver ──────────────────────────────────────────────────────────────────

def resolve_trades():
    """Check Polymarket for resolved markets and update paper trades."""
    trades     = load_trades()
    open_paper = [t for t in trades
                  if t.get("paper") and not t.get("resolved") and t.get("market_id")]

    if not open_paper:
        print("  No open trades to resolve.")
        return 0

    print(f"  Checking {len(open_paper)} open trades...")
    updated = 0

    for trade in open_paper:
        try:
            r = requests.get(
                f"{GAMMA_API}/markets",
                params={"conditionId": trade["market_id"]},
                headers=HEADERS, timeout=10
            )
            if r.status_code != 200:
                continue

            data = r.json()
            m    = (data[0] if isinstance(data, list) and data else data)
            if not m or (not m.get("closed") and not m.get("archived")):
                continue

            outcomes_raw = m.get("outcomes", "[]")
            prices_raw   = m.get("outcomePrices", "[]")
            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
            prices   = [float(p) for p in (
                json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            )]
            if not outcomes or not prices:
                continue

            winner = outcomes[prices.index(max(prices))].lower()

            # Map winner to trade outcome
            source = trade.get("source", "")
            if source == "soccer":
                home = trade.get("home", "").lower()
                away = trade.get("away", "").lower()
                if home[:5] in winner or winner in home[:8]:
                    actual = "HOME"
                elif away[:5] in winner or winner in away[:8]:
                    actual = "AWAY"
                else:
                    actual = "DRAW"
            else:
                # Weather / binary markets
                if winner in ("yes", "over", "above", "higher"):
                    actual = "YES"
                else:
                    actual = "NO"

            won    = (actual == trade["outcome"])
            mp     = trade.get("market_prob", 0.5)
            profit = round(trade["bet_size"] * (1/mp - 1), 2) if (won and mp > 0) else -trade["bet_size"]

            trade["resolved"] = True
            trade["result"]   = actual
            trade["profit"]   = profit
            updated += 1
            print(f"  {'✓ WON' if won else '✗ LOST'}  {trade['question'][:50]}  "
                  f"P/L: €{profit:+.2f}")
            time.sleep(0.2)

        except Exception as e:
            print(f"  Resolve error: {e}")
            continue

    if updated:
        save_trades(trades)
        print(f"  Resolved {updated} trades.")
    return updated


# ── Soccer scanner ────────────────────────────────────────────────────────────

def run_soccer_scanner(dry_run=False):
    """Run the Pinnacle vs Polymarket soccer strategy."""
    print("\n── Soccer: Polymarket vs Pinnacle ───────────────────────────")

    if not ODDS_API_KEY:
        print("  No ODDS_API_KEY — skipping soccer strategy")
        print("  Get free key at: https://the-odds-api.com")
        return []

    # Step 1: Fetch Polymarket soccer markets (free)
    events       = fetch_soccer_events(limit=300)
    all_markets  = parse_soccer_markets(events)
    low_vol      = [m for m in all_markets
                    if float(m.get("volume", 0) or 0) <= SOCCER_MAX_VOLUME]
    print(f"  {len(all_markets)} markets parsed → {len(low_vol)} under ${SOCCER_MAX_VOLUME:,}")

    if not low_vol:
        print("  No low-volume markets — Odds API call skipped (quota preserved)")
        return []

    # Step 2: Only now fetch Pinnacle lines
    pinnacle_games = []
    for sport in SPORTS:
        games = fetch_pinnacle_odds(sport)
        pinnacle_games.extend(games)
        time.sleep(1)
    print(f"  {len(pinnacle_games)} Pinnacle games fetched")

    if not pinnacle_games:
        return []

    # Step 3: Find edge
    now      = datetime.now(timezone.utc)
    cutoff   = now + timedelta(minutes=SOCCER_MIN_KICKOFF)
    opps     = []
    skipped  = 0

    for poly in low_vol:
        pinnacle = match_to_pinnacle(poly["home"], poly["away"], pinnacle_games)
        if not pinnacle:
            continue

        # Skip started or imminent matches
        commence = pinnacle.get("commence_time", "")
        if commence:
            try:
                kickoff = datetime.fromisoformat(commence.replace("Z", "+00:00"))
                if kickoff <= cutoff:
                    skipped += 1
                    continue
            except ValueError:
                pass

        checks = [
            ("HOME", pinnacle["p_home"], poly["p_home"]),
            ("AWAY", pinnacle["p_away"], poly["p_away"]),
        ]

        for outcome, pin_prob, poly_prob in checks:
            if not poly_prob or poly_prob < SOCCER_MIN_POLY_PROB:
                continue
            edge = pin_prob - poly_prob
            if edge < SOCCER_MIN_EDGE or edge > SOCCER_MAX_EDGE:
                continue
            bet = kelly_bet(edge, poly_prob)
            if bet < 0.50:
                continue
            opps.append({
                "strategy":      "soccer",
                "question":      poly["event_title"],
                "home":          poly["home"],
                "away":          poly["away"],
                "outcome":       outcome,
                "edge":          edge,
                "poly_prob":     poly_prob,
                "pinnacle_prob": pin_prob,
                "market_prob":   poly_prob,
                "model_prob":    pin_prob,
                "volume":        float(poly.get("volume", 0) or 0),
                "sport":         pinnacle["sport_name"],
                "bet_size":      bet,
                "condition_id":  poly["condition_id"],
                "url":           poly["url"],
                "poly_market":   poly,
            })

    if skipped:
        print(f"  Skipped {skipped} matches (started or <{SOCCER_MIN_KICKOFF}min to kickoff)")

    opps.sort(key=lambda x: x["edge"], reverse=True)

    if opps:
        print(f"\n  {'Edge':>6}  {'Bet':>6}  {'Poly%':>6}  {'Pin%':>6}  "
              f"{'Vol':>8}  Match")
        print(f"  {'-'*68}")
        for o in opps[:8]:
            print(f"  {o['edge']*100:>5.1f}%  €{o['bet_size']:>5.2f}  "
                  f"{o['poly_prob']*100:>5.1f}%  {o['pinnacle_prob']*100:>5.1f}%  "
                  f"${o['volume']:>7,.0f}  {o['home']} vs {o['away']}  [{o['sport']}]")
    else:
        print("  No soccer opportunities found.")

    return opps


# ── Weather scanner ───────────────────────────────────────────────────────────

def run_weather_scanner(dry_run=False):
    """Run the Open-Meteo vs Polymarket weather strategy."""
    print("\n── Weather: Open-Meteo vs Polymarket ────────────────────────")

    events          = fetch_weather_events(limit=200)
    weather_markets = parse_weather_markets(events)
    print(f"  {len(weather_markets)} weather markets found on Polymarket")

    if not weather_markets:
        print("  No weather markets available.")
        return []

    opps = scan_weather_markets(
        weather_markets,
        min_edge=WEATHER_MIN_EDGE,
        max_volume=WEATHER_MAX_VOLUME
    )

    if opps:
        print(f"\n  {'Edge':>6}  {'Bet':>6}  {'Poly%':>6}  {'Mdl%':>6}  "
              f"{'Days':>5}  {'Fcst°C':>7}  Market")
        print(f"  {'-'*75}")
        for o in opps[:8]:
            bet = kelly_bet(o["edge"], o["market_prob"])
            o["bet_size"] = bet
            print(f"  {o['edge']*100:>5.1f}%  €{bet:>5.2f}  "
                  f"{o['poly_prob']*100:>5.1f}%  {o['model_prob']*100:>5.1f}%  "
                  f"{o['days_out']:>5}  {o['forecast_max_c']:>6.1f}°  "
                  f"{o['question'][:40]}")
    else:
        print("  No weather opportunities found.")

    # Prepare unified opportunity format
    unified = []
    for o in opps:
        bet = o.get("bet_size") or kelly_bet(o["edge"], o["market_prob"])
        unified.append({
            "strategy":     "weather",
            "question":     o["question"],
            "outcome":      o["outcome"],
            "edge":         o["edge"],
            "market_prob":  o["market_prob"],
            "model_prob":   o["model_prob_for_trade"],
            "volume":       o["volume"],
            "bet_size":     bet,
            "condition_id": o["market"]["condition_id"],
            "url":          o["market"]["url"],
            "extra": {
                "city":           o["city"],
                "threshold_c":    o["threshold"],
                "direction":      o["direction"],
                "date":           o["date"],
                "days_out":       o["days_out"],
                "forecast_max_c": o["forecast_max_c"],
                "model_prob_raw": o["model_prob"],
            },
        })

    return unified


# ── Main ──────────────────────────────────────────────────────────────────────

def run(dry_run=False):
    print(f"\n{'='*62}")
    print(f"  Prediction Market Bot")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Mode: {'DRY RUN' if dry_run else 'PAPER TRADE'} | "
          f"Bankroll €{BANKROLL:.0f} | Half-Kelly")
    print(f"{'='*62}")

    # Step 1: Resolve finished trades
    print("\n── Step 1: Resolve Finished Trades ──────────────────────────")
    resolve_trades()

    # Step 2: Run both scanners
    soccer_opps  = run_soccer_scanner(dry_run)
    weather_opps = run_weather_scanner(dry_run)

    all_opps = sorted(
        soccer_opps + weather_opps,
        key=lambda x: x["edge"], reverse=True
    )

    # Step 3: Log top trades
    print(f"\n── Step 3: Log Trades ───────────────────────────────────────")
    if not all_opps:
        print("  No opportunities found across all strategies.")
    else:
        print(f"  {len(all_opps)} total opportunities "
              f"({len(soccer_opps)} soccer, {len(weather_opps)} weather)\n")

        if not dry_run:
            logged = 0
            for o in all_opps[:MAX_TRADES_PER_RUN]:
                log_trade(
                    question     = o["question"],
                    event_title  = o.get("question", ""),
                    outcome      = o["outcome"],
                    edge         = o["edge"],
                    market_prob  = o["market_prob"],
                    model_prob   = o["model_prob"],
                    bet_size     = o["bet_size"],
                    source       = o["strategy"],
                    condition_id = o["condition_id"],
                    url          = o["url"],
                    extra        = o.get("extra"),
                )
                logged += 1
            print(f"\n  Logged {logged} paper trades.")
        else:
            print("  Dry run — not logging. Set dry_run=false to log.")

    # Summary
    trades   = load_trades()
    paper    = [t for t in trades if t.get("paper")]
    resolved = [t for t in paper if t.get("resolved") and t.get("profit") is not None]
    total_pl = sum(t["profit"] for t in resolved)
    deployed = sum(t["bet_size"] for t in paper)
    wins     = sum(1 for t in resolved if t["profit"] > 0)
    roi      = (total_pl / deployed * 100) if deployed > 0 else 0

    by_source = {}
    for t in paper:
        s = t.get("source", "unknown")
        by_source.setdefault(s, {"total": 0, "resolved": 0, "pl": 0})
        by_source[s]["total"] += 1
        if t.get("resolved") and t.get("profit") is not None:
            by_source[s]["resolved"] += 1
            by_source[s]["pl"] += t["profit"]

    print(f"\n── Summary ──────────────────────────────────────────────────")
    for src, stats in by_source.items():
        print(f"  [{src}] {stats['total']} trades | "
              f"{stats['resolved']} resolved | P/L €{stats['pl']:+.2f}")
    print(f"  Total: {len(paper)} trades | {len(resolved)} resolved | "
          f"{wins}W/{len(resolved)-wins}L | ROI {roi:+.1f}%")
    print(f"  Bankroll: €{BANKROLL - deployed + total_pl:.2f}")
    print(f"\n  Done ✓\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
