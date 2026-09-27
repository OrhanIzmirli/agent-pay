"""
Demo / smoke test against a RUNNING backend (uvicorn app.main:app --port 8000).

    python demo_scenarios.py            # both scenarios, simulated readings
    python demo_scenarios.py --live     # scenario 1 uses real Open-Meteo pulls
    python demo_scenarios.py --base http://localhost:8000

Scenario 1 - agreement: both sources report similar rainfall -> the full
             formula payout goes out in one transaction, dispute_status=none,
             the watchdog is never called.
Scenario 2 - disagreement: source A and B are forced far apart -> the floor
             is paid immediately, the delta is escrowed, the watchdog explains
             what it sees, then a human releases the escrow (second real
             transaction) and we check the wallet balance moved by exactly
             floor + delta (+ two network fees).

Every real transaction is printed as a Solana explorer devnet link.
"""

import argparse
import sys
import time

import httpx

FEE_SOL = 0.000005  # devnet base fee per signature
TOLERANCE_SOL = 0.00002


def hr(title: str) -> None:
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


def tx_link(sig: str | None) -> str:
    return f"https://explorer.solana.com/tx/{sig}?cluster=devnet" if sig else "(no transaction)"


def show_readings(ev: dict) -> None:
    for r in ev["readings"]:
        tag = "live" if r["live"] else "SIMULATED"
        print(f"    {r['source']:<28} {r['observed_mm']:>8.1f} mm   [{tag}]")
    print(f"    payout_ratio_floor   = {ev['payout_ratio_floor']:.3f}")
    print(f"    payout_ratio_ceiling = {ev['payout_ratio_ceiling']:.3f}")
    print(f"    spread               = {ev['spread']:.3f}  (tolerance {ev['tolerance']})")
    print(f"    dispute_status       = {ev['dispute_status']}")


def show_dispute(ev: dict) -> None:
    d = ev.get("dispute")
    if not d:
        print("    (watchdog not called)")
        return
    who = f"AI ({d['model']})" if d["ai_used"] else "heuristic fallback (no ANTHROPIC_API_KEY or model unreachable)"
    print(f"    watchdog: {who}")
    print(f"    suspected cause: {d['suspected_cause']}")
    print(f"    recommendation:  {d['recommendation']}")
    print("    explanation:")
    for line in d["summary"].split(". "):
        if line.strip():
            print(f"      {line.strip().rstrip('.')}.")
    print("    evidence:")
    for e in d["evidence"]:
        print(f"      - {e}")


class Api:
    def __init__(self, base: str):
        self.c = httpx.Client(base_url=base, timeout=180.0)

    def balance(self) -> float:
        return self.c.get("/wallet/balance").json()["balance_sol"]

    def create(self, **kw) -> dict:
        r = self.c.post("/policy", json=kw)
        r.raise_for_status()
        return r.json()

    def evaluate(self, pid: str, simulate: list[dict] | None = None) -> dict:
        r = self.c.post(f"/policy/{pid}/evaluate", json={"simulate": simulate} if simulate else None)
        if r.status_code != 200:
            sys.exit(f"evaluate failed ({r.status_code}): {r.text}")
        return r.json()

    def resolve(self, pid: str, release: bool) -> dict:
        r = self.c.post(f"/policy/{pid}/resolve", params={"release": str(release).lower()})
        if r.status_code != 200:
            sys.exit(f"resolve failed ({r.status_code}): {r.text}")
        return r.json()


