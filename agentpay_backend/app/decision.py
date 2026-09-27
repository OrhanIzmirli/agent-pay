"""
Dispute WATCHDOG - not a decision-maker.

Role of this module after the parametric-insurance pivot:

  * The payout amount is decided by formula.py, full stop. Nothing in here
    returns, adjusts or approves an amount.
  * The AI only wakes up when two or more independent sources disagree
    beyond a tolerance (formula.sources_disagree). The floor payout has
    ALREADY been sent on-chain by the time we're called; only the
    ceiling-minus-floor delta is sitting in escrow.
  * Sources can be in different units: rainfall totals (mm) from two
    weather models, and optionally a satellite crop-health vote (NDVI).
    Every reading arrives with its payout ratio already computed by main.py
    from the policy's mapping for that unit, so the watchdog compares
    *ratios*, and it is expected to cross-reference the satellite against
    the weather ("weather models say drought, but the field is still
    green") - that is a far stronger explanation than weather-vs-weather.
  * What it does: investigate why the sources disagree, cross-reference
    them for signs of a sensor fault / manipulation, explain the situation
    in plain language, and recommend one of two things:
        "auto_resolve" -> dispute_status = investigating; the next evaluate
                          cycle may settle the escrow from fresh readings
        "escalate"     -> dispute_status = escalated; a human must call
                          POST /policy/{id}/resolve
  * What it can NOT do: release or void the escrow, or send a payment. The
    DisputeReport it returns carries no amounts at all, and main.py never
    reads anything from it except the recommendation + text for display.

A deterministic guardrail sits on top of the AI: a spread of
HARD_ESCALATE_SPREAD or more is always escalated, whatever the model says.
If no API key is configured (or the call fails) a rule-based investigator
produces the same DisputeReport shape so the demo never depends on the
model being reachable.
"""

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.formula import DEFAULT_TOLERANCE, payout_amount, payout_ratio
from app.models import DisputeReport, Policy, SourceReading
from app.products import PRODUCTS

# Spread (ceiling - floor payout ratio) at/above which we never auto-resolve.
HARD_ESCALATE_SPREAD = 0.40

WATCHDOG_MODEL = os.environ.get("WATCHDOG_MODEL", "claude-opus-5")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_DEFAULT_MODEL = "openai/gpt-oss-120b"  # llama-3.3-70b-versatile left Groq's free tier


def _secret(name: str) -> str:
    """Environment first, then the gitignored .env next to app/ (same discipline as the other keys)."""
    val = os.environ.get(name, "").strip()
    if val:
        return val
    try:
        for line in (Path(__file__).resolve().parent.parent / ".env").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == name:
                return value.strip().strip("'\"")
    except OSError:
        pass
    return ""

def system_prompt(ctx: "DisputeContext") -> str:
    """Built from the policy's own catalog entry (label, metric, unit); crop-only language (satellite NDVI) appears
    only when a satellite reading is actually part of the dispute."""
    sources = f"independent readings of the policy's own metric, {ctx.metric.lower()} (in {ctx.unit})"
    if ctx.satellite:
        sources += (" and a satellite vegetation-index reading of the field itself (NDVI, where roughly 0.6+ is a healthy canopy and "
                    "below 0.3 is bare or dead vegetation)")
    if ctx.status:
        sources += " and status-type evidence from an independent domain"
    lines = [
        f"You are the dispute watchdog for a parametric insurance contract ({ctx.label}) on Solana devnet.",
        "",
        "Context you must respect:",
        "- The payout is computed by a fixed formula from observed readings. You do not set, adjust or approve any amount, and you must not suggest one.",
        f"- Two or more independent sources for the same policy and window disagree beyond tolerance. Sources here: {sources}. Each reading has already been mapped to a payout ratio with the policy's own thresholds, so compare the ratios.",
        "- The floor amount (the least-loss reading's payout) is paid on-chain immediately; it can be 0, in which case nothing is paid yet. Only the disputed delta is held in escrow. State the real floor amount from the readings; never claim the floor was paid when it is 0.",
    ]
    if ctx.status:
        lines.append("- Status readings (unit status, e.g. ticketmaster_event_status) are independent non-measurement evidence: cancelled/postponed = ratio 1.0, still going ahead = 0.0.")
    lines.append(
        "- Your job: work out why the sources disagree, look for signs of a sensor fault, data outage or manipulation (for example one source reporting exactly zero while another reports a large value, "
        "a reading that is implausible for the place or situation, or a spread far larger than normal variance between independent providers), explain the situation in plain language a non-technical reviewer can act on, "
        "and recommend either \"auto_resolve\" (the disagreement looks like ordinary variance and fresh readings next cycle can be trusted to settle it) or \"escalate\" (a human should look before the escrow is released or voided).")
    if ctx.satellite and ctx.measured:
        lines.append(f"- A satellite reading is present: cross-reference it explicitly against the {ctx.metric.lower()} readings: they measure the cause, the satellite measures the outcome (crop health). "
                     "Spell out any conflict between them, naming the satellite reading and its date.")
    lines.append("- Prefer \"escalate\" whenever manipulation or a fault is plausible. Be concrete in the evidence list: cite the actual numbers you were given.")
    return "\n".join(lines)


REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "2-4 plain-language sentences for the reviewer"},
        "suspected_cause": {"type": "string", "description": "short label, e.g. 'model spread', 'sensor dropout', 'possible manipulation', 'weather vs satellite conflict'"},
        "evidence": {"type": "array", "items": {"type": "string"}, "description": "3-6 concrete, checkable points"},
        "recommendation": {"type": "string", "enum": ["auto_resolve", "escalate"]},
    },
    "required": ["summary", "suspected_cause", "evidence", "recommendation"],
    "additionalProperties": False,
}


def _ratio(policy: Policy, r: SourceReading) -> float:
    """Reading -> payout ratio. main.py normally pre-fills r.payout_ratio;
    recompute from the policy mapping when it didn't."""
    if r.payout_ratio is not None:
        return float(r.payout_ratio)
    if r.unit == "ndvi":
        return payout_ratio(r.observed_mm, policy.ndvi_trigger, policy.ndvi_exit)
    if r.unit == "status":
        return r.observed_mm  # implied ratio
    return payout_ratio(r.observed_mm, policy.trigger_mm, policy.exit_mm)


def _fmt(r: SourceReading) -> str:
    if r.unit == "status":
        return f"event status '{r.status}'"
    return f"NDVI {r.observed_mm:.2f}" if r.unit == "ndvi" else f"{r.observed_mm:.1f} {r.unit}"


