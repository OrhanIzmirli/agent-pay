"""
Payment module — real Solana DEVNET implementation.

This used to be a mock (kept in `payment_mock.py` for reference / demo
fallback). It now actually talks to Solana devnet:

  - the AGENT keypair (the wallet that pays) is loaded from
    `AGENT_WALLET_KEYPAIR` (JSON array of ints, same format `solana-keygen`
    produces) or generated on first run and saved to `wallet.json`
  - a separate SERVICE / treasury keypair (the wallet that gets paid) is
    generated the same way into `service_wallet.json`; main.py uses its
    address as the default recipient unless `SERVICE_WALLET_ADDRESS` is set
  - both *.json files hold private keys and are gitignored
  - `send_payment()` sends a real transfer and waits for confirmation
  - `get_wallet_balance()` / `get_payment_history()` read straight from
    chain — this is why no database is needed for payment history

Function names and return shape are unchanged from the mock, so nothing
in `main.py` / `decision.py` needed to change.

Parametric-insurance pivot: `send_payment()` / `get_wallet_balance()` /
`ensure_funded()` are untouched. Added `open_escrow()` / `release_escrow()`
/ `void_escrow()` on top of the JSON ledger in escrow.py. The ledger is
off-chain by default (AGENT_PAY_ONCHAIN_ESCROW=1 switches to the PDA vault in solana_vault.py), but the two money
movements - floor payout and delta release - are real devnet transfers.
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from app.escrow import annotate, get_escrow, list_escrows, mark_released, mark_voided, open_escrow  # noqa: F401 - re-exported

from solana.rpc.async_api import AsyncClient
from solana.exceptions import SolanaRpcException
from solana.rpc.commitment import Confirmed
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.message import Message
from solders.transaction import Transaction

DEVNET_RPC = "https://api.devnet.solana.com"
LAMPORTS_PER_SOL = 1_000_000_000

_ROOT = Path(__file__).parent.parent
WALLET_PATH = _ROOT / "wallet.json"                  # agent (payer)
SERVICE_WALLET_PATH = _ROOT / "service_wallet.json"  # service / treasury (payee)


@dataclass
class PaymentResult:
    success: bool
    tx_signature: str
    amount_sol: float
    to_address: str
    confirmed_at: float  # unix timestamp


def _load_or_create_keypair(path: Path, env_var: str | None = None) -> Keypair:
    """Load a devnet keypair from `env_var` (JSON array of ints) if set,
    else from `path`, else create one and save it to `path`."""
    env_key = os.environ.get(env_var) if env_var else None
    if env_key:
        secret = json.loads(env_key)
        return Keypair.from_bytes(bytes(secret))

    if path.exists():
        secret = json.loads(path.read_text())
        return Keypair.from_bytes(bytes(secret))

    kp = Keypair()
    path.write_text(json.dumps(list(bytes(kp))))
    print(f"[payment] generated new keypair -> {path.name} (address {kp.pubkey()})")
    return kp


# The agent's own wallet: this is what pays.
_keypair = _load_or_create_keypair(WALLET_PATH, "AGENT_WALLET_KEYPAIR")
AGENT_PUBLIC_KEY = str(_keypair.pubkey())

# The service / treasury wallet: this is what gets paid. Only its public
# key is used by the app; the keypair file exists so the team can inspect
# / sweep it later. Override the recipient with SERVICE_WALLET_ADDRESS.
_service_keypair = _load_or_create_keypair(SERVICE_WALLET_PATH, "SERVICE_WALLET_KEYPAIR")
SERVICE_PUBLIC_KEY = str(_service_keypair.pubkey())


async def ensure_funded(min_sol: float = 0.1) -> None:
    """Airdrop devnet SOL into the agent wallet if balance is low. Devnet
    faucet is rate-limited, so this is best-effort — call it once at
    startup, not per-request."""
    async with AsyncClient(DEVNET_RPC) as client:
        bal = await client.get_balance(_keypair.pubkey(), commitment=Confirmed)
        current_sol = bal.value / LAMPORTS_PER_SOL
        if current_sol < min_sol:
            await client.request_airdrop(_keypair.pubkey(), int(1 * LAMPORTS_PER_SOL))


async def ensure_rent_exempt(address: str) -> None:
    """Solana rejects a transfer that would leave an account below the
    rent-exempt minimum (~0.00089 SOL). A brand-new service wallet has 0
    SOL, so the very first micropayment (0.0001-0.002 SOL) would fail with
    "insufficient funds for rent". Top it up once from the agent wallet at
    startup; after that, arbitrarily small payments go through."""
    to_pubkey = Pubkey.from_string(address)
    async with AsyncClient(DEVNET_RPC) as client:
        bal = (await client.get_balance(to_pubkey, commitment=Confirmed)).value
        min_rent = (await client.get_minimum_balance_for_rent_exemption(0)).value
    if bal >= min_rent:
        return
    top_up = min_rent - bal
    print(f"[payment] service wallet {address} is below rent-exempt minimum; "
          f"one-off top-up of {top_up / LAMPORTS_PER_SOL:.6f} SOL from agent wallet")
    sig = await _send_lamports(top_up, to_pubkey)
    print(f"[payment] top-up confirmed: {sig}")


class PaymentPending(Exception):
    """A transaction was (or may have been) submitted but its outcome could not be established.
    NOT a failure: the payment may already have landed, so callers must never blindly send another one.
    `signature` is known before the send, so it can always be re-checked later."""

    def __init__(self, signature: str, amount_sol: float | None = None, to_address: str | None = None,
                 last_valid_block_height: int | None = None):
        super().__init__(f"payment {signature} submitted but not yet confirmed")
        self.signature, self.amount_sol, self.to_address = signature, amount_sol, to_address
        self.last_valid_block_height = last_valid_block_height


CONFIRM_ATTEMPTS = 8       # status polls after a send
CONFIRM_BACKOFF = 1.0      # seconds, doubled per attempt
CONFIRM_BACKOFF_CAP = 4.0


async def check_signature(signature: str, last_valid_block_height: int | None = None, attempts: int = 1) -> str:
    """Where is this transaction? 'confirmed' | 'failed' (landed with an error) | 'expired' (its blockhash
    is dead and it never landed: it can never land) | 'unknown' (RPC unreachable or still in flight).
    Retries with exponential backoff; every RPC error just means 'not established yet'."""
    for i in range(attempts):
        try:
            async with AsyncClient(DEVNET_RPC) as client:
                # Height first, THEN status: if the height is already past the blockhash's validity and the
                # status afterwards is still empty, the transaction cannot have landed in between.
                height = None
                if last_valid_block_height is not None:
                    height = (await client.get_block_height(Confirmed)).value
                st = (await client.get_signature_statuses([Signature.from_string(signature)], search_transaction_history=True)).value[0]
                if st is not None:
                    if st.err:
                        return "failed"
                    if str(st.confirmation_status).lower().endswith(("confirmed", "finalized")):
                        return "confirmed"
                elif height is not None and height > last_valid_block_height:
                    return "expired"
        except Exception as e:  # noqa: BLE001 - rate limit / network: try again, never conclude anything
            print(f"[payment] signature check {i + 1}/{attempts} for {signature[:12]}... inconclusive: {type(e).__name__}")
        if i < attempts - 1:
            await asyncio.sleep(min(CONFIRM_BACKOFF * 2 ** i, CONFIRM_BACKOFF_CAP))
    return "unknown"


async def _send_lamports(lamports: int, to_pubkey: Pubkey) -> str:
    """Build, sign, send and confirm a plain SOL transfer. Returns the signature, raises PaymentPending
    when the outcome cannot be established, RuntimeError when it definitely did not (or cannot) land."""
    async with AsyncClient(DEVNET_RPC) as client:
        latest_blockhash = await client.get_latest_blockhash()
        ix = transfer(
            TransferParams(
                from_pubkey=_keypair.pubkey(),
                to_pubkey=to_pubkey,
                lamports=lamports,
            )
        )
        msg = Message.new_with_blockhash(
            [ix], _keypair.pubkey(), latest_blockhash.value.blockhash
        )
        tx = Transaction([_keypair], msg, latest_blockhash.value.blockhash)

        signature = str(tx.signatures[0])  # deterministic: known before (and whatever happens to) the send
        last_valid = latest_blockhash.value.last_valid_block_height
        try:
            await client.send_transaction(tx)
        except SolanaRpcException as e:
            # Transport-level failure (429, timeout, reset): the node may or may not have received it.
            print(f"[payment] send of {signature[:12]}... raised {type(e).__name__}; checking status before concluding anything")
        # any other exception = the node rejected it (e.g. preflight: insufficient funds): definitely not sent
    state = await check_signature(signature, last_valid, attempts=CONFIRM_ATTEMPTS)
    if state == "confirmed":
        return signature
    if state == "failed":
        raise RuntimeError(f"transaction {signature} landed but failed on-chain")
    if state == "expired":
        raise RuntimeError(f"transaction {signature} expired before landing (safe to retry)")
    raise PaymentPending(signature, last_valid_block_height=last_valid)


async def send_payment(amount_sol: float, to_address: str) -> PaymentResult:
    """Send `amount_sol` SOL to `to_address` on devnet and wait for confirmation."""
    # round(), not int(): 0.0001 * 1e9 is 100000.00000000001 in floating point.
    lamports = round(amount_sol * LAMPORTS_PER_SOL)
    to_pubkey = Pubkey.from_string(to_address)
    try:
        signature = await _send_lamports(lamports, to_pubkey)
    except PaymentPending as p:
        p.amount_sol, p.to_address = amount_sol, to_address
        raise

    return PaymentResult(
        success=True,
        tx_signature=signature,
        amount_sol=amount_sol,
        to_address=to_address,
        confirmed_at=time.time(),
    )


async def get_wallet_balance(address: str) -> float:
    async with AsyncClient(DEVNET_RPC) as client:
        pubkey = Pubkey.from_string(address)
        bal = await client.get_balance(pubkey, commitment=Confirmed)
        return bal.value / LAMPORTS_PER_SOL


async def get_payment_history(address: str) -> list[dict]:
    """Re-query past transactions straight from chain — no DB needed."""
    async with AsyncClient(DEVNET_RPC) as client:
        pubkey = Pubkey.from_string(address)
        # Confirmed (not the default finalized) so a payment made a second
        # ago already shows up in the history widget.
        sigs = await client.get_signatures_for_address(pubkey, limit=20, commitment=Confirmed)
        return [
            {"signature": str(s.signature), "slot": s.slot, "err": s.err}
            for s in sigs.value
        ]


# ---------------------------------------------------------------------------
# Escrow (ledger in escrow.py; the release is a real on-chain transfer)
# ---------------------------------------------------------------------------

async def _offchain_release_escrow(policy_id: str, resolved_by: str = "human", amount_sol: float | None = None) -> dict:
    """Send the escrowed delta to the payee (second real transaction) and
    mark the ledger entry released. `amount_sol` (optional) releases only
    part of the delta and voids the rest. Raises if nothing is pending."""
    entry = get_escrow(policy_id)
    if not entry or entry["status"] != "pending":
        raise ValueError(f"no pending escrow for policy {policy_id}")
    amount = entry["amount_sol"] if amount_sol is None else min(amount_sol, entry["amount_sol"])
    if amount <= 0:
        return mark_voided(policy_id, resolved_by)
    prior = entry.get("pending_release")
    if prior:  # an earlier release was submitted but never confirmed: settle THAT one, never send a second
        state = await check_signature(prior["signature"], prior.get("last_valid_block_height"), attempts=3)
        if state == "confirmed":
            return mark_released(policy_id, prior["signature"], resolved_by, prior["amount_sol"])
        if state == "unknown":
            raise PaymentPending(prior["signature"], prior["amount_sol"], entry["payee"], prior.get("last_valid_block_height"))
        annotate(policy_id, pending_release=None)  # failed / expired: it can never land, a fresh send is safe
    try:
        payment = await send_payment(amount, entry["payee"])
    except PaymentPending as p:
        annotate(policy_id, pending_release={"signature": p.signature, "amount_sol": amount, "last_valid_block_height": p.last_valid_block_height})
        raise
    return mark_released(policy_id, payment.tx_signature, resolved_by, amount)


def _offchain_void_escrow(policy_id: str, resolved_by: str = "human") -> dict:
    """Nothing moves on-chain - the delta never left the agent wallet."""
    return mark_voided(policy_id, resolved_by)


release_escrow = _offchain_release_escrow
void_escrow = _offchain_void_escrow

# AGENT_PAY_ONCHAIN_ESCROW=1 swaps in the program-controlled PDA escrow (app/solana_vault.py); unset/0
# keeps the JSON-ledger behaviour above as the safe fallback. Must stay at the bottom of this module.
if os.environ.get("AGENT_PAY_ONCHAIN_ESCROW", "0").strip().lower() in ("1", "true", "yes"):
    from app.solana_vault import open_escrow, release_escrow, void_escrow  # noqa: E402,F401,F811
    print("[payment] escrow backend: on-chain PDA vault")
else:
    print("[payment] escrow backend: off-chain JSON ledger")
