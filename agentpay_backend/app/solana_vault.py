"""
On-chain escrow: the disputed delta sits in a program-controlled PDA (agent_pay_vault, devnet).

Same three functions as the JSON-ledger escrow in payment.py, selected with
AGENT_PAY_ONCHAIN_ESCROW=1 (see the bottom of payment.py). Instructions are built by hand from the
program's IDL (app/idl/agent_pay_vault.json); no anchorpy needed.

Design notes
- The JSON ledger is still written for every escrow, so get_escrow()/list_escrows() and the API
  shape are unchanged; the ledger entry gains escrow_pda / escrow_explorer_link / tx signatures.
  The chain is where the money is; the ledger is the index.
- If the on-chain open fails (RPC down, PDA already used by an earlier escrow of the same policy),
  the escrow falls back to the plain off-chain ledger (onchain=False) instead of failing an
  evaluation whose floor was already paid. Such entries resolve through the off-chain path.
- The PDA keeps its rent-exempt reserve (~0.0016 SOL) after resolution so the Released/Voided
  record survives; only the held amount moves.
"""

import asyncio
import hashlib
import json
import os
from pathlib import Path

from concurrent.futures import ThreadPoolExecutor

from solana.exceptions import SolanaRpcException
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solders.instruction import AccountMeta, Instruction
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.transaction import Transaction

import app.escrow as ledger
from app import payment as _pay  # payment.py imports this module last, so its keypair exists already
from app.escrow import get_escrow, mark_voided  # noqa: F401 - mark_voided used for the off-chain fallback

_IDL = json.loads((Path(__file__).parent / "idl" / "agent_pay_vault.json").read_text())
PROGRAM_ID = Pubkey.from_string(os.environ.get("AGENT_PAY_VAULT_PROGRAM_ID") or _IDL["address"])
_DISC = {i["name"]: bytes(i["discriminator"]) for i in _IDL["instructions"]}
_EXPLORER = "https://explorer.solana.com/{kind}/{id}?cluster=devnet"


def policy_hash(policy_id: str) -> bytes:
    return hashlib.sha256(policy_id.encode()).digest()


def escrow_pda(policy_id: str) -> Pubkey:
    return Pubkey.find_program_address([b"escrow", policy_hash(policy_id)], PROGRAM_ID)[0]


async def _send_async(instruction: Instruction) -> str:
    """Send and confirm. Same contract as payment._send_lamports: returns the signature, raises
    PaymentPending when the outcome can't be established, RuntimeError when it definitely didn't land."""
    kp = _pay._keypair
    async with AsyncClient(_pay.DEVNET_RPC, commitment=Confirmed, timeout=30) as client:
        latest = (await client.get_latest_blockhash()).value
        bh = latest.blockhash
        tx = Transaction([kp], Message.new_with_blockhash([instruction], kp.pubkey(), bh), bh)
        sig = str(tx.signatures[0])  # known before the send
        try:
            await client.send_transaction(tx)  # preflight simulation raises with the program's error
        except SolanaRpcException as e:
            print(f"[vault] send of {sig[:12]}... raised {type(e).__name__}; checking status before concluding anything")
    state = await _pay.check_signature(sig, latest.last_valid_block_height, attempts=_pay.CONFIRM_ATTEMPTS)
    if state == "confirmed":
        return sig
    if state in ("failed", "expired"):
        raise RuntimeError(f"vault transaction {sig} {'landed but failed' if state == 'failed' else 'expired before landing (safe to retry)'}")
    raise _pay.PaymentPending(sig, last_valid_block_height=latest.last_valid_block_height)


def _send(instruction: Instruction) -> str:
    """Blocking variant for the synchronous open/void entry points (main.py calls them without await):
    runs on a private event loop in a worker thread, so it works from inside FastAPI's running loop."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _send_async(instruction)).result()


def _check_sync(prior: dict) -> str:
    """Blocking signature re-check for the synchronous void path (private loop in a worker thread)."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _pay.check_signature(prior["signature"], prior.get("last_valid_block_height"), 3)).result()


def _u64(n: int) -> bytes:
    return n.to_bytes(8, "little")


def _links(pda: Pubkey) -> dict:
    return {"escrow_pda": str(pda), "escrow_explorer_link": _EXPLORER.format(kind="address", id=pda)}


