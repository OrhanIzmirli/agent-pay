"""
Product catalog: one settlement engine, pluggable across verticals.

Every product is the SAME formula (formula.payout_ratio), the same floor /
ceiling split and the same escrow flow. A product only fixes three things:
the metric (unit + label), the default trigger/exit pair, and where the
readings come from. `icon_hint` (a lucide icon name), `period`, `subject`,
`place_prompt`, `location_noun`, `payee_label` and `theme` (the colour theme,
shared with the Landing page) are display copy the Playground reads verbatim,
so a new vertical needs only a catalog entry. `trigger_mm` / `exit_mm` on Policy keep their names for
API compatibility, but they are read in `metric_unit` (mm for rain, min for
delay).

TRAVEL_DELAY is an honest DEMO: no live flight/train delay feed is integrated,
so its readings are two independently-labelled simulated delay feeds
(data_sources.simulate_reading), run through the exact same dispute/escrow
mechanics as the live weather products. The API response says so.
"""

from app.models import ProductType

PRODUCTS: dict[str, dict] = {
    ProductType.CROP_DROUGHT.value: {
        "theme": "farm",
        "icon_hint": "sun", "period": "this week", "subject": "a wheat field", "place_prompt": "Where is the field?",
        "location_noun": "field", "payee_label": "Farmer's wallet",
        "label": "Crop drought cover",
        "tagline": "Wheat, barley and other crops that suffer when the week is too dry.",
        "metric_unit": "mm", "metric_label": "Rainfall",
        "direction": "less", "trigger": 40.0, "exit": 10.0,
        "sources": "live", "source_note": "Two live weather models (Open-Meteo best match, ECMWF IFS), optional satellite NDVI.",
    },
    ProductType.CROP_EXCESS_RAIN.value: {
        "theme": "farm",
        "icon_hint": "cloud-rain", "period": "this week", "subject": "a corn field", "place_prompt": "Where is the field?",
        "location_noun": "field", "payee_label": "Farmer's wallet",
        "label": "Crop excess-rain cover",
        "tagline": "Corn and other crops that drown when the week is too wet.",
        "metric_unit": "mm", "metric_label": "Rainfall",
        "direction": "more", "trigger": 80.0, "exit": 140.0,
        "sources": "live", "source_note": "Two live weather models (Open-Meteo best match, ECMWF IFS), optional satellite NDVI.",
    },
    ProductType.EVENT_WEATHER_CANCEL.value: {
        "theme": "event",
        "icon_hint": "umbrella", "venue_lookup": True, "period": "over the event", "subject": "an open-air event", "place_prompt": "Where is the event?",
        "location_noun": "event", "payee_label": "Organiser's wallet",
        "label": "Concert / festival rain-out cover",
        "tagline": "Any real rain over the event window voids the event; the cover pays the organiser.",
        "metric_unit": "mm", "metric_label": "Rainfall",
        "direction": "more", "trigger": 5.0, "exit": 25.0,
        "sources": "live", "source_note": "Same two live weather models as the crop products, read over the event window.",
    },
    ProductType.TRAVEL_DELAY.value: {
        "theme": "flight",
        "icon_hint": "plane", "period": "on the day", "subject": "a journey", "place_prompt": "Which route?",
        "location_noun": "journey", "payee_label": "Traveller's wallet",
        "label": "Flight / train delay cover",
        "tagline": "Pays the traveller when the delay passes 30 minutes, in full at 3 hours.",
        "metric_unit": "min", "metric_label": "Delay",
        "direction": "more", "trigger": 30.0, "exit": 180.0,
        "sources": "demo", "source_note": "DEMO data source: two simulated delay feeds. No live flight/train API is integrated; the dispute/escrow mechanics are the same as the live weather products.",
    },
}


def product(product_type: str) -> dict:
    try:
        return PRODUCTS[product_type]
    except KeyError:
        raise ValueError(f"unknown product_type {product_type!r}; valid: {', '.join(PRODUCTS)}")


def cover_for(product_type: str) -> str:
    """Map a product's direction onto the existing cover flag."""
    return "drought" if product(product_type)["direction"] == "less" else "excess_rain"


def catalog() -> list[dict]:
    return [{"product_type": k, **v} for k, v in PRODUCTS.items()]
