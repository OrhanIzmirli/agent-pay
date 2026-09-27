"""
Off-chain escrow ledger (escrow_ledger.json).

This is the JSON ledger. It is the default escrow backend and the fallback; with
AGENT_PAY_ONCHAIN_ESCROW=1 the delta is instead locked in a program-controlled PDA
(app/solana_vault.py, program agent_pay_vault) and this ledger only indexes it.
In the JSON-ledger mode the two money movements that matter are real devnet
transactions:

  1. the floor payout          -> payment.send_payment() at evaluate time
  2. the released delta        -> payment.release_escrow() at resolve time

A voided escrow moves nothing (the SOL never left the agent wallet).

payment.py / payment_mock.py wrap these helpers so main.py can keep swapping
the payment backend with a single import change.
"""

import json
import time
from pathlib import Path

_ROOT = Path(__file__).parent.parent
LEDGER_PATH = _ROOT / "escrow_ledger.json"

PENDING, RELEASED, VOIDED = "pending", "released", "voided"


def _load() -> dict[str, dict]:
    if not LEDGER_PATH.exists():
        return {}
    try:
        return json.loads(LEDGER_PATH.read_text() or "{}")
    except json.JSONDecodeError:
        # A half-written file shouldn't take the demo down; start fresh but
        # keep the corrupt copy around for inspection.
        LEDGER_PATH.rename(LEDGER_PATH.with_suffix(".corrupt.json"))
        return {}


def _save(ledger: dict[str, dict]) -> None:
    tmp = LEDGER_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, indent=2))
    tmp.replace(LEDGER_PATH)


def open_escrow(policy_id: str, payee: str, amount_sol: float, reason: str) -> dict:
    """Record a pending delta. One escrow per policy at a time."""
    if amount_sol <= 0:
        raise ValueError("escrow amount must be positive")
    ledger = _load()
    existing = ledger.get(policy_id)
    if existing and existing["status"] == PENDING:
        raise ValueError(f"policy {policy_id} already has a pending escrow")
    entry = {
        "policy_id": policy_id,
        "payee": payee,
        "amount_sol": amount_sol,
        "reason": reason,
        "status": PENDING,
        "opened_at": time.time(),
        "resolved_at": None,
        "resolved_by": None,          # "human" | "auto"
        "release_tx_signature": None,
    }
    ledger[policy_id] = entry
    _save(ledger)
    print(f"[escrow] opened {amount_sol} SOL for policy {policy_id}: {reason}")
    return entry


def get_escrow(policy_id: str) -> dict | None:
    return _load().get(policy_id)


def list_escrows() -> list[dict]:
    return list(_load().values())


def _resolve(policy_id: str, status: str, resolved_by: str, tx_signature: str | None, released_amount_sol: float) -> dict:
    ledger = _load()
    entry = ledger.get(policy_id)
    if not entry:
        raise ValueError(f"no escrow for policy {policy_id}")
    if entry["status"] != PENDING:
        raise ValueError(f"escrow for policy {policy_id} is already {entry['status']}")
    entry.update(
        status=status,
        resolved_at=time.time(),
        resolved_by=resolved_by,
        release_tx_signature=tx_signature,
        released_amount_sol=released_amount_sol,
        voided_amount_sol=round(entry["amount_sol"] - released_amount_sol, 9),
    )
    _save(ledger)
    print(f"[escrow] {status} {released_amount_sol}/{entry['amount_sol']} SOL for policy {policy_id} (by {resolved_by})")
    return entry


def mark_released(policy_id: str, tx_signature: str, resolved_by: str = "human", amount_sol: float | None = None) -> dict:
    """Call AFTER the delta transfer confirmed on-chain. `amount_sol` < the
    escrowed amount records a partial release (rest voided)."""
    entry = get_escrow(policy_id)
    if not entry:
        raise ValueError(f"no escrow for policy {policy_id}")
    released = entry["amount_sol"] if amount_sol is None else min(amount_sol, entry["amount_sol"])
    return _resolve(policy_id, RELEASED, resolved_by, tx_signature, released)


def mark_voided(policy_id: str, resolved_by: str = "human") -> dict:
    return _resolve(policy_id, VOIDED, resolved_by, None, 0.0)


def annotate(policy_id: str, **fields) -> dict:
    """Attach extra fields (e.g. the on-chain PDA) to a ledger entry."""
    ledger = _load()
    entry = ledger.get(policy_id)
    if not entry:
        raise ValueError(f"no escrow for policy {policy_id}")
    entry.update(fields)
    _save(ledger)
    return entry
