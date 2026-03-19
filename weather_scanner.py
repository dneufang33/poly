"""
Weather Scanner
===============
Compares Open-Meteo weather forecasts (free, no key needed)
to Polymarket weather/temperature markets.

Two market types are handled differently:

  THRESHOLD markets ("exceed 20°C", "above 20°C", "below 20°C"):
    → P(daily_max > threshold), normal CDF model

  BRACKET markets ("be 20°C", "reach exactly 20°C"):
    → P(threshold-0.5 ≤ daily_max ≤ threshold+0.5), normal PDF model
    → Polymarket structures these as 1°C bucket markets

Markets with no explicit directional language are skipped entirely.
"""

import requests
import re
from datetime import datetime, timezone, timedelta
import math

OPEN_METEO_BASE = "https://api.open-meteo.com/v1"

# Per-run cache: avoids re-fetching the same (city, date) combination.
_forecast_cache: dict = {}
_session = requests.Session()

# Known city coordinates for location matching
CITY_COORDS = {
    "london":        (51.5074, -0.1278),
    "paris":         (48.8566,  2.3522),
    "berlin":        (52.5200, 13.4050),
    "madrid":        (40.4168, -3.7038),
    "rome":          (41.9028, 12.4964),
    "amsterdam":     (52.3676,  4.9041),
    "brussels":      (50.8503,  4.3517),
    "vienna":        (48.2082, 16.3738),
    "zurich":        (47.3769,  8.5417),
    "new york":      (40.7128, -74.0060),
    "los angeles":   (34.0522, -118.2437),
    "chicago":       (41.8781, -87.6298),
    "miami":         (25.7617, -80.1918),
    "washington":    (38.9072, -77.0369),
    "toronto":       (43.6532, -79.3832),
    "sydney":        (-33.8688, 151.2093),
    "tokyo":         (35.6762, 139.6503),
    "dubai":         (25.2048,  55.2708),
    "singapore":     (1.3521,  103.8198),
}

# Words that indicate a bracket ("be exactly") market
BRACKET_KEYWORDS = ["be ", " be "]

# Words that indicate a threshold market and their direction
ABOVE_KEYWORDS = ["exceed", "above", "over", "higher than", "more than",
                  "at least", "reach or exceed", "or higher", "or above"]
BELOW_KEYWORDS = ["below", "under", "less than", "colder than",
                  "no more than", "or lower", "or below"]


# ── Extraction helpers ────────────────────────────────────────────────────────

def extract_location(question):
    """
    Returns (city_name, lat, lon) for the first known city found, or None.
    """
    q = question.lower()
    for city, (lat, lon) in CITY_COORDS.items():
        if city in q:
            return city, lat, lon
    return None


def extract_temperature_threshold(question):
    """
    Extracts (threshold_celsius, direction, unit, market_type) from question text.

    market_type is one of:
      "above"   — threshold market, question asks if temp exceeds value
      "below"   — threshold market, question asks if temp stays under value
      "bracket" — exact-bucket market, question asks if temp lands on value

    Returns None if no temperature or no clear market type can be determined.
    """
    q = question.lower()

    # --- Detect market type ---
    if any(kw in q for kw in ABOVE_KEYWORDS):
        market_type = "above"
    elif any(kw in q for kw in BELOW_KEYWORDS):
        market_type = "below"
    elif any(kw in q for kw in BRACKET_KEYWORDS):
        market_type = "bracket"
    else:
        # No directional language found — skip to avoid misfires
        return None

    # --- Extract temperature value ---
    celsius_match    = re.search(r'(-?\d+(?:\.\d+)?)\s*°?\s*c(?:elsius)?(?:\b|°)', q)
    fahrenheit_match = re.search(r'(-?\d+(?:\.\d+)?)\s*°?\s*f(?:ahrenheit)?(?:\b|°)', q)
    degrees_match    = re.search(r'(-?\d+(?:\.\d+)?)\s*degrees?', q)

    if celsius_match:
        return float(celsius_match.group(1)), market_type, "celsius"
    if fahrenheit_match:
        temp_f = float(fahrenheit_match.group(1))
        return (temp_f - 32) * 5 / 9, market_type, "fahrenheit"
    if degrees_match:
        return float(degrees_match.group(1)), market_type, "celsius"

    return None