def scenario_agreement(api: Api, live: bool) -> None:
    hr("SCENARIO 1 - sources agree -> instant full formula payout, no AI")
    policy = api.create(region="Warsaw, PL", lat=52.23, lon=21.01, trigger_mm=40, exit_mm=10, sum_insured_sol=0.01)
    print(f"  policy {policy['id']}: trigger {policy['trigger_mm']} mm, exit {policy['exit_mm']} mm, "
          f"sum insured {policy['sum_insured_sol']} SOL, window {policy['window_start']}..{policy['window_end']}")
    before = api.balance()
    sim = None if live else [{"mm": 24.0, "label": "A"}, {"mm": 25.5, "label": "B"}]
    t0 = time.time()
    ev = api.evaluate(policy["id"], sim)
    print(f"  evaluated in {time.time() - t0:.1f}s ({'live Open-Meteo' if live else 'simulated'} readings)")
    show_readings(ev)
    print(f"  paid now: {ev['floor_amount_sol']} SOL  -> {tx_link(ev['floor_tx_signature'])}")
    print(f"  escrowed: {ev['escrow_amount_sol']} SOL")
    show_dispute(ev)
    after = api.balance()
    print(f"  wallet: {before:.6f} -> {after:.6f} SOL (moved {before - after:.6f})")
    if live and ev["dispute_status"] != "none":
        print("  NOTE: the live products disagreed today - that's scenario 2 happening for real.")
    else:
        assert ev["dispute_status"] == "none", ev["dispute_status"]
        assert ev["dispute"] is None, "watchdog should not run when sources agree"
        assert ev["escrow"] is None
        expected = ev["floor_amount_sol"] + (FEE_SOL if ev["floor_tx_signature"] else 0)
        assert abs((before - after) - expected) < TOLERANCE_SOL, f"balance moved {before - after}, expected ~{expected}"
        print("  OK: single formula-driven payout, dispute_status=none, no AI call")


def scenario_disagreement(api: Api) -> None:
    hr("SCENARIO 2 - sources disagree -> pay the floor, escrow the delta, AI explains")
    policy = api.create(region="Warsaw, PL", lat=52.23, lon=21.01, trigger_mm=40, exit_mm=10, sum_insured_sol=0.01)
    print(f"  policy {policy['id']}: trigger 40 mm, exit 10 mm, sum insured 0.01 SOL")
    before = api.balance()
    # A says 30 mm (ratio 0.333), B says 12 mm (ratio 0.933): spread 0.6 -> escalated.
    # Use e.g. 30 vs 24 (spread 0.2) to see "investigating" instead.
    t0 = time.time()
    ev = api.evaluate(policy["id"], [{"mm": 30.0, "label": "A"}, {"mm": 12.0, "label": "B"}])
    print(f"  evaluated in {time.time() - t0:.1f}s")
    show_readings(ev)
    print(f"  STEP 1 paid now (floor):  {ev['floor_amount_sol']} SOL -> {tx_link(ev['floor_tx_signature'])}")
    print(f"  STEP 2 escrowed (delta):  {ev['escrow_amount_sol']} SOL  [{ev['escrow']['status']}]")
    show_dispute(ev)
    assert ev["dispute_status"] in ("investigating", "escalated"), ev["dispute_status"]
    assert ev["escrow"] and ev["escrow"]["status"] == "pending"
    mid = api.balance()
    print(f"  wallet after floor: {before:.6f} -> {mid:.6f} SOL (moved {before - mid:.6f})")

    print("\n  Human reviewer resolves: RELEASE the escrowed delta")
    res = api.resolve(policy["id"], release=True)
    print(f"  released {res['escrow']['released_amount_sol']} SOL -> {tx_link(res['release_tx_signature'])}")
    print(f"  dispute_status = {res['dispute_status']}, escrow = {res['escrow']['status']}")
    after = api.balance()
    total_paid = ev["floor_amount_sol"] + ev["escrow_amount_sol"]
    fees = FEE_SOL * (1 if ev["floor_tx_signature"] else 0) + FEE_SOL
    print(f"  wallet: {before:.6f} -> {after:.6f} SOL (moved {before - after:.6f}; expected {total_paid:.6f} + fees {fees:.6f})")
    assert abs((before - after) - (total_paid + fees)) < TOLERANCE_SOL, "final balance does not match floor + delta + fees"
    assert abs(total_paid - ev["ceiling_amount_sol"]) < 1e-9, "floor + delta must equal the ceiling payout"
    print("  OK: floor paid instantly, delta escrowed then released, final balance correct")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--live", action="store_true", help="scenario 1 pulls real Open-Meteo readings")
    args = ap.parse_args()
    api = Api(args.base)
    try:
        api.c.get("/").raise_for_status()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"backend not reachable at {args.base}: {e}")
    scenario_agreement(api, args.live)
    scenario_disagreement(api)
    hr("DONE - open the explorer links above during the pitch")


if __name__ == "__main__":
    main()
