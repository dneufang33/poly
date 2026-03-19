"""
Sports Edge Model — Football Elo Rating System
================================================
Builds Elo ratings for football teams from historical results,
then compares implied win probability to prediction market prices
to find exploitable edges.

Data source: football-data.org (free tier, no API key needed for some leagues)
             OR a bundled historical dataset for offline use

Usage:
    python sports_model.py --update        # Update Elo ratings from recent results
    python sports_model.py --scan          # Scan for upcoming match edges
    python sports_model.py --ratings       # Print current team ratings
    python sports_model.py --match "Man City" "Arsenal"  # Single match estimate
"""

import json
import math
import os
import time
import requests
import argparse
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ── Config ────────────────────────────────────────────────────────────────────

ELO_FILE      = "elo_ratings.json"
RESULTS_FILE  = "match_results.json"

# Elo parameters
ELO_DEFAULT   = 1500     # Starting rating for new teams
ELO_K         = 32       # K-factor: higher = faster adaptation
HOME_ADVANTAGE = 60      # Elo points added to home team

# Edge threshold to flag a bet
MIN_EDGE      = 0.06     # 6%

# Free football data API (no key required for basic use)
# Docs: https://www.football-data.org/documentation/quickstart
FOOTBALL_API  = "https://api.football-data.org/v4"
# Free tier API key — get yours at football-data.org (takes 30 seconds)
FOOTBALL_KEY  = os.getenv("FOOTBALL_API_KEY", "")

# Leagues to track (football-data.org competition codes)
# Note: Champions League (CL) requires a paid tier — kept here but handled gracefully
LEAGUES = {
    "PL":  "Premier League",
    "PD":  "La Liga",
    "BL1": "Bundesliga",
    "SA":  "Serie A",
    "FL1": "Ligue 1",
}

# Delay between API calls to avoid 429 rate limiting (free tier = 10 req/min)
API_DELAY = 7  # seconds

# ── Elo Core ──────────────────────────────────────────────────────────────────

def expected_score(rating_a, rating_b):
    """
    Standard Elo expected score formula.
    Returns probability that team A wins (ignoring draws for now).
    """
    return 1 / (1 + 10 ** ((rating_b - rating_a) / 400))


def update_elo(rating_a, rating_b, score_a, score_b, k=ELO_K):
    """
    Update Elo ratings after a match.
    score_a/score_b: actual goals scored.
    Returns (new_rating_a, new_rating_b)
    """
    # Convert result to 0/0.5/1
    if score_a > score_b:
        actual_a = 1.0
    elif score_a < score_b:
        actual_a = 0.0
    else:
        actual_a = 0.5

    # Goal difference multiplier (rewards big wins more)
    goal_diff = abs(score_a - score_b)
    if goal_diff <= 1:
        gdm = 1.0
    elif goal_diff == 2:
        gdm = 1.5
    else:
        gdm = (11 + goal_diff) / 8  # FiveThirtyEight formula

    exp_a = expected_score(rating_a, rating_b)
    exp_b = 1 - exp_a

    new_a = rating_a + k * gdm * (actual_a - exp_a)
    new_b = rating_b + k * gdm * ((1 - actual_a) - exp_b)

    return round(new_a, 2), round(new_b, 2)


def win_draw_loss_probs(elo_home, elo_away, home_advantage=HOME_ADVANTAGE):
    """
    Convert Elo ratings to W/D/L probabilities.
    Uses home advantage adjustment and a draw probability model.

    Returns: (p_home_win, p_draw, p_away_win)
    """
    # Adjusted ratings
    adj_home = elo_home + home_advantage
    adj_away = elo_away

    # Raw win probability (no draws)
    p_home_raw = expected_score(adj_home, adj_away)
    p_away_raw = 1 - p_home_raw

    # Estimate draw probability based on how close the teams are
    # Closer teams → more draws. Peaks around 27% for equal teams.
    rating_diff = abs(adj_home - adj_away)
    p_draw = 0.27 * math.exp(-0.001 * rating_diff ** 1.5)
    p_draw = max(0.08, min(0.35, p_draw))  # clamp between 8–35%

    # Redistribute remaining probability
    remaining = 1 - p_draw
    p_home = p_home_raw * remaining
    p_away = p_away_raw * remaining

    return round(p_home, 4), round(p_draw, 4), round(p_away, 4)


