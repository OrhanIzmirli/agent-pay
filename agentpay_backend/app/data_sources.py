"""
Data sources.

PARAMETRIC INSURANCE (current role) - see the "Rainfall readings" section
at the bottom. `fetch_rainfall_readings()` pulls TWO independent rainfall
totals for the same lat/lon + window from Open-Meteo using two different
`models=` products (best_match and ECMWF IFS by default). Same provider,
different underlying models/assimilation, no API key. `simulate_reading()`
lets the demo force a disagreement on stage.

LEGACY (v1 "agent pays for API calls" demo) - everything between here and
that section. Kept for reference; main.py no longer calls it.

Once the agent has paid, it has to come back with something *real* - a
placeholder string after a real on-chain payment looks fake to judges.
This module maps a query to a free, keyless public API:

  - crypto price  -> CoinGecko  (fallback: Coinbase spot price)
  - weather       -> Open-Meteo (geocoding + current conditions)
  - FX rate       -> Frankfurter (ECB reference rates; exchangerate.host
                     now requires an API key, so we don't use it)

Anything else - or any API failure/timeout - degrades gracefully to a
placeholder string so POST /agent/request never 500s because a third-party
API is down. `DataResult.live` tells the caller whether it was real data.
"""

import asyncio
import os
import re
from dataclasses import dataclass

import httpx

from app.models import SourceReading

HTTP_TIMEOUT = 8.0  # seconds - keep the demo snappy even if an API hangs
HEADERS = {"User-Agent": "agent-pay-hackathon-demo/1.0", "Accept": "application/json"}

# Where the hackathon is; used when a weather query names no location.
DEFAULT_CITY = "Warsaw"


@dataclass
class DataResult:
    source: str   # e.g. "CoinGecko", "Open-Meteo", "Frankfurter", "none"
    text: str     # human-readable answer to show in the UI
    live: bool    # True if this came from a real external API call


# ---------------------------------------------------------------------------
# Query classification helpers
# ---------------------------------------------------------------------------

# alias -> (coingecko id, ticker). Only unambiguous words are listed on
# purpose (e.g. "link", "dot", "ton" would false-match ordinary English).
COINS: dict[str, tuple[str, str]] = {
    "sol": ("solana", "SOL"), "solana": ("solana", "SOL"),
    "btc": ("bitcoin", "BTC"), "bitcoin": ("bitcoin", "BTC"),
    "eth": ("ethereum", "ETH"), "ethereum": ("ethereum", "ETH"),
    "usdc": ("usd-coin", "USDC"),
    "usdt": ("tether", "USDT"), "tether": ("tether", "USDT"),
    "bonk": ("bonk", "BONK"),
    "jup": ("jupiter-exchange-solana", "JUP"), "jupiter": ("jupiter-exchange-solana", "JUP"),
    "doge": ("dogecoin", "DOGE"), "dogecoin": ("dogecoin", "DOGE"),
    "ada": ("cardano", "ADA"), "cardano": ("cardano", "ADA"),
    "xrp": ("ripple", "XRP"), "ripple": ("ripple", "XRP"),
    "avax": ("avalanche-2", "AVAX"), "avalanche": ("avalanche-2", "AVAX"),
    "bnb": ("binancecoin", "BNB"),
    "matic": ("matic-network", "MATIC"), "polygon": ("matic-network", "MATIC"),
    "pyth": ("pyth-network", "PYTH"),
    "ray": ("raydium", "RAY"), "raydium": ("raydium", "RAY"),
}

FIAT_CODES = {
    "usd", "eur", "gbp", "pln", "try", "jpy", "chf", "cad", "aud", "czk",
    "sek", "nok", "dkk", "huf", "cny", "inr", "brl", "mxn", "krw", "nzd",
    "sgd", "hkd", "zar", "ron", "bgn", "ils", "thb", "php", "idr", "myr",
}
FIAT_WORDS = {
    "dollar": "usd", "dollars": "usd",
    "euro": "eur", "euros": "eur",
    "pound": "gbp", "pounds": "gbp", "sterling": "gbp",
    "zloty": "pln", "zlotys": "pln", "zloty": "pln",
    "lira": "try", "liras": "try",
    "yen": "jpy",
    "franc": "chf", "francs": "chf",
}

