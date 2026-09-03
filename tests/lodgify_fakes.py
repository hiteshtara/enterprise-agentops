"""A scripted stand-in for Lodgify's messaging endpoints.

Every test in the messaging suites drives the real client through an
`httpx.MockTransport`. No test opens a socket, no test uses a real credential,
and no test can reach the live account.

The booking and thread payloads deliberately carry the fields that must be
dropped -- guest name, email and phone, `source_text`, financial totals -- so a
sanitization test is asserting against a payload shaped like the real one rather
than a convenient one. All of it is invented; none is real guest data.
"""

import json
from typing import Any

import httpx

from app.connectors.lodgify.inbox import LodgifyInbox
from app.connectors.lodgify.messaging_client import LodgifyMessagingClient
from app.connectors.lodgify.messaging_tools import LodgifyMessagingTools

FAKE_KEY = "test-only-not-a-real-lodgify-key"

ROSLINDALE_ID = 680420

BOSTON_CONDO_ID = 681293

THREAD_A = "11111111-1111-4111-8111-111111111111"

THREAD_B = "22222222-2222-4222-8222-222222222222"


def booking(
    booking_id: int,
    thread_uid: str,
    property_id: int = ROSLINDALE_ID,
    source: str = "BookingCom",
    status: str = "Booked",
) -> dict[str, Any]:
    """A booking row shaped like the real one, PII included so it can be
    asserted absent from everything downstream."""
    return {
        "id": booking_id,
        "thread_uid": thread_uid,
        "property_id": property_id,
        "source": source,
        # Untrusted free text, observed holding an embedded JSON blob upstream.
        # Nothing may parse or forward this.
        "source_text": json.dumps(
            {
                "listingId": "9999999999",
                "confirmationCode": "HMFAKE0000",
                "threadId": "1234567890",
            }
        ),
        "status": status,
        "arrival": "2026-11-25",
        "departure": "2026-11-29",
        "guest": {
            "name": "Fixture Guest",
            "email": "fixture.guest@example.invalid",
            "phone": "+15550000000",
        },
        "created_from_ip": "203.0.113.10",
        "total_amount": 1234.56,
        "amount_paid": 1234.56,
        "amount_due": 0.0,
        "currency_code": "USD",
        "notes": "internal note that must not travel",
        "updated_at": "2026-09-02T12:00:00",
    }


def message(
    identifier: str,
    sender: str,
    body: str,
    created_at: str,
    subject: str | None = "Re: your stay",
    message_status: str | None = "Delivered",
    route: str | None = "BookingCom",
) -> dict[str, Any]:
    return {
        "id": abs(hash(identifier)) % 1_000_000,
        "message_id": identifier,
        "type": sender,
        "subject": subject,
        "message": body,
        "date_created": created_at,
        "is_read": True,
        "is_imported": False,
        "message_status": message_status,
        "route": route,
        "attachments": [],
    }


def thread(
    thread_uid: str,
    messages: list[dict[str, Any]],
    is_read: bool = True,
) -> dict[str, Any]:
    """A thread payload. `messages` is returned newest-first, as upstream does."""
    return {
        "thread_uid": thread_uid,
        "subject": "Booking enquiry",
        "is_read": is_read,
        "is_closed": False,
        "last_message_date": messages[0]["date_created"] if messages else None,
        "guest_name": "Fixture Guest",
        "guest_email": "fixture.guest@example.invalid",
        "error_title": None,
        "error_message": None,
        "messages": list(reversed(messages)),
    }


class FakeLodgify:
    """Routes MockTransport requests and records what was sent.

    Thread reads can be scripted with a queue so a test can make the post-send
    re-read differ from the pre-send snapshot -- which is the whole point of the
    verification algorithm.
    """

    def __init__(
        self,
        bookings: list[dict[str, Any]] | None = None,
        threads: dict[str, dict[str, Any]] | None = None,
        thread_sequence: dict[str, list[dict[str, Any]]] | None = None,
        post_status: int = 200,
        post_raises: Exception | None = None,
        bookings_status: int = 200,
        thread_status: int = 200,
    ) -> None:
        self.bookings = bookings if bookings is not None else []
        self.threads = threads or {}
        self.thread_sequence = thread_sequence or {}
        self.post_status = post_status
        self.post_raises = post_raises
        self.bookings_status = bookings_status
        self.thread_status = thread_status

        self.requests: list[httpx.Request] = []

    @property
    def posts(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.method == "POST"]

    @property
    def booking_reads(self) -> list[httpx.Request]:
        return [
            request
            for request in self.requests
            if request.method == "GET"
            and request.url.path == "/v2/reservations/bookings"
        ]

    @property
    def thread_reads(self) -> list[httpx.Request]:
        return [
            request
            for request in self.requests
            if request.method == "GET" and "/v2/messaging/" in request.url.path
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)

        path = request.url.path

        if request.method == "POST":
            if self.post_raises is not None:
                raise self.post_raises

            # The live endpoint answers 200 with an empty body and no
            # identifier. Reproducing that exactly is what makes the
            # snapshot/diff tests meaningful.
            return httpx.Response(self.post_status, content=b"")

        if path == "/v2/reservations/bookings":
            if self.bookings_status != 200:
                return httpx.Response(self.bookings_status, json={})

            # Paginate the way upstream does, so a test can put a booking on
            # page two and prove the scan reaches it. A fixture shorter than one
            # page still comes back whole on page one, exactly as before.
            page = int(request.url.params.get("page", "1"))
            size = int(request.url.params.get("size", "50"))
            start = (page - 1) * size

            return httpx.Response(
                200,
                json={"items": self.bookings[start : start + size]},
            )

        if path.startswith("/v2/messaging/"):
            if self.thread_status != 200:
                return httpx.Response(self.thread_status, json={})

            uid = path.rsplit("/", 1)[-1]

            queue = self.thread_sequence.get(uid)

            if queue:
                return httpx.Response(200, json=queue.pop(0))

            return httpx.Response(200, json=self.threads.get(uid, {"messages": []}))

        return httpx.Response(404, json={})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def inbox(self, key_provider=None) -> LodgifyInbox:
        return LodgifyInbox(
            LodgifyMessagingClient(
                api_key_provider=key_provider or (lambda: FAKE_KEY),
                transport=self.transport(),
            )
        )

    def tools(self, key_provider=None) -> LodgifyMessagingTools:
        return LodgifyMessagingTools(self.inbox(key_provider))
