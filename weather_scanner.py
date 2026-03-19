"""
Weather Scanner
===============
Compares Open-Meteo weather forecasts (free, no key needed)
to Polymarket weather/temperature markets.

Strategy: Open-Meteo uses ECMWF ensemble forecasts — the gold standard
in numerical weather prediction. When Polymarket prices a temperature
threshold differently from what the forecast implies, there's edge.

Example:
  Polymarket: "Will Paris exceed 20°C on April 15?" → 45%
  ECMWF forecast: 73% chance of exceeding 20°C
  Edge: 28% — bet YES

Open-Meteo API: https://open-meteo.com (free, no key, no rate limit)
"""

import requests
import json
import re
from datetime import datetime, timezone, timedelta
import math

OPEN_METEO_BASE = "https://api.open-meteo.com/v1"

# Known city coordinates for location matching
CITY_COORDS = {
    "london":        (51.5074, -0.1278),
    "paris":         (48.8566, 2.3522),
    "berlin":        (52.5200, 13.4050),
    "madrid":        (40.4168, -3.7038),
    "rome":          (41.9028, 12.4964),
    "amsterdam":     (52.3676, 4.9041),
    "brussels":      (50.8503, 4.3517),
    "vienna":        (48.2082, 16.3738),
    "zurich":        (47.3769, 8.5417),
    "new york":      (40.7128, -74.0060),
    "los angeles":   (34.0522, -118.2437),
    "chicago":       (41.8781, -87.6298),
    "miami":         (25.7617, -80.1918),
    "washington":    (38.9072, -77.0369),
    "toronto":       (43.6532, -79.3832),
    "sydney":        (-33.8688, 151.2093),
    "tokyo":         (35.6762, 139.6503),
    "dubai":         (25.2048, 55.2708),
    "singapore":     (1.3521, 103.8198),
}


def extract_location(question):
    """
    Try to extract a city name from a weather market question.
    Returns (city_name, lat, lon) or None.
    """
    q = question.lower()
    for city, (lat, lon) in CITY_COORDS.items():
        if city in q:
            return city, lat, lon
    return None


def extract_temperature_threshold(question):
    """
    Extract temperature threshold and direction from question.
    e.g. "Will London exceed 20°C?" → (20, "above", "celsius")
    e.g. "Will NYC be above 75°F?" → (75, "above", "fahrenheit")
    """
    q = question.lower()

    # Direction
    direction = "above"
    if any(w in q for w in ["below", "under", "less than", "colder"]):
        direction = "below"

    # Temperature with unit
    # Match patterns like "20°c", "20 c", "75°f", "75 f", "20 degrees"
    celsius_match = re.search(r'(-?\d+(?:\.\d+)?)\s*°?\s*c(?:elsius)?(?:\b|°)', q)
    fahrenheit_match = re.search(r'(-?\d+(?:\.\d+)?)\s*°?\s*f(?:ahrenheit)?(?:\b|°)', q)

    if celsius_match:
        return float(celsius_match.group(1)), direction, "celsius"
    if fahrenheit_match:
        temp_f = float(fahrenheit_match.group(1))
        temp_c = (temp_f - 32) * 5/9
        return temp_c, direction, "fahrenheit"

    # Generic number that might be a temperature
    generic = re.search(r'(\d+(?:\.\d+)?)\s*degrees?', q)
    if generic:
        return float(generic.group(1)), direction, "celsius"

    return None


def extract_date_from_question(question, end_date_iso):
    """
    Try to get the relevant date from the question or end_date.
    Returns a date string YYYY-MM-DD.
    """
    q = question.lower()

    # Try to extract explicit date
    date_match = re.search(r'(\d{4}-\d{2}-\d{2})', question)
    if date_match:
        return date_match.group(1)

    # Try month/day patterns
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
    }
    for month_name, month_num in months.items():
        if month_name in q:
            day_match = re.search(r'\b(\d{1,2})\b', q)
            if day_match:
                day = int(day_match.group(1))
                year = datetime.now(timezone.utc).year
                try:
                    return f"{year}-{month_num:02d}-{day:02d}"
                except ValueError:
                    pass

    # Fall back to end_date
    if end_date_iso:
        return end_date_iso[:10]

    return None


