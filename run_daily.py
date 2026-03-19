"""
Daily Runner — Polymarket Paper Trading
========================================
1. Resolve yesterday's finished matches
2. Update Elo ratings (football-data.org)
3. Fetch ALL soccer events from Polymarket (/events?tag_slug=soccer)
4. Parse match markets from those events
5. Cross-reference fixtures with Elo model → find edge
6. Log paper trades to trades.json (nothing sent to Polymarket)
"""

import argparse
import time
from datetime import datetime, timezone

from sports_model import (
    update_ratings_from_results, init_ratings_from_seed,
    estimate_match, get_rating, win_draw_loss_probs, HOME_ADVANTAGE,
    FOOTBALL_KEY, LEAGUES, fetch_recent_results, fetch_upcoming_fixtures, API_DELAY,
)
from polymarket import (
    fetch_all_soccer_events, parse_soccer_markets,
    match_fixture_to_market, log_paper_trade,
    resolve_paper_trades, load_trades,
)

MIN_EDGE   = 0.05   # 5% minimum edge
PAPER_BET  = 10     # hypothetical $USD per trade
MAX_TRADES = 5


def get_recent_results():
    if not FOOTBALL_KEY:
        return []
    results = []
    for code in LEAGUES:
        for m in fetch_recent_results(code, days_back=2):
            try:
                score = m["score"]["fullTime"]
                if score["home"] is None:
                    continue
                results.append({
                    "home": m["homeTeam"]["name"], "away": m["awayTeam"]["name"],
                    "score_home": score["home"], "score_away": score["away"],
                })
            except (KeyError, TypeError):
                continue
        time.sleep(API_DELAY)
    return results


def get_upcoming_fixtures(days_ahead=7):
    if not FOOTBALL_KEY:
        return []
    fixtures = []
    for code in LEAGUES:
        fixtures.extend(fetch_upcoming_fixtures(code, days_ahead=days_ahead))
        time.sleep(API_DELAY)
    return fixtures


