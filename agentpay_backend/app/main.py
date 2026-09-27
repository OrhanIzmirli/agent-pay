"""
Agent Pay backend - parametric weather insurance on Solana devnet.

Flow for POST /policy/{id}/evaluate:
  1. Pull TWO independent rainfall readings for the policy's lat/lon and
     window (data_sources.fetch_rainfall_readings), or take simulated ones
     from the request body for the stage demo. Optionally add a THIRD vote:
     a satellite NDVI reading of the field (Agromonitoring) in its own
     units with its own trigger/exit on the policy.
  2. formula.py turns each reading into a payout ratio. The least-loss
     reading is the FLOOR (worst case for the policyholder), the most-loss
     reading the CEILING. (Drought cover: wettest/driest. Excess-rain cover:
     the other way round - same formula, exit > trigger.)
  3. Pay the floor amount NOW, on-chain, via payment.send_payment(). This
     happens whether or not the sources agree - a poisoned feed can only
     ever freeze the disputed slice, never the whole payout.
  4. If the sources disagree beyond tolerance: run the watchdog
     (decision.investigate) for a plain-language explanation and a
     recommendation, then hold ceiling-minus-floor in the escrow ledger.
     The watchdog never touches money.
  5. The escrowed delta is released (second real transaction) or voided by
     a human on POST /policy/{id}/resolve, or by the next evaluate cycle if
     the watchdog said auto-resolve and the fresh readings agree.

API contract is what the frontend builds against (Swagger at /docs).
"""

import os
import time
from dataclasses import asdict
from datetime import date, timedelta
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, field_validator

from app.data_sources import (
    AGROMONITORING_API_KEY,
    default_field_polygon,
    fetch_rainfall_readings,
    get_reading_source_satellite,
    simulate_reading,
    simulate_satellite_reading,
)
from app.decision import active_provider, investigate
from app.formula import DEFAULT_TOLERANCE, escrow_delta, floor_ceiling_from_ratios, payout_amount, payout_ratio, sources_disagree
from app.models import DisputeStatus, EvaluationResult, Policy, ProductType, SourceReading
from app.satellite_image import cache_images, crop_to_field, fetch_upstream, get_cached, render_simulated_ndvi, tile_url
from app.payment import (
    AGENT_PUBLIC_KEY,
    SERVICE_PUBLIC_KEY,
    PaymentPending,
    check_signature,
    ensure_funded,
    ensure_rent_exempt,
    get_escrow,
    get_payment_history,
    get_wallet_balance,
    list_escrows,
    open_escrow,
    release_escrow,
    send_payment,
    void_escrow,
)
from app.policy_store import get_policy, get_record, list_records, new_policy_id, save_policy
from app.ticketmaster import fetch_event_status, status_reading
from app.products import catalog, cover_for, product
from app.proof import build_proof

app = FastAPI(title="Agent Pay - parametric weather insurance")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Default payee (the "policyholder" wallet for the demo). Defaults to the
# service wallet generated in payment.py; override with SERVICE_WALLET_ADDRESS.
SERVICE_WALLET_ADDRESS = os.environ.get("SERVICE_WALLET_ADDRESS") or SERVICE_PUBLIC_KEY

# Hard cap on a single policy so a typo can't drain the demo wallet.
MAX_SUM_INSURED_SOL = float(os.environ.get("MAX_SUM_INSURED_SOL", "0.05"))

SOLANA_CLUSTER = "devnet"


def explorer_url(signature: str | None) -> str | None:
    if not signature:
        return None
    return f"https://explorer.solana.com/tx/{signature}?cluster={SOLANA_CLUSTER}"


