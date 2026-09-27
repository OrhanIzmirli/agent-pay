"""
Original mock implementation — kept as a fallback. If the live demo wifi
is bad or devnet RPC is flaky, swap the import in main.py from
`app.payment` to `app.payment_mock` and everything still works, just
without real on-chain confirmation (signatures start with `MOCK`).

Exposes the same surface as payment.py, including the escrow helpers, so
the swap is a one-line change.
"""

import time
import uuid
from dataclasses import dataclass

from app.escrow import get_escrow, list_escrows, mark_released, mark_voided, open_escrow  # noqa: F401 - re-exported

AGENT_PUBLIC_KEY = "MOCKAgentWallet1111111111111111111111111111"
SERVICE_PUBLIC_KEY = "MOCKServiceWallet111111111111111111111111111"

_balance_sol = 0.5


@dataclass
class PaymentResult:
    success: bool
    tx_signature: str
    amount_sol: float
    to_address: str
    confirmed_at: float


async def ensure_funded(min_sol: float = 0.1) -> None:
    return None


async def ensure_rent_exempt(address: str) -> None:
    return None


async def send_payment(amount_sol: float, to_address: str) -> PaymentResult:
    global _balance_sol
    fake_signature = f"MOCK{uuid.uuid4().hex[:44]}"
    _balance_sol -= amount_sol
    return PaymentResult(
        success=True,
        tx_signature=fake_signature,
        amount_sol=amount_sol,
        to_address=to_address,
        confirmed_at=time.time(),
    )


async def get_wallet_balance(address: str) -> float:
    return _balance_sol


async def get_payment_history(address: str) -> list[dict]:
    return []


async def release_escrow(policy_id: str, resolved_by: str = "human", amount_sol: float | None = None) -> dict:
    entry = get_escrow(policy_id)
    if not entry or entry["status"] != "pending":
        raise ValueError(f"no pending escrow for policy {policy_id}")
    amount = entry["amount_sol"] if amount_sol is None else min(amount_sol, entry["amount_sol"])
    if amount <= 0:
        return mark_voided(policy_id, resolved_by)
    payment = await send_payment(amount, entry["payee"])
    return mark_released(policy_id, payment.tx_signature, resolved_by, amount)


def void_escrow(policy_id: str, resolved_by: str = "human") -> dict:
    return mark_voided(policy_id, resolved_by)
