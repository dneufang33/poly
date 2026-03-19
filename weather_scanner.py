"""
Weather Scanner v3
==================
Compares temperature forecasts from TWO independent sources to Polymarket
weather markets.  A trade is only considered when:

  1. The market closes in MIN_HOURS_TO_CLOSE … MAX_HOURS_TO_CLOSE hours
     (not too close to resolution, not too far out — sweet spot ~6-30h)
  2. Both forecast sources predict a daily max within MODEL_AGREEMENT_C of
     each other  (they agree on what the weather will be)
  3. Both sources independently show edge in the SAME direction (YES or NO)

Forecast sources
----------------
  Primary   : Open-Meteo ECMWF  — global, free, no key
  Secondary :
    US cities → NOAA/NWS hourly forecast  (api.weather.gov, no key)
    All other → MET Norway compact        (api.met.no, no key)
    Fallback  → Open-Meteo GFS model      (different model, independent signal)

Sigma (forecast uncertainty) is now a continuous function of hours-to-close
rather than coarse day buckets:
  ≤ 6h  → ±0.8°C   (very reliable, cold front either arrived or not)
  ≤12h  → ±1.2°C
  ≤18h  → ±1.5°C
  ≤24h  → ±2.0°C
  ≤36h  → ±2.8°C
  > 36h → ±3.5°C

Market types
------------
  "above"   Will temp EXCEED threshold?  → cumulative P(T > threshold)
  "below"   Will temp STAY UNDER?        → cumulative P(T < threshold)
  "bracket" Will temp BE exactly bucket? → P(threshold±0.5°C)
"""

import re
import math
from datetime import datetime, timezone, timedelta

import requests

# ── Constants ──────────────────────────────────────────────────────────────────

OPEN_METEO_BASE = "https://api.open-meteo.com/v1"
NWS_BASE        = "https://api.weather.gov"
METNO_BASE      = "https://api.met.no/weatherapi/locationforecast/2.0/compact"

_BOT_UA = "PredictionMarketBot/1.0 (research; contact via github)"

# Shared session with proper User-Agent (required by MET Norway ToS)
_session = requests.Session()
_session.headers.update({"User-Agent": _BOT_UA})

# Per-run caches keyed by (source, lat, lon, date_str)
_cache: dict = {}

# ── City data ──────────────────────────────────────────────────────────────────

CITY_COORDS = {
    "london":      (51.5074,  -0.1278),
    "paris":       (48.8566,   2.3522),
    "berlin":      (52.5200,  13.4050),
    "madrid":      (40.4168,  -3.7038),
    "rome":        (41.9028,  12.4964),
    "amsterdam":   (52.3676,   4.9041),
    "brussels":    (50.8503,   4.3517),
    "vienna":      (48.2082,  16.3738),
    "zurich":      (47.3769,   8.5417),
    "new york":    (40.7128, -74.0060),
    "los angeles": (34.0522, -118.2437),
    "chicago":     (41.8781, -87.6298),
    "miami":       (25.7617, -80.1918),
    "washington":  (38.9072, -77.0369),
    "toronto":     (43.6532, -79.3832),
    "sydney":      (-33.8688, 151.2093),
    "tokyo":       (35.6762,  139.6503),
    "dubai":       (25.2048,   55.2708),
    "singapore":   (1.3521,  103.8198),
}

# NWS only covers the contiguous US + some territories
NWS_CITIES = {"new york", "los angeles", "chicago", "miami", "washington"}

# ── Market-type detection keywords ────────────────────────────────────────────

ABOVE_KEYWORDS   = ["exceed", "above", "over ", "higher than", "more than",
                     "at least", "reach or exceed", "or higher", "or above"]
BELOW_KEYWORDS   = ["below", "under ", "less than", "colder than",
                     "no more than", "or lower", "or below"]
BRACKET_KEYWORDS = [" be ", "be exactly", "equal"]


# ── Extraction helpers ─────────────────────────────────────────────────────────

def extract_location(question):
    """Returns (city, lat, lon) for the first known city in question, or None."""
    q = question.lower()
    for city, (lat, lon) in CITY_COORDS.items():
        if city in q:
            return city, lat, lon
    return None