@dataclass
class DisputeContext:
    policy: Policy
    readings: list[SourceReading]
    floor_ratio: float
    ceiling_ratio: float

    @property
    def spread(self) -> float:
        return self.ceiling_ratio - self.floor_ratio

    @property
    def measured(self) -> list[SourceReading]:
        """Readings of the policy's own metric (rainfall mm, delay min, ...): everything except satellite and status evidence."""
        return [r for r in self.readings if r.unit not in ("ndvi", "status")]

    @property
    def label(self) -> str:
        return PRODUCTS.get(self.policy.product_type, {}).get("label", "parametric cover")

    @property
    def subject(self) -> str:
        return PRODUCTS.get(self.policy.product_type, {}).get("subject", "the insured subject")

    @property
    def metric(self) -> str:
        return self.policy.metric_label

    @property
    def unit(self) -> str:
        return self.policy.metric_unit

    @property
    def noun(self) -> str:
        return self.policy.metric_label.lower()

    @property
    def status(self) -> list[SourceReading]:
        return [r for r in self.readings if r.unit == "status"]

    @property
    def floor_amount(self) -> float:
        return payout_amount(self.policy.sum_insured_sol, self.floor_ratio)

    @property
    def ceiling_amount(self) -> float:
        return payout_amount(self.policy.sum_insured_sol, self.ceiling_ratio)

    def floor_clause(self) -> str:
        """What actually happened to the floor, from the real numbers. Never assumes it was paid."""
        if self.floor_amount > 0:
            return f"The floor ({self.floor_ratio:.3f}, {self.floor_amount:.4f} SOL) is paid immediately"
        return "The floor is 0, so nothing has been paid yet"

    @property
    def satellite(self) -> list[SourceReading]:
        return [r for r in self.readings if r.unit == "ndvi"]

    def describe(self) -> str:
        p = self.policy
        direction = f"higher {self.noun} = more loss" if p.exit_mm > p.trigger_mm else f"lower {self.noun} = more loss"
        lines = [
            f"Policy {p.id}: {self.label}, insuring {self.subject}; location {p.region} (lat {p.lat}, lon {p.lon}), window {p.window_start} to {p.window_end}; {direction}.",
            f"{self.metric} mapping: trigger {p.trigger_mm:g} {self.unit} (no loss), exit {p.exit_mm:g} {self.unit} (total loss).",
        ]
        if self.satellite:
            lines.append(f"Satellite mapping: NDVI trigger {p.ndvi_trigger} (healthy, no loss), exit {p.ndvi_exit} (total loss).")
        lines.append("Readings:")
        for r in self.readings:
            live = "live API" if r.live else "SIMULATED / injected"
            when = f", captured {r.captured_at}" if r.captured_at else ""
            lines.append(f"  - {r.source}: {_fmt(r)} -> payout ratio {_ratio(p, r):.3f} ({live}{when}). {r.detail}".rstrip())
        if self.measured and self.satellite:
            w = sum(_ratio(p, r) for r in self.measured) / len(self.measured)
            s = _ratio(p, self.satellite[0])
            lines.append(
                f"Cross-reference: the {self.noun} readings imply an average payout ratio of {w:.3f}; "
                f"the satellite crop-health reading implies {s:.3f}."
            )
        if self.status:
            lines.append("Status sources (e.g. ticketmaster_event_status) are not continuous values: a cancelled/postponed event implies ratio 1.0, an event still going ahead implies 0.0.")
        lines.append(
            f"Payout ratio floor {self.floor_ratio:.3f} (least-loss reading; pays {self.floor_amount:.4f} SOL now"
            f"{' - nothing is paid at the floor' if self.floor_amount <= 0 else ''}), "
            f"ceiling {self.ceiling_ratio:.3f} (most-loss reading; up to {self.ceiling_amount:.4f} SOL in total), "
            f"spread {self.spread:.3f}. The difference, {self.ceiling_amount - self.floor_amount:.4f} SOL, is held in escrow only if the sources disagree beyond tolerance."
        )
        return "\n".join(lines)


_SENT = re.compile(r"(?<=[a-z0-9)%])[.;]" + chr(92) + "s+(?=[A-Z])|" + chr(92) + "n")
_PAID = re.compile(r"already paid|has been paid|have been paid|(?:is|was|were|been) paid|paid (?:now|immediately|at once)", re.I)
_NOTHING_PAID = re.compile(r"nothing|not (?:yet )?(?:been )?paid|no (?:sol|payment|payout)|unpaid|" + chr(92) + "b0(?:" + chr(92) + ".0+)?" + chr(92) + "s*sol" + chr(92) + "s+paid|0 sol paid", re.I)
_CLAIMS_ZERO = re.compile(r"floor(?: payout| amount| ratio)? (?:is|was|=|of) (?:zero|0(?:\.0+)?)(?!\.?\d)|nothing (?:has been |is |was )?paid", re.I)


def _contradicts_floor(text: str, ctx: "DisputeContext") -> bool:
    """Does any sentence about the floor say something the real amount contradicts? A floor of 0 was never paid
    ('already paid' next to 0 SOL is wrong); a positive floor was paid at once ('nothing paid' is wrong)."""
    for sentence in _SENT.split(unicodedata.normalize("NFKC", text)):   # models emit U+202F etc. inside "0 SOL"
        if "floor" not in sentence.lower():
            continue
        if ctx.floor_amount <= 0 and _PAID.search(sentence) and not _NOTHING_PAID.search(sentence.replace("(already paid)", "(ALREADYPAID)")):
            return True
        if ctx.floor_amount <= 0 and "(already paid)" in sentence.lower():
            return True
        if ctx.floor_amount > 0 and _CLAIMS_ZERO.search(sentence):
            return True
    return False