@app.on_event("startup")
async def startup():
    # Best-effort devnet airdrop so the demo wallet has funds. Devnet
    # faucet is rate-limited - if this fails, fund the wallet manually via
    # https://faucet.solana.com using the address printed below.
    print(f"Agent wallet (pays):        {AGENT_PUBLIC_KEY}")
    print(f"Default payee (is paid):    {SERVICE_WALLET_ADDRESS}")
    print(f"Watchdog explanations:      {active_provider()}")
    try:
        await ensure_funded()
    except Exception as e:
        print(f"Airdrop failed (fund manually if needed): {e}")
    # A fresh payee wallet can't receive a sub-rent micropayment (Solana
    # rejects it). One-off top-up so the first demo payment doesn't fail.
    try:
        await ensure_rent_exempt(SERVICE_WALLET_ADDRESS)
    except Exception as e:
        print(f"Payee wallet rent top-up failed (first payment may be rejected): {e}")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class PolicyCreate(BaseModel):
    region: str = Field(..., min_length=1, examples=["Warsaw, PL"])
    lat: float = Field(..., ge=-90, le=90, examples=[52.23])
    lon: float = Field(..., ge=-180, le=180, examples=[21.01])
    # Thresholds are read in the product's metric_unit (mm for rain, min for delay). Optional: when
    # omitted they come from the product catalog.
    trigger_mm: float | None = Field(None, ge=0, description="metric value where the payout starts (0%)", examples=[40])
    exit_mm: float | None = Field(None, ge=0, description="metric value where the payout is full (100%)", examples=[10])
    # Product (vertical). Default keeps the original crop-drought behaviour. Setting a product fixes
    # the metric and, unless overridden, the cover direction and thresholds.
    product_type: ProductType | None = Field(None, description="crop_drought | crop_excess_rain | event_weather_cancel | travel_delay")
    metric_unit: str | None = Field(None, description="defaults from the product, e.g. mm or min")
    metric_label: str | None = Field(None, description="defaults from the product, e.g. Rainfall or Delay")
    venue_name: str | None = Field(None, max_length=200, description="event products: optional, looked up on Ticketmaster for an independent event-status reading")
    ticketmaster_event_id: str | None = Field(None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$", description="event products: optional Ticketmaster event id (takes precedence over venue_name)")
    sum_insured_sol: float = Field(..., gt=0, examples=[0.01])
    payee_pubkey: str | None = Field(None, description="defaults to the service wallet")
    window_start: str | None = Field(None, description="ISO date, inclusive; default = 7 days ago")
    window_end: str | None = Field(None, description="ISO date, inclusive; default = yesterday")
    # Cover direction. DECISION (flagged): excess-rain cover reuses the SAME
    # formula with exit_mm > trigger_mm - the sign flip inverts the direction
    # (see formula.payout_ratio). No separate flood formula was needed; only
    # the validation below differs.
    cover: Literal["drought", "excess_rain"] | None = Field(None, description="drought: trigger > exit; excess_rain: exit > trigger. Defaults from the product (drought when none)")
    # Satellite vote thresholds in NDVI units (third independent vote; see data_sources.py design note).
    ndvi_trigger: float = Field(0.55, gt=-1, lt=1, description="NDVI at/above this = healthy, no loss")
    ndvi_exit: float = Field(0.25, gt=-1, lt=1, description="NDVI at/below this = total loss")
    field_polygon: dict | None = Field(None, description="GeoJSON Feature (Polygon, 1-3000 ha); default = ~25 ha square around lat/lon")

    @field_validator("window_start", "window_end")
    @classmethod
    def _iso_date(cls, v: str | None) -> str | None:
        if v is not None:
            date.fromisoformat(v)
        return v


class SimulatedReading(BaseModel):
    mm: float = Field(..., ge=0)
    label: str | None = None  # defaults to A, B, C...


class EvaluateRequest(BaseModel):
    # DEMO ONLY: when present these replace the live Open-Meteo pulls so a
    # disagreement can be forced on stage.
    simulate: list[SimulatedReading] | None = None
    # Satellite crop-health vote (Agromonitoring NDVI). Off by default; the
    # live pull needs AGROMONITORING_API_KEY. `simulate_satellite_ndvi` is
    # the demo override, same pattern as `simulate` above.
    include_satellite: bool = False
    simulate_satellite_ndvi: float | None = Field(None, ge=-1, le=1)
    # DEMO ONLY, event products: stand-in for the Ticketmaster status code (e.g. "canceled", "onsale").
    simulate_event_status: str | None = Field(None, max_length=20)


class BalanceResponse(BaseModel):
    address: str
    balance_sol: float


class HistoryTransaction(BaseModel):
    # Mirrors the dicts get_payment_history() builds from
    # get_signatures_for_address: signature, slot and err (None on success).
    signature: str
    slot: int
    err: Any | None = None


class HistoryResponse(BaseModel):
    address: str
    transactions: list[HistoryTransaction]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _looks_like_pubkey(s: str) -> bool:
    return 32 <= len(s) <= 44 and all(c.isalnum() for c in s)


def _with_links(escrow: dict | None) -> dict | None:
    if not escrow:
        return None
    out = dict(escrow)
    out["release_explorer_url"] = explorer_url(escrow.get("release_tx_signature"))
    return out


def _evaluation_to_dict(ev: EvaluationResult, **extra: Any) -> dict:
    d = asdict(ev)
    d["dispute_status"] = ev.dispute_status.value
    d["floor_explorer_url"] = explorer_url(ev.floor_tx_signature)
    d["escrow"] = _with_links(ev.escrow)
    d["escrow_pda"] = (ev.escrow or {}).get("escrow_pda")
    d["escrow_explorer_link"] = (ev.escrow or {}).get("escrow_explorer_link")
    d["spread"] = round(ev.payout_ratio_ceiling - ev.payout_ratio_floor, 6)
    d["tolerance"] = DEFAULT_TOLERANCE
    d.update(extra)
    return d


def _policy_response(policy_id: str) -> dict:
    record = get_record(policy_id)
    if not record:
        raise HTTPException(404, f"policy {policy_id} not found")
    record = dict(record)
    record["escrow"] = _with_links(get_escrow(policy_id))
    return record


def _reading_ratio(policy: Policy, r: SourceReading) -> float:
    """Each unit has its own trigger/exit pair on the policy; the formula is
    the same. This is the one place the satellite vote meets the formula."""
    if r.unit == "ndvi":
        return payout_ratio(r.observed_mm, policy.ndvi_trigger, policy.ndvi_exit)
    if r.unit == "status":  # event-status evidence carries its implied ratio directly
        return r.payout_ratio or 0.0
    return payout_ratio(r.observed_mm, policy.trigger_mm, policy.exit_mm)


async def _collect_readings(policy: Policy, req: EvaluateRequest | None, warnings: list[str]) -> list[SourceReading]:
    # Primary votes: two weather models, or the simulated override (values are in policy.metric_unit).
    if req and req.simulate:
        if len(req.simulate) < 1:
            raise HTTPException(422, "simulate needs at least one reading")
        readings = [simulate_reading(s.mm, s.label or f"{chr(65 + i)}", unit=policy.metric_unit) for i, s in enumerate(req.simulate)]
    elif policy.product_type == ProductType.TRAVEL_DELAY.value:
        # DEMO data source. No live flight/train delay API is integrated, so the travel product
        # runs on two independently-labelled simulated delay feeds (the same pattern the
        # force-disagreement demo uses), through the same dispute/escrow mechanics.
        readings = [simulate_reading(35.0, "delay-feed-A", unit="min"), simulate_reading(40.0, "delay-feed-B", unit="min")]
        warnings.append("Travel delay is a demo data source (two simulated delay feeds); same dispute/escrow mechanics as the live weather products.")
    else:
        readings = await fetch_rainfall_readings(policy.lat, policy.lon, policy.window_start, policy.window_end)

    # Optional satellite vote.
    if req and req.simulate_satellite_ndvi is not None:
        sat = simulate_satellite_reading(req.simulate_satellite_ndvi)
        sat.image_url = f"/policy/{policy.id}/satellite/image?kind=ndvi"
        readings.append(sat)
    elif req and req.include_satellite and not policy.product_type.startswith("crop_"):
        warnings.append("Satellite vote only applies to crop products; skipped.")
    elif req and req.include_satellite:
        if not AGROMONITORING_API_KEY:
            warnings.append("Satellite vote skipped: AGROMONITORING_API_KEY is not set.")
        else:
            try:
                res = await get_reading_source_satellite(
                    policy.field_polygon or default_field_polygon(policy.lat, policy.lon),
                    policy.window_start, policy.window_end,
                    polygon_id=policy.satellite_polygon_id, name=f"agent-pay {policy.id}",
                )
                policy.satellite_polygon_id = res.polygon_id  # registered once, reused next cycle
                cache_images(policy.id, res.images, res.reading.captured_at)
                if res.images.get("ndvi_tile"):
                    res.reading.image_url = f"/policy/{policy.id}/satellite/image?kind=ndvi_tile"
                elif res.images.get("ndvi"):
                    res.reading.image_url = f"/policy/{policy.id}/satellite/image?kind=ndvi"
                readings.append(res.reading)
            except Exception as e:  # noqa: BLE001 - dropped, never read as a zero
                print(f"[main] satellite source failed for {policy.id}: {e!r}")
                warnings.append(f"Satellite vote unavailable ({type(e).__name__}); evaluated on weather sources only.")

    # Optional cross-domain vote (event products only): the ticketed event's own status. One more
    # entry in the same list; absent, failed or unmatched -> skipped, the weather readings stand alone.
    if policy.product_type == ProductType.EVENT_WEATHER_CANCEL.value:
        if req and req.simulate_event_status:
            sim = status_reading(req.simulate_event_status, "injected for demo", live=False)
            if sim:
                sim.source = "ticketmaster_event_status:simulated"
                readings.append(sim)
            else:
                warnings.append(f"Ignored unknown simulated event status {req.simulate_event_status!r}.")
        else:
            tm, note = await fetch_event_status(policy)
            if tm:
                readings.append(tm)
            if note:
                warnings.append(note)

    for r in readings:
        r.payout_ratio = round(_reading_ratio(policy, r), 6)
    return readings


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    return {"status": "ok", "service": "Agent Pay backend", "mode": "parametric-insurance"}


@app.get("/wallet/balance", response_model=BalanceResponse)
async def wallet_balance():
    # The insurer's own wallet - the balance that visibly goes down as
    # policies pay out during the demo.
    bal = await get_wallet_balance(AGENT_PUBLIC_KEY)
    return {"address": AGENT_PUBLIC_KEY, "balance_sol": bal}


@app.get("/wallet/history", response_model=HistoryResponse)
async def wallet_history():
    txs = await get_payment_history(AGENT_PUBLIC_KEY)
    return {"address": AGENT_PUBLIC_KEY, "transactions": txs}


# Old paths kept as aliases so nothing already deployed breaks.
app.get("/agent/balance", response_model=BalanceResponse, include_in_schema=False)(wallet_balance)
app.get("/agent/history", response_model=HistoryResponse, include_in_schema=False)(wallet_history)


@app.get("/escrow")
def escrow_ledger():
    return {"escrows": [_with_links(e) for e in list_escrows()]}


@app.post("/policy", status_code=201)
def create_policy(req: PolicyCreate):
    # Resolve the product layer: everything below this block is the original engine.
    ptype = (req.product_type or ProductType.CROP_DROUGHT).value
    prod = product(ptype)
    cover = req.cover or cover_for(ptype)
    trigger = req.trigger_mm if req.trigger_mm is not None else prod["trigger"]
    exit_ = req.exit_mm if req.exit_mm is not None else prod["exit"]
    metric_unit = req.metric_unit or prod["metric_unit"]
    metric_label = req.metric_label or prod["metric_label"]
    if trigger <= 0 and cover == "drought":
        raise HTTPException(422, "trigger must be positive")
    req = req.model_copy(update={"cover": cover, "trigger_mm": trigger, "exit_mm": exit_})
    if req.cover == "drought" and req.exit_mm >= req.trigger_mm:
        raise HTTPException(422, "drought cover: exit_mm must be lower than trigger_mm")
    if req.cover == "excess_rain" and req.exit_mm <= req.trigger_mm:
        raise HTTPException(422, "excess_rain cover: exit_mm must be higher than trigger_mm")
    if req.ndvi_exit >= req.ndvi_trigger:
        raise HTTPException(422, "ndvi_exit must be lower than ndvi_trigger")
    if req.field_polygon is not None:
        geom = req.field_polygon.get("geometry") if req.field_polygon.get("type") == "Feature" else req.field_polygon
        if not geom or geom.get("type") != "Polygon" or not geom.get("coordinates"):
            raise HTTPException(422, "field_polygon must be a GeoJSON Feature or Polygon geometry")
    if req.sum_insured_sol > MAX_SUM_INSURED_SOL:
        raise HTTPException(422, f"sum_insured_sol capped at {MAX_SUM_INSURED_SOL} SOL for the demo")
    payee = (req.payee_pubkey or "").strip() or SERVICE_WALLET_ADDRESS
    if not _looks_like_pubkey(payee):
        raise HTTPException(422, "payee_pubkey does not look like a Solana address")
    if ptype == ProductType.EVENT_WEATHER_CANCEL.value:
        # Events are dated in the future (a concert to insure, not one that already happened), unlike the
        # crop/travel products which settle a week that has already elapsed. Default to the coming week so an
        # un-dated event policy still lands somewhere Ticketmaster could plausibly have a matching event.
        start = req.window_start or date.today().isoformat()
        end = req.window_end or (date.fromisoformat(start) + timedelta(days=6)).isoformat()
    else:
        end = req.window_end or (date.today() - timedelta(days=1)).isoformat()
        start = req.window_start or (date.fromisoformat(end) - timedelta(days=6)).isoformat()
    if start > end:
        raise HTTPException(422, "window_start must not be after window_end")

    policy = Policy(
        id=new_policy_id(),
        region=req.region.strip(),
        lat=req.lat,
        lon=req.lon,
        trigger_mm=req.trigger_mm,
        exit_mm=req.exit_mm,
        sum_insured_sol=req.sum_insured_sol,
        payee_pubkey=payee,
        window_start=start,
        window_end=end,
        cover=req.cover,
        ndvi_trigger=req.ndvi_trigger,
        ndvi_exit=req.ndvi_exit,
        field_polygon=req.field_polygon,
        product_type=ptype,
        metric_unit=metric_unit,
        metric_label=metric_label,
        venue_name=(req.venue_name or "").strip() or None if ptype == ProductType.EVENT_WEATHER_CANCEL.value else None,
        ticketmaster_event_id=req.ticketmaster_event_id if ptype == ProductType.EVENT_WEATHER_CANCEL.value else None,
    )
    save_policy(policy)
    return _policy_response(policy.id)


@app.get("/products")
def list_products():
    """One settlement engine, pluggable across verticals: the catalog the
    Playground presets are built from."""
    return {"formula": "payout_ratio = clamp((trigger - observed) / (trigger - exit), 0, 1)", "products": catalog()}


@app.get("/policy/{policy_id}/satellite/image", responses={200: {"content": {"image/png": {}}}, 404: {"description": "No satellite evidence for this policy"}})
async def satellite_image(policy_id: str, kind: Literal["ndvi", "truecolor", "ndvi_tile", "truecolor_tile"] = "ndvi"):
    """Proxy for the field's satellite image. Real scenes are fetched from
    Agromonitoring with the server-side key; a simulated satellite reading
    gets a synthetic, clearly stamped NDVI raster so the demo has a visual.
    `*_tile` kinds serve the 256 px map tile containing the field (better
    on stage than the polygon-clipped image, which is ~10 m/px)."""
    record = get_record(policy_id)
    if not record:
        raise HTTPException(404, f"policy {policy_id} not found")
    cached = get_cached(policy_id)
    upstream = (cached or {}).get("images", {}).get(kind)
    if upstream and kind.endswith("_tile"):
        upstream = tile_url(upstream, float(record["lat"]), float(record["lon"]))
    if upstream:
        try:
            png = await fetch_upstream(upstream)
            if kind.endswith("_tile"):
                png = crop_to_field(png)
            return Response(content=png, media_type="image/png",
                            headers={"Cache-Control": "private, max-age=3600"})
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"could not fetch satellite image: {e}")
    last = record.get("last_evaluation") or {}
    sim = next((r for r in last.get("readings", []) if r.get("unit") == "ndvi" and not r.get("live")), None)
    if sim and kind == "ndvi":
        return Response(content=render_simulated_ndvi(float(sim["observed_mm"])), media_type="image/png",
                        headers={"Cache-Control": "no-store"})
    raise HTTPException(404, "no satellite evidence for this policy")