WEATHER_WORDS = (
    "weather", "temperature", "forecast", "rain", "raining", "snow",
    "sunny", "how hot", "how cold", "humidity", "windy",
)
FX_WORDS = ("exchange rate", "fx", "convert", "conversion", "forex")

WMO_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog",
    51: "light drizzle", 53: "moderate drizzle", 55: "dense drizzle",
    56: "freezing drizzle", 57: "dense freezing drizzle",
    61: "slight rain", 63: "moderate rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain",
    71: "slight snow", 73: "moderate snow", 75: "heavy snow", 77: "snow grains",
    80: "slight rain showers", 81: "moderate rain showers", 82: "violent rain showers",
    85: "slight snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with slight hail", 99: "thunderstorm with heavy hail",
}


def _words(q: str) -> list[str]:
    return re.findall(r"[^\W\d_]+", q.lower())


def _find_coin(q: str) -> tuple[str, str] | None:
    for w in _words(q):
        if w in COINS:
            return COINS[w]
    return None


def _find_fiats(q: str) -> list[str]:
    """Distinct fiat codes mentioned in the query, in order of appearance."""
    found: list[str] = []
    for w in _words(q):
        code = w if w in FIAT_CODES else FIAT_WORDS.get(w)
        if code and code not in found:
            found.append(code)
    return found


def _find_amount(q: str) -> float:
    m = re.search(r"(\d+(?:[.,]\d+)?)", q)
    if not m:
        return 1.0
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return 1.0


def _extract_location(q: str) -> str:
    """Pull 'Warsaw' out of 'what's the weather in Warsaw today?'."""
    stop = r"today|now|right now|tomorrow|currently|tonight|this week|at the moment|please"
    m = re.search(
        rf"\b(?:in|at|for)\s+([^?,.!]+?)(?=\s*(?:[?,.!]|$|\b(?:{stop})\b))",
        q,
        re.IGNORECASE,
    )
    if m:
        loc = m.group(1).strip()
        if loc and loc.lower() not in ("the", "my city", "here"):
            return loc
    return DEFAULT_CITY


def _fmt_price(p: float) -> str:
    if p >= 1:
        return f"{p:,.2f}"
    return f"{p:.6f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

async def _crypto_price(client: httpx.AsyncClient, cg_id: str, ticker: str, vs: str) -> DataResult:
    vs = vs.lower()
    # Primary: CoinGecko (has 24h change). The public API is rate-limited
    # (roughly 10-30 req/min) - if we hit 429 during the demo, fall through
    # to Coinbase's public spot price.
    try:
        r = await client.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": cg_id, "vs_currencies": vs, "include_24hr_change": "true"},
        )
        r.raise_for_status()
        entry = r.json()[cg_id]
        price = float(entry[vs])
        change = entry.get(f"{vs}_24h_change")
        change_txt = f", 24h {change:+.2f}%" if change is not None else ""
        return DataResult(
            source="CoinGecko",
            text=f"{ticker} is trading at {_fmt_price(price)} {vs.upper()}{change_txt} (source: CoinGecko, live)",
            live=True,
        )
    except Exception as e:  # noqa: BLE001 - any failure -> try the fallback
        print(f"[data_sources] CoinGecko failed ({e!r}); trying Coinbase")

    r = await client.get(f"https://api.coinbase.com/v2/prices/{ticker}-{vs.upper()}/spot")
    r.raise_for_status()
    price = float(r.json()["data"]["amount"])
    return DataResult(
        source="Coinbase",
        text=f"{ticker} is trading at {_fmt_price(price)} {vs.upper()} (source: Coinbase spot, live)",
        live=True,
    )