# ── Rating Store ──────────────────────────────────────────────────────────────

def load_ratings():
    if os.path.exists(ELO_FILE):
        with open(ELO_FILE) as f:
            return json.load(f)
    return {}


def save_ratings(ratings):
    with open(ELO_FILE, "w") as f:
        json.dump(ratings, f, indent=2)


def get_rating(ratings, team):
    return ratings.get(team, ELO_DEFAULT)


# ── Seeded ratings (offline fallback) ────────────────────────────────────────

SEED_RATINGS = {
    # Premier League
    "Manchester City FC": 1820, "Arsenal FC": 1790, "Liverpool FC": 1810,
    "Chelsea FC": 1720, "Manchester United FC": 1680, "Tottenham Hotspur FC": 1690,
    "Newcastle United FC": 1700, "Aston Villa FC": 1710, "Brighton & Hove Albion FC": 1660,
    "West Ham United FC": 1620, "Brentford FC": 1610, "Fulham FC": 1600,
    "Crystal Palace FC": 1590, "Wolverhampton Wanderers FC": 1580, "Everton FC": 1570,
    "Nottingham Forest FC": 1560, "Bournemouth FC": 1550, "Luton Town FC": 1480,
    "Burnley FC": 1470, "Sheffield United FC": 1460,
    # La Liga
    "Real Madrid CF": 1870, "FC Barcelona": 1830, "Atletico de Madrid": 1780,
    "Real Sociedad de Fútbol": 1690, "Athletic Club": 1680, "Villarreal CF": 1670,
    "Real Betis Balompié": 1650, "Sevilla FC": 1640, "Girona FC": 1660,
    # Bundesliga
    "Bayer 04 Leverkusen": 1800, "FC Bayern München": 1840, "Borussia Dortmund": 1760,
    "RB Leipzig": 1750, "VfB Stuttgart": 1720, "Eintracht Frankfurt": 1700,
    "SC Freiburg": 1670, "Union Berlin": 1650, "Borussia Mönchengladbach": 1630,
    # Serie A
    "Inter Milan": 1800, "AC Milan": 1770, "Juventus FC": 1760,
    "AS Roma": 1720, "SSC Napoli": 1740, "Atalanta BC": 1750,
    "Lazio": 1700, "Fiorentina": 1680,
    # Ligue 1
    "Paris Saint-Germain FC": 1830, "AS Monaco FC": 1720, "Olympique de Marseille": 1700,
    "Olympique Lyonnais": 1690, "LOSC Lille": 1710, "RC Lens": 1680,
}


def init_ratings_from_seed():
    """Load seed ratings if no ELO file exists yet."""
    if not os.path.exists(ELO_FILE):
        save_ratings(SEED_RATINGS)
        print(f"  Initialised ratings for {len(SEED_RATINGS)} teams from seed data.")
    return load_ratings()


# ── Football Data API ─────────────────────────────────────────────────────────

def api_headers():
    headers = {"Accept": "application/json"}
    if FOOTBALL_KEY:
        headers["X-Auth-Token"] = FOOTBALL_KEY
    return headers


def fetch_recent_results(league_code, days_back=14):
    """Fetch completed matches from the last N days."""
    if not FOOTBALL_KEY:
        print(f"  No FOOTBALL_API_KEY set — skipping live data fetch for {league_code}")
        return []

    date_from = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    date_to   = datetime.now().strftime("%Y-%m-%d")

    url = f"{FOOTBALL_API}/competitions/{league_code}/matches"
    params = {"dateFrom": date_from, "dateTo": date_to, "status": "FINISHED"}

    try:
        r = requests.get(url, headers=api_headers(), params=params, timeout=10)
        r.raise_for_status()
        return r.json().get("matches", [])
    except Exception as e:
        print(f"  API error for {league_code}: {e}")
        return []


