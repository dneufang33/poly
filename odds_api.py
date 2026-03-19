"""
Odds API Module — Pinnacle Sharp Lines
=======================================
Uses The Odds API (the-odds-api.com) free tier.
500 requests/month — only called when low-volume markets exist.

Get your free key: https://the-odds-api.com
Add to GitHub Secrets as: ODDS_API_KEY
"""

import os
import requests
import time

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
ODDS_API_KEY  = os.getenv("ODDS_API_KEY", "")

SPORTS = [
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_germany_bundesliga",
    "soccer_italy_serie_a",
    "soccer_france_ligue_one",
    "soccer_uefa_champs_league",
    "basketball_nba",
]

SPORT_NAMES = {
    "soccer_epl":                "Premier League",
    "soccer_spain_la_liga":      "La Liga",
    "soccer_germany_bundesliga": "Bundesliga",
    "soccer_italy_serie_a":      "Serie A",
    "soccer_france_ligue_one":   "Ligue 1",
    "soccer_uefa_champs_league": "Champions League",
    "basketball_nba":            "NBA",
}

ODDS_API_ALIASES = {
    "Manchester City":          ["Man City"],
    "Manchester United":        ["Man Utd", "Man United"],
    "Tottenham Hotspur":        ["Tottenham", "Spurs"],
    "Newcastle United":         ["Newcastle"],
    "Brighton and Hove Albion": ["Brighton"],
    "West Ham United":          ["West Ham"],
    "Wolverhampton Wanderers":  ["Wolves"],
    "Nottingham Forest":        ["Nottingham Forest"],
    "AFC Bournemouth":          ["Bournemouth"],
    "Atletico Madrid":          ["Atletico"],
    "Bayern Munich":            ["Bayern"],
    "Borussia Dortmund":        ["Dortmund"],
    "Bayer Leverkusen":         ["Leverkusen"],
    "Inter Milan":              ["Inter", "Internazionale"],
    "Paris Saint-Germain":      ["PSG"],
}


def fetch_pinnacle_odds(sport_key):
    if not ODDS_API_KEY:
        return []
    params = {
        "apiKey":     ODDS_API_KEY,
        "regions":    "eu",
        "markets":    "h2h",
        "bookmakers": "pinnacle",
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }
    try:
        r = requests.get(
            f"{ODDS_API_BASE}/sports/{sport_key}/odds",
            params=params, timeout=15
        )
        remaining = r.headers.get("x-requests-remaining", "?")
        used      = r.headers.get("x-requests-used", "?")
        print(f"  [{sport_key}] quota: {used} used, {remaining} remaining")
        r.raise_for_status()
        games = r.json()
    except requests.exceptions.HTTPError as e:
        if r.status_code == 401:
            print("  Invalid ODDS_API_KEY")
        elif r.status_code == 429:
            print("  Odds API rate limit hit")
        else:
            print(f"  Odds API error {r.status_code}: {e}")
        return []
    except Exception as e:
        print(f"  Odds API connection error: {e}")
        return []

    return [g for g in (parse_game(g, sport_key) for g in games) if g]


def parse_game(game, sport_key):
    try:
        home_team = game["home_team"]
        away_team = game["away_team"]
        commence  = game.get("commence_time", "")
        event_id  = game.get("id", "")

        pinnacle = next((b for b in game.get("bookmakers", [])
                         if b.get("key") == "pinnacle"), None)
        if not pinnacle:
            return None

        h2h = next((m for m in pinnacle.get("markets", [])
                    if m.get("key") == "h2h"), None)
        if not h2h:
            return None

        raw_probs = {o["name"]: 1/float(o["price"])
                     for o in h2h.get("outcomes", []) if float(o["price"]) > 0}
        total = sum(raw_probs.values())
        if total <= 0:
            return None

        probs  = {k: v/total for k, v in raw_probs.items()}
        p_home = probs.get(home_team, 0)
        p_away = probs.get(away_team, 0)
        p_draw = probs.get("Draw", None)

        if not p_home or not p_away:
            return None

        return {
            "home_team":     home_team,
            "away_team":     away_team,
            "p_home":        round(p_home, 4),
            "p_away":        round(p_away, 4),
            "p_draw":        round(p_draw, 4) if p_draw else None,
            "commence_time": commence,
            "sport_key":     sport_key,
            "sport_name":    SPORT_NAMES.get(sport_key, sport_key),
            "event_id":      event_id,
        }
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def fetch_all_pinnacle_odds():
    all_games = []
    for sport in SPORTS:
        games = fetch_pinnacle_odds(sport)
        all_games.extend(games)
        time.sleep(1)
    return all_games


def names_match(odds_api_name, poly_name):
    a = odds_api_name.lower().strip()
    b = poly_name.lower().strip()
    if a == b or a in b or b in a:
        return True
    for canonical, aliases in ODDS_API_ALIASES.items():
        if canonical.lower() in a or a in canonical.lower():
            for alias in aliases:
                if alias.lower() in b or b in alias.lower():
                    return True
    first_a = a.split()[0]
    if len(first_a) > 3 and first_a in b:
        return True
    return False


def match_to_pinnacle(poly_home, poly_away, pinnacle_games):
    for g in pinnacle_games:
        if names_match(g["home_team"], poly_home) and names_match(g["away_team"], poly_away):
            return g
        if names_match(g["home_team"], poly_away) and names_match(g["away_team"], poly_home):
            return {**g,
                    "home_team": g["away_team"], "away_team": g["home_team"],
                    "p_home": g["p_away"], "p_away": g["p_home"]}
    return None
