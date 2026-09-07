"""Realized booking history: what enters it, what never leaves the connector.

Two separable concerns, both here because they share fixtures:

  * `PriceLabsClient.reservations` -- pagination, de-duplication, and the fact
    that guest-identifying fields are not merely unused but structurally
    incapable of leaving the client.
  * `app.pricing_history` -- which reservations count as evidence, and the two
    derivations. Every rejection is an exclusion, never a repair.

Every value is invented. No test in this file reaches PriceLabs.
"""

import datetime

import pytest

from app.connectors.pricelabs.client import PriceLabsClient
from app.connectors.pricelabs.errors import PriceLabsUnavailable
from app.pricing_history import (
    _date,
    build_history,
    history_sample,
    load_history,
)

BUNKERS = "680444___747423"
ARBORETUM = "681301___748348"


def reservation(**over):
    """A provider row, including the fields that must never propagate."""
    base = {
        "reservation_id": "17396297___747423",
        "listing_id": BUNKERS,
        "listing_name": "Boston Bunkers",
        "check_in": "2026-06-15",
        "check_out": "2026-06-22",
        "booking_status": "booked",
        "booked_date": "2026-05-16T16:46:04.000Z",
        "rental_revenue": "1400.0",
        "total_cost": "1783.0",
        "no_of_days": 7,
        "currency": "USD",
        "cancelled_on": None,
        "booking_channel": "bcom",
        "channelConfirmationCode": "6370869227|5956008896",
        "guestName": "A Real Person",
        "cleaning_fees": 200,
        "guest_count": 2,
    }

    base.update(over)

    return base


class FakeTransport(PriceLabsClient):
    """A client whose `_request` returns scripted pages. Nothing else stubbed.

    Subclassing the real client rather than imitating it is the point: the
    pagination, de-duplication and field-construction under test are the
    production ones.
    """

    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def _request(self, method, path, json_body=None):
        self.requests.append(path)

        index = len(self.requests) - 1

        return self.pages[min(index, len(self.pages) - 1)]


def page(rows, next_page=False):
    return {"pms_name": "lodgify", "next_page": next_page, "data": rows}


# -- pagination and de-duplication ----------------------------------------


def test_pagination_follows_next_page_to_completion():
    """One page is not the history. A caller that reads one silently truncates."""
    client = FakeTransport(
        [
            page([reservation(reservation_id=f"r{i}") for i in range(3)], True),
            page([reservation(reservation_id=f"r{i}") for i in range(3, 6)], True),
            page([reservation(reservation_id="r6")], False),
        ]
    )

    rows = client.reservations(BUNKERS, "lodgify", "2025-01-01", "2026-12-31")

    assert len(rows) == 7
    assert len(client.requests) == 3
    assert "offset=0" in client.requests[0]
    assert "offset=500" in client.requests[1]
    assert "offset=1000" in client.requests[2]


def test_an_empty_page_ends_the_walk_even_if_next_page_stays_true():
    """The provider's own cursor was observed returning 0 rows with True."""
    client = FakeTransport([page([reservation()], True), page([], True)])

    rows = client.reservations(BUNKERS, "lodgify", "2025-01-01", "2026-12-31")

    assert len(rows) == 1
    assert len(client.requests) == 2


def test_a_repeated_page_does_not_create_duplicate_records():
    """A reservation counted twice would weight a median twice.

    Cheap to prevent, and near-impossible to notice afterwards -- the number
    would simply be a little wrong.
    """
    same = [reservation(reservation_id="dup")]

    client = FakeTransport([page(same, True), page(same, True), page(same, False)])

    rows = client.reservations(BUNKERS, "lodgify", "2025-01-01", "2026-12-31")

    assert len(rows) == 1


# -- reservation ids: the key pagination de-duplication rests on ----------


def test_a_valid_reservation_id_is_retained():
    client = FakeTransport([page([reservation(reservation_id="keep-me")])])

    rows = client.reservations(BUNKERS, "lodgify", "2025-01-01", "2026-12-31")

    assert [r["reservation_id"] for r in rows] == ["keep-me"]