def fetch_temperature_forecast(lat, lon, date_str):
    """
    Fetch hourly temperature forecast from Open-Meteo for a specific date.
    Returns list of hourly temperatures (Celsius) for that day.
    """
    params = {
        "latitude":        lat,
        "longitude":       lon,
        "hourly":          "temperature_2m",
        "start_date":      date_str,
        "end_date":        date_str,
        "timezone":        "UTC",
        "forecast_days":   16,
    }
    try:
        r = requests.get(f"{OPEN_METEO_BASE}/forecast", params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        temps = data.get("hourly", {}).get("temperature_2m", [])
        return [t for t in temps if t is not None]
    except Exception as e:
        print(f"  Open-Meteo error: {e}")
        return []


def fetch_ensemble_forecast(lat, lon, date_str):
    """
    Fetch ensemble forecast for uncertainty quantification.
    Uses Open-Meteo's ensemble model (51 members) for probability estimation.
    Returns list of daily max temperatures across ensemble members.
    """
    params = {
        "latitude":      lat,
        "longitude":     lon,
        "daily":         "temperature_2m_max",
        "start_date":    date_str,
        "end_date":      date_str,
        "timezone":      "UTC",
        "models":        "icon_seamless",
    }
    try:
        r = requests.get(f"{OPEN_METEO_BASE}/forecast", params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        daily_max = data.get("daily", {}).get("temperature_2m_max", [])
        return daily_max[0] if daily_max else None
    except Exception:
        return None


def estimate_probability(forecast_temps, threshold, direction, date_str):
    """
    Convert a temperature forecast to a probability.

    For a single forecast value, we use a normal distribution around
    the forecast with typical forecast error (±2°C for 5-day forecast,
    ±4°C for 10-day, ±6°C for 15-day).
    """
    if not forecast_temps:
        return None

    # Use daily max (most markets ask about max temp)
    daily_max = max(forecast_temps)

    # Estimate forecast error based on how far out we are
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        days_out    = (target_date - datetime.now(timezone.utc)).days
        days_out    = max(0, days_out)
    except ValueError:
        days_out = 5

    # Forecast uncertainty grows with time
    if days_out <= 3:
        sigma = 1.5
    elif days_out <= 7:
        sigma = 2.5
    elif days_out <= 10:
        sigma = 4.0
    else:
        sigma = 5.5

    # P(max > threshold) using normal CDF approximation
    z = (daily_max - threshold) / sigma

    # Approximate normal CDF: P(Z < z)
    def normal_cdf(z):
        return 0.5 * (1 + math.erf(z / math.sqrt(2)))

    if direction == "above":
        prob = 1 - normal_cdf(-z)  # P(temp > threshold) = P(Z > (thresh - mean)/sigma)
        prob = 1 - normal_cdf((threshold - daily_max) / sigma)
    else:
        prob = normal_cdf((threshold - daily_max) / sigma)

    return round(max(0.02, min(0.98, prob)), 4)


def scan_weather_markets(weather_markets, min_edge=0.06, max_volume=50_000):
    """
    Main scanner: compare Open-Meteo forecast probabilities to Polymarket prices.
    Returns list of opportunities with edge > min_edge.
    """
    opportunities = []

    for market in weather_markets:
        vol = float(market.get("volume", 0) or 0)
        if vol > max_volume:
            continue

        question = market.get("question", "") or market.get("event_title", "")
        end_date = market.get("end_date", "")
        poly_prob = market.get("p_yes", 0.5)

        # Extract location
        loc = extract_location(question)
        if not loc:
            continue
        city, lat, lon = loc

        # Extract temperature threshold
        temp_info = extract_temperature_threshold(question)
        if not temp_info:
            continue
        threshold, direction, unit = temp_info

        # Extract date
        date_str = extract_date_from_question(question, end_date)
        if not date_str:
            continue

        # Check date is in the future and within forecast range
        try:
            target = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            days_out = (target - datetime.now(timezone.utc)).days
            if days_out < 0 or days_out > 15:
                continue
        except ValueError:
            continue

        # Fetch forecast
        temps = fetch_temperature_forecast(lat, lon, date_str)
        if not temps:
            continue

        # Estimate probability
        model_prob = estimate_probability(temps, threshold, direction, date_str)
        if model_prob is None:
            continue

        edge = model_prob - poly_prob
        if abs(edge) < min_edge:
            continue

        outcome = "YES" if edge > 0 else "NO"
        if outcome == "NO":
            edge = poly_prob - model_prob
            market_prob = 1 - poly_prob   # we're betting NO, so our cost is 1 - poly_yes
            model_prob_for_trade = 1 - model_prob
        else:
            market_prob = poly_prob
            model_prob_for_trade = model_prob

        forecast_max = max(temps)
        opportunities.append({
            "question":    question,
            "city":        city,
            "threshold":   threshold,
            "direction":   direction,
            "date":        date_str,
            "days_out":    days_out,
            "outcome":     outcome,
            "edge":        round(edge, 4),
            "poly_prob":   round(poly_prob, 4),
            "model_prob":  round(model_prob, 4),
            "market_prob": round(market_prob, 4),
            "model_prob_for_trade": round(model_prob_for_trade, 4),
            "forecast_max_c": round(forecast_max, 1),
            "volume":      vol,
            "market":      market,
        })

    return sorted(opportunities, key=lambda x: x["edge"], reverse=True)
