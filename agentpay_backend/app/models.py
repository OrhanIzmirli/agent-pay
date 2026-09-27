"""
Domain models for the parametric weather insurance pivot.

Plain dataclasses on purpose: the API layer (main.py) wraps them in pydantic
for validation/serialisation, and the pure formula module (formula.py) only
needs the numbers. Nothing here does I/O.
"""

import time
from dataclasses import dataclass, field
from enum import Enum


class ProductType(str, Enum):
    """Which vertical a policy belongs to. The settlement engine (formula.py,
    floor/ceiling, escrow) is identical for all of them; only the metric,
    thresholds and copy differ. See products.py for the catalog."""
    CROP_DROUGHT = "crop_drought"              # rainfall mm, less rain = more payout
    CROP_EXCESS_RAIN = "crop_excess_rain"      # rainfall mm, more rain = more payout
    EVENT_WEATHER_CANCEL = "event_weather_cancel"  # rainfall mm over the event window, any real rain voids the event
    TRAVEL_DELAY = "travel_delay"              # delay minutes; DEMO data source (no live delay feed integrated)


class DisputeStatus(str, Enum):
    NONE = "none"                    # sources agreed - full formula payout, no AI involved
    INVESTIGATING = "investigating"  # sources disagreed; watchdog says safe to auto-resolve next cycle
    ESCALATED = "escalated"          # sources disagreed; needs a human on POST /policy/{id}/resolve
    RESOLVED = "resolved"            # escrowed delta released or voided (by a human or the next cycle)


@dataclass
class Policy:
    id: str
    region: str
    lat: float
    lon: float
    trigger_mm: float        # rainfall at/above this -> no payout
    exit_mm: float           # rainfall at/below this -> full payout
    sum_insured_sol: float
    payee_pubkey: str
    window_start: str        # ISO date (inclusive)
    window_end: str          # ISO date (inclusive)
    created_at: float = field(default_factory=time.time)
    settled: bool = False    # True once nothing is owed or pending any more
    # --- satellite / cover additions (all defaulted so v1 records still load) ---
    cover: str = "drought"   # "drought" (trigger > exit) | "excess_rain" (exit > trigger)
    ndvi_trigger: float = 0.55   # satellite vote: NDVI at/above this -> no loss (healthy canopy)
    ndvi_exit: float = 0.25      # NDVI at/below this -> total loss (bare / dead vegetation)
    field_polygon: dict | None = None        # GeoJSON Feature; default = ~25 ha square around lat/lon
    satellite_polygon_id: str | None = None  # Agromonitoring polygon id, registered once
    # --- product generalisation (defaults keep every existing crop policy identical) ---
    product_type: str = "crop_drought"       # ProductType value
    metric_unit: str = "mm"                  # unit of trigger_mm / exit_mm / weather readings ("mm", "min", ...)
    metric_label: str = "Rainfall"           # human name of the metric ("Rainfall", "Delay")
    # --- optional cross-domain evidence for event products (Ticketmaster event status) ---
    venue_name: str | None = None
    ticketmaster_event_id: str | None = None
    # A floor payment that was submitted but whose confirmation could not be established. While set, evaluate
    # re-checks THIS transaction and never sends another (see payment.PaymentPending).
    pending_payment: dict | None = None


@dataclass
class SourceReading:
    source: str          # label, e.g. "open-meteo:best_match", "agromonitoring:ndvi", "simulated:A"
    observed_mm: float   # observed value in `unit`. For unit="ndvi" this holds the NDVI
                         # (field name kept for API compatibility with the v1 frontend)
    live: bool           # True if this came from a real external API call
    detail: str = ""     # human-readable note (daily breakdown, error, ...)
    fetched_at: float = field(default_factory=time.time)
    unit: str = "mm"                 # "mm" | "ndvi" | "status"
    payout_ratio: float | None = None  # filled in by main.py from the policy's mapping for this unit
    image_url: str | None = None     # backend-relative proxy URL of the satellite image, if any
    captured_at: str | None = None   # ISO date of the satellite acquisition, if any
    status: str | None = None        # unit="status" only: the raw event status code, e.g. "canceled"


@dataclass
class DisputeReport:
    """What the watchdog (decision.py) produced. Deliberately contains no
    amounts - the AI can only describe and recommend, never pay."""
    summary: str              # plain-language explanation for the UI / a human reviewer
    suspected_cause: str      # e.g. "model spread", "sensor dropout", "possible manipulation"
    evidence: list[str]       # bullet points the reviewer can check
    recommendation: str       # "auto_resolve" | "escalate"
    ai_used: bool             # False when the heuristic fallback ran
    model: str                # model id, or "heuristic"


@dataclass
class EvaluationResult:
    policy_id: str
    readings: list[SourceReading]
    payout_ratio_floor: float     # worst case for the policyholder (wettest reading)
    payout_ratio_ceiling: float   # best case for the policyholder (driest reading)
    floor_amount_sol: float       # paid immediately, on-chain
    ceiling_amount_sol: float
    escrow_amount_sol: float      # ceiling - floor, held off-chain until resolved
    floor_tx_signature: str | None
    dispute_status: DisputeStatus
    dispute: DisputeReport | None
    escrow: dict | None           # ledger entry from escrow.py, if one was opened
    elapsed_ms: float
