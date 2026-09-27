"""
Tiny JSON-backed policy store (policies.json). Same idea as the escrow
ledger: good enough for a hackathon, trivially swappable for a DB later.
"""

import json
import uuid
from dataclasses import asdict
from pathlib import Path

from app.models import Policy

_ROOT = Path(__file__).parent.parent
STORE_PATH = _ROOT / "policies.json"


def _load() -> dict[str, dict]:
    if not STORE_PATH.exists():
        return {}
    try:
        return json.loads(STORE_PATH.read_text() or "{}")
    except json.JSONDecodeError:
        STORE_PATH.rename(STORE_PATH.with_suffix(".corrupt.json"))
        return {}


def _save(store: dict[str, dict]) -> None:
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=2))
    tmp.replace(STORE_PATH)


def new_policy_id() -> str:
    return f"pol_{uuid.uuid4().hex[:10]}"


def save_policy(policy: Policy, last_evaluation: dict | None = None) -> dict:
    store = _load()
    record = store.get(policy.id, {})
    record.update(asdict(policy))
    if last_evaluation is not None:
        record["last_evaluation"] = last_evaluation
    record.setdefault("last_evaluation", None)
    store[policy.id] = record
    _save(store)
    return record


def get_policy(policy_id: str) -> Policy | None:
    record = _load().get(policy_id)
    if not record:
        return None
    return Policy(**{k: v for k, v in record.items() if k in Policy.__dataclass_fields__})


def get_record(policy_id: str) -> dict | None:
    """Policy + last_evaluation, as stored."""
    return _load().get(policy_id)


def list_records() -> list[dict]:
    return sorted(_load().values(), key=lambda r: r.get("created_at", 0), reverse=True)
