"""
Sharp Line Runner — Pinnacle vs Polymarket
==========================================
Runs every 5 minutes via GitHub Actions (11:00-23:00 UTC).

Flow:
  1. Resolve finished paper trades
  2. Fetch ALL Polymarket soccer events (free, unlimited)
  3. Filter to low-volume markets only (under $8k)
  4. IF any found → call Odds API for Pinnacle lines (preserves quota)
  5. Compare Pinnacle prob vs Polymarket price → find edge
  6. Log paper trades with half-Kelly sizing on €100 bankroll

Filters applied:
  - Volume < $8,000 (inefficient markets only)
  - Edge between 5%-40% (below = no signal, above = likely matching error)
  - Polymarket price > 2% (not illiquid)
  - Kickoff > 30 minutes away (no live markets)
"""

import argparse
import json
import os
import time
import requests
import math
from datetime import datetime, timezone, timedelta

from polymarket import (
    parse_soccer_markets, fetch_all_soccer_events,
    load_trades, save_trades, TRADES_FILE,
)
from odds_api import (
    fetch_pinnacle_odds, match_to_pinnacle, ODDS_API_KEY, SPORTS,
)
from market_watcher import get_candidate_events

GAMMA_API      = "https://gamma-api.polymarket.com"
HEADERS        = {"User-Agent": "Mozilla/5.0"}

BANKROLL       = 100.0
KELLY_FRACTION = 0.5
MIN_EDGE       = 0.05
MAX_EDGE       = 0.40
MAX_VOLUME     = 8_000
MIN_POLY_PROB  = 0.02
MAX_BET        = 20.0
MAX_TRADES     = 5
MINUTES_BEFORE_KICKOFF = 30


def kelly_bet(edge, poly_prob, bankroll=BANKROLL, fraction=KELLY_FRACTION):
    if poly_prob <= 0 or poly_prob >= 1:
        return 1.0
    pin_prob = poly_prob + edge
    b        = (1 / poly_prob) - 1
    q        = 1 - pin_prob
    if b <= 0:
        return 1.0
    kelly_f = max(0, (b * pin_prob - q) / b)
    return round(min(kelly_f * fraction * bankroll, MAX_BET), 2)


def resolve_finished_trades():
    trades     = load_trades()
    open_paper = [t for t in trades
                  if t.get("paper") and not t.get("resolved")
                  and t.get("source") == "pinnacle"
                  and t.get("market_id")]
    if not open_paper:
        print("  No open Pinnacle trades to resolve.")
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
            m    = data[0] if isinstance(data, list) and data else data
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

            winner     = outcomes[prices.index(max(prices))].lower()
            home, away = trade.get("home","").lower(), trade.get("away","").lower()

            if home[:5] in winner or winner in home[:8]:
                actual = "HOME"
            elif away[:5] in winner or winner in away[:8]:
                actual = "AWAY"
            else:
                actual = "DRAW"

            won    = (actual == trade["outcome"])
            mp     = trade.get("market_prob", 0.5)
            profit = round(trade["bet_size"] * (1/mp - 1), 2) if (won and mp > 0) else -trade["bet_size"]

            trade["resolved"] = True
            trade["result"]   = actual
            trade["profit"]   = profit
            updated += 1
            print(f"  {'✓ WON' if won else '✗ LOST'}  {trade['question'][:50]}  P/L: €{profit:+.2f}")
            time.sleep(0.3)

        except Exception as e:
            print(f"  Resolve error: {e}")

    if updated:
        save_trades(trades)
        print(f"  Resolved {updated} trades.")
    return updated


