"""A confirmation-check failure after a real send must never lead to a second payment, and must never be
reported as a plain failure (money may already have moved)."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from solana.exceptions import SolanaRpcException
from solders.hash import Hash
from solders.pubkey import Pubkey

import app.escrow as escrow_mod
import app.main as main
import app.payment as pay
import app.policy_store as store

PAYEE = str(Pubkey.new_unique())


class FakeClient:
    """Stands in for AsyncClient: the send succeeds, every status/height query is rate-limited."""
    sent = []
    statuses = "rate_limited"     # or a callable returning a status object / None

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get_latest_blockhash(self):
        return SimpleNamespace(value=SimpleNamespace(blockhash=Hash.default(), last_valid_block_height=1_000))
    async def send_transaction(self, tx):
        FakeClient.sent.append(str(tx.signatures[0]))
        return SimpleNamespace(value=tx.signatures[0])
    async def get_block_height(self, *a, **k):
        raise SolanaRpcException(Exception("429 too many requests"))
    async def get_signature_statuses(self, *a, **k):
        raise SolanaRpcException(Exception("429 too many requests"))


@pytest.fixture(autouse=True)
def fast_fake_rpc(monkeypatch):
    FakeClient.sent = []
    monkeypatch.setattr(pay, "AsyncClient", FakeClient)
    monkeypatch.setattr(pay, "CONFIRM_ATTEMPTS", 3)
    monkeypatch.setattr(pay, "CONFIRM_BACKOFF", 0.0)


def test_confirmation_failure_after_send_is_pending_not_failed():
    with pytest.raises(pay.PaymentPending) as exc:
        asyncio.run(pay.send_payment(0.001, PAYEE))
    assert len(FakeClient.sent) == 1                       # sent exactly once, never retried
    assert exc.value.signature == FakeClient.sent[0]       # the signature is known and reportable
    assert exc.value.amount_sol == 0.001 and exc.value.to_address == PAYEE


def test_status_check_retries_before_giving_up(monkeypatch):
    calls = []
    class Flaky(FakeClient):
        async def get_block_height(self, *a, **k): return SimpleNamespace(value=10)
        async def get_signature_statuses(self, *a, **k):
            calls.append(1)
            if len(calls) < 3: raise SolanaRpcException(Exception("429"))
            return SimpleNamespace(value=[SimpleNamespace(err=None, confirmation_status="TransactionConfirmationStatus.Confirmed")])
    monkeypatch.setattr(pay, "AsyncClient", Flaky)
    monkeypatch.setattr(pay, "CONFIRM_ATTEMPTS", 5)
    result = asyncio.run(pay.send_payment(0.001, PAYEE))
    assert result.success and len(calls) == 3 and len(Flaky.sent) == 1


def test_expired_blockhash_is_a_definite_failure_safe_to_retry(monkeypatch):
    class Expired(FakeClient):
        async def get_block_height(self, *a, **k): return SimpleNamespace(value=2_000)   # past last_valid 1_000
        async def get_signature_statuses(self, *a, **k): return SimpleNamespace(value=[None])
    monkeypatch.setattr(pay, "AsyncClient", Expired)
    with pytest.raises(RuntimeError, match="expired"):
        asyncio.run(pay.send_payment(0.001, PAYEE))


# ------------------------------------------------------------------------------------ API level
@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "policies.json")
    monkeypatch.setattr(escrow_mod, "LEDGER_PATH", tmp_path / "escrow_ledger.json")
    sends = []
    state = {"check": "unknown"}
    async def fake_send(amount, to):
        sends.append(amount)
        raise pay.PaymentPending("sig" + str(len(sends)), amount, to, 1_000)
    async def fake_check(sig, lv=None, attempts=1): return state["check"]
    monkeypatch.setattr("app.decision._secret", lambda name: "")       # hermetic: rule-based watchdog, no Groq call
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(main, "send_payment", fake_send)
    monkeypatch.setattr(main, "check_signature", fake_check)
    return TestClient(main.app), sends, state       # no `with`: startup (airdrop etc.) is not run


def _policy(client):
    r = client.post("/policy", json={"region": "Warsaw", "lat": 52.23, "lon": 21.01, "sum_insured_sol": 0.01, "product_type": "crop_drought", "payee_pubkey": PAYEE})
    assert r.status_code == 201, r.text
    return r.json()["id"]


SIM = {"simulate": [{"mm": 30, "label": "A"}, {"mm": 12, "label": "B"}]}   # disagreement: floor > 0 and an escrow


def test_unknown_confirmation_returns_pending_and_never_sends_twice(api):
    client, sends, state = api
    pid = _policy(client)
    r1 = client.post(f"/policy/{pid}/evaluate", json=SIM)
    assert r1.status_code == 202 and r1.json()["status"] == "pending_confirmation"
    assert r1.json()["tx_signature"] == "sig1" and "explorer.solana.com/tx/sig1" in r1.json()["floor_explorer_url"]
    assert len(sends) == 1
    for _ in range(3):                                    # a client hammering /evaluate while the state is unknown
        r = client.post(f"/policy/{pid}/evaluate", json=SIM)
        assert r.status_code == 202 and r.json()["tx_signature"] == "sig1"
    assert len(sends) == 1                                # still exactly one payment attempt
    assert client.get(f"/policy/{pid}").json().get("escrow") is None   # nothing else was done on unknown state


def test_late_confirmation_completes_the_evaluation_without_a_second_payment(api):
    client, sends, state = api
    pid = _policy(client)
    assert client.post(f"/policy/{pid}/evaluate", json=SIM).status_code == 202
    state["check"] = "confirmed"
    r = client.post(f"/policy/{pid}/evaluate", json=SIM)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["floor_tx_signature"] == "sig1" and len(sends) == 1
    assert body["dispute_status"] in ("escalated", "investigating") and body["escrow"]["status"] == "pending"
    assert body["escrow_amount_sol"] == pytest.approx(0.006) and "no second payment was sent" in body["note"]


def test_expired_pending_payment_allows_a_fresh_attempt(api):
    client, sends, state = api
    pid = _policy(client)
    assert client.post(f"/policy/{pid}/evaluate", json=SIM).status_code == 202
    state["check"] = "expired"                            # can never land: retrying cannot double pay
    assert client.post(f"/policy/{pid}/evaluate", json=SIM).status_code == 202
    assert len(sends) == 2 and [s for s in sends] == [pytest.approx(0.00333333, rel=1e-3)] * 2
