"""Unit tests for the pure payout formula. Run: pytest -q"""

import pytest

from app.formula import escrow_delta, payout_amount, payout_ratio, sources_disagree, split_floor_ceiling


def test_ratio_is_zero_at_or_above_trigger():
    assert payout_ratio(40, trigger_mm=40, exit_mm=10) == 0.0
    assert payout_ratio(95, trigger_mm=40, exit_mm=10) == 0.0


def test_ratio_is_one_at_or_below_exit():
    assert payout_ratio(10, trigger_mm=40, exit_mm=10) == 1.0
    assert payout_ratio(0, trigger_mm=40, exit_mm=10) == 1.0


def test_ratio_is_linear_between():
    assert payout_ratio(25, trigger_mm=40, exit_mm=10) == pytest.approx(0.5)
    assert payout_ratio(32.5, trigger_mm=40, exit_mm=10) == pytest.approx(0.25)


def test_ratio_rejects_bad_parameters():
    with pytest.raises(ValueError):
        payout_ratio(5, trigger_mm=10, exit_mm=10)
    with pytest.raises(ValueError):
        payout_ratio(-1, trigger_mm=40, exit_mm=10)


def test_payout_amount_scales_and_rounds_to_lamports():
    assert payout_amount(0.01, 0.5) == 0.005
    assert payout_amount(0.01, 1.0) == 0.01
    assert payout_amount(0.01, 0.0) == 0.0
    assert payout_amount(0.0001, 1 / 3) == 0.000033333


def test_floor_is_wettest_and_ceiling_is_driest():
    floor, ceiling = split_floor_ceiling([30, 12], trigger_mm=40, exit_mm=10)
    assert floor == pytest.approx(1 / 3)     # 30 mm -> less loss
    assert ceiling == pytest.approx(28 / 30)  # 12 mm -> more loss


def test_single_reading_has_no_spread():
    floor, ceiling = split_floor_ceiling([20], trigger_mm=40, exit_mm=10)
    assert floor == ceiling


def test_disagreement_threshold():
    assert not sources_disagree(0.50, 0.60)          # exactly 10% is still agreement
    assert sources_disagree(0.50, 0.61)
    assert sources_disagree(0.50, 0.55, tolerance=0.02)
    with pytest.raises(ValueError):
        sources_disagree(0.6, 0.5)


def test_escrow_delta_plus_floor_equals_ceiling():
    floor, ceiling = 0.3, 0.8
    delta = escrow_delta(0.01, floor, ceiling)
    assert delta == pytest.approx(0.005)
    assert payout_amount(0.01, floor) + delta == pytest.approx(payout_amount(0.01, ceiling))


def test_poisoned_dry_source_only_moves_the_delta():
    """An attacker forcing one feed to 0 mm can raise the ceiling but not
    lower the floor: the honest reading still gets paid immediately."""
    honest_mm, poisoned_mm = 30, 0
    floor, ceiling = split_floor_ceiling([honest_mm, poisoned_mm], trigger_mm=40, exit_mm=10)
    assert floor == payout_ratio(honest_mm, 40, 10)
    assert ceiling == 1.0
    assert sources_disagree(floor, ceiling)


def test_excess_rain_cover_uses_same_formula_with_exit_above_trigger():
    # trigger 80 mm (no loss), exit 140 mm (total loss): more rain = more loss
    assert payout_ratio(60, trigger_mm=80, exit_mm=140) == 0.0
    assert payout_ratio(110, trigger_mm=80, exit_mm=140) == pytest.approx(0.5)
    assert payout_ratio(150, trigger_mm=80, exit_mm=140) == 1.0


def test_ndvi_vote_maps_with_its_own_thresholds():
    from app.formula import floor_ceiling_from_ratios
    weather = payout_ratio(34, trigger_mm=40, exit_mm=10)          # 0.2 - weather says mostly fine
    satellite = payout_ratio(0.20, trigger_mm=0.55, exit_mm=0.25)  # 1.0 - field looks dead
    floor, ceiling = floor_ceiling_from_ratios([weather, weather, satellite])
    assert floor == pytest.approx(0.2)
    assert ceiling == 1.0
    assert sources_disagree(floor, ceiling)
