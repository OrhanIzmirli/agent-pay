"""
"Show your work": a proof object attached to every evaluation so the payout
is never a black box. Pure functions; the amount is computed elsewhere, this
only restates it with the real numbers substituted and hashes the inputs.

The hash is sha256 over a canonical JSON dump of the readings (sorted keys,
no whitespace). It is returned in the API and printed to the console for
tamper-evidence; putting it on-chain is future work.
"""

import hashlib
import json
from datetime import datetime, timezone

from app.formula import DEFAULT_TOLERANCE
from app.models import DisputeReport, Policy, SourceReading

FORMULA = "payout_ratio = clamp((trigger - observed) / (trigger - exit), 0, 1)"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _unit(policy: Policy, r: SourceReading) -> str:
    return r.unit if r.unit in ("ndvi", "status") else policy.metric_unit


def _thresholds(policy: Policy, r: SourceReading) -> tuple[float, float]:
    return (policy.ndvi_trigger, policy.ndvi_exit) if r.unit == "ndvi" else (policy.trigger_mm, policy.exit_mm)


def _fmt(v: float) -> str:
    return f"{v:g}"


def _value(r: SourceReading):
    return r.status if r.unit == "status" else r.observed_mm


def calc_line(policy: Policy, r: SourceReading, ratio: float) -> str:
    if r.unit == "status":
        return f"event status '{r.status}' -> implied ratio {ratio:.4f} (independent, non-weather evidence)"
    t, x = _thresholds(policy, r)
    return f"clamp(({_fmt(t)} - {_fmt(r.observed_mm)}) / ({_fmt(t)} - {_fmt(x)}), 0, 1) = {ratio:.4f}"


def inputs_hash(policy: Policy, readings: list[SourceReading]) -> str:
    canonical = [
        {"source": r.source, "value": _value(r), "unit": _unit(policy, r), "live": r.live, "fetched_at": _iso(r.fetched_at)}
        for r in readings
    ]
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def build_proof(policy: Policy, readings: list[SourceReading], floor_ratio: float, ceiling_ratio: float, dispute: DisputeReport | dict | None) -> dict:
    ratios = [(r, r.payout_ratio if r.payout_ratio is not None else 0.0) for r in readings]
    worst = min(ratios, key=lambda p: p[1])   # least loss -> floor
    best = max(ratios, key=lambda p: p[1])    # most loss -> ceiling
    return {
        "readings": [
            {"source": r.source, "value": _value(r), "unit": _unit(policy, r), "live": r.live,
             "fetched_at": _iso(r.fetched_at), "payout_ratio": round(ratio, 6), "calc": calc_line(policy, r, ratio)}
            for r, ratio in ratios
        ],
        "trigger": policy.trigger_mm,
        "exit": policy.exit_mm,
        "metric_unit": policy.metric_unit,
        "metric_label": policy.metric_label,
        "product_type": policy.product_type,
        "formula": FORMULA,
        "floor_calc": f"floor  = {calc_line(policy, worst[0], floor_ratio)}  [{worst[0].source}]",
        "ceiling_calc": f"ceiling = {calc_line(policy, best[0], ceiling_ratio)}  [{best[0].source}]",
        "tolerance": DEFAULT_TOLERANCE,
        "inputs_hash": inputs_hash(policy, readings),
        "ai_involvement": "explanation-only \u2014 did not set the amount" if dispute else "none",
        "data_source_note": "demo data source, same dispute/escrow mechanics as the live weather products" if policy.product_type == "travel_delay" else None,
    }