@app.get("/policy")
def list_policies():
    return {"policies": [dict(r, escrow=_with_links(get_escrow(r["id"]))) for r in list_records()]}


@app.get("/policy/{policy_id}")
def get_policy_route(policy_id: str):
    return _policy_response(policy_id)


async def _finish_fresh(policy: Policy, readings: list[SourceReading], note: str | None, floor_tx: str | None,
                        floor_ratio: float, ceiling_ratio: float, floor_amount: float, ceiling_amount: float,
                        delta: float, disagree: bool, start: float) -> dict:
    """Everything after the floor payment of a fresh cycle: watchdog + escrow the delta, proof, persistence.
    Split out so an evaluation whose floor payment was confirmed LATER can be completed from stored numbers."""
    policy_id = policy.id
    policy.pending_payment = None
    # 4. disagreement? watchdog + escrow the delta ------------------------
    dispute = None
    escrow = None
    if disagree and delta > 0:
        report = await investigate(policy, readings, floor_ratio, ceiling_ratio)
        dispute = asdict(report)
        status = DisputeStatus.ESCALATED if report.recommendation == "escalate" else DisputeStatus.INVESTIGATING
        escrow = open_escrow(
            policy_id,
            policy.payee_pubkey,
            delta,
            reason=f"{report.suspected_cause}: floor {floor_ratio:.3f} vs ceiling {ceiling_ratio:.3f} (spread {ceiling_ratio - floor_ratio:.3f})",
        )
    else:
        status = DisputeStatus.NONE
        policy.settled = True
        if len(readings) == 1:
            note = (note + " " if note else "") + "Only one source responded; paid per formula without cross-checking."

    result = EvaluationResult(
        policy_id=policy_id,
        readings=readings,
        payout_ratio_floor=floor_ratio,
        payout_ratio_ceiling=ceiling_ratio,
        floor_amount_sol=floor_amount,
        ceiling_amount_sol=ceiling_amount,
        escrow_amount_sol=delta if escrow else 0.0,
        floor_tx_signature=floor_tx,
        dispute_status=status,
        dispute=dispute,
        escrow=escrow,
        elapsed_ms=round((time.time() - start) * 1000, 1),
    )
    proof = build_proof(policy, readings, floor_ratio, ceiling_ratio, dispute)
    print(f"[proof] policy {policy_id} inputs_hash={proof['inputs_hash']} floor={floor_ratio:.4f} ceiling={ceiling_ratio:.4f} ai={proof['ai_involvement']}")
    payload = _evaluation_to_dict(result, note=note, cycle="fresh", proof=proof)
    save_policy(policy, last_evaluation=payload)
    return payload