def open_escrow(policy_id: str, payee_pubkey: str, amount_sol: float, reason: str) -> dict:
    """Lock the delta in the policy's PDA. Falls back to the off-chain ledger if that fails."""
    entry = ledger.open_escrow(policy_id, payee_pubkey, amount_sol, reason)  # also enforces one pending escrow
    lamports = round(amount_sol * _pay.LAMPORTS_PER_SOL)
    pda = escrow_pda(policy_id)
    try:
        authority = _pay._keypair.pubkey()
        ix = Instruction(PROGRAM_ID, _DISC["initialize_escrow"] + policy_hash(policy_id) + _u64(lamports) + bytes(Pubkey.from_string(payee_pubkey)), [
            AccountMeta(pda, is_signer=False, is_writable=True),
            AccountMeta(authority, is_signer=True, is_writable=True),
            AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        ])
        sig = _send(ix)
    except _pay.PaymentPending as p:
        # It may well have landed: treat the escrow as on-chain (never as off-chain, which could strand the
        # locked funds). If it never lands, a later release/void fails loudly and no money has moved.
        print(f"[vault] open of {policy_id} unconfirmed ({p.signature}); recorded as on-chain, unconfirmed")
        return ledger.annotate(policy_id, onchain=True, open_unconfirmed=True, open_tx_signature=p.signature, lamports=lamports, **_links(pda))
    except Exception as e:  # noqa: BLE001 - never fail an evaluation whose floor is already paid
        print(f"[vault] on-chain open failed for {policy_id}, using off-chain ledger: {e!r}")
        return ledger.annotate(policy_id, onchain=False, onchain_error=str(e)[:200])
    print(f"[vault] locked {lamports} lamports for {policy_id} in PDA {pda} ({sig})")
    return ledger.annotate(policy_id, onchain=True, open_tx_signature=sig, lamports=lamports, **_links(pda))


def _resolve_ix(name: str, policy_id: str, payee: str, args: bytes) -> Instruction:
    return Instruction(PROGRAM_ID, _DISC[name] + args, [
        AccountMeta(escrow_pda(policy_id), is_signer=False, is_writable=True),
        AccountMeta(_pay._keypair.pubkey(), is_signer=True, is_writable=True),
        AccountMeta(Pubkey.from_string(payee), is_signer=False, is_writable=True),
    ])


async def release_escrow(policy_id: str, resolved_by: str = "human", amount_sol: float | None = None) -> dict:
    """Pay the held delta (or part of it; the rest returns to the authority) out of the PDA to the payee."""
    entry = get_escrow(policy_id)
    if not entry or entry["status"] != "pending":
        raise ValueError(f"no pending escrow for policy {policy_id}")
    held = entry["amount_sol"]
    amount = held if amount_sol is None else min(amount_sol, held)
    if amount <= 0:
        return void_escrow(policy_id, resolved_by)
    if not entry.get("onchain"):
        return await _pay._offchain_release_escrow(policy_id, resolved_by, amount_sol)
    lamports = round(amount * _pay.LAMPORTS_PER_SOL)
    prior = entry.get("pending_release")
    if prior:  # settle the earlier unconfirmed release instead of sending another
        state = await _pay.check_signature(prior["signature"], prior.get("last_valid_block_height"), attempts=3)
        if state == "confirmed":
            return ledger.mark_released(policy_id, prior["signature"], resolved_by, prior["amount_sol"])
        if state == "unknown":
            raise _pay.PaymentPending(prior["signature"], prior["amount_sol"], entry["payee"], prior.get("last_valid_block_height"))
        ledger.annotate(policy_id, pending_release=None)
    try:
        sig = await _send_async(_resolve_ix("release_escrow", policy_id, entry["payee"], _u64(lamports)))
    except _pay.PaymentPending as p:
        ledger.annotate(policy_id, pending_release={"signature": p.signature, "amount_sol": amount, "last_valid_block_height": p.last_valid_block_height})
        raise
    return ledger.mark_released(policy_id, sig, resolved_by, amount)


def void_escrow(policy_id: str, resolved_by: str = "human") -> dict:
    """Return the held delta from the PDA to the authority."""
    entry = get_escrow(policy_id)
    if not entry or entry["status"] != "pending":
        raise ValueError(f"no pending escrow for policy {policy_id}")
    if not entry.get("onchain"):
        return mark_voided(policy_id, resolved_by)
    prior = entry.get("pending_void")
    if prior:  # an earlier void was submitted but never confirmed
        state = _check_sync(prior)
        if state == "confirmed":
            mark_voided(policy_id, resolved_by)
            return ledger.annotate(policy_id, void_tx_signature=prior["signature"], pending_void=None)
        if state == "unknown":
            raise _pay.PaymentPending(prior["signature"], last_valid_block_height=prior.get("last_valid_block_height"))
        ledger.annotate(policy_id, pending_void=None)
    try:
        sig = _send(_resolve_ix("void_escrow", policy_id, entry["payee"], b""))
    except _pay.PaymentPending as p:
        ledger.annotate(policy_id, pending_void={"signature": p.signature, "last_valid_block_height": p.last_valid_block_height})
        raise
    mark_voided(policy_id, resolved_by)
    return ledger.annotate(policy_id, void_tx_signature=sig)
