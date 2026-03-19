"""
Prediction Market Trading System
=================================
Connects to Manifold Markets API to:
1. Fetch open markets
2. Score edge (your probability vs market price)
3. Auto-place bets when edge > threshold
4. Log all activity to trades.json for dashboard

Usage:
    python trader.py --scan        # Scan for opportunities
    python trader.py --bet <id>    # Manually bet on a market
    python trader.py --history     # Print trade history

Setup:
    pip install requests python-dotenv
    Create a .env file with: MANIFOLD_API_KEY=your_key_here
    Get your API key at: https://manifold.markets/profile
"""

import requests
import json
import os
import argparse
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

API_BASE = "https://api.manifold.markets/v0"
API_KEY = os.getenv("MANIFOLD_API_KEY", "")  # Set in .env file
TRADES_FILE = "trades.json"

# Edge model settings
MIN_EDGE = 0.07          # Minimum edge (%) to consider a bet. 0.07 = 7%
DEFAULT_BET = 10         # Default bet size in Mana (Manifold's currency)
MAX_BET = 50             # Hard cap per bet
MIN_LIQUIDITY = 100      # Skip markets with very thin liquidity

# ── API Helpers ───────────────────────────────────────────────────────────────

def get_headers():
    if not API_KEY:
        raise ValueError("No API key found. Add MANIFOLD_API_KEY=your_key to .env file")
    return {"Authorization": f"Key {API_KEY}", "Content-Type": "application/json"}