def log_sharp_trade(opp, bet_size):
    trades   = load_trades()
    today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    question = opp["poly_market"].get("event_title", f"{opp['home']} vs {opp['away']}")

    for t in trades:
        if (t.get("question") == question
                and t.get("outcome") == opp["outcome"]
                and t.get("timestamp", "").startswith(today)):
            print(f"  Skipping duplicate: {question[:50]} {opp['outcome']}")
            return

    trade = {
        "timestamp":      datetime.now(timezone.utc).isoformat(),
        "paper":          True,
        "source":         "pinnacle",
        "sport":          opp["sport"],
        "market_id":      opp["poly_market"].get("condition_id", ""),
        "question":       question,
        "home":           opp["home"],
        "away":           opp["away"],
        "outcome":        opp["outcome"],
        "edge":           round(opp["edge"], 4),
        "market_prob":    round(opp["poly_prob"], 4),
        "pinnacle_prob":  round(opp["pinnacle_prob"], 4),
        "my_estimate":    round(opp["pinnacle_prob"], 4),
        "bet_size":       bet_size,
        "poly_volume":    round(opp.get("volume", 0), 2),
        "prob_before":    round(opp["poly_prob"], 4),
        "prob_after":     None,
        "polymarket_url": opp["poly_market"].get("url", ""),
        "resolved":       False,
        "result":         None,
        "profit":         None,
    }
    trades.append(trade)
    save_trades(trades)
    print(f"  📝 {opp['outcome']} — {question[:50]}")
    print(f"     Edge {opp['edge']*100:.1f}% | Poly {opp['poly_prob']*100:.1f}% "
          f"Pinnacle {opp['pinnacle_prob']*100:.1f}% | Bet €{bet_size:.2f} | "
          f"Vol ${opp.get('volume',0):,.0f}")


