"""Product catalog + proof object. The formula tests in test_formula.py are untouched."""

from app.formula import payout_ratio, split_floor_ceiling, sources_disagree
from app.models import Policy, ProductType, SourceReading
from app.products import PRODUCTS, catalog, cover_for, product
from app.proof import build_proof, inputs_hash


def _policy(ptype: str) -> Policy:
    p = product(ptype)
    return Policy("pol_t", "Test", 52.0, 21.0, p["trigger"], p["exit"], 0.01, "x", "2026-09-19", "2026-09-25",
                  cover=cover_for(ptype), product_type=ptype, metric_unit=p["metric_unit"], metric_label=p["metric_label"])


def test_catalog_has_all_four_products_with_consistent_directions():
    kinds = {c["product_type"] for c in catalog()}
    assert kinds == {e.value for e in ProductType}
    for k, p in PRODUCTS.items():
        assert (p["trigger"] > p["exit"]) == (p["direction"] == "less"), k


def test_event_rainout_uses_the_same_formula_as_the_crop_products():
    p = _policy("event_weather_cancel")   # trigger 5 mm, exit 25 mm: any real rain voids the event
    assert payout_ratio(2, p.trigger_mm, p.exit_mm) == 0.0
    assert payout_ratio(15, p.trigger_mm, p.exit_mm) == 0.5
    assert payout_ratio(40, p.trigger_mm, p.exit_mm) == 1.0


def test_travel_delay_floor_ceiling_and_dispute_in_minutes():
    p = _policy("travel_delay")           # trigger 30 min, exit 180 min
    floor, ceiling = split_floor_ceiling([20, 150], p.trigger_mm, p.exit_mm)
    assert floor == 0.0 and abs(ceiling - 0.8) < 1e-9
    assert sources_disagree(floor, ceiling)
    floor, ceiling = split_floor_ceiling([140, 150], p.trigger_mm, p.exit_mm)
    assert not sources_disagree(floor, ceiling)


def test_proof_substitutes_real_numbers_and_hash_is_canonical():
    p = _policy("travel_delay")
    a = SourceReading("simulated:delay-feed-A", 20.0, False, unit="min", payout_ratio=0.0, fetched_at=1_700_000_000)
    b = SourceReading("simulated:delay-feed-B", 150.0, False, unit="min", payout_ratio=0.8, fetched_at=1_700_000_001)
    proof = build_proof(p, [a, b], 0.0, 0.8, None)
    assert proof["floor_calc"].startswith("floor  = clamp((30 - 20) / (30 - 180), 0, 1) = 0.0000")
    assert proof["ceiling_calc"].startswith("ceiling = clamp((30 - 150) / (30 - 180), 0, 1) = 0.8000")
    assert proof["metric_unit"] == "min" and proof["ai_involvement"] == "none"
    assert proof["data_source_note"].startswith("demo data source")
    assert len(proof["inputs_hash"]) == 64
    assert inputs_hash(p, [a, b]) == inputs_hash(p, [a, b])           # deterministic
    assert inputs_hash(p, [a, b]) != inputs_hash(p, [b, a])           # order is part of the record
    b2 = SourceReading(b.source, 151.0, b.live, unit="min", payout_ratio=b.payout_ratio, fetched_at=b.fetched_at)
    assert inputs_hash(p, [a, b]) != inputs_hash(p, [a, b2])          # tamper-evident


def test_ai_involvement_flag_when_a_dispute_report_exists():
    p = _policy("crop_drought")
    a = SourceReading("open-meteo:best_match", 30.0, True, payout_ratio=1 / 3)
    b = SourceReading("open-meteo:ecmwf_ifs025", 12.0, True, payout_ratio=14 / 15)
    proof = build_proof(p, [a, b], 1 / 3, 14 / 15, {"summary": "x"})
    assert proof["ai_involvement"].startswith("explanation-only")
    assert proof["data_source_note"] is None


# --- Ticketmaster cross-domain event-status source -------------------------------------------
import asyncio

from app.ticketmaster import fetch_event_status, implied_ratio, status_reading


def test_status_mapping():
    assert implied_ratio("canceled") == implied_ratio("cancelled") == implied_ratio("postponed") == 1.0
    assert implied_ratio("onsale") == implied_ratio("rescheduled") == 0.0
    assert implied_ratio("???") is None