def fetch_markets(limit=50):
    """Fetch open binary markets from Manifold."""
    params = {
        "limit": limit,
        "contractType": "BINARY",   # Only yes/no markets
    }
    r = requests.get(f"{API_BASE}/markets", params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def fetch_market(market_id):
    """Fetch a single market by ID."""
    r = requests.get(f"{API_BASE}/market/{market_id}", timeout=10)
    r.raise_for_status()
    return r.json()


def get_my_balance():
    """Fetch current Mana balance."""
    r = requests.get(f"{API_BASE}/me", headers=get_headers(), timeout=10)
    r.raise_for_status()
    return r.json().get("balance", 0)


def place_bet(market_id, outcome, amount):
    """
    Place a bet on a market.
    outcome: "YES" or "NO"
    amount: Mana to bet (integer)
    """
    payload = {
        "contractId": market_id,
        "outcome": outcome,
        "amount": amount,
    }
    r = requests.post(
        f"{API_BASE}/bet",
        headers=get_headers(),
        json=payload,
        timeout=10
    )
    r.raise_for_status()
    return r.json()

# ── Edge Model ────────────────────────────────────────────────────────────────

def estimate_probability(market):
    """
    Simple edge model. In v1 this is a heuristic placeholder.
    
    Your job: replace this function with real signal logic.
    
    Ideas to improve this:
    - Pull Metaculus forecasts for matching questions
    - Run sentiment analysis on market title via LLM
    - Use base rates (e.g. "will X happen by Y date" type questions)
    - Check if market probability has moved sharply recently (mean reversion signal)
    
    Returns: float between 0 and 1, or None if no estimate possible
    """
    title = market.get("question", "").lower()
    current_prob = market.get("probability", 0.5)
    volume = market.get("volume", 0)
    
    # Skip markets with very little activity
    if volume < MIN_LIQUIDITY:
        return None
    
    # Heuristic 1: Very new markets (<10 traders) are often mispriced
    # The crowd hasn't converged yet
    unique_bettors = market.get("uniqueBettorCount", 0)
    if unique_bettors < 10:
        # Assume 10% pull toward 50% (uncertainty premium)
        estimate = current_prob * 0.9 + 0.5 * 0.1
        return estimate
    
    # Heuristic 2: Extreme probabilities on long-horizon markets
    # Markets > 90% or < 10% often have narrative bias
    close_time = market.get("closeTime", 0)
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    days_remaining = (close_time - now_ms) / (1000 * 86400)
    
    if days_remaining > 60:
        if current_prob > 0.88:
            return current_prob - 0.05   # Slight fade of extreme optimism
        if current_prob < 0.12:
            return current_prob + 0.05   # Slight fade of extreme pessimism
    
    # No strong signal — skip
    return None


def calculate_edge(market_prob, my_estimate, outcome):
    """
    Calculate edge for a given bet direction.
    Edge = (my estimated probability) - (market implied probability)
    
    For YES bet: edge = my_estimate - market_prob
    For NO bet:  edge = (1 - my_estimate) - (1 - market_prob)
                      = market_prob - my_estimate
    """
    if outcome == "YES":
        return my_estimate - market_prob
    else:
        return market_prob - my_estimate


def kelly_bet(edge, prob, bankroll, fraction=0.25):
    """
    Kelly criterion bet sizing, scaled down by fraction for safety.
    Full Kelly is too aggressive — use quarter-Kelly as default.
    
    edge: your edge as decimal (e.g. 0.08 for 8%)
    prob: market probability for the outcome you're betting on
    bankroll: current balance
    fraction: Kelly fraction (0.25 = quarter Kelly)
    """
    if prob <= 0 or prob >= 1:
        return DEFAULT_BET
    
    # Kelly formula: f* = (bp - q) / b
    # where b = odds - 1, p = win prob, q = 1 - p
    odds = (1 / prob) - 1  # Decimal odds
    q = 1 - (prob + edge)  # Adjusted loss probability
    p = prob + edge         # Adjusted win probability
    
    if odds <= 0:
        return DEFAULT_BET
    
    kelly_fraction = (odds * p - q) / odds
    raw_bet = kelly_fraction * fraction * bankroll
    
    # Clamp between sensible bounds
    return max(DEFAULT_BET, min(MAX_BET, int(raw_bet)))

# ── Scanner ───────────────────────────────────────────────────────────────────

def scan_for_opportunities(limit=100, auto_bet=False):
    """
    Scan open markets for edge opportunities.
    Prints a ranked table of best bets found.
    """
    print(f"\n{'='*60}")
    print(f"  Scanning {limit} markets for edge opportunities...")
    print(f"  Min edge threshold: {MIN_EDGE*100:.0f}%")
    print(f"{'='*60}\n")

    markets = fetch_markets(limit=limit)
    opportunities = []

    for m in markets:
        if m.get("isResolved"):
            continue
        if m.get("outcomeType") != "BINARY":
            continue

        prob = m.get("probability", 0.5)
        estimate = estimate_probability(m)

        if estimate is None:
            continue

        # Check both directions
        for outcome in ["YES", "NO"]:
            edge = calculate_edge(prob, estimate, outcome)
            if edge >= MIN_EDGE:
                opportunities.append({
                    "id": m["id"],
                    "question": m["question"][:70],
                    "market_prob": prob,
                    "my_estimate": estimate,
                    "outcome": outcome,
                    "edge": edge,
                    "volume": m.get("volume", 0),
                    "url": m.get("url", ""),
                })

    # Sort by edge descending
    opportunities.sort(key=lambda x: x["edge"], reverse=True)

    if not opportunities:
        print("  No opportunities found above threshold.\n")
        return []

    print(f"  Found {len(opportunities)} opportunities:\n")
    print(f"  {'#':<3} {'Edge':>6} {'Bet':>4} {'Mkt%':>6} {'Est%':>6}  Question")
    print(f"  {'-'*70}")

    for i, o in enumerate(opportunities[:10], 1):
        print(
            f"  {i:<3} {o['edge']*100:>5.1f}%  {o['outcome']:<3}  "
            f"{o['market_prob']*100:>5.1f}%  {o['my_estimate']*100:>5.1f}%  "
            f"{o['question']}"
        )

    if auto_bet:
        balance = get_my_balance()
        print(f"\n  Auto-betting top opportunities (balance: M{balance:.0f})\n")
        for o in opportunities[:3]:
            bet_size = kelly_bet(o["edge"], o["market_prob"], balance)
            print(f"  Placing M{bet_size} {o['outcome']} on: {o['question'][:50]}...")
            try:
                result = place_bet(o["id"], o["outcome"], bet_size)
                log_trade(o, bet_size, result)
                print(f"  ✓ Bet placed. New prob: {result.get('probAfter', '?')}")
            except Exception as e:
                print(f"  ✗ Failed: {e}")

    return opportunities

# ── Trade Logger ──────────────────────────────────────────────────────────────

def load_trades():
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE) as f:
            return json.load(f)
    return []


