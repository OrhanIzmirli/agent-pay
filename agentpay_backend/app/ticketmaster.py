"""
Cross-domain evidence for EVENT_WEATHER_CANCEL: the ticketed event's own status.

Rain is one kind of evidence that an event may not go ahead; the organiser's
ticketing system saying "cancelled" is a different kind, from an independent
domain. This module turns Ticketmaster Discovery API's `dates.status.code`
into ONE extra SourceReading (unit "status") whose payout_ratio is implied
directly (1.0 / 0.0). It joins the same readings list as the weather sources,
so floor/ceiling and dispute handling use the existing formula unchanged.

The source is strictly optional: no key, no venue/event id, no match, a
timeout or an HTTP error all return None and the evaluation proceeds on the
weather readings alone. Free tier; the key is read from TICKETMASTER_API_KEY
(environment, or a gitignored `.env` next to `app/`), never hardcoded.
"""

import os
from datetime import date, timedelta
from pathlib import Path

import httpx

from app.models import Policy, SourceReading

BASE = "https://app.ticketmaster.com/discovery/v2"
TIMEOUT = 6.0

# Ticketmaster status codes: onsale | offsale | canceled | postponed | rescheduled
# (they spell "canceled" with one l; both spellings are accepted).
_HAPPENING = {"onsale", "offsale", "rescheduled"}  # still going ahead (possibly on a new date)
_NOT_HAPPENING = {"cancelled", "canceled", "postponed"}


def implied_ratio(status: str) -> float | None:
    """Status -> implied payout ratio; None for a code we don't understand (source skipped)."""
    code = status.strip().lower()
    if code in _NOT_HAPPENING:
        return 1.0
    if code in _HAPPENING:
        return 0.0
    return None


def status_reading(status: str, detail: str, live: bool) -> SourceReading | None:
    ratio = implied_ratio(status)
    if ratio is None:
        return None
    return SourceReading(source="ticketmaster_event_status", observed_mm=ratio, live=live, detail=detail,
                         unit="status", payout_ratio=ratio, status=status.strip().lower())


def _api_key() -> str:
    key = os.environ.get("TICKETMASTER_API_KEY", "").strip()
    if key:
        return key
    env_file = Path(__file__).resolve().parent.parent / ".env"
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "TICKETMASTER_API_KEY":
                return value.strip().strip("'\"")
    except OSError:
        pass
    return ""


async def _get(client: httpx.AsyncClient, path: str, params: dict) -> dict:
    r = await client.get(f"{BASE}{path}", params=params)
    r.raise_for_status()
    return r.json()


async def fetch_event_status(policy: Policy) -> tuple[SourceReading | None, str | None]:
    """Returns (reading, note). reading is None whenever the source can't speak;
    note explains why, for the evaluation warnings. Never raises."""
    if not (policy.ticketmaster_event_id or policy.venue_name):
        return None, None
    key = _api_key()
    if not key:
        return None, "Event-status source skipped: TICKETMASTER_API_KEY is not set."
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers={"Accept": "application/json"}) as client:
            if policy.ticketmaster_event_id:
                ev = await _get(client, f"/events/{policy.ticketmaster_event_id}.json", {"apikey": key})
            else:
                venues = (await _get(client, "/venues.json", {"apikey": key, "keyword": policy.venue_name, "size": 1})).get("_embedded", {}).get("venues", [])
                if not venues:
                    return None, f"Event-status source skipped: no Ticketmaster venue matches {policy.venue_name!r}."
                # Events carry a UTC timestamp but a LOCAL date; pad the UTC search a day each way,
                # then keep only events whose local date is inside the policy window.
                lo = (date.fromisoformat(policy.window_start) - timedelta(days=1)).isoformat()
                hi = (date.fromisoformat(policy.window_end) + timedelta(days=1)).isoformat()
                events = (await _get(client, "/events.json", {
                    "apikey": key, "venueId": venues[0]["id"], "size": 50, "sort": "date,asc",
                    "startDateTime": f"{lo}T00:00:00Z", "endDateTime": f"{hi}T23:59:59Z",
                })).get("_embedded", {}).get("events", [])
                events = [e for e in events if policy.window_start <= e.get("dates", {}).get("start", {}).get("localDate", "") <= policy.window_end]
                if not events:
                    return None, f"Event-status source skipped: no Ticketmaster event at {policy.venue_name!r} in the window."
                ev = events[0]
        code = ev["dates"]["status"]["code"]
        reading = status_reading(code, f"Ticketmaster event {ev.get('id', '?')} '{ev.get('name', '')}': dates.status.code={code}", live=True)
        if reading is None:
            return None, f"Event-status source skipped: unrecognised Ticketmaster status {code!r}."
        return reading, None
    except Exception as e:  # noqa: BLE001 - optional evidence: dropped, never fails the evaluation
        print(f"[ticketmaster] lookup failed for {policy.id}: {e!r}")
        return None, f"Event-status source unavailable ({type(e).__name__}); evaluated on weather sources only."
