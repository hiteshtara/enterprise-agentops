"""Fix B: enough provenance to read an incident, and not one field more.

On 2026-09-07, five pricing approvals were created and approved in ninety
seconds and the audit trail could not say where they came from: every event
carried the same shared demo identity. The source was recovered only from a
uvicorn access log that happened to survive, and only because the CORS
preflights gave the client away.

Two properties are under test, and the second is the constraint:

  * an incident can now be attributed to a **request** and a **process**; and
  * nothing here identifies a **person**. No IP, no user-agent, no device
    fingerprint, no token, no raw session value -- and a test asserts that,
    rather than a comment claiming it.

Every value is invented. Nothing in this file reaches PriceLabs.
"""

import json

import pytest

from app.provenance import (
    DEFAULT_SOURCE,
    INSTANCE_ID,
    SOURCE_HEADER,
    ActorSource,
    normalise_source,
    snapshot,
)


def audited(api, role="OPERATOR", **kwargs):
    """Drive one HTTP request that reliably writes audit events.

    Uses a scripted model provider so no OpenAI call is made: the point is the
    request, not the reasoning.
    """
    from app.seed_data import seed_migration_batches
    from tests.fakes import ScriptedModelProvider, final_response, tool_response

    seed_migration_batches(api.module.database)

    api.module.agent.model = ScriptedModelProvider(
        [
            tool_response("query_migration_batches", {"status": "FAILED", "limit": 5}),
            final_response("Five batches failed."),
        ]
    )

    return api.client(role).post(
        "/agent/run",
        json={"message": "Show failed batches."},
        **kwargs,
    )


# -- the source is declared, never detected -------------------------------


def test_a_console_request_is_recorded_as_ui(api):
    audited(api, headers={SOURCE_HEADER: "UI"})

    events = api.module.audit_store.list_events(limit=10)

    assert events, "the request should have produced audit events"
    assert {e["actor_source"] for e in events} == {ActorSource.UI.value}


def test_a_request_that_declares_nothing_defaults_to_api(api):
    """`API` rather than `UNKNOWN`: something did reach the HTTP API."""
    audited(api)

    events = api.module.audit_store.list_events(limit=10)

    assert events
    assert {e["actor_source"] for e in events} == {ActorSource.API.value}


@pytest.mark.parametrize(
    "declared",
    ["", "   ", "browser", "UI; DROP TABLE audit_events", "ui\nJOB", "🙂", None],
)
def test_an_unrecognised_source_normalises_rather_than_being_stored(declared):
    """A provenance field must never become free text a caller controls.

    Rejecting with a 400 was considered and not chosen: a field that can fail
    a request would let observability break the thing it observes. So it
    normalises, and the stored value is always one of the closed set.
    """
    result = normalise_source(declared)

    assert result is DEFAULT_SOURCE
    assert result.value in {s.value for s in ActorSource}


@pytest.mark.parametrize("declared", ["UI", "ui", " Ui ", "JOB", "cli", "TEST"])
def test_a_recognised_source_is_accepted_in_any_case(declared):
    assert normalise_source(declared).value == declared.strip().upper()


def test_an_invalid_source_never_reaches_the_database(api):
    audited(api, headers={SOURCE_HEADER: "definitely-not-valid"})

    for event in api.module.audit_store.list_events(limit=20):
        assert event["actor_source"] in {s.value for s in ActorSource} | {None}


def test_the_source_is_not_inferred_from_a_user_agent(api):
    """A browser-looking user-agent must not make a request read as UI."""
    audited(api, headers={"User-Agent": "Mozilla/5.0 (Macintosh) Chrome/120"})

    events = api.module.audit_store.list_events(limit=10)

    assert events
    assert {e["actor_source"] for e in events} == {ActorSource.API.value}


# -- request correlation --------------------------------------------------


def test_two_requests_get_different_request_ids(api):
    first = api.client("ADMIN").get("/runs")
    second = api.client("ADMIN").get("/runs")

    assert first.headers["X-Request-Id"] != second.headers["X-Request-Id"]


def test_every_event_from_one_request_shares_its_request_id(api):
    """The question the incident could not answer: was that one call or five?"""
    response = audited(api)

    request_id = response.headers["X-Request-Id"]
    events = api.module.audit_store.list_events(limit=50)

    assert len(events) > 1, "one run should audit several events"
    assert {e["request_id"] for e in events} == {request_id}


def test_five_separate_requests_are_five_separate_ids(api):
    """The exact shape of the incident: five actions, ninety seconds."""
    ids = {
        api.client("ADMIN").get("/runs").headers["X-Request-Id"] for _ in range(5)
    }

    assert len(ids) == 5


def test_a_request_id_is_returned_to_the_caller(api):
    """So a client can correlate its own logs with ours without guessing."""
    response = api.client("ADMIN").get("/runs")

    assert response.headers.get("X-Request-Id")