def extract_temperature_threshold(question):
    """
    Returns (threshold_celsius, market_type, unit) or None.

    market_type: "above" | "below" | "bracket"
    Skips markets with no recognisable directional language.
    """
    q = question.lower()

    if any(kw in q for kw in ABOVE_KEYWORDS):
        market_type = "above"
    elif any(kw in q for kw in BELOW_KEYWORDS):
        market_type = "below"
    elif any(kw in q for kw in BRACKET_KEYWORDS):
        market_type = "bracket"
    else:
        return None  # no directional language → skip

    c_match = re.search(r'(-?\d+(?:\.\d+)?)\s*°?\s*c(?:elsius)?(?:\b|°)', q)
    f_match = re.search(r'(-?\d+(?:\.\d+)?)\s*°?\s*f(?:ahrenheit)?(?:\b|°)', q)
    d_match = re.search(r'(-?\d+(?:\.\d+)?)\s*degrees?', q)

    if c_match:
        return float(c_match.group(1)), market_type, "celsius"
    if f_match:
        return (float(f_match.group(1)) - 32) * 5 / 9, market_type, "fahrenheit"
    if d_match:
        return float(d_match.group(1)), market_type, "celsius"
    return None


def extract_date_from_question(question, end_date_iso):
    """
    Returns YYYY-MM-DD string.

    Key fix: searches for the day number AFTER the month name, so "31°C on
    March 22" correctly returns March 22, not March 31.
    """
    q = question.lower()

    iso = re.search(r'(\d{4}-\d{2}-\d{2})', question)
    if iso:
        return iso.group(1)

    months = {
        "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
        "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
    }
    for name, num in months.items():
        pos = q.find(name)
        if pos == -1:
            continue
        after = q[pos + len(name):]
        dm = re.search(r'\b(\d{1,2})\b', after)
        if not dm:
            continue
        day = int(dm.group(1))
        if not 1 <= day <= 31:
            continue
        year = datetime.now(timezone.utc).year
        try:
            dt = datetime(year, num, day, tzinfo=timezone.utc)
            if dt < datetime.now(timezone.utc) - timedelta(days=1):
                dt = datetime(year + 1, num, day, tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    return end_date_iso[:10] if end_date_iso else None


def parse_end_datetime(end_date_iso):
    """Parse Polymarket end_date string → UTC datetime, or None."""
    if not end_date_iso:
        return None
    try:
        return datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.strptime(end_date_iso[:10], "%Y-%m-%d").replace(
                tzinfo=timezone.utc)
        except ValueError:
            return None


# ── Sigma: dynamic forecast uncertainty ───────────────────────────────────────

def hours_to_sigma(hours):
    """
    Returns forecast uncertainty σ in °C as a function of lead time in hours.

    Derived from published NWP verification stats (ECMWF/GFS RMSE for 2m temp).
    Short-range errors are dominated by boundary-layer and surface energy
    budget uncertainty; beyond ~24h synoptic-scale errors dominate.
    """
    if hours <=  6: return 0.8
    if hours <= 12: return 1.2
    if hours <= 18: return 1.5
    if hours <= 24: return 2.0
    if hours <= 36: return 2.8
    return 3.5


# ── Probability model ──────────────────────────────────────────────────────────

def normal_cdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def estimate_probability(daily_max_c, threshold, market_type, hours_until_close):
    """
    P(market resolves YES) given:
      daily_max_c      : forecast daily maximum temperature (°C)
      threshold        : market temperature threshold (°C)
      market_type      : "above" | "below" | "bracket"
      hours_until_close: hours until market closes (drives sigma)

    Returns probability in [0.01, 0.99].
    """
    sigma = hours_to_sigma(hours_until_close)

    if market_type == "above":
        p = 1 - normal_cdf((threshold - daily_max_c) / sigma)
    elif market_type == "below":
        p = normal_cdf((threshold - daily_max_c) / sigma)
    else:  # bracket: ±0.5°C window
        p = (normal_cdf((threshold + 0.5 - daily_max_c) / sigma) -
             normal_cdf((threshold - 0.5 - daily_max_c) / sigma))

    return round(max(0.01, min(0.99, p)), 4)


# ── Forecast fetchers ──────────────────────────────────────────────────────────

def _cached(key, fn):
    """Simple cache wrapper."""
    if key not in _cache:
        _cache[key] = fn()
    return _cache[key]


def fetch_openmeteo_max(lat, lon, date_str, model="best_match"):
    """
    Open-Meteo hourly forecast → daily max °C.
    model="best_match" uses ECMWF (primary); model="gfs_seamless" for secondary.
    """
    key = ("ometo", model, round(lat, 3), round(lon, 3), date_str)
    def _fetch():
        params = {
            "latitude":   lat,
            "longitude":  lon,
            "hourly":     "temperature_2m",
            "start_date": date_str,
            "end_date":   date_str,
            "timezone":   "UTC",
            "models":     model,
        }
        try:
            r = _session.get(f"{OPEN_METEO_BASE}/forecast", params=params, timeout=15)
            r.raise_for_status()
            temps = [t for t in r.json().get("hourly", {})
                     .get("temperature_2m", []) if t is not None]
            return max(temps) if temps else None
        except Exception as e:
            print(f"  Open-Meteo ({model}) error: {e}")
            return None
    return _cached(key, _fetch)


def fetch_nws_max(lat, lon, date_str):
    """
    NOAA/NWS hourly forecast → daily max °C for the given date.
    Only works for US locations (api.weather.gov).
    Two-step: /points → forecastHourly URL → filter by date.
    """
    key = ("nws", round(lat, 3), round(lon, 3), date_str)
    def _fetch():
        try:
            # Step 1: resolve grid
            r1 = _session.get(f"{NWS_BASE}/points/{lat:.4f},{lon:.4f}",
                              timeout=10)
            r1.raise_for_status()
            hourly_url = r1.json()["properties"]["forecastHourly"]

            # Step 2: get hourly forecast
            r2 = _session.get(hourly_url, timeout=10)
            r2.raise_for_status()
            periods = r2.json()["properties"]["periods"]

            # Filter to date_str, collect temperatures (NWS returns °F)
            temps_f = [
                p["temperature"] for p in periods
                if p.get("startTime", "").startswith(date_str)
                and isinstance(p.get("temperature"), (int, float))
            ]
            if not temps_f:
                return None
            max_f = max(temps_f)
            return round((max_f - 32) * 5 / 9, 2)          # convert to °C
        except Exception as e:
            print(f"  NWS error: {e}")
            return None
    return _cached(key, _fetch)


def fetch_metno_max(lat, lon, date_str):
    """
    MET Norway compact forecast → daily max °C.
    Global coverage; requires User-Agent header (already set on _session).
    """
    key = ("metno", round(lat, 3), round(lon, 3), date_str)
    def _fetch():
        try:
            r = _session.get(METNO_BASE,
                             params={"lat": round(lat, 4), "lon": round(lon, 4)},
                             timeout=15)
            r.raise_for_status()
            series = r.json()["properties"]["timeseries"]
            temps = [
                entry["properties"]["instant"]["details"]["air_temperature"]
                for entry in series
                if entry.get("time", "").startswith(date_str)
            ]
            return max(temps) if temps else None
        except Exception as e:
            print(f"  MET Norway error: {e}")
            return None
    return _cached(key, _fetch)


def fetch_secondary_max(city, lat, lon, date_str):
    """
    Route to the best available secondary source for this city.
    NWS for US cities; MET Norway for all others.
    Falls back to Open-Meteo GFS if both fail.
    """
    if city in NWS_CITIES:
        result = fetch_nws_max(lat, lon, date_str)
        if result is not None:
            return result, "NWS"

    result = fetch_metno_max(lat, lon, date_str)
    if result is not None:
        return result, "MET Norway"

    # Final fallback: Open-Meteo GFS (independent model from primary ECMWF)
    result = fetch_openmeteo_max(lat, lon, date_str, model="gfs_seamless")
    return result, "Open-Meteo GFS"


# ── Main scanner ───────────────────────────────────────────────────────────────

def scan_weather_markets(
    weather_markets,
    min_edge              = 0.06,   # minimum edge for both sources
    max_edge              = 0.50,   # above = likely corrupted market data
    max_volume            = 50_000,
    min_hours_to_close    = 6,      # don't trade within 6h of resolution
    max_hours_to_close    = 30,     # don't trade more than 30h out
    model_agreement_c     = 2.0,    # max °C difference between the two sources
):
    """
    Scans Polymarket weather markets and returns opportunities where BOTH
    independent forecast sources agree there is edge.

    A trade passes all of these gates:
      ① Volume ≤ max_volume
      ② Recognisable city with known coordinates
      ③ Explicit directional language in the question
      ④ Extractable temperature threshold
      ⑤ Market closes in [min_hours_to_close, max_hours_to_close]
      ⑥ Primary source (Open-Meteo ECMWF) available
      ⑦ Secondary source (NWS / MET Norway / GFS) available
      ⑧ |primary_max - secondary_max| ≤ model_agreement_c
      ⑨ Both sources show edge in the SAME direction
      ⑩ min_edge < consensus_edge ≤ max_edge
    """
    opportunities   = []
    skip_no_dir     = 0
    skip_timing     = 0
    skip_no_data    = 0
    skip_disagree   = 0
    skip_suspicious = 0
    now             = datetime.now(timezone.utc)

    for market in weather_markets:
        vol = float(market.get("volume", 0) or 0)
        if vol > max_volume:
            continue

        question  = market.get("question", "") or market.get("event_title", "")
        end_date  = market.get("end_date", "")
        poly_prob = market.get("p_yes", 0.5)

        # ① Location
        loc = extract_location(question)
        if not loc:
            continue
        city, lat, lon = loc

        # ② Directional language + threshold
        temp_info = extract_temperature_threshold(question)
        if temp_info is None:
            skip_no_dir += 1
            continue
        threshold, market_type, _unit = temp_info

        # ③ Date
        date_str = extract_date_from_question(question, end_date)
        if not date_str:
            continue

        # ④ Hours-to-close filter
        end_dt = parse_end_datetime(end_date)
        if end_dt is None:
            # Fall back to midnight of target date
            try:
                end_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc) + timedelta(days=1)
            except ValueError:
                continue

        hours_until_close = (end_dt - now).total_seconds() / 3600
        if not (min_hours_to_close <= hours_until_close <= max_hours_to_close):
            skip_timing += 1
            continue

        # ⑤ Primary forecast (Open-Meteo ECMWF)
        primary_max = fetch_openmeteo_max(lat, lon, date_str, model="best_match")
        if primary_max is None:
            skip_no_data += 1
            continue

        # ⑥ Secondary forecast
        secondary_max, secondary_src = fetch_secondary_max(city, lat, lon, date_str)
        if secondary_max is None:
            skip_no_data += 1
            continue

        # ⑦ Model agreement check
        if abs(primary_max - secondary_max) > model_agreement_c:
            skip_disagree += 1
            continue

        # Use the average of both as the consensus forecast
        consensus_max = (primary_max + secondary_max) / 2

        # ⑧ Probability from each source independently
        p_primary   = estimate_probability(primary_max,   threshold,
                                           market_type, hours_until_close)
        p_secondary = estimate_probability(secondary_max, threshold,
                                           market_type, hours_until_close)

        edge_primary   = p_primary   - poly_prob
        edge_secondary = p_secondary - poly_prob

        # Both must show edge in the same direction
        if (edge_primary > 0) != (edge_secondary > 0):
            skip_disagree += 1
            continue

        # Both must individually exceed min_edge
        if abs(edge_primary) < min_edge or abs(edge_secondary) < min_edge:
            continue

        # Consensus edge: use the more conservative (smaller absolute) of the two
        edge_raw = min(abs(edge_primary), abs(edge_secondary))
        if edge_primary > 0:
            outcome     = "YES"
            market_prob = poly_prob
            model_prob  = (p_primary + p_secondary) / 2
        else:
            outcome     = "NO"
            market_prob = 1 - poly_prob
            model_prob  = 1 - (p_primary + p_secondary) / 2

        # ⑨ Max edge guard (corrupted market data)
        if edge_raw > max_edge:
            skip_suspicious += 1
            continue

        opportunities.append({
            "question":         question,
            "city":             city,
            "threshold":        threshold,
            "threshold_c":      round(threshold, 1),
            "market_type":      market_type,
            "direction":        market_type,        # back-compat with log_trade
            "date":             date_str,
            "hours_until_close": round(hours_until_close, 1),
            "outcome":          outcome,
            "edge":             round(edge_raw, 4),
            "poly_prob":        round(poly_prob, 4),
            "model_prob":       round(model_prob, 4),
            "market_prob":      round(market_prob, 4),
            "model_prob_for_trade": round(model_prob, 4),
            # Per-source detail for transparency
            "forecast_primary_c":   round(primary_max, 1),
            "forecast_secondary_c": round(secondary_max, 1),
            "forecast_consensus_c": round(consensus_max, 1),
            "secondary_source":     secondary_src,
            "sigma_c":              round(hours_to_sigma(hours_until_close), 2),
            "volume":           vol,
            "market":           market,
        })

    # ── Reporting ────────────────────────────────────────────────────────────
    if skip_no_dir:
        print(f"  ℹ {skip_no_dir} markets skipped — no directional language")
    if skip_timing:
        print(f"  ℹ {skip_timing} markets skipped — outside "
              f"{min_hours_to_close}–{max_hours_to_close}h window")
    if skip_no_data:
        print(f"  ℹ {skip_no_data} markets skipped — forecast unavailable")
    if skip_disagree:
        print(f"  ℹ {skip_disagree} markets skipped — models disagree "
              f"(>{model_agreement_c}°C apart or opposite direction)")
    if skip_suspicious:
        print(f"  ⚠ {skip_suspicious} markets skipped — edge >{max_edge*100:.0f}% "
              f"(likely inverted prices)")

    # ── Deduplicate sub-markets ───────────────────────────────────────────────
    seen: dict = {}
    for opp in opportunities:
        key = (opp["city"], round(opp["threshold"], 1),
               opp["market_type"], opp["date"], opp["outcome"])
        if key not in seen or opp["edge"] > seen[key]["edge"]:
            seen[key] = opp

    deduped = list(seen.values())
    if len(deduped) < len(opportunities):
        print(f"  ℹ Deduplicated {len(opportunities)} → "
              f"{len(deduped)} unique conditions")

    return sorted(deduped, key=lambda x: x["edge"], reverse=True)