def run(dry_run=False):
    print(f"\n{'='*62}")
    print(f"  Polymarket Paper Trader")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Mode: {'DRY RUN' if dry_run else 'PAPER TRADE'} — nothing sent to Polymarket")
    print(f"{'='*62}")

    # Step 1: Resolve finished matches
    print("\n── Step 1: Resolve Finished Matches ─────────────────────────")
    if FOOTBALL_KEY:
        recent = get_recent_results()
        print(f"  Fetched {len(recent)} recent results")
        resolve_paper_trades(recent)
    else:
        print("  Skipping — no FOOTBALL_API_KEY")

    # Step 2: Update Elo ratings
    print("\n── Step 2: Update Elo Ratings ───────────────────────────────")
    if FOOTBALL_KEY:
        update_ratings_from_results()
    else:
        print("  Using seed ratings")
    ratings = init_ratings_from_seed()

    # Step 3: Fetch ALL Polymarket soccer events in one go
    print("\n── Step 3: Fetch Polymarket Soccer Markets ──────────────────")
    events      = fetch_all_soccer_events(limit=300)
    poly_markets = parse_soccer_markets(events)
    print(f"  {len(events)} soccer events fetched")
    print(f"  {len(poly_markets)} match markets parsed (home vs away format)")
    if poly_markets:
        print(f"  Sample: {poly_markets[0]['home']} vs {poly_markets[0]['away']}  "
              f"H:{poly_markets[0]['p_home']*100:.0f}%  "
              f"D:{poly_markets[0].get('p_draw') and poly_markets[0]['p_draw']*100:.0f}%  "
              f"A:{poly_markets[0]['p_away']*100:.0f}%")

    # Step 4: Find edge
    print("\n── Step 4: Scan for Edge (Elo vs Polymarket) ────────────────")
    opportunities = []

    if FOOTBALL_KEY:
        fixtures = get_upcoming_fixtures(days_ahead=7)
        print(f"  {len(fixtures)} upcoming fixtures from football-data.org\n")
        for f in fixtures:
            try:
                home = f["homeTeam"]["name"]
                away = f["awayTeam"]["name"]
            except (KeyError, TypeError):
                continue
            est  = estimate_match(home, away, ratings)
            poly = match_fixture_to_market(home, away, poly_markets)
            if not poly:
                continue
            checks = [
                ("HOME", est["p_home_win"], poly["p_home"]),
                ("AWAY", est["p_away_win"], poly["p_away"]),
            ]
            if poly.get("p_draw") is not None:
                checks.append(("DRAW", est["p_draw"], poly["p_draw"]))
            for outcome, elo_prob, poly_prob in checks:
                if not poly_prob or poly_prob <= 0:
                    continue
                edge = elo_prob - poly_prob
                if edge >= MIN_EDGE:
                    opportunities.append({
                        "home": home, "away": away,
                        "outcome": outcome, "edge": edge,
                        "elo_prob": elo_prob, "poly_prob": poly_prob,
                        "poly_market": poly,
                    })
    else:
        # No football-data.org key: match Polymarket markets directly against seed ratings
        print("  No FOOTBALL_API_KEY — comparing Polymarket markets against seed Elo\n")
        for poly in poly_markets:
            r_home = get_rating(ratings, poly["home"])
            r_away = get_rating(ratings, poly["away"])
            if r_home == 1500 and r_away == 1500:
                continue
            p_home, p_draw, p_away = win_draw_loss_probs(r_home, r_away, HOME_ADVANTAGE)
            checks = [
                ("HOME", p_home, poly["p_home"]),
                ("AWAY", p_away, poly["p_away"]),
            ]
            if poly.get("p_draw") is not None:
                checks.append(("DRAW", p_draw, poly["p_draw"]))
            for outcome, elo_prob, poly_prob in checks:
                if not poly_prob or poly_prob <= 0:
                    continue
                edge = elo_prob - poly_prob
                if edge >= MIN_EDGE:
                    opportunities.append({
                        "home": poly["home"], "away": poly["away"],
                        "outcome": outcome, "edge": edge,
                        "elo_prob": elo_prob, "poly_prob": poly_prob,
                        "poly_market": poly,
                    })

    opportunities.sort(key=lambda x: x["edge"], reverse=True)

    # Step 5: Log paper trades
    print(f"\n── Step 5: Paper Trades ─────────────────────────────────────")
    if not opportunities:
        print(f"  No edge above {MIN_EDGE*100:.0f}% found.")
        print(f"  Matched {sum(1 for p in poly_markets if match_fixture_to_market(p['home'], p['away'], poly_markets))} "
              f"Polymarket markets to Elo ratings")
    else:
        print(f"  Found {len(opportunities)} opportunities\n")
        print(f"  {'Edge':>6}  {'Bet':>5}  {'Poly%':>6}  {'Elo%':>6}  Match")
        print(f"  {'-'*65}")
        for o in opportunities[:10]:
            print(f"  {o['edge']*100:>5.1f}%  {o['outcome']:<5}  "
                  f"{o['poly_prob']*100:>5.1f}%  {o['elo_prob']*100:>5.1f}%  "
                  f"{o['home']} vs {o['away']}")
        if not dry_run:
            print(f"\n  Logging top {min(MAX_TRADES, len(opportunities))} trades...\n")
            for o in opportunities[:MAX_TRADES]:
                log_paper_trade(
                    fixture_home=o["home"], fixture_away=o["away"],
                    outcome=o["outcome"], edge=o["edge"],
                    market_prob=o["poly_prob"], model_prob=o["elo_prob"],
                    bet_size=PAPER_BET, poly_market=o["poly_market"],
                )
        else:
            print("\n  Dry run — not logging.")

    # Summary
    trades   = load_trades()
    paper    = [t for t in trades if t.get("paper")]
    resolved = [t for t in paper if t.get("resolved") and t.get("profit") is not None]
    total_pl = sum(t["profit"] for t in resolved)
    wins     = sum(1 for t in resolved if t["profit"] > 0)

    print(f"\n── Summary ──────────────────────────────────────────────────")
    print(f"  Paper trades: {len(paper)}  |  Resolved: {len(resolved)}")
    print(f"  Win rate:     {wins/max(len(resolved),1)*100:.1f}%  ({wins}W/{len(resolved)-wins}L)")
    print(f"  Total P/L:    ${total_pl:+.2f}")
    print(f"\n  Done ✓\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
