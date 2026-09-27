"""
The payout formula. This is the ONLY thing that decides how much is paid.

    payout_ratio  = clamp((trigger_mm - observed_mm) / (trigger_mm - exit_mm), 0, 1)
    payout_amount = sum_insured_sol * payout_ratio

Pure functions, no I/O, no state - so they're trivially unit-testable and
there's no way for the AI layer (decision.py) to influence an amount.

Floor / ceiling: with several independent readings for the same window,
the *wettest* reading gives the lowest ratio (worst case for the
policyholder) and the *driest* the highest. We pay the floor right away
and escrow only the delta; see main.py for the flow.
"""

from collections.abc import Iterable

DEFAULT_TOLERANCE = 0.10  # >10% spread in payout_ratio = the sources "disagree"


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def payout_ratio(observed_mm: float, trigger_mm: float, exit_mm: float) -> float:
    """0 = no loss (observed at/beyond trigger), 1 = total loss (observed
    at/beyond exit). Linear in between.

    Direction is implied by which threshold is larger, so ONE formula covers
    both products:
      - drought cover:     trigger > exit  (40 mm -> 10 mm): less rain, more loss
      - excess-rain cover: exit > trigger  (80 mm -> 140 mm): more rain, more loss
        (trigger - observed) / (trigger - exit) flips sign in both numerator
        and denominator, so the same expression yields the right ratio.
    Decision flagged in main.py / README: we did NOT add a separate flood
    formula because the shape is identical; only the validation differs.

    The same function is also used for the satellite NDVI vote with NDVI
    thresholds (e.g. trigger 0.55 -> exit 0.25) - units are the caller's."""
    if trigger_mm == exit_mm:
        raise ValueError("trigger and exit must differ")
    if observed_mm < 0:
        raise ValueError("observed value cannot be negative")
    return clamp((trigger_mm - observed_mm) / (trigger_mm - exit_mm))


def payout_amount(sum_insured_sol: float, ratio: float) -> float:
    """SOL owed for a given ratio. Rounded to lamport precision (1e-9 SOL)
    so floor + delta always adds up to exactly the ceiling on-chain."""
    if sum_insured_sol < 0:
        raise ValueError("sum_insured_sol cannot be negative")
    return round(sum_insured_sol * clamp(ratio), 9)


def split_floor_ceiling(
    readings_mm: Iterable[float], trigger_mm: float, exit_mm: float
) -> tuple[float, float]:
    """(floor_ratio, ceiling_ratio) across independent readings of the same
    window. Floor = wettest reading (least loss), ceiling = driest reading
    (most loss). With a single reading both are equal."""
    ratios = [payout_ratio(mm, trigger_mm, exit_mm) for mm in readings_mm]
    if not ratios:
        raise ValueError("at least one reading is required")
    return min(ratios), max(ratios)


def floor_ceiling_from_ratios(ratios: Iterable[float]) -> tuple[float, float]:
    """Same as split_floor_ceiling but for readings that were ALREADY turned
    into payout ratios - needed once the votes come in different units
    (rainfall mm from weather models, NDVI from satellite). Floor = least
    loss, ceiling = most loss, whatever the unit."""
    rs = [clamp(r) for r in ratios]
    if not rs:
        raise ValueError("at least one ratio is required")
    return min(rs), max(rs)


def sources_disagree(floor_ratio: float, ceiling_ratio: float, tolerance: float = DEFAULT_TOLERANCE) -> bool:
    """True when the spread between the best and worst case payout ratio
    is wider than we're willing to settle on without a second look."""
    if ceiling_ratio < floor_ratio:
        raise ValueError("ceiling_ratio must be >= floor_ratio")
    return (ceiling_ratio - floor_ratio) > tolerance


def escrow_delta(sum_insured_sol: float, floor_ratio: float, ceiling_ratio: float) -> float:
    """The disputed slice: what we'd owe on top of the floor if the driest
    reading turns out to be right. This is what goes into escrow."""
    return round(payout_amount(sum_insured_sol, ceiling_ratio) - payout_amount(sum_insured_sol, floor_ratio), 9)