@pytest.mark.parametrize("missing", [None, ""])
def test_a_row_without_a_reservation_id_is_excluded(missing):
    """It cannot be de-duplicated, so it is not trustworthy history.

    An id is never synthesised from dates, prices or any guest field: that
    would both invent identity and propagate exactly what this method exists
    not to carry.
    """
    client = FakeTransport(
        [page([reservation(reservation_id=missing), reservation(reservation_id="ok")])]
    )

    rows = client.reservations(BUNKERS, "lodgify", "2025-01-01", "2026-12-31")

    assert [r["reservation_id"] for r in rows] == ["ok"]


def test_several_rows_without_ids_do_not_collapse_into_one_history_row():
    """The failure this prevents, stated as its own case.

    Treating None as a de-duplication key would fold four distinct
    reservations into one, silently thinning the sample distribution a median
    is computed from -- a quiet change to the evidence, not a visible error.
    """
    client = FakeTransport(
        [
            page(
                [reservation(reservation_id=None, rental_revenue=f"{100 + i}.0")
                 for i in range(4)]
                + [reservation(reservation_id="real")]
            )
        ]
    )

    rows = client.reservations(BUNKERS, "lodgify", "2025-01-01", "2026-12-31")

    assert len(rows) == 1
    assert rows[0]["reservation_id"] == "real"


def test_a_non_string_reservation_id_is_excluded():
    client = FakeTransport([page([reservation(reservation_id=12345)])])

    assert client.reservations(BUNKERS, "lodgify", "2025-01-01", "2026-12-31") == []


def test_a_cursor_that_never_stops_is_refused_rather_than_walked_forever():
    client = FakeTransport([page([reservation(reservation_id="x")], True)] * 2)

    # Every page after the first is the same object, so ids repeat and rows
    # stop accumulating -- but `next_page` stays true, which is the hazard.
    with pytest.raises(PriceLabsUnavailable, match="did not stop paginating"):
        client.reservations(BUNKERS, "lodgify", "2025-01-01", "2026-12-31")


# -- what leaves the connector --------------------------------------------


#: Every field the connector constructs, and nothing else. Widening this set
#: is an explicit decision each time, which is exactly why the assertion is
#: an equality against a literal rather than a subset check: a field that
#: appears without someone editing this line is a bug.
#:
#: `check_out` and `booking_channel` were added 2026-09-06 for outcome
#: tracking. `check_out` because a night is occupied when
#: `check_in <= night < check_out`, and the arrival date alone cannot say
#: whether a multi-night stay covers a given night; `booking_channel` because
#: it is a coarse category (`bcom` / `airbnb` / `vrbo` / `manual` / `others`)
#: rather than an identifier.
MINIMAL_FIELDS = {
    "reservation_id",
    "listing_id",
    "booked_date",
    "check_in",
    "check_out",
    "no_of_days",
    "rental_revenue",
    "booking_status",
    "booking_channel",
}


def test_only_the_minimal_pricing_fields_leave_the_connector():
    client = FakeTransport([page([reservation()])])

    rows = client.reservations(BUNKERS, "lodgify", "2025-01-01", "2026-12-31")

    assert set(rows[0]) == MINIMAL_FIELDS


@pytest.mark.parametrize("field", ["guestName", "channelConfirmationCode"])
def test_guest_identifying_fields_never_leave_the_connector(field):
    """Absence is the safety property, not a filter applied downstream.

    Results are constructed field by field, so a payload that grows a new
    guest field cannot reach a trace, an audit event, a metric or a browser --
    it simply is not read.
    """
    client = FakeTransport([page([reservation()])])

    rows = client.reservations(BUNKERS, "lodgify", "2025-01-01", "2026-12-31")

    assert field not in rows[0]
    assert not any(field in row for row in rows)

    # ...and not hiding in a value either.
    assert "A Real Person" not in str(rows)
    assert "6370869227" not in str(rows)


def test_fields_carried_for_pricing_are_not_quietly_dropped():
    """The converse: a minimal record still has to be enough to price with."""
    client = FakeTransport([page([reservation()])])

    row = client.reservations(BUNKERS, "lodgify", "2025-01-01", "2026-12-31")[0]

    assert history_sample(row) is not None


# -- eligibility: exclusions, never repairs -------------------------------


def test_a_realized_booking_becomes_a_sample():
    assert history_sample(reservation()) == (30, 200.0)