async def _weather(client: httpx.AsyncClient, location: str) -> DataResult:
    geo = await client.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": location, "count": 1, "language": "en", "format": "json"},
    )
    geo.raise_for_status()
    results = geo.json().get("results") or []
    if not results:
        raise ValueError(f"location not found: {location!r}")
    place = results[0]
    name = place["name"]
    country = place.get("country", "")

    fc = await client.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
        },
    )
    fc.raise_for_status()
    cur = fc.json()["current"]
    desc = WMO_CODES.get(int(cur.get("weather_code", -1)), "unknown conditions")
    where = f"{name}, {country}" if country else name
    return DataResult(
        source="Open-Meteo",
        text=(
            f"Weather in {where}: {cur['temperature_2m']} C ({desc}), feels like "
            f"{cur['apparent_temperature']} C, humidity {cur['relative_humidity_2m']}%, "
            f"wind {cur['wind_speed_10m']} km/h (source: Open-Meteo, live)"
        ),
        live=True,
    )


async def _fx_rate(client: httpx.AsyncClient, base: str, quote: str, amount: float) -> DataResult:
    base, quote = base.upper(), quote.upper()
    r = await client.get(
        "https://api.frankfurter.dev/v1/latest",
        params={"base": base, "symbols": quote},
    )
    r.raise_for_status()
    body = r.json()
    rate = float(body["rates"][quote])
    date = body.get("date", "")
    if amount != 1:
        amount_txt = f"{amount:g} {base} = {amount * rate:,.4f} {quote}"
    else:
        amount_txt = f"1 {base} = {rate:,.4f} {quote}"
    return DataResult(
        source="Frankfurter",
        text=f"{amount_txt} (ECB reference rate {date}, source: Frankfurter, live)",
        live=True,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def fallback_result(query: str) -> DataResult:
    return DataResult(
        source="none",
        text=(
            f"No live data source matched '{query}'. Supported right now: crypto "
            "prices (e.g. 'current price of SOL'), weather (e.g. 'weather in Warsaw') "
            "and FX rates (e.g. 'USD to PLN exchange rate')."
        ),
        live=False,
    )


def classify(query: str) -> str:
    """'crypto' | 'weather' | 'fx' | 'none'. Exposed so the decision logic
    could use it later (e.g. refuse to pay when there's nothing to buy)."""
    q = query.lower()
    if _find_coin(q):
        return "crypto"
    fiats = _find_fiats(q)
    if len(fiats) >= 2 or (fiats and any(w in q for w in FX_WORDS)):
        return "fx"
    if any(w in q for w in WEATHER_WORDS):
        return "weather"
    return "none"


async def fetch_result(query: str) -> DataResult:
    """Look up real data for `query`. Never raises - returns a fallback
    DataResult (live=False) if nothing matches or the API call fails."""
    q = query.lower()
    kind = classify(q)
    if kind == "none":
        return fallback_result(query)

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=HEADERS, follow_redirects=True) as client:
            if kind == "crypto":
                cg_id, ticker = _find_coin(q)  # type: ignore[misc]
                fiats = _find_fiats(q)
                return await _crypto_price(client, cg_id, ticker, fiats[0] if fiats else "usd")
            if kind == "fx":
                fiats = _find_fiats(q)
                base = fiats[0]
                quote = fiats[1] if len(fiats) > 1 else ("eur" if base == "usd" else "usd")
                return await _fx_rate(client, base, quote, _find_amount(q))
            if kind == "weather":
                return await _weather(client, _extract_location(query))
    except Exception as e:  # noqa: BLE001 - never let a 3rd-party API 500 the demo
        print(f"[data_sources] {kind} lookup failed: {e!r}")
        fb = fallback_result(query)
        fb.text = f"Live {kind} lookup failed ({type(e).__name__}) - placeholder answer for '{query}'."
        return fb

    return fallback_result(query)


# ---------------------------------------------------------------------------
# Rainfall readings (parametric insurance)
# ---------------------------------------------------------------------------