def _guardrail(report: DisputeReport, ctx: DisputeContext) -> DisputeReport:
    """Deterministic overrides applied to every provider's report: big spreads always go to a human, and
    the summary always states what happened to the floor (a model may omit it or get it wrong)."""
    sentences = [s.strip() for s in _SENT.split(unicodedata.normalize("NFKC", report.summary)) if s.strip()]
    correct_clause = f"{ctx.floor_clause()}."
    if not sentences or not any("floor" in s.lower() for s in sentences):
        report.summary = f"{report.summary.rstrip()} {correct_clause}" if sentences else correct_clause
    else:
        # A sentence that names the floor and gets it wrong is REPLACED, not just followed by a
        # correction - two sentences that contradict each other in the same paragraph is worse
        # than an unexplained gap.
        rebuilt = [correct_clause if ("floor" in s.lower() and _contradicts_floor(s, ctx))
                   else (s if s.endswith((".", "!", "?")) else s + ".")
                   for s in sentences]
        report.summary = " ".join(rebuilt)
    fixed = f"Floor: ratio {ctx.floor_ratio:.3f} = {ctx.floor_amount:.4f} SOL ({'paid immediately' if ctx.floor_amount > 0 else 'nothing paid yet'})"
    report.evidence = [fixed if _contradicts_floor(e, ctx) else e for e in report.evidence]
    if ctx.spread >= HARD_ESCALATE_SPREAD and report.recommendation != "escalate":
        report.recommendation = "escalate"
        report.evidence.append(
            f"Guardrail: spread {ctx.spread:.2f} is at/above the hard limit {HARD_ESCALATE_SPREAD:.2f}; escalated regardless of the model's view."
        )
    return report