def _pending_response(policy: Policy, tx_signature: str, amount_sol: float | None, floor_ratio: float | None, ceiling_ratio: float | None) -> JSONResponse:
    """202, not 502: the payment may already have landed. The client must NOT retry the payment; POSTing
    /evaluate again only re-checks this transaction."""
    return JSONResponse(status_code=202, content={
        "status": "pending_confirmation",
        "policy_id": policy.id,
        "tx_signature": tx_signature,
        "floor_explorer_url": explorer_url(tx_signature),
        "floor_amount_sol": amount_sol,
        "payout_ratio_floor": floor_ratio,
        "payout_ratio_ceiling": ceiling_ratio,
        "message": "The payment was submitted but its confirmation could not be established (RPC unavailable). "
                   "It may already have landed - do not send it again. POST /evaluate again to re-check this transaction; "
                   "no second payment will be sent while it is unresolved.",
    })


@app.post(
    "/policy/{policy_id}/evaluate",
    responses={502: {"description": "Payment or data source failed"}},
)
async def evaluate_policy(policy_id: str, req: EvaluateRequest | None = None):
    start = time.time()
    policy = get_policy(policy_id)
    if not policy:
        raise HTTPException(404, f"policy {policy_id} not found")
    record = get_record(policy_id) or {}
    last = record.get("last_evaluation")
    escrow = get_escrow(policy_id)
    pending = bool(escrow and escrow["status"] == "pending")

    if policy.settled and not pending:
        raise HTTPException(409, "policy already settled - create a new policy to run another cycle")

    # 0. an earlier floor payment whose confirmation was never established: settle THAT, never pay twice -----
    pp = policy.pending_payment
    if pp:
        state = await check_signature(pp["signature"], pp.get("last_valid_block_height"), attempts=3)
        if state == "confirmed":
            print(f"[main] pending floor payment {pp['signature']} for {policy_id} is now confirmed; completing the evaluation without a second payment")
            note = ("Floor payment confirmed after an earlier unconfirmed submission; no second payment was sent. " + (pp.get("note") or "")).strip()
            return await _finish_fresh(policy, [SourceReading(**d) for d in pp["readings"]], note, pp["signature"], pp["floor_ratio"],
                                       pp["ceiling_ratio"], pp["floor_amount"], pp["ceiling_amount"], pp["delta"], pp["disagree"], start)
        if state == "unknown":
            return _pending_response(policy, pp["signature"], pp["amount_sol"], pp["floor_ratio"], pp["ceiling_ratio"])
        print(f"[main] pending floor payment {pp['signature']} for {policy_id} is {state}: it can never land, evaluating afresh")
        policy.pending_payment = None
        save_policy(policy)

    # 1. readings ---------------------------------------------------------
    warnings: list[str] = []
    try:
        readings = await _collect_readings(policy, req, warnings)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 - every source down
        return JSONResponse(status_code=502, content={"error": "data_unavailable", "detail": str(e), "policy_id": policy_id})

    # 2. formula (the only thing that decides amounts) ---------------------
    # Readings may be in different units (mm, NDVI); each was mapped to a
    # payout ratio in _collect_readings, so floor/ceiling are taken over ratios.
    floor_ratio, ceiling_ratio = floor_ceiling_from_ratios([r.payout_ratio or 0.0 for r in readings])
    floor_amount = payout_amount(policy.sum_insured_sol, floor_ratio)
    ceiling_amount = payout_amount(policy.sum_insured_sol, ceiling_ratio)
    delta = escrow_delta(policy.sum_insured_sol, floor_ratio, ceiling_ratio)
    disagree = len(readings) > 1 and sources_disagree(floor_ratio, ceiling_ratio)
    note = " ".join(warnings) or None

    # ---------------------------------------------------------------------
    # Follow-up cycle: an escrow is already open for this policy. The floor
    # was paid last time; we only decide what happens to the delta.
    # ---------------------------------------------------------------------
    if pending and last:
        paid_floor_ratio = float(last["payout_ratio_floor"])
        floor_tx = last.get("floor_tx_signature")
        dispute = last.get("dispute")
        status = DisputeStatus(last["dispute_status"])

        if status == DisputeStatus.ESCALATED:
            note = "Escrow is escalated - waiting for a human on POST /policy/{id}/resolve. No new payment."
        elif not disagree:
            # Watchdog said auto-resolve and the fresh readings agree: settle
            # the delta from the formula. Owed on top of the paid floor:
            agreed_ratio = ceiling_ratio  # == floor_ratio within tolerance
            top_up = round(payout_amount(policy.sum_insured_sol, agreed_ratio) - payout_amount(policy.sum_insured_sol, paid_floor_ratio), 9)
            try:
                escrow = await release_escrow(policy_id, resolved_by="auto", amount_sol=max(0.0, top_up))
            except PaymentPending as p:
                return _pending_response(policy, p.signature, p.amount_sol, None, None)
            except Exception as e:  # noqa: BLE001
                return JSONResponse(status_code=502, content={"error": "payment_failed", "detail": str(e), "policy_id": policy_id})
            status = DisputeStatus.RESOLVED
            policy.settled = True
            released = escrow.get("released_amount_sol", 0.0)
            note = (
                f"Sources agree this cycle (ratio {agreed_ratio:.3f}); escrow auto-resolved: "
                f"{released} SOL released, {escrow.get('voided_amount_sol', 0.0)} SOL voided."
            )
        else:
            # Still disagreeing: let the watchdog look again (it may now escalate).
            report = await investigate(policy, readings, floor_ratio, ceiling_ratio)
            dispute = asdict(report)
            status = DisputeStatus.ESCALATED if report.recommendation == "escalate" else DisputeStatus.INVESTIGATING
            note = "Sources still disagree; escrow unchanged."

        result = EvaluationResult(
            policy_id=policy_id,
            readings=readings,
            payout_ratio_floor=paid_floor_ratio,
            payout_ratio_ceiling=float(last["payout_ratio_ceiling"]),
            floor_amount_sol=float(last["floor_amount_sol"]),
            ceiling_amount_sol=float(last["ceiling_amount_sol"]),
            escrow_amount_sol=float(last["escrow_amount_sol"]),
            floor_tx_signature=floor_tx,
            dispute_status=status,
            dispute=dispute,
            escrow=escrow,
            elapsed_ms=round((time.time() - start) * 1000, 1),
        )
        proof = build_proof(policy, readings, floor_ratio, ceiling_ratio, dispute)
        print(f"[proof] policy {policy_id} inputs_hash={proof['inputs_hash']} floor={floor_ratio:.4f} ceiling={ceiling_ratio:.4f} ai={proof['ai_involvement']}")
        payload = _evaluation_to_dict(result, note=note, cycle="follow_up", proof=proof,
                                      fresh_payout_ratio_floor=floor_ratio, fresh_payout_ratio_ceiling=ceiling_ratio)
        save_policy(policy, last_evaluation=payload)
        return payload

    # ---------------------------------------------------------------------
    # Fresh cycle.
    # ---------------------------------------------------------------------
    # 3. pay the floor NOW - whether or not the sources agree -------------
    floor_tx = None
    if floor_amount > 0:
        try:
            payment = await send_payment(floor_amount, policy.payee_pubkey)
        except PaymentPending as p:
            policy.pending_payment = {
                "signature": p.signature, "amount_sol": floor_amount, "last_valid_block_height": p.last_valid_block_height,
                "readings": [asdict(r) for r in readings], "note": note, "floor_ratio": floor_ratio, "ceiling_ratio": ceiling_ratio,
                "floor_amount": floor_amount, "ceiling_amount": ceiling_amount, "delta": delta, "disagree": disagree, "created_at": time.time(),
            }
            save_policy(policy)
            print(f"[main] floor payment for {policy_id} unconfirmed ({p.signature}); stored as pending_payment, not retrying")
            return _pending_response(policy, p.signature, floor_amount, floor_ratio, ceiling_ratio)
        except Exception as e:  # noqa: BLE001 - RPC down, no funds, bad blockhash...
            print(f"[main] floor payment failed for {policy_id}: {e!r}")
            return JSONResponse(
                status_code=502,
                content={
                    "error": "payment_failed",
                    "detail": str(e),
                    "policy_id": policy_id,
                    "floor_amount_sol": floor_amount,
                    "payout_ratio_floor": floor_ratio,
                    "payout_ratio_ceiling": ceiling_ratio,
                },
            )
        floor_tx = payment.tx_signature
    else:
        note = ((note + " ") if note else "") + "Floor payout is 0 SOL (least-loss reading at/beyond trigger) - nothing sent on-chain for the floor."

    return await _finish_fresh(policy, readings, note, floor_tx, floor_ratio, ceiling_ratio, floor_amount, ceiling_amount, delta, disagree, start)