def test_status_reading_shows_up_distinctly_in_proof():
    p = _policy("event_weather_cancel")
    weather = [SourceReading("open-meteo:best_match", 0.0, True, payout_ratio=0.0), SourceReading("open-meteo:ecmwf_ifs025", 0.0, True, payout_ratio=0.0)]
    tm = status_reading("canceled", "x", live=True)
    tm_ratio = tm.payout_ratio
    proof = build_proof(p, weather + [tm], 0.0, tm_ratio, None)
    last = proof["readings"][-1]
    assert (last["source"], last["value"], last["unit"]) == ("ticketmaster_event_status", "canceled", "status")
    assert proof["readings"][0]["unit"] == "mm"


def test_lookup_skipped_without_key_or_identifiers(monkeypatch):
    p = _policy("event_weather_cancel")
    assert asyncio.run(fetch_event_status(p)) == (None, None)          # no venue/event id: silent
    p.venue_name = "Some Venue"
    monkeypatch.setattr("app.ticketmaster._api_key", lambda: "")
    reading, note = asyncio.run(fetch_event_status(p))
    assert reading is None and "TICKETMASTER_API_KEY" in note


def test_lookup_failure_never_raises(monkeypatch):
    p = _policy("event_weather_cancel"); p.ticketmaster_event_id = "abc"
    monkeypatch.setattr("app.ticketmaster._api_key", lambda: "k")
    async def boom(*a, **k): raise TimeoutError()
    monkeypatch.setattr("app.ticketmaster._get", boom)
    reading, note = asyncio.run(fetch_event_status(p))
    assert reading is None and "unavailable" in note


# --- watchdog explanation must match the real floor, incl. status-implied sources -------------
from app.decision import DisputeContext, investigate_heuristic


def _ctx(readings, floor, ceiling):
    p = _policy("event_weather_cancel")
    for r in readings:
        r.payout_ratio = r.payout_ratio if r.payout_ratio is not None else payout_ratio(r.observed_mm, p.trigger_mm, p.exit_mm)
    return DisputeContext(p, readings, floor, ceiling)


def test_status_vs_weather_explanation_states_zero_floor():
    w = [SourceReading("a", 0.0, True), SourceReading("b", 0.0, True)]
    rep = investigate_heuristic(_ctx(w + [status_reading("cancelled", "x", True)], 0.0, 1.0))
    assert rep.recommendation == "escalate" and rep.suspected_cause == "event status vs measurement conflict"
    assert "floor is 0" in rep.summary and "nothing has been paid" in rep.summary
    assert "floor has been paid" not in rep.summary and "ordinary variance" not in rep.summary


def test_positive_floor_explanation_states_the_amount():
    rep = investigate_heuristic(_ctx([SourceReading("a", 20.0, True), SourceReading("b", 22.0, True)], 0.75, 0.85))
    assert "paid immediately" in rep.summary and "0.0075 SOL" in rep.summary


# --- Groq provider (network mocked) ------------------------------------------------------------
import httpx
import pytest

import app.decision as dec


def _event_ctx():
    w = [SourceReading("a", 0.0, True), SourceReading("b", 0.0, True)]
    return _ctx(w + [status_reading("cancelled", "x", True)], 0.0, 1.0)


class _FakeResp:
    def __init__(self, payload, status=200): self._p, self.status_code = payload, status
    def raise_for_status(self):
        if self.status_code >= 400: raise httpx.HTTPStatusError("boom", request=None, response=None)
    def json(self): return self._p


def _mock_groq(monkeypatch, content, seen):
    async def post(self, url, headers=None, json=None, **kw):
        seen.update(url=url, headers=headers, body=json)
        return _FakeResp({"model": "openai/gpt-oss-120b", "choices": [{"message": {"content": content}}]})
    monkeypatch.setattr(httpx.AsyncClient, "post", post)


