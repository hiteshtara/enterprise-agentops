"""Operational attribution: which process, which request, which surface.

On 2026-09-07 five pricing approvals were created and approved in ninety
seconds, and the audit trail could not say where they came from. Every event
carried `actor_user_id = admin@agentguard.local`, which is a shared demo
identity, so it could not distinguish the console from a script, a stray test
process, or a second backend. The answer was eventually recovered from a
uvicorn access log that happened to still exist -- and only because the CORS
preflights gave the client away.

This module closes that gap with three fields, and deliberately stops there.

**It identifies a process and a request, never a person.** No IP address, no
user-agent, no device fingerprint. The question being answered is "which of
our processes did this", not "who was sitting at which machine" -- and the
second question is not one this system needs to ask.

`actor_source` is taken from an explicit header the client sets, never
inferred from a user-agent string. A caller that lies about it is not a threat
model this solves: nothing is authorized on the basis of these fields, and
that is the point. They are for reading an incident afterwards.
"""

import contextvars
import uuid
from enum import Enum

#: Minted once per backend process, at import. Two backends -- a stale one and
#: a fresh one, or a test process pointed at the wrong database -- stamp
#: different values, which is precisely what the incident could not establish.
#: It survives in the audit trail after the process itself is gone.
INSTANCE_ID = str(uuid.uuid4())

#: The header the console sets. Named explicitly so it is obviously a
#: declaration by the client, not a fact derived about it.
SOURCE_HEADER = "X-AgentGuard-Source"


class ActorSource(str, Enum):
    """Which surface an action arrived through.

    A closed set. An unrecognised value normalises to `API` rather than being
    stored, so this column can never become a free-text field that carries
    whatever a caller decided to put in a header.
    """

    UI = "UI"
    API = "API"
    CLI = "CLI"
    JOB = "JOB"
    TEST = "TEST"


#: What a request that declared nothing is recorded as. `API` rather than
#: `UNKNOWN`: something did reach the HTTP API, and that much is true.
DEFAULT_SOURCE = ActorSource.API


def normalise_source(value: str | None) -> ActorSource:
    """A header value as a known source. Never raises, never passes text through.

    Rejecting an unknown value with a 400 was considered and not chosen: a
    provenance field that can fail a request would make observability able to
    break the thing it observes. Normalising keeps the request working and
    records the honest answer -- it came through the API and said nothing
    recognisable.
    """
    if not value:
        return DEFAULT_SOURCE

    try:
        return ActorSource(value.strip().upper())

    except ValueError:
        return DEFAULT_SOURCE


#: Per-request state. `ContextVar` rather than a global so concurrent requests
#: in the same process cannot read each other's values.
_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agentguard_request_id",
    default=None,
)

_actor_source: contextvars.ContextVar[ActorSource] = contextvars.ContextVar(
    "agentguard_actor_source",
    default=DEFAULT_SOURCE,
)


def new_request_id() -> str:
    return str(uuid.uuid4())


def bind(request_id: str, source: ActorSource):
    """Bind provenance for the current request. Returns the reset tokens."""
    return _request_id.set(request_id), _actor_source.set(source)


def reset(tokens) -> None:
    """Restore what was bound before, so context does not leak between tasks."""
    request_token, source_token = tokens

    _request_id.reset(request_token)
    _actor_source.reset(source_token)


def current_request_id() -> str | None:
    """The id of the request being handled, or None outside one.

    None is honest and expected: a scheduled job or a CLI invocation is not an
    HTTP request, and inventing an id for it would imply a caller that does
    not exist.
    """
    return _request_id.get()


def current_source() -> ActorSource:
    return _actor_source.get()


def snapshot() -> dict[str, str | None]:
    """The three fields, as a store writes them."""
    return {
        "request_id": current_request_id(),
        "actor_source": current_source().value,
        "instance_id": INSTANCE_ID,
    }


__all__ = [
    "DEFAULT_SOURCE",
    "INSTANCE_ID",
    "SOURCE_HEADER",
    "ActorSource",
    "bind",
    "current_request_id",
    "current_source",
    "new_request_id",
    "normalise_source",
    "reset",
    "snapshot",
]