@app.post(
    "/policy/{policy_id}/resolve",
    responses={502: {"description": "Release transfer failed"}, 409: {"description": "Nothing pending"}},
)
async def resolve_policy(policy_id: str, release: bool = Query(..., description="true = pay the escrowed delta, false = void it")):
    """Human resolution of an escrowed delta. The AI never calls this."""
    policy = get_policy(policy_id)
    if not policy:
        raise HTTPException(404, f"policy {policy_id} not found")
    escrow = get_escrow(policy_id)
    if not escrow or escrow["status"] != "pending":
        raise HTTPException(409, "no pending escrow for this policy")

    if release:
        try:
            escrow = await release_escrow(policy_id, resolved_by="human")
        except PaymentPending as p:
            return _pending_response(policy, p.signature, p.amount_sol, None, None)
        except Exception as e:  # noqa: BLE001
            print(f"[main] escrow release failed for {policy_id}: {e!r}")
            return JSONResponse(status_code=502, content={"error": "payment_failed", "detail": str(e), "policy_id": policy_id})
    else:
        try:
            escrow = void_escrow(policy_id, resolved_by="human")
        except PaymentPending as p:
            return _pending_response(policy, p.signature, None, None, None)

    policy.settled = True
    record = get_record(policy_id) or {}
    last = dict(record.get("last_evaluation") or {})
    last["dispute_status"] = DisputeStatus.RESOLVED.value
    last["escrow"] = _with_links(escrow)
    last["note"] = f"Escrow {escrow['status']} by a human reviewer."
    save_policy(policy, last_evaluation=last)

    return {
        "policy_id": policy_id,
        "dispute_status": DisputeStatus.RESOLVED.value,
        "released": bool(release),
        "escrow": _with_links(escrow),
        "release_tx_signature": escrow.get("release_tx_signature"),
        "release_explorer_url": explorer_url(escrow.get("release_tx_signature")),
    }