def test_groq_uses_same_prompt_and_real_amounts(monkeypatch):
    seen = {}
    monkeypatch.setattr(dec, "_secret", lambda n: "gk" if n == "GROQ_API_KEY" else "")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _mock_groq(monkeypatch, '{"summary":"s","suspected_cause":"c","evidence":["e"],"recommendation":"escalate"}', seen)
    rep = asyncio.run(dec.investigate_with_groq(_event_ctx()))
    assert seen["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert seen["body"]["model"] == "openai/gpt-oss-120b"
    assert seen["body"]["messages"][0]["content"] == dec.system_prompt(_event_ctx())
    user = seen["body"]["messages"][1]["content"]
    assert _event_ctx().describe() in user and "pays 0.0000 SOL now" in user and "nothing is paid at the floor" in user
    assert rep.ai_used and rep.model.startswith("groq:")


def test_precedence_and_fallbacks(monkeypatch):
    p = _policy("event_weather_cancel"); readings = _event_ctx().readings
    calls = []
    async def groq(ctx): calls.append("groq"); raise TimeoutError()
    async def claude(ctx): calls.append("claude"); return "CLAUDE"
    monkeypatch.setattr(dec, "investigate_with_groq", groq); monkeypatch.setattr(dec, "investigate_with_llm", claude)
    run = lambda: asyncio.run(dec.investigate(p, readings, 0.0, 1.0))
    monkeypatch.setattr(dec, "_secret", lambda n: "k" if n == "GROQ_API_KEY" else "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert run() == "CLAUDE" and calls == ["groq", "claude"]          # Groq first, then Claude when Groq fails
    monkeypatch.delenv("ANTHROPIC_API_KEY"); calls.clear()
    rep = run()                                                        # Groq fails, no Claude -> rule-based
    assert rep.model == "heuristic" and "groq: TimeoutError" in rep.evidence[-1] and "floor is 0" in rep.summary
    monkeypatch.setattr(dec, "_secret", lambda n: ""); calls.clear()
    assert run().model == "heuristic" and calls == []                  # no keys at all -> rule-based, no calls


def test_guardrail_appends_real_floor_statement_when_provider_omits_it():
    from app.models import DisputeReport
    ctx = _event_ctx()
    rep = dec._guardrail(DisputeReport(summary="Weather says dry, status says cancelled.", suspected_cause="c", evidence=[], recommendation="escalate", ai_used=True, model="m"), ctx)
    assert rep.summary.endswith("The floor is 0, so nothing has been paid yet.")
    ok = dec._guardrail(DisputeReport(summary="The floor is 0.", suspected_cause="c", evidence=[], recommendation="escalate", ai_used=True, model="m"), ctx)
    assert ok.summary == "The floor is 0."   # already mentions it: untouched



# --- watchdog is product-independent: ratio-based branches, catalog vocabulary ------------------------------
import re as _re

def _pctx(ptype, values, floor_ratio=None, ceiling_ratio=None):
    p = _policy(ptype)
    rs = [SourceReading(f"src{i}", float(v), False, unit=p.metric_unit) for i, v in enumerate(values)]
    for r in rs:
        r.payout_ratio = payout_ratio(r.observed_mm, p.trigger_mm, p.exit_mm)
    ratios = [r.payout_ratio for r in rs]
    return DisputeContext(p, rs, min(ratios), max(ratios))


@pytest.mark.parametrize("ptype,values", [("crop_excess_rain", [90, 130]), ("travel_delay", [20, 150]),
                                          ("event_weather_cancel", [10, 24]), ("crop_drought", [30, 12])])
def test_heuristic_never_calls_a_guardrail_escalation_ordinary_variance(ptype, values):
    ctx = _pctx(ptype, values)
    assert ctx.spread >= dec.HARD_ESCALATE_SPREAD
    rep = investigate_heuristic(ctx)
    assert rep.recommendation == "escalate"
    assert "ordinary variance" not in rep.summary and rep.suspected_cause != "model spread"
    assert ctx.floor_clause() in rep.summary or "floor" in rep.summary.lower()


def test_heuristic_moderate_spread_is_ordinary_variance_and_consistent():
    ctx = _pctx("crop_excess_rain", [100, 112])          # ratios 0.333 / 0.533, spread 0.2 < hard limit
    rep = investigate_heuristic(ctx)
    assert rep.recommendation == "auto_resolve" and "ordinary variance" in rep.summary


def test_heuristic_flat_zero_dropout_works_for_delay_too():
    rep = investigate_heuristic(_pctx("travel_delay", [0, 150]))
    assert rep.suspected_cause == "sensor dropout" and "0 min" in rep.summary and "mm" not in rep.summary


CROP_WORDS = _re.compile(r"\b(crop|rain|rainfall|drought|NDVI|satellite|vegetation|mm|field|weather)\b", _re.I)

def test_travel_prompt_and_context_use_no_crop_vocabulary():
    ctx = _pctx("travel_delay", [20, 150])
    text = dec.system_prompt(ctx) + ctx.describe() + investigate_heuristic(ctx).summary
    assert not CROP_WORDS.search(text), CROP_WORDS.search(text).group(0)
    assert "Flight / train delay cover" in text and "Delay mapping: trigger 30 min" in text and "higher delay = more loss" in text


def test_crop_prompt_mentions_satellite_only_when_a_satellite_reading_exists():
    ctx = _pctx("crop_drought", [30, 12])
    assert "NDVI" not in dec.system_prompt(ctx) and "Satellite mapping" not in ctx.describe()
    sat = SourceReading("agromonitoring:ndvi", 0.2, False, unit="ndvi"); sat.payout_ratio = 0.8
    ctx2 = DisputeContext(ctx.policy, ctx.readings + [sat], ctx.floor_ratio, 0.8)
    assert "NDVI" in dec.system_prompt(ctx2) and "Satellite mapping" in ctx2.describe() and "rainfall" in dec.system_prompt(ctx2).lower()


def test_guardrail_corrects_a_floor_paid_claim_next_to_a_zero_floor():
    from app.models import DisputeReport
    ctx = _event_ctx()                                            # floor is 0
    rep = dec._guardrail(DisputeReport(summary="The floor payout is 0 SOL (already paid) and 0.01 SOL is held.", suspected_cause="c",
                                       evidence=["Floor ratio 0.000 (0 SOL paid now)", "The floor was already paid on-chain", "Spread 1.0"], recommendation="escalate", ai_used=True, model="m"), ctx)
    assert rep.summary.endswith("The floor is 0, so nothing has been paid yet.")
    assert rep.evidence[0] == "Floor ratio 0.000 (0 SOL paid now)"          # truthful line untouched
    assert rep.evidence[1] == "Floor: ratio 0.000 = 0.0000 SOL (nothing paid yet)" and rep.evidence[2] == "Spread 1.0"


def test_guardrail_corrects_nothing_paid_claim_next_to_a_positive_floor():
    from app.models import DisputeReport
    ctx = _pctx("crop_drought", [30, 12])                          # floor 0.333 -> 0.0033 SOL, paid at once
    rep = dec._guardrail(DisputeReport(summary="The floor is 0, so nothing has been paid.", suspected_cause="c", evidence=[], recommendation="escalate", ai_used=True, model="m"), ctx)
    assert rep.summary.endswith("The floor (0.333, 0.0033 SOL) is paid immediately.")


def test_guardrail_handles_unicode_spaces_from_models():
    from app.models import DisputeReport
    ctx = _event_ctx()
    rep = dec._guardrail(DisputeReport(summary="Floor payout is 0\u202fSOL (already paid).", suspected_cause="c",
                                       evidence=["Floor ratio 0.000 (0\u202fSOL paid now)"], recommendation="escalate", ai_used=True, model="m"), ctx)
    assert rep.summary.endswith("nothing has been paid yet.") and rep.evidence == ["Floor ratio 0.000 (0\u202fSOL paid now)"]


def test_guardrail_does_not_mistake_a_small_positive_amount_for_zero():
    from app.models import DisputeReport
    ctx = _pctx("crop_excess_rain", [90, 130])                     # floor 0.167 -> 0.0017 SOL
    good = "The floor payout of 0.0017 SOL has already been sent, but the delta remains in escrow."
    rep = dec._guardrail(DisputeReport(summary=good, suspected_cause="c", evidence=["Floor ratio 0.167 (0.0017 SOL)"], recommendation="escalate", ai_used=True, model="m"), ctx)
    assert rep.summary == good and rep.evidence == ["Floor ratio 0.167 (0.0017 SOL)"]      # untouched, nothing appended
    bad = dec._guardrail(DisputeReport(summary="The floor payout is 0.000 SOL.", suspected_cause="c", evidence=[], recommendation="escalate", ai_used=True, model="m"), ctx)
    assert bad.summary.endswith("is paid immediately.")


def test_every_product_has_a_known_colour_theme():
    assert {p["theme"] for p in catalog()} <= {"farm", "event", "flight"}
    assert {c["product_type"]: c["theme"] for c in catalog()} == {"crop_drought": "farm", "crop_excess_rain": "farm", "event_weather_cancel": "event", "travel_delay": "flight"}