def investigate_heuristic(ctx: DisputeContext) -> DisputeReport:
    """Rule-based fallback with the same shape as the AI report. Runs when no provider key is set, or when
    every provider fails. Every branch works on payout RATIOS (the product-independent quantity) and reads its
    vocabulary from the policy's own catalog entry, so it is correct for any product and never contradicts
    the deterministic guardrail (which escalates any spread >= HARD_ESCALATE_SPREAD)."""
    p = ctx.policy
    least_loss = min(ctx.readings, key=lambda r: _ratio(p, r))
    most_loss = max(ctx.readings, key=lambda r: _ratio(p, r))
    evidence = [
        f"{r.source} reports {_fmt(r)} -> payout ratio {_ratio(p, r):.2f} ({'live' if r.live else 'simulated'}"
        f"{', captured ' + r.captured_at if r.captured_at else ''})"
        for r in ctx.readings
    ]
    evidence.append(f"Payout ratio spread is {ctx.spread:.2f} (tolerance {DEFAULT_TOLERANCE:.2f})")

    measured, satellite = ctx.measured, ctx.satellite
    if measured and satellite:
        sat = satellite[0]
        m_ratio = sum(_ratio(p, r) for r in measured) / len(measured)
        s_ratio = _ratio(p, sat)
        m_txt = " and ".join(f"{r.source} ({_fmt(r)})" for r in measured)
        when = f" captured {sat.captured_at}" if sat.captured_at else ""
        if abs(m_ratio - s_ratio) > DEFAULT_TOLERANCE:
            evidence.append(f"{ctx.metric} readings imply ratio {m_ratio:.2f}; satellite implies {s_ratio:.2f}")
            if m_ratio > s_ratio:
                summary = (
                    f"The {ctx.noun} readings ({m_txt}) suggest a significant loss (payout ratio about {m_ratio:.2f}), "
                    f"but satellite imagery of the field{when} shows vegetation that is still healthy "
                    f"(NDVI {sat.observed_mm:.2f}, ratio {s_ratio:.2f}). A {ctx.noun} reading that did not translate into crop damage, "
                    "or a data feed that overstates the event, is worth a reviewer's look before the delta is released."
                )
            else:
                summary = (
                    f"Satellite imagery of the field{when} shows stressed or missing vegetation "
                    f"(NDVI {sat.observed_mm:.2f}, ratio {s_ratio:.2f}) while the {ctx.noun} readings ({m_txt}) "
                    f"suggest little or no loss (ratio about {m_ratio:.2f}). The damage may have a cause the {ctx.noun} "
                    "index does not capture (heat, pests, a different field), so a reviewer should check the imagery before deciding."
                )
            summary += f" {ctx.floor_clause()}."
            return _guardrail(
                DisputeReport(summary=summary, suspected_cause=f"{ctx.noun} vs satellite conflict", evidence=evidence,
                              recommendation="escalate", ai_used=False, model="heuristic"),
                ctx,
            )

    if ctx.status:
        st = ctx.status[0]
        others = [r for r in ctx.readings if r.unit != "status"]
        o_ratio = sum(_ratio(p, r) for r in others) / len(others) if others else 0.0
        s_ratio = _ratio(p, st)
        o_txt = " and ".join(f"{r.source} ({_fmt(r)})" for r in others) or "no other source"
        if others and abs(s_ratio - o_ratio) > DEFAULT_TOLERANCE:
            evidence.append(f"Measured sources imply ratio {o_ratio:.2f}; event status implies {s_ratio:.2f}")
            if s_ratio > o_ratio:
                summary = (
                    f"The event's own status is '{st.status}' (implied ratio {s_ratio:.2f}) while {o_txt} imply little or no loss "
                    f"(ratio about {o_ratio:.2f}). The event may have been called off for a reason the {ctx.noun} reading does not capture "
                    f"(artist, permits, safety, ticket sales), so a reviewer should confirm before the extra {ctx.ceiling_amount - ctx.floor_amount:.4f} SOL is released. "
                    f"{ctx.floor_clause()}."
                )
            else:
                summary = (
                    f"{o_txt} imply a loss (ratio about {o_ratio:.2f}) but the event's status is '{st.status}' (implied ratio {s_ratio:.2f}), "
                    f"i.e. it appears to be going ahead. A reviewer should check before the delta is released. {ctx.floor_clause()}."
                )
            return _guardrail(
                DisputeReport(summary=summary, suspected_cause="event status vs measurement conflict", evidence=evidence,
                              recommendation="escalate", ai_used=False, model="heuristic"),
                ctx,
            )

    # Sources of the same kind (or no cross-domain conflict): decide on the ratio spread alone.
    zeros = [r for r in measured if r.observed_mm == 0]
    nonzero = [r for r in measured if r.observed_mm > 0]
    if zeros and nonzero and ctx.spread > DEFAULT_TOLERANCE:
        zero, other = zeros[0], max(nonzero, key=lambda r: r.observed_mm)
        cause = "sensor dropout"
        summary = (
            f"{zero.source} reports exactly 0 {ctx.unit} while {other.source} reports {other.observed_mm:.1f} {ctx.unit}. "
            f"A flat zero next to a real {ctx.noun} value usually means a data outage or a stuck feed, not a genuine reading. "
            "That zero would change the payout, so it should not be trusted without a human check."
        )
        recommendation = "escalate"
    elif ctx.spread >= HARD_ESCALATE_SPREAD:
        cause = "possible manipulation or fault"
        summary = (
            f"The most-loss source ({most_loss.source}, {_fmt(most_loss)}, ratio {_ratio(p, most_loss):.2f}) and the least-loss source "
            f"({least_loss.source}, {_fmt(least_loss)}, ratio {_ratio(p, least_loss):.2f}) imply payout ratios {ctx.spread:.2f} apart. "
            "That is well beyond normal variance between independent sources and the difference favours a larger payout, "
            "so a reviewer should confirm before release."
        )
        recommendation = "escalate"
    else:
        cause = "model spread"
        summary = (
            f"The sources differ ({_fmt(least_loss)} vs {_fmt(most_loss)}, payout ratios {_ratio(p, least_loss):.2f} vs {_ratio(p, most_loss):.2f}) "
            "but stay in the same range; this looks like ordinary variance between providers rather than a fault. "
            f"{ctx.floor_clause()}; the delta can be settled from fresh readings on the next cycle."
        )
        recommendation = "auto_resolve"
    summary += "" if "floor" in summary.lower() else f" {ctx.floor_clause()}."

    return _guardrail(
        DisputeReport(summary=summary, suspected_cause=cause, evidence=evidence,
                      recommendation=recommendation, ai_used=False, model="heuristic"),
        ctx,
    )