def run(dry_run=False):
    print(f"\n{'='*62}")
    print(f"  Sharp Line Scanner (Pinnacle vs Polymarket)")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Mode: {'DRY RUN' if dry_run else 'PAPER TRADE'} | "
          f"Bankroll €{BANKROLL:.0f} | Half-Kelly | Max vol ${MAX_VOLUME:,}")
    print(f"{'='*62}")

    if not ODDS_API_KEY:
        print("\n  ⚠ No ODDS_API_KEY — get free key at the-odds-api.com")
        return

    # Step 1: Resolve finished trades
    print("\n── Step 1: Resolve Finished Trades ──────────────────────────")
    resolve_finished_trades()

    # Step 2: Fetch Polymarket first (free, no quota)
    print("\n── Step 2: Fetch Polymarket Soccer Markets ──────────────────")
    all_events   = fetch_all_soccer_events(limit=300)
    poly_markets = parse_soccer_markets(all_events)

    # Filter to low-volume only
    low_vol = [m for m in poly_markets
               if float(m.get("volume", 0) or 0) <= MAX_VOLUME]
    print(f"  {len(all_events)} events → {len(poly_markets)} match markets → "
          f"{len(low_vol)} under ${MAX_VOLUME:,}")

    if not low_vol:
        print("  No low-volume markets found — skipping Odds API (quota preserved)")
        print("  Done ✓\n")
        return

    # Step 3: Only NOW call Odds API — we have confirmed candidates
    print("\n── Step 3: Fetch Pinnacle Lines (conditional on Step 2) ─────")
    pinnacle_games = []
    for sport in SPORTS:
        games = fetch_pinnacle_odds(sport)
        pinnacle_games.extend(games)
        time.sleep(1)
    print(f"  {len(pinnacle_games)} Pinnacle games fetched across {len(SPORTS)} sports")

    if not pinnacle_games:
        print("  No Pinnacle lines returned — check ODDS_API_KEY")
        return

    g = pinnacle_games[0]
    d = f"{g['p_draw']*100:.1f}%" if g.get("p_draw") else "N/A"
    print(f"  Sample: {g['home_team']} vs {g['away_team']} "
          f"H:{g['p_home']*100:.1f}% D:{d} A:{g['p_away']*100:.1f}%")

    # Step 4: Find edge
    print("\n── Step 4: Compare Pinnacle vs Polymarket ───────────────────")
    now         = datetime.now(timezone.utc)
    cutoff      = now + timedelta(minutes=MINUTES_BEFORE_KICKOFF)
    opportunities = []
    skipped_live  = 0

    for poly in low_vol:
        pinnacle = match_to_pinnacle(poly["home"], poly["away"], pinnacle_games)
        if not pinnacle:
            continue

        # Skip if match already started or too close to kickoff
        commence = pinnacle.get("commence_time", "")
        if commence:
            try:
                kickoff = datetime.fromisoformat(commence.replace("Z", "+00:00"))
                if kickoff <= cutoff:
                    skipped_live += 1
                    continue
            except ValueError:
                pass

        vol = float(poly.get("volume", 0) or 0)

        checks = [
            ("HOME", pinnacle["p_home"], poly["p_home"]),
            ("AWAY", pinnacle["p_away"], poly["p_away"]),
        ]
        if pinnacle.get("p_draw") and poly.get("p_draw"):
            checks.append(("DRAW", pinnacle["p_draw"], poly["p_draw"]))

        for outcome, pin_prob, poly_prob in checks:
            if not poly_prob or poly_prob < MIN_POLY_PROB:
                continue
            edge = pin_prob - poly_prob
            if edge < MIN_EDGE:
                continue
            if edge > MAX_EDGE:
                print(f"  ⚠ Skipping {edge*100:.0f}% edge on "
                      f"{poly['home']} vs {poly['away']} — likely matching error")
                continue
            bet = kelly_bet(edge, poly_prob)
            if bet < 0.50:
                continue
            opportunities.append({
                "home": poly["home"], "away": poly["away"],
                "outcome": outcome, "edge": edge,
                "pinnacle_prob": pin_prob, "poly_prob": poly_prob,
                "volume": vol, "sport": pinnacle["sport_name"],
                "bet_size": bet, "poly_market": poly,
            })

    if skipped_live:
        print(f"  Skipped {skipped_live} markets (already started or <{MINUTES_BEFORE_KICKOFF}min to kickoff)")

    opportunities.sort(key=lambda x: x["edge"], reverse=True)

    # Step 5: Log trades
    print(f"\n── Step 5: Paper Trades ─────────────────────────────────────")
    if not opportunities:
        print(f"  No valid edge found above {MIN_EDGE*100:.0f}%")
        print(f"  ({len(low_vol)} poly markets checked, {len(pinnacle_games)} Pinnacle lines)")
    else:
        print(f"  Found {len(opportunities)} opportunities\n")
        print(f"  {'Edge':>6}  {'Bet':>6}  {'Poly%':>6}  {'Pin%':>6}  {'Vol':>8}  Match")
        print(f"  {'-'*72}")
        for o in opportunities[:10]:
            print(f"  {o['edge']*100:>5.1f}%  €{o['bet_size']:>5.2f}  "
                  f"{o['poly_prob']*100:>5.1f}%  {o['pinnacle_prob']*100:>5.1f}%  "
                  f"${o['volume']:>7,.0f}  {o['home']} vs {o['away']}  [{o['sport']}]")

        if not dry_run:
            print(f"\n  Logging top {min(MAX_TRADES, len(opportunities))} trades...\n")
            for o in opportunities[:MAX_TRADES]:
                log_sharp_trade(o, o["bet_size"])
        else:
            print(f"\n  Dry run — not logging. Set dry_run=false to log trades.")

    # Summary
    trades   = load_trades()
    ptrades  = [t for t in trades if t.get("source") == "pinnacle"]
    resolved = [t for t in ptrades if t.get("resolved") and t.get("profit") is not None]
    total_pl = sum(t["profit"] for t in resolved)
    deployed = sum(t["bet_size"] for t in ptrades)
    wins     = sum(1 for t in resolved if t["profit"] > 0)
    roi      = (total_pl / deployed * 100) if deployed > 0 else 0

    print(f"\n── Summary ──────────────────────────────────────────────────")
    print(f"  Trades: {len(ptrades)} total | {len(resolved)} resolved | "
          f"{wins}W/{len(resolved)-wins}L")
    print(f"  P/L: €{total_pl:+.2f} | ROI: {roi:+.1f}% | "
          f"Bankroll: €{BANKROLL - deployed + total_pl:.2f}")
    print(f"\n  Done ✓\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