def save_trades(trades):
    with open(TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=2)


def log_trade(opportunity, bet_size, api_response):
    """Log a placed bet for dashboard tracking."""
    trades = load_trades()
    trades.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market_id": opportunity["id"],
        "question": opportunity["question"],
        "outcome": opportunity["outcome"],
        "edge": opportunity["edge"],
        "market_prob": opportunity["market_prob"],
        "my_estimate": opportunity["my_estimate"],
        "bet_size": bet_size,
        "prob_before": opportunity["market_prob"],
        "prob_after": api_response.get("probAfter"),
        "bet_id": api_response.get("betId"),
        "resolved": False,
        "profit": None,
    })
    save_trades(trades)
    print(f"  Trade logged to {TRADES_FILE}")


def add_dummy_trades():
    """
    Populate trades.json with realistic sample data so the dashboard
    works immediately without needing a real API key.
    """
    import random
    random.seed(42)

    questions = [
        "Will Bitcoin exceed $100k by end of 2025?",
        "Will the ECB cut rates in Q1 2025?",
        "Will Anthropic release Claude 4 before June 2025?",
        "Will Germany enter recession in 2025?",
        "Will OpenAI reach $200B valuation by end of 2025?",
        "Will SpaceX Starship reach orbit in 2025?",
        "Will Polymarket exceed $1B monthly volume in 2025?",
        "Will UK rejoin EU single market by 2030?",
        "Will GPT-5 score >90% on MMLU benchmark?",
        "Will Euro hit parity with USD in 2025?",
        "Will Nvidia market cap exceed $4T in 2025?",
        "Will there be a US recession in 2025?",
        "Will Apple release AR glasses in 2025?",
        "Will inflation in EU drop below 2% by mid-2025?",
        "Will a new G7 country adopt Bitcoin as legal tender?",
    ]

    trades = []
    balance = 1000
    for i, q in enumerate(questions):
        edge = random.uniform(0.05, 0.18)
        market_prob = random.uniform(0.2, 0.8)
        my_estimate = market_prob + edge
        outcome = "YES" if random.random() > 0.4 else "NO"
        bet_size = random.randint(10, 45)
        won = random.random() < 0.58  # Slight positive edge
        profit = bet_size * (1 / market_prob - 1) if won else -bet_size

        ts = datetime(2025, 1 + (i // 3), 1 + (i * 2 % 28), tzinfo=timezone.utc)

        trades.append({
            "timestamp": ts.isoformat(),
            "market_id": f"dummy-{i}",
            "question": q,
            "outcome": outcome,
            "edge": round(edge, 4),
            "market_prob": round(market_prob, 4),
            "my_estimate": round(my_estimate, 4),
            "bet_size": bet_size,
            "prob_before": round(market_prob, 4),
            "prob_after": round(market_prob + 0.01, 4),
            "bet_id": f"bet-{i:04d}",
            "resolved": True,
            "profit": round(profit, 2),
        })

    save_trades(trades)
    print(f"  Wrote {len(trades)} sample trades to {TRADES_FILE}")

# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prediction Market Trader")
    parser.add_argument("--scan", action="store_true", help="Scan for opportunities")
    parser.add_argument("--auto", action="store_true", help="Auto-bet top opportunities")
    parser.add_argument("--history", action="store_true", help="Print trade history")
    parser.add_argument("--demo", action="store_true", help="Generate demo trade data")
    parser.add_argument("--limit", type=int, default=100, help="Markets to scan")
    args = parser.parse_args()

    if args.demo:
        add_dummy_trades()
    elif args.scan:
        scan_for_opportunities(limit=args.limit, auto_bet=args.auto)
    elif args.history:
        trades = load_trades()
        print(f"\n  {len(trades)} trades on record\n")
        for t in trades[-10:]:
            profit_str = f"P/L: {t['profit']:+.1f}" if t["profit"] is not None else "open"
            print(f"  {t['timestamp'][:10]}  {t['outcome']:<3}  {t['question'][:50]}  {profit_str}")
    else:
        parser.print_help()