def fetch_upcoming_fixtures(league_code, days_ahead=7):
    """Fetch scheduled matches in the next N days."""
    if not FOOTBALL_KEY:
        return []

    date_from = datetime.now().strftime("%Y-%m-%d")
    date_to   = (datetime.now() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    url = f"{FOOTBALL_API}/competitions/{league_code}/matches"
    params = {"dateFrom": date_from, "dateTo": date_to, "status": "SCHEDULED,TIMED"}

    try:
        r = requests.get(url, headers=api_headers(), params=params, timeout=10)
        r.raise_for_status()
        return r.json().get("matches", [])
    except Exception as e:
        print(f"  API error for {league_code}: {e}")
        return []


# ── Rating Updater ────────────────────────────────────────────────────────────

def update_ratings_from_results():
    """Fetch recent results and update Elo ratings."""
    ratings = init_ratings_from_seed()
    total_updated = 0

    for code, name in LEAGUES.items():
        matches = fetch_recent_results(code)
        updated = 0

        for m in matches:
            try:
                home = m["homeTeam"]["name"]
                away = m["awayTeam"]["name"]
                score = m["score"]["fullTime"]
                if score["home"] is None or score["away"] is None:
                    continue

                r_home = get_rating(ratings, home)
                r_away = get_rating(ratings, away)
                new_home, new_away = update_elo(r_home, r_away, score["home"], score["away"])
                ratings[home] = new_home
                ratings[away] = new_away
                updated += 1
            except (KeyError, TypeError):
                continue

        if updated:
            print(f"  {name}: updated {updated} matches")
            total_updated += updated
        time.sleep(API_DELAY)

    save_ratings(ratings)
    print(f"\n  Total: {total_updated} matches processed. Ratings saved to {ELO_FILE}")
    return ratings


# ── Match Probability ─────────────────────────────────────────────────────────

def estimate_match(home_team, away_team, ratings=None, neutral=False):
    """
    Estimate win/draw/loss probabilities for a match.
    neutral=True for cup finals or neutral venues.
    """
    if ratings is None:
        ratings = init_ratings_from_seed()

    r_home = get_rating(ratings, home_team)
    r_away = get_rating(ratings, away_team)

    ha = 0 if neutral else HOME_ADVANTAGE
    p_home, p_draw, p_away = win_draw_loss_probs(r_home, r_away, ha)

    return {
        "home": home_team,
        "away": away_team,
        "elo_home": r_home,
        "elo_away": r_away,
        "p_home_win": p_home,
        "p_draw": p_draw,
        "p_away_win": p_away,
    }


# ── Opportunity Scanner ───────────────────────────────────────────────────────

def scan_sports_opportunities(manifold_markets=None):
    """
    Compare Elo-derived probabilities to market prices.

    If manifold_markets is provided (list of dicts), matches them to
    upcoming fixtures. Otherwise uses upcoming fixtures from the API
    and prints model probabilities for manual comparison.
    """
    ratings = init_ratings_from_seed()
    print(f"\n{'='*65}")
    print(f"  Football Edge Scanner — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*65}\n")

    opportunities = []

    # If no live market data, show model probs for upcoming fixtures
    if not FOOTBALL_KEY:
        print("  No API key — showing model probabilities for sample fixtures.\n")
        sample_fixtures = [
            ("Arsenal FC",           "Manchester City FC",     False),
            ("FC Bayern München",    "Bayer 04 Leverkusen",    False),
            ("Real Madrid CF",       "FC Barcelona",           False),
            ("Paris Saint-Germain FC", "AS Monaco FC",         False),
            ("Inter Milan",          "AC Milan",               False),
        ]
        print(f"  {'Home':<30} {'Away':<30} {'H%':>5} {'D%':>5} {'A%':>5}")
        print(f"  {'-'*70}")
        for home, away, neutral in sample_fixtures:
            est = estimate_match(home, away, ratings, neutral)
            print(
                f"  {home:<30} {away:<30} "
                f"{est['p_home_win']*100:>4.1f}% "
                f"{est['p_draw']*100:>4.1f}% "
                f"{est['p_away_win']*100:>4.1f}%"
            )
        print("\n  Add FOOTBALL_API_KEY to .env to fetch live fixtures and market prices.\n")
        return []

    # Fetch real upcoming fixtures
    all_fixtures = []
    for code in LEAGUES:
        fixtures = fetch_upcoming_fixtures(code, days_ahead=7)
        all_fixtures.extend(fixtures)
        time.sleep(API_DELAY)

    print(f"  Found {len(all_fixtures)} upcoming fixtures in next 7 days\n")

    for m in all_fixtures:
        try:
            home = m["homeTeam"]["name"]
            away = m["awayTeam"]["name"]
            date = m["utcDate"][:10]

            est = estimate_match(home, away, ratings)

            # If we have manifold market data, compare
            if manifold_markets:
                matched = find_matching_market(home, away, manifold_markets)
                if matched:
                    market_prob = matched.get("probability", 0.5)
                    # Assume market is pricing home win
                    edge = est["p_home_win"] - market_prob
                    if abs(edge) >= MIN_EDGE:
                        outcome = "YES" if edge > 0 else "NO"
                        opportunities.append({
                            "date": date,
                            "home": home,
                            "away": away,
                            "model_home_win": est["p_home_win"],
                            "market_prob": market_prob,
                            "edge": abs(edge),
                            "outcome": outcome,
                            "market_id": matched.get("id"),
                            "question": matched.get("question"),
                        })
            else:
                # Just print model output
                print(
                    f"  {date}  {home:<28} vs {away:<28}  "
                    f"H:{est['p_home_win']*100:.0f}% "
                    f"D:{est['p_draw']*100:.0f}% "
                    f"A:{est['p_away_win']*100:.0f}%"
                )
        except (KeyError, TypeError):
            continue

    if opportunities:
        opportunities.sort(key=lambda x: x["edge"], reverse=True)
        print(f"\n  {'Edge':>6}  {'Bet':>4}  {'Mkt%':>6}  {'Mdl%':>6}  Match")
        print(f"  {'-'*65}")
        for o in opportunities:
            print(
                f"  {o['edge']*100:>5.1f}%  {o['outcome']:<4}  "
                f"{o['market_prob']*100:>5.1f}%  "
                f"{o['model_home_win']*100:>5.1f}%  "
                f"{o['home']} vs {o['away']}"
            )

    return opportunities


def find_matching_market(home, away, markets):
    """
    Fuzzy-match a fixture to a Manifold market question.
    Looks for both team names appearing in the question text.
    """
    home_short = home.split()[0].lower()
    away_short = away.split()[0].lower()

    for m in markets:
        q = m.get("question", "").lower()
        if home_short in q and away_short in q:
            return m
    return None


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Football Elo Edge Scanner")
    parser.add_argument("--update",  action="store_true", help="Update ratings from recent results")
    parser.add_argument("--scan",    action="store_true", help="Scan for edge opportunities")
    parser.add_argument("--ratings", action="store_true", help="Print current team ratings")
    parser.add_argument("--match",   nargs=2, metavar=("HOME", "AWAY"), help="Estimate a single match")
    args = parser.parse_args()

    if args.update:
        update_ratings_from_results()

    elif args.scan:
        scan_sports_opportunities()

    elif args.ratings:
        ratings = init_ratings_from_seed()
        sorted_r = sorted(ratings.items(), key=lambda x: x[1], reverse=True)
        print(f"\n  {'Team':<40} {'Elo':>6}")
        print(f"  {'-'*50}")
        for team, elo in sorted_r[:30]:
            print(f"  {team:<40} {elo:>6.0f}")

    elif args.match:
        home, away = args.match
        est = estimate_match(home, away)
        print(f"\n  {home} vs {away}")
        print(f"  Home win:  {est['p_home_win']*100:.1f}%")
        print(f"  Draw:      {est['p_draw']*100:.1f}%")
        print(f"  Away win:  {est['p_away_win']*100:.1f}%")
        print(f"  Elo: {home} {est['elo_home']} | {away} {est['elo_away']}\n")

    else:
        parser.print_help()