# Two independent Open-Meteo products for the same point + window. Both are
# free and keyless. `best_match` blends national/regional models; ECMWF IFS
# is a single global model, so they genuinely disagree from time to time.
# (`era5` from the archive API is another option, but it lags ~5 days and
# returns nulls for recent days, which is awkward for a live demo.)
RAIN_MODEL_A = os.environ.get("RAIN_MODEL_A", "best_match")
RAIN_MODEL_B = os.environ.get("RAIN_MODEL_B", "ecmwf_ifs025")
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"


async def _rainfall_total(client: httpx.AsyncClient, lat: float, lon: float, start: str, end: str, model: str) -> SourceReading:
    r = await client.get(
        OPEN_METEO_FORECAST,
        params={
            "latitude": lat,
            "longitude": lon,
            "daily": "precipitation_sum",
            "timezone": "UTC",
            "start_date": start,
            "end_date": end,
            "models": model,
        },
    )
    r.raise_for_status()
    daily = r.json()["daily"]
    days = daily["time"]
    values = daily["precipitation_sum"]
    # A null day means the model has no data for it. Treat it as missing,
    # NOT as 0 mm - a phantom zero would inflate the payout.
    present = [(d, v) for d, v in zip(days, values) if v is not None]
    if not present:
        raise ValueError(f"{model}: no precipitation data for {start}..{end}")
    total = round(sum(v for _, v in present), 2)
    missing = len(days) - len(present)
    detail = f"{len(present)} day(s) summed" + (f", {missing} missing" if missing else "")
    detail += "; daily mm: " + ", ".join(f"{v:g}" for _, v in present)
    return SourceReading(source=f"open-meteo:{model}", observed_mm=total, live=True, detail=detail)


async def fetch_rainfall_readings(lat: float, lon: float, window_start: str, window_end: str) -> list[SourceReading]:
    """Two independent rainfall totals for the same window. A source that
    fails is DROPPED (not reported as 0 mm) so a dead feed can never look
    like a drought. Raises only if every source fails."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=HEADERS, follow_redirects=True) as client:
        results = await asyncio.gather(
            _rainfall_total(client, lat, lon, window_start, window_end, RAIN_MODEL_A),
            _rainfall_total(client, lat, lon, window_start, window_end, RAIN_MODEL_B),
            return_exceptions=True,
        )
    readings: list[SourceReading] = []
    for model, res in zip((RAIN_MODEL_A, RAIN_MODEL_B), results):
        if isinstance(res, SourceReading):
            readings.append(res)
        else:
            print(f"[data_sources] rainfall source {model} failed: {res!r}")
    if not readings:
        raise RuntimeError("all rainfall sources failed")
    return readings


def simulate_reading(mm: float, label: str = "simulated", unit: str = "mm") -> SourceReading:
    """DEMO ONLY. Fabricate a reading so we can force a disagreement on
    stage instead of waiting for real weather to misbehave. Marked
    live=False so it's always visibly fake in the UI and the watchdog
    report. `unit` follows the policy's metric (mm for rain, min for the
    travel-delay demo product); the value is stored in observed_mm either way."""
    if mm < 0:
        raise ValueError("simulated reading cannot be negative")
    return SourceReading(source=f"simulated:{label}", observed_mm=float(mm), live=False, detail="injected for demo", unit=unit)


# ---------------------------------------------------------------------------
# Satellite crop-health evidence (Agromonitoring NDVI)
# ---------------------------------------------------------------------------
#
# HOW THE SATELLITE VOTE ENTERS THE FORMULA - design note
#
# Two options were on the table: (a) convert an NDVI decline into a fake
# "rainfall-mm equivalent" so it slots into trigger_mm/exit_mm, or (b) keep
# NDVI in its own units and let it vote in *payout-ratio space* with its own
# trigger/exit pair on the policy (ndvi_trigger / ndvi_exit).
#
# We chose (b). The formula is the same linear clamp either way, but (a)
# would require inventing an NDVI->mm regression we can't defend on stage,
# and it would hide the fact that the satellite measures the *outcome*
# (canopy health) while the weather models measure the *cause* (rain).
# Keeping the units honest is exactly what makes "weather says drought but
# the field is green" a meaningful thing for the watchdog to say. The cost
# is one extra call site: main.py turns each reading into a ratio with the
# mapping for its unit and uses formula.floor_ceiling_from_ratios().
#
# Agromonitoring is a two-step API: register a polygon once (id is cached
# on the policy), then query NDVI history / imagery for that polygon id and
# a time window. Image URLs returned by the API embed the API key, so they
# are NEVER sent to the browser - main.py proxies them.

AGROMONITORING_API_KEY = os.environ.get("AGROMONITORING_API_KEY", "")
AGROMONITORING_BASE = "https://api.agromonitoring.com/agro/1.0"
SATELLITE_LOOKBACK_DAYS = 20   # NDVI observations are sparse (clouds, ~5-day revisit); widen the search
SATELLITE_MAX_CLOUDS = 60.0    # ignore scenes cloudier than this (%)
SATELLITE_MIN_COVERAGE = 30.0  # ignore scenes covering less than this % of the field


@dataclass
class SatelliteResult:
    reading: SourceReading
    polygon_id: str
    images: dict[str, str]   # kind ("ndvi" | "truecolor") -> upstream URL (contains the API key!)


def default_field_polygon(lat: float, lon: float, size_m: float = 1000.0) -> dict:
    """A square field of ~size_m x size_m around the policy point (1 km ->
    100 ha, inside Agromonitoring's 1-3000 ha limit). GeoJSON is [lon, lat]."""
    import math
    dlat = (size_m / 2) / 111_320.0
    dlon = (size_m / 2) / (111_320.0 * max(0.01, math.cos(math.radians(lat))))
    ring = [
        [lon - dlon, lat - dlat], [lon + dlon, lat - dlat],
        [lon + dlon, lat + dlat], [lon - dlon, lat + dlat],
        [lon - dlon, lat - dlat],
    ]
    return {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [ring]}}


