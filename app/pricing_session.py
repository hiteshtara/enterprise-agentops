"""The owner's deliberate window for sending pricing changes.

A session is the layer between *"this installation is permitted to support
live pricing"* -- a deployment decision, held in the environment -- and *"the
owner has decided to make changes right now, to these listings"*. Before it
existed, the only way to distinguish those was to edit `.env` for every
pricing decision, which is neither operable nor auditable.

**It only ever narrows.** The write gate reads:

    writes_enabled()                      env, the deployment kill switch
    AND listing in automation_allowlist() env, the deployment ceiling
    AND an active session covers it       here
    AND ... verification, approval, fingerprint, guardrails

Every clause is an `AND`, and this one is third. A session cannot widen either
environment control, cannot bring a listing into scope that the deployment
excludes, and cannot make a write possible when the kill switch is shut. If
this module returned `True` for everything, the deployment controls would
still refuse.

**In memory, deliberately.** Requirement: a restart means SAFE MODE. Held in a
process global rather than a table so that is a property of the substrate
rather than a rule someone has to remember -- there is no row to survive, and
no code path that could restore one. The audit trail is separate and durable:
starting and ending a session are audited, so the *history* persists while the
*capability* does not.

**Single process.** One module global means a session started on one worker
does not arm another. That is fail-closed -- the second worker simply refuses
-- but it would be confusing, and it assumes the single-process deployment in
use today. A multi-worker deployment needs this reconsidered, not merely
scaled.

**Absolute expiry, no renewal.** `expires_at` is computed once at creation and
never extended; activity does not prolong it. Expiry is evaluated on read, so
a lapsed session is inert the instant it lapses whether or not anything
noticed -- no timer, no background thread, no clock to trust. To continue,
the owner starts a new session, which is a fresh deliberate act with its own
audit event.
"""

import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

#: The longest a window may last. A hard server-side cap, not a default a
#: caller can talk its way past: a request for longer is refused rather than
#: quietly clamped, so nobody believes they have more time than they do.
MAX_SESSION_MINUTES = 30

#: What the console asks for when the owner does not choose.
DEFAULT_SESSION_MINUTES = 30


@dataclass(frozen=True)
class PricingSession:
    """One open window. Frozen: a session is never edited, only replaced."""

    id: str
    started_by_user_id: str
    started_at: datetime
    expires_at: datetime
    listing_ids: frozenset[str]

    def covers(self, listing_id: str, now: datetime | None = None) -> bool:
        """Does this session permit that listing, right now?

        Both halves matter and neither is sufficient: an unexpired session
        that does not name the listing permits nothing, and a session naming
        every listing permits nothing once it has lapsed.
        """
        return self.active(now) and listing_id in self.listing_ids

    def active(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) < self.expires_at

    def remaining_seconds(self, now: datetime | None = None) -> int:
        left = (self.expires_at - (now or datetime.now(UTC))).total_seconds()

        return max(0, int(left))


class PricingSessionStore:
    """Holds at most one session, in this process, behind a lock.

    Holds no database and no provider client: it can neither persist a session
    nor act on one. Deciding whether a write may happen is the write gate's
    job; this only answers what the owner asked for.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session: PricingSession | None = None

    def active(self, now: datetime | None = None) -> PricingSession | None:
        """The live session, or None. Expiry is applied here, on every read.

        A lapsed session is discarded as it is read rather than left lying
        around looking live to anything that inspects the slot directly.
        """
        with self._lock:
            if self._session is None:
                return None

            if not self._session.active(now):
                self._session = None

                return None

            return self._session

    def start(
        self,
        listing_ids: set[str] | frozenset[str],
        started_by_user_id: str,
        duration_minutes: int = DEFAULT_SESSION_MINUTES,
        now: datetime | None = None,
    ) -> PricingSession:
        """Open a window. Replaces any existing one.

        Refuses rather than repairs: an empty selection, an over-long duration
        or a missing actor are all mistakes worth surfacing, and silently
        substituting a default would hand someone a window they did not ask
        for.
        """
        chosen = frozenset(listing_ids)

        if not chosen:
            raise ValueError(
                "A pricing session must name at least one listing."
            )

        if not (started_by_user_id or "").strip():
            raise ValueError("A pricing session must record who opened it.")

        if duration_minutes < 1 or duration_minutes > MAX_SESSION_MINUTES:
            raise ValueError(
                f"A pricing session may last 1 to {MAX_SESSION_MINUTES} minutes."
            )

        moment = now or datetime.now(UTC)

        session = PricingSession(
            id=str(uuid.uuid4()),
            started_by_user_id=started_by_user_id,
            started_at=moment,
            # Computed once. Nothing extends it.
            expires_at=moment + timedelta(minutes=duration_minutes),
            listing_ids=chosen,
        )

        with self._lock:
            self._session = session

        return session

    def end(self) -> PricingSession | None:
        """Close immediately. Returns what was ended, or None if nothing was."""
        with self._lock:
            ended, self._session = self._session, None

            return ended

    def permits(self, listing_id: str, now: datetime | None = None) -> bool:
        """Does a live session cover this listing?

        **This is not permission to write.** It is one clause of the gate, and
        the deployment controls are evaluated separately and first. A caller
        that treats this as the whole answer has written a bug.
        """
        session = self.active(now)

        return session is not None and session.covers(listing_id, now)


#: The process-wide store. One per process, gone when the process is.
SESSIONS = PricingSessionStore()


def to_payload(
    session: PricingSession | None,
    now: datetime | None = None,
) -> dict:
    """What the console is told. Carries no capability, only status."""
    if session is None:
        return {
            "mode": "SAFE",
            "active": False,
            "expires_at": None,
            "remaining_seconds": 0,
            "listing_ids": [],
            "started_by_user_id": None,
        }

    return {
        "mode": "LIVE",
        "active": True,
        "expires_at": session.expires_at.isoformat(),
        "remaining_seconds": session.remaining_seconds(now),
        "listing_ids": sorted(session.listing_ids),
        "started_by_user_id": session.started_by_user_id,
    }


__all__ = [
    "DEFAULT_SESSION_MINUTES",
    "MAX_SESSION_MINUTES",
    "SESSIONS",
    "PricingSession",
    "PricingSessionStore",
    "to_payload",
]