def test_a_cancelled_reservation_is_excluded():
    """`booking_status` is authoritative.

    `cancelled_on` is not usable: every cancelled row on this account carries
    the epoch sentinel `1970-01-01T00:00:00.000Z`, so filtering on it would
    exclude nothing at all.
    """
    row = reservation(
        booking_status="cancelled",
        cancelled_on="1970-01-01T00:00:00.000Z",
    )

    assert history_sample(row) is None


@pytest.mark.parametrize("revenue", ["0.0", "0", 0, -50, "-50"])
def test_a_reservation_with_no_revenue_is_excluded(revenue):
    """Owner blocks arrive as `manual` reservations with zero revenue.

    A $0 ADR is not a cheap booking; treating it as one would drag a lead-band
    median down and argue for a price reduction that nothing supports.
    """
    assert history_sample(reservation(rental_revenue=revenue)) is None


@pytest.mark.parametrize("nights", [0, -1, None, "7", 7.5])
def test_a_reservation_without_whole_nights_is_excluded(nights):
    assert history_sample(reservation(no_of_days=nights)) is None


# -- date parsing: two accepted forms, and no salvage ---------------------


@pytest.mark.parametrize(
    "value",
    [
        "2026-06-15",                      # plain date
        "2026-06-15T16:46:04.000Z",        # ISO timestamp, Zulu
        "2026-06-15T16:46:04+00:00",       # ISO timestamp, explicit offset
        "2026-06-15T16:46:04-04:00",       # ISO timestamp, non-zero offset
        "2026-06-15T16:46:04",             # ISO timestamp, naive
        # Genuinely ISO-8601, in its basic (separator-less) form. Accepted
        # because it parses in full, not because a prefix was rescued -- the
        # provider sends extended format, and rejecting a valid ISO date to
        # look stricter would be inventing a rule rather than keeping one.
        "20260615",
    ],
)
def test_the_accepted_date_forms_parse(value):
    assert _date(value) == datetime.date(2026, 6, 15)


@pytest.mark.parametrize(
    "value",
    [
        "2026-09-10BROKEN",       # a valid date with garbage welded on
        "2026-06-15 not a time",  # valid prefix, invalid remainder
        "2026-06-15Textra",
        "2026-13-45",             # impossible month and day
        "2026-02-30",             # impossible day for the month
        "not-a-date",
        "15/06/2026",             # a real format, not an accepted one
        "",
        None,
        12345,
        datetime.date(2026, 6, 15),   # already a date: still not a string
    ],
)
def test_anything_else_is_rejected_rather_than_salvaged(value):
    """No ten-character prefix is rescued from a malformed value.

    An earlier version fell back to `date.fromisoformat(value[:10])`, which
    turned "2026-09-10BROKEN" into 2026-09-10 -- a malformed value repaired
    into a plausible one, inside the input to a price-lowering decision.
    """
    assert _date(value) is None


def test_a_malformed_date_never_becomes_a_sample():
    """The parser's strictness, carried through to the exclusion."""
    for field in ("booked_date", "check_in"):
        assert history_sample(reservation(**{field: "2026-09-10BROKEN"})) is None


@pytest.mark.parametrize("bad", [None, "", "not-a-date", "2026-13-45", 12345])
def test_a_malformed_booked_date_is_excluded(bad):
    assert history_sample(reservation(booked_date=bad)) is None


@pytest.mark.parametrize("bad", [None, "", "not-a-date", "2026-02-30"])
def test_a_malformed_check_in_is_excluded(bad):
    assert history_sample(reservation(check_in=bad)) is None


def test_a_negative_lead_time_is_excluded_rather_than_clamped():
    """Three live rows have `booked_date` after `check_in`.

    Not clamped to zero: whatever produced them is not a same-day booking, and
    guessing which it is would put fiction into the floor evidence.
    """
    row = reservation(check_in="2026-06-15", booked_date="2026-06-20T10:00:00.000Z")

    assert history_sample(row) is None


def test_a_same_day_booking_is_kept():
    """Zero lead time is a real booking; only negative is impossible."""
    row = reservation(check_in="2026-06-15", booked_date="2026-06-15T10:00:00.000Z")

    sample = history_sample(row)

    assert sample is not None
    assert sample[0] == 0


# -- the two derivations --------------------------------------------------