def _unix(day: str) -> int:
    from datetime import datetime, timezone
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())


async def register_polygon(client: httpx.AsyncClient, polygon_geojson: dict, name: str) -> str:
    """Step (a): create the polygon, return its id. `duplicated=true` so
    re-registering the same field on a fresh policy doesn't 4xx."""
    r = await client.post(
        f"{AGROMONITORING_BASE}/polygons",
        params={"appid": AGROMONITORING_API_KEY, "duplicated": "true"},
        json={"name": name, "geo_json": polygon_geojson},
    )
    r.raise_for_status()
    return str(r.json()["id"])


async def get_reading_source_satellite(
    polygon_geojson: dict,
    window_start: str,
    window_end: str,
    polygon_id: str | None = None,
    name: str = "agent-pay-field",
) -> SatelliteResult:
    """Step (b): NDVI for the field over the window (widened backwards by
    SATELLITE_LOOKBACK_DAYS because clear scenes are sparse). Returns the
    mean NDVI of the latest usable scene as a SourceReading in unit="ndvi",
    plus the matching NDVI / true-colour image URLs. Raises on any failure;
    main.py drops the source (never treats it as zero)."""
    if not AGROMONITORING_API_KEY:
        raise RuntimeError("AGROMONITORING_API_KEY not set")
    from datetime import date, timedelta

    start = _unix((date.fromisoformat(window_start) - timedelta(days=SATELLITE_LOOKBACK_DAYS)).isoformat())
    end = _unix(window_end) + 86_399
    async with httpx.AsyncClient(timeout=25.0, headers=HEADERS, follow_redirects=True) as client:
        if not polygon_id:
            polygon_id = await register_polygon(client, polygon_geojson, name)
        params = {"polyid": polygon_id, "start": start, "end": end, "appid": AGROMONITORING_API_KEY}

        # Scene selection goes through the imagery search, whose cloud figure
        # matches the picture we'll show (the ndvi/history endpoint reports
        # cloudiness differently and can pick a scene that is visibly overcast).
        # The number and the image therefore always come from the SAME scene.
        imgs = await client.get(f"{AGROMONITORING_BASE}/image/search", params=params)
        imgs.raise_for_status()
        scenes = [
            s for s in imgs.json()
            if float(s.get("cl", 100)) <= SATELLITE_MAX_CLOUDS and float(s.get("dc", 0)) >= SATELLITE_MIN_COVERAGE
            and (s.get("stats") or {}).get("ndvi")
        ]
        if not scenes:
            raise ValueError(f"no usable scene for polygon {polygon_id} in {window_start}..{window_end} (+{SATELLITE_LOOKBACK_DAYS}d lookback)")
        # Latest clear scene wins; a hazy recent scene loses to a clear older one.
        scenes.sort(key=lambda s: (0 if float(s["cl"]) <= 20 else 1, -int(s["dt"])))
        latest = scenes[0]
        st = await client.get(latest["stats"]["ndvi"])
        st.raise_for_status()
        stats = st.json()
        if stats.get("mean") is None:
            raise ValueError("scene has no NDVI statistics")
        ndvi = round(float(stats["mean"]), 3)

        images = {k: v for k, v in (latest.get("image") or {}).items() if k in ("ndvi", "truecolor") and v}
        # The clipped polygon image is tiny for a small field (~10 m/px), so
        # also keep the {z}/{x}/{y} tile templates: a 256 px tile centred on
        # the field is far better on stage (main.py crops it to the field).
        for k, v in (latest.get("tile") or {}).items():
            if k in ("ndvi", "truecolor") and v:
                images[f"{k}_tile"] = v

        # Trend across the usable scenes in the lookback (best effort).
        trend = ""
        try:
            hist = await client.get(f"{AGROMONITORING_BASE}/ndvi/history", params=params)
            hist.raise_for_status()
            # Overcast observations report near-zero NDVI (cloud, not crop);
            # keep the same cloud/coverage filter as the scene selection.
            means = [
                (int(h["dt"]), float(h["data"]["mean"])) for h in hist.json()
                if h.get("data") and h["data"].get("mean") is not None
                and float(h.get("cl", 100)) <= SATELLITE_MAX_CLOUDS and float(h.get("dc", 0)) >= SATELLITE_MIN_COVERAGE
            ]
            means.sort()
            if len(means) > 1:
                trend = f"; history {means[0][1]:.2f} -> {means[-1][1]:.2f} over {len(means)} observations"
        except Exception as e:  # noqa: BLE001 - trend is decoration
            print(f"[data_sources] ndvi history failed: {e!r}")

    from datetime import datetime, timezone
    captured = datetime.fromtimestamp(int(latest["dt"]), tz=timezone.utc).date().isoformat()
    detail = (
        f"mean NDVI of the latest clear scene ({latest.get('type', 'satellite')}, {captured}, "
        f"clouds {float(latest.get('cl', 0)):.0f}%, coverage {float(latest.get('dc', 100)):.0f}%; "
        f"field min {float(stats.get('min', 0)):.2f} / max {float(stats.get('max', 0)):.2f}){trend}"
    )
    reading = SourceReading(
        source="agromonitoring:ndvi", observed_mm=max(0.0, ndvi), live=True, detail=detail,
        unit="ndvi", captured_at=captured,
    )
    return SatelliteResult(reading=reading, polygon_id=polygon_id, images=images)


def simulate_satellite_reading(ndvi_value: float, label: str = "satellite") -> SourceReading:
    """DEMO ONLY. Mirror of simulate_reading() for the satellite vote, so we
    can force "weather says fine, field looks dead" (or the reverse) on
    stage. NDVI is clamped to 0..1; live=False so it's visibly fake."""
    if not -1.0 <= ndvi_value <= 1.0:
        raise ValueError("NDVI must be between -1 and 1")
    return SourceReading(
        source=f"simulated:{label}", observed_mm=max(0.0, float(ndvi_value)), live=False,
        detail="injected for demo", unit="ndvi",
    )