def extract_date_from_question(question, end_date_iso):
    """
    Extracts a YYYY-MM-DD date string from the question text.

    Key fix vs previous version: when a month name is found, we search for the
    day number AFTER the month name, not across the whole string. This prevents
    the temperature value (e.g. "31" in "31°C on March 22") from being
    mistaken for the day.
    """
    q = question.lower()

    # 1. Explicit ISO date
    iso_match = re.search(r'(\d{4}-\d{2}-\d{2})', question)
    if iso_match:
        return iso_match.group(1)

    # 2. "Month Day" patterns — search for the day AFTER the month keyword
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
    }
    for month_name, month_num in months.items():
        pos = q.find(month_name)
        if pos == -1:
            continue
        # Only look at text that comes AFTER the month name
        after_month = q[pos + len(month_name):]
        day_match = re.search(r'\b(\d{1,2})\b', after_month)
        if not day_match:
            continue
        day = int(day_match.group(1))
        if not 1 <= day <= 31:
            continue
        year = datetime.now(timezone.utc).year
        try:
            dt = datetime(year, month_num, day, tzinfo=timezone.utc)
            # If the resulting date is in the past by more than a day,
            # the market must be referring to next year
            if dt < datetime.now(timezone.utc) - timedelta(days=1):
                dt = datetime(year + 1, month_num, day, tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue  # invalid day for month (e.g. Feb 31)

    # 3. Fall back to the market's end_date
    if end_date_iso:
        return end_date_iso[:10]

    return None


# ── Forecast & probability ────────────────────────────────────────────────────

def fetch_temperature_forecast(lat, lon, date_str):
    """
    Returns list of hourly temperatures (°C) for date_str at (lat, lon).
    Results cached per (lat, lon, date) — many markets share city+date.
    """
    cache_key = (round(lat, 3), round(lon, 3), date_str)
    if cache_key in _forecast_cache:
        return _forecast_cache[cache_key]

    params = {
        "latitude":   lat,
        "longitude":  lon,
        "hourly":     "temperature_2m",
        "start_date": date_str,
        "end_date":   date_str,
        "timezone":   "UTC",
    }
    try:
        r = _session.get(f"{OPEN_METEO_BASE}/forecast", params=params, timeout=15)
        r.raise_for_status()
        result = [t for t in r.json().get("hourly", {}).get("temperature_2m", [])
                  if t is not None]
    except Exception as e:
        print(f"  Open-Meteo error: {e}")
        result = []

    _forecast_cache[cache_key] = result
    return result


def forecast_sigma(days_out):
    """Typical temperature forecast error (°C) as a function of lead time."""
    if days_out <= 3:  return 1.5
    if days_out <= 7:  return 2.5
    if days_out <= 10: return 4.0
    return 5.5


def normal_cdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def estimate_probability(forecast_temps, threshold, market_type, date_str):
    """
    Convert a temperature forecast into a probability matching market_type.

    market_type == "above"  : P(daily_max > threshold)   — cumulative CDF
    market_type == "below"  : P(daily_max < threshold)   — cumulative CDF
    market_type == "bracket": P(threshold-0.5 ≤ daily_max ≤ threshold+0.5)
                              — 1°C bucket, uses normal PDF

    Returns None if forecast is empty.
    """
    if not forecast_temps:
        return None

    daily_max = max(forecast_temps)

    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        days_out = max(0, (target - datetime.now(timezone.utc)).days)
    except ValueError:
        days_out = 5

    sigma = forecast_sigma(days_out)

    if market_type == "above":
        prob = 1 - normal_cdf((threshold - daily_max) / sigma)
    elif market_type == "below":
        prob = normal_cdf((threshold - daily_max) / sigma)
    else:  # bracket
        prob = (normal_cdf((threshold + 0.5 - daily_max) / sigma) -
                normal_cdf((threshold - 0.5 - daily_max) / sigma))

    return round(max(0.01, min(0.99, prob)), 4)


# ── Main scanner ──────────────────────────────────────────────────────────────

def scan_weather_markets(weather_markets, min_edge=0.06, max_edge=0.50,
                         max_volume=50_000, min_days_out=1):
    """
    Compare Open-Meteo model probabilities to Polymarket prices.

    Filters applied (all must pass before a trade is considered):
      1. Volume ≤ max_volume
      2. Recognisable city
      3. Explicit directional language (above / below / bracket)
         — questions with no direction keyword are skipped
      4. Extractable temperature value
      5. Extractable date that is ≥ min_days_out days away and ≤ 15 days away
      6. min_edge < computed_edge ≤ max_edge
         (max_edge guard catches inverted YES/NO prices in the API)

    Deduplication: a Polymarket event often spawns multiple binary sub-markets
    for the same condition at different prices. Only the best-priced (highest
    edge) sub-market per (city, threshold, market_type, date, outcome) is kept.
    """
    opportunities  = []
    skip_no_dir    = 0
    skip_suspicious = 0

    for market in weather_markets:
        vol = float(market.get("volume", 0) or 0)
        if vol > max_volume:
            continue

        question  = market.get("question", "") or market.get("event_title", "")
        end_date  = market.get("end_date", "")
        poly_prob = market.get("p_yes", 0.5)

        # --- Location ---
        loc = extract_location(question)
        if not loc:
            continue
        city, lat, lon = loc

        # --- Temperature & market type ---
        temp_info = extract_temperature_threshold(question)
        if temp_info is None:
            skip_no_dir += 1
            continue
        threshold, market_type, unit = temp_info

        # --- Date (must be found and far enough ahead) ---
        date_str = extract_date_from_question(question, end_date)
        if not date_str:
            continue

        try:
            target   = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            days_out = (target - datetime.now(timezone.utc)).days
        except ValueError:
            continue

        if days_out < min_days_out or days_out > 15:
            continue

        # --- Forecast & model probability ---
        temps = fetch_temperature_forecast(lat, lon, date_str)
        if not temps:
            continue

        model_prob = estimate_probability(temps, threshold, market_type, date_str)
        if model_prob is None:
            continue

        # --- Edge calculation ---
        # poly_prob = price of YES, where YES means the question resolves true.
        # For "above" markets: YES = temp exceeds threshold.
        # For "below" markets: YES = temp stays under threshold.
        # For "bracket" markets: YES = temp lands in the bucket.
        # model_prob is the probability of the question resolving YES.
        raw_edge = model_prob - poly_prob

        if abs(raw_edge) < min_edge:
            continue

        if raw_edge > 0:
            outcome              = "YES"
            edge                 = raw_edge
            market_prob          = poly_prob
            model_prob_for_trade = model_prob
        else:
            outcome              = "NO"
            edge                 = poly_prob - model_prob          # always positive
            market_prob          = 1 - poly_prob                   # cost of NO share
            model_prob_for_trade = 1 - model_prob

        if edge > max_edge:
            skip_suspicious += 1
            continue

        opportunities.append({
            "question":    question,
            "city":        city,
            "threshold":   threshold,
            "threshold_c": round(threshold, 1),
            "market_type": market_type,   # "above" / "below" / "bracket"
            "direction":   market_type,   # kept for backwards compat with log_trade
            "date":        date_str,
            "days_out":    days_out,
            "outcome":     outcome,
            "edge":        round(edge, 4),
            "poly_prob":   round(poly_prob, 4),
            "model_prob":  round(model_prob, 4),
            "market_prob": round(market_prob, 4),
            "model_prob_for_trade": round(model_prob_for_trade, 4),
            "forecast_max_c": round(max(temps), 1),
            "volume":      vol,
            "market":      market,
        })

    # --- Reporting ---
    if skip_no_dir:
        print(f"  ℹ Skipped {skip_no_dir} markets with no directional language")
    if skip_suspicious:
        print(f"  ⚠ Skipped {skip_suspicious} markets with edge >{max_edge*100:.0f}% "
              f"(likely inverted YES/NO prices)")

    # --- Deduplicate sub-markets ---
    # A single Polymarket event spawns multiple binary sub-markets for the same
    # condition at different prices. Keep only the one with the best edge.
    seen: dict = {}
    for opp in opportunities:
        key = (opp["city"], round(opp["threshold"], 1),
               opp["market_type"], opp["date"], opp["outcome"])
        if key not in seen or opp["edge"] > seen[key]["edge"]:
            seen[key] = opp

    deduped = list(seen.values())
    if len(deduped) < len(opportunities):
        print(f"  ℹ Deduplicated {len(opportunities)} sub-markets → "
              f"{len(deduped)} unique conditions")

    return sorted(deduped, key=lambda x: x["edge"], reverse=True)