async def investigate_with_llm(ctx: DisputeContext) -> DisputeReport:
    """Ask Claude to investigate. Structured JSON output so the shape is
    guaranteed; anything unexpected raises and the caller falls back."""
    import anthropic  # imported lazily so the backend runs without the SDK/key

    client = anthropic.AsyncAnthropic(timeout=60.0, max_retries=1)
    response = await client.messages.create(
        model=WATCHDOG_MODEL,
        max_tokens=4000,
        system=system_prompt(ctx),
        messages=[{"role": "user", "content": ctx.describe() + "\n\nInvestigate and report."}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": REPORT_SCHEMA}},
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("watchdog model refused the request")
    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text)
    return _guardrail(
        DisputeReport(
            summary=str(data["summary"]),
            suspected_cause=str(data["suspected_cause"]),
            evidence=[str(e) for e in data["evidence"]],
            recommendation=data["recommendation"],
            ai_used=True,
            model=response.model,
        ),
        ctx,
    )


async def investigate_with_groq(ctx: DisputeContext) -> DisputeReport:
    """Same system prompt and same context text as the Claude path; only the network call differs.
    Groq's OpenAI-compatible endpoint (free tier). JSON mode + the schema spelled out in the user
    message, then the same validation and guardrail as every other path."""
    template = {
        "summary": "2-4 plain-language sentences for the reviewer",
        "suspected_cause": "short label, e.g. model spread",
        "evidence": ["3-6 concrete points citing the actual numbers"],
        "recommendation": "auto_resolve or escalate",
    }
    schema_hint = (
        "\n\nInvestigate and report. Reply with ONE JSON object and nothing else, using EXACTLY these four keys "
        "(no others, no nesting beyond the evidence array of strings): " + json.dumps(template)
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {_secret('GROQ_API_KEY')}"},
            json={
                "model": _secret("GROQ_MODEL") or GROQ_DEFAULT_MODEL,
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system_prompt(ctx)},
                    {"role": "user", "content": ctx.describe() + schema_hint},
                ],
            },
        )
    resp.raise_for_status()
    body = resp.json()
    data = json.loads(body["choices"][0]["message"]["content"])
    recommendation = data["recommendation"]
    if recommendation not in ("auto_resolve", "escalate"):
        raise ValueError(f"unexpected recommendation {recommendation!r}")
    return _guardrail(
        DisputeReport(
            summary=str(data["summary"]),
            suspected_cause=str(data["suspected_cause"]),
            evidence=[str(e) for e in data["evidence"]],
            recommendation=recommendation,
            ai_used=True,
            model=f"groq:{body.get('model', GROQ_DEFAULT_MODEL)}",
        ),
        ctx,
    )


def active_provider() -> str:
    """Which explanation provider `investigate()` will try first (same precedence: Groq, Claude, rule-based). For the startup banner."""
    if _secret("GROQ_API_KEY"):
        return f"Groq ({_secret('GROQ_MODEL') or GROQ_DEFAULT_MODEL})"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return f"Claude ({WATCHDOG_MODEL})"
    return "rule-based (no GROQ_API_KEY / ANTHROPIC_API_KEY)"


async def investigate(policy: Policy, readings: list[SourceReading], floor_ratio: float, ceiling_ratio: float) -> DisputeReport:
    """Entry point used by main.py. Never raises - the demo must not stall
    because the model is unreachable. Never returns an amount."""
    ctx = DisputeContext(policy, readings, floor_ratio, ceiling_ratio)
    # Precedence: Groq (free tier) -> Claude -> rule-based. A failing provider falls through to the next.
    providers = []
    if _secret("GROQ_API_KEY"):
        providers.append(("groq", investigate_with_groq))
    if os.environ.get("ANTHROPIC_API_KEY"):
        providers.append(("anthropic", investigate_with_llm))
    if not providers:
        print("[watchdog] no GROQ_API_KEY / ANTHROPIC_API_KEY set - using heuristic investigator")
        return investigate_heuristic(ctx)
    failures = []
    for name, call in providers:
        try:
            return await call(ctx)
        except Exception as e:  # noqa: BLE001 - rate limit, network, refusal, bad JSON...
            print(f"[watchdog] {name} call failed ({e!r})")
            failures.append(f"{name}: {type(e).__name__}")
    report = investigate_heuristic(ctx)
    report.evidence.append(f"AI investigation unavailable ({'; '.join(failures)}); rule-based analysis shown instead.")
    return report