@pytest.mark.parametrize(
    ("check_in", "booked", "expected"),
    [
        ("2026-06-15", "2026-05-16T16:46:04.000Z", 30),
        ("2026-06-15", "2026-06-14T23:59:59.000Z", 1),
        ("2026-06-15", "2025-06-15T00:00:00.000Z", 365),
        ("2026-03-01", "2026-02-27T12:00:00.000Z", 2),
    ],
)
def test_days_out_is_check_in_minus_booked_date(check_in, booked, expected):
    """Whole days between two calendar dates -- the time of day is dropped."""
    sample = history_sample(reservation(check_in=check_in, booked_date=booked))

    assert sample is not None
    assert sample[0] == expected


@pytest.mark.parametrize(
    ("revenue", "nights", "expected"),
    [
        ("1400.0", 7, 200.0),
        ("509.0", 4, 127.25),
        ("346.0", 3, pytest.approx(115.3333, rel=1e-4)),
        (883.0, 3, pytest.approx(294.3333, rel=1e-4)),
    ],
)
def test_adr_is_rental_revenue_over_nights(revenue, nights, expected):
    """`rental_revenue`, never `total_cost`.

    `total_cost` reads 0.0 on a cancelled row while `rental_revenue` does not,
    and exceeds it on a booked row because it carries cleaning fees and taxes.
    Its meaning changes with status.
    """
    sample = history_sample(reservation(rental_revenue=revenue, no_of_days=nights))

    assert sample is not None
    assert sample[1] == expected


def test_total_cost_is_never_used():
    """A row whose total_cost would give a very different answer."""
    sample = history_sample(
        reservation(rental_revenue="700.0", total_cost="9999.0", no_of_days=7)
    )

    assert sample == (30, 100.0)


# -- assembling the map ---------------------------------------------------


class MultiListingClient:
    """Records exactly which listings were fetched, and how often."""

    def __init__(self, rows_by_listing=None, fail=False):
        self.rows = rows_by_listing or {}
        self.calls = []
        self.fail = fail

    def reservations(self, listing_id, pms, start, end):
        self.calls.append(listing_id)

        if self.fail:
            raise PriceLabsUnavailable("down")

        return self.rows.get(listing_id, [])


def test_history_is_keyed_by_listing_and_fetches_each_one_once():
    client = MultiListingClient(
        {
            BUNKERS: [reservation(), reservation(reservation_id="b2")],
            ARBORETUM: [reservation(listing_id=ARBORETUM, reservation_id="a1")],
        }
    )

    history = build_history(client, [BUNKERS, ARBORETUM])

    assert client.calls == [BUNKERS, ARBORETUM]
    assert set(history) == {BUNKERS, ARBORETUM}
    assert len(history[BUNKERS]) == 2
    assert history[ARBORETUM] == [(30, 200.0)]


def test_a_listing_with_no_usable_bookings_is_absent_rather_than_empty():
    """`history_adr` is None either way, and absent says so more plainly."""
    client = MultiListingClient({BUNKERS: [reservation(booking_status="cancelled")]})

    history = build_history(client, [BUNKERS])

    assert history == {}


def test_a_failed_fetch_returns_none_and_a_reason_not_an_empty_map():
    """The distinction the whole failure path rests on.

    `{}` means these properties genuinely have no usable bookings. `None`
    means we do not know. Since absent history makes LOWER unreachable,
    returning `{}` for a failure would make a network error look exactly like
    a considered decision not to lower a price.
    """
    history, problem = load_history(MultiListingClient(fail=True), [BUNKERS])

    assert history is None
    assert problem and "could not be loaded" in problem


def test_a_successful_load_reports_no_problem():
    history, problem = load_history(
        MultiListingClient({BUNKERS: [reservation()]}), [BUNKERS]
    )

    assert history == {BUNKERS: [(30, 200.0)]}
    assert problem is None


def test_the_requested_window_is_bounded_and_looks_both_ways():
    """A booking made today for next spring is evidence about lead time now."""
    seen = {}

    class Recorder(MultiListingClient):
        def reservations(self, listing_id, pms, start, end):
            seen["start"], seen["end"] = start, end

            return []

    build_history(Recorder(), [BUNKERS], today=datetime.date(2026, 9, 6))

    assert seen["start"] < "2026-09-06" < seen["end"]