def test_outside_a_request_the_id_is_none_rather_than_invented(database):
    """A scheduled job is not an HTTP call. Null is the honest answer."""
    from app.audit_store import AuditStore

    store = AuditStore(database=database)
    store.record("TEST_EVENT", {"note": "written outside any request"})

    event = store.list_events(limit=1)[0]

    assert event["request_id"] is None
    # ...but the process is still identified, which is the useful half here.
    assert event["instance_id"] == INSTANCE_ID


# -- process identity -----------------------------------------------------


def test_every_event_from_one_process_shares_an_instance_id(api):
    """Two separate requests, one process, one instance id.

    Driven through a route that actually audits: a GET writes no events, and
    a test that skips when it finds none is a test that never runs.
    """
    first = audited(api)
    second = audited(api)

    assert first.headers["X-Request-Id"] != second.headers["X-Request-Id"]

    events = api.module.audit_store.list_events(limit=50)

    assert len(events) > 1
    assert len({e["request_id"] for e in events}) == 2, "two distinct requests"
    assert {e["instance_id"] for e in events} == {api.module.provenance.INSTANCE_ID}


def test_the_instance_id_is_a_uuid_minted_once(database):
    import uuid

    from app.audit_store import AuditStore

    uuid.UUID(INSTANCE_ID)

    store = AuditStore(database=database)
    store.record("TEST_EVENT", {})
    store.record("TEST_EVENT", {})

    events = store.list_events(limit=2)

    assert {e["instance_id"] for e in events} == {INSTANCE_ID}


# -- the constraint: a process and a request, never a person --------------


def test_the_snapshot_carries_only_the_three_fields():
    assert set(snapshot()) == {"request_id", "actor_source", "instance_id"}


def test_no_network_or_device_identifier_is_stored(api):
    """The line this feature must not cross.

    Sent with the headers a fingerprinting implementation would reach for.
    None of them may appear anywhere in the stored event.
    """
    audited(
        api,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh) Chrome/120",
            "X-Forwarded-For": "203.0.113.7",
            "Accept-Language": "en-GB,en;q=0.9",
            SOURCE_HEADER: "UI",
        },
    )

    stored = json.dumps(api.module.audit_store.list_events(limit=50))

    for leaked in ("203.0.113.7", "Mozilla", "Chrome", "en-GB", "X-Forwarded-For"):
        assert leaked not in stored


def test_no_column_exists_that_could_hold_a_network_identifier():
    """Absence is the safety property. There is nowhere to put one."""
    from app.db_models import AuditEventRecord

    columns = set(AuditEventRecord.__table__.columns.keys())

    for forbidden in (
        "ip",
        "ip_address",
        "remote_addr",
        "user_agent",
        "device_id",
        "fingerprint",
        "session_id",
        "token",
    ):
        assert forbidden not in columns


def test_no_token_or_raw_session_value_is_persisted(api):
    """The bearer token must not leak into the audit trail via provenance."""
    from app.seed_data import seed_migration_batches
    from tests.fakes import ScriptedModelProvider, final_response, tool_response

    seed_migration_batches(api.module.database)
    api.module.agent.model = ScriptedModelProvider(
        [
            tool_response("query_migration_batches", {"status": "FAILED", "limit": 5}),
            final_response("done"),
        ]
    )

    client = api.client("OPERATOR")
    token = client.headers["Authorization"].removeprefix("Bearer ").strip()

    client.post("/agent/run", json={"message": "Show failed batches."})

    stored = json.dumps(api.module.audit_store.list_events(limit=50))

    assert token not in stored
    assert "Bearer" not in stored


def test_session_ref_was_not_added_because_tokens_carry_no_jti():
    """Recorded as a decision, not an omission.

    A `session_ref` was authorised *only if* the existing tokens already had a
    suitable `jti`. They do not -- `issue_token` mints `sub`/`iss`/`iat`/`exp`
    and nothing else -- and adding one would mean changing auth token
    issuance, which is well outside a provenance fix. So it was not added.
    """
    import inspect

    from app import security
    from app.db_models import AuditEventRecord

    assert "jti" not in inspect.getsource(security.issue_token)
    assert "session_ref" not in AuditEventRecord.__table__.columns


# -- nothing is authorized on any of it -----------------------------------


def test_declaring_a_source_grants_no_authority(api):
    """Provenance is read after the fact. It is not a credential.

    A VIEWER claiming `UI`, `CLI` or `JOB` is still a VIEWER.
    """
    for declared in ("UI", "CLI", "JOB", "TEST"):
        response = api.client("VIEWER").post(
            "/pricing/cleanup/run",
            headers={SOURCE_HEADER: declared},
        )

        assert response.status_code == 403


def test_an_anonymous_request_still_needs_authentication(api):
    response = api.anonymous().get("/runs", headers={SOURCE_HEADER: "UI"})

    assert response.status_code == 401
