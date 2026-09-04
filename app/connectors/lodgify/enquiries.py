"""Open enquiries: listing, resolution, and thread reads. GET only.

Deliberately separate from `app/connectors/lodgify/inbox.py`. That module is
the booked-guest pipeline -- discovery, activity, drafts, and the one governed
send. This one is the operator's on-demand enquiry helper, and it has no send
method at all. The absence is the safety property: there is no code path from
this module to a provider write.

Provider facts this module is built on, verified live against the account on
2026-09-03 and not to be re-derived:

  * `GET /v1/reservation` is the enquiry-bearing list. Paging is **offset and
    limit**. `page`/`size` are silently ignored, which would mean every page
    after the first was a duplicate of the first -- so those spellings must
    never appear here.
  * Rows carry `type` of `Booking`, `Enquiry` or `ClosedPeriod`. `ClosedPeriod`
    rows are calendar blocks, not conversations.
  * **The `type=Enquiry` query filter is ignored by the provider.** Filtering
    happens here, in our code, on the row's own `type`. Nothing may rely on the
    provider having narrowed the list.
  * `GET /v1/reservation/enquiry/{id}` answers 500 for every enquiry tried. It
    is not used, and must not be.
  * The thread is read with `GET /v2/messaging/{threadGuid}`, the same endpoint
    and the same client the booking path uses.

Provider identifiers stop here, exactly as they do in the inbox module. Above
this line everything speaks `enquiry_ref`; the numeric id and the `thread_uid`
exist on `ResolvedEnquiry` and nowhere above it.
"""

from dataclasses import dataclass
from typing import Any

from app.connectors.lodgify.config import LODGIFY_PROPERTIES
from app.connectors.lodgify.errors import LodgifyUnavailable
from app.connectors.lodgify.inbox import read_messages
from app.connectors.lodgify.messaging_client import LodgifyMessagingClient
from app.connectors.lodgify.messaging_models import ConversationMessage
from app.connectors.lodgify.refs import enquiry_ref_for, is_well_formed_enquiry_ref

RESERVATION_PATH = "/v1/reservation"

# The narrowing that makes this affordable: `status=Open` takes the list from
# the whole archive to ~117 rows, about three pages.
OPEN_STATUS = "Open"

ENQUIRY_TYPE = "Enquiry"

# Rows per request. The provider honours `limit=50`.
PAGE_SIZE = 50

# A safety cap on paging, not a working limit. `status=Open` is ~117 rows
# against the live account, so 12 pages is six times the observed need and
# still bounds a provider that starts answering `next` forever.
MAX_PAGES = 12

MIN_LIMIT = 1

MAX_LIMIT = 100

DEFAULT_LIMIT = 20

UNKNOWN_ENQUIRY = "Unknown enquiry."

PROPERTIES_BY_ID = {prop.lodgify_property_id: prop for prop in LODGIFY_PROPERTIES}


@dataclass(frozen=True)
class ResolvedEnquiry:
    """One open enquiry, resolved to what the provider needs.

    Never returned to a caller. `enquiry_id` and `thread_uid` exist on this
    object and nowhere above it; `summary()` is the only projection that
    crosses the API boundary.
    """

    enquiry_ref: str
    enquiry_id: int
    thread_uid: str
    property_slug: str | None
    property_name: str | None
    source: str | None
    arrival: str | None
    departure: str | None
    is_replied: bool | None
    # Read for ordering only, and deliberately absent from `summary()`. The
    # operator needs the newest enquiry first; they do not need a timestamp
    # they cannot act on.
    created_at: str | None

    def summary(self) -> dict[str, Any]:
        """The safe metadata, and only that.

        No guest name, email or phone. No numeric id, no `thread_uid`, no
        `upgraded_enquiry_id`, no totals, and no `source_text` -- which
        docs/LODGIFY_API.md section 6 records as untrusted free text observed
        carrying an embedded JSON blob.
        """
        return {
            "enquiry_ref": self.enquiry_ref,
            "property_slug": self.property_slug,
            "property_name": self.property_name,
            "source": self.source,
            "arrival": self.arrival,
            "departure": self.departure,
            "is_replied": self.is_replied,
        }


def read_enquiry(row: dict[str, Any]) -> ResolvedEnquiry | None:
    """Construct the internal view of one enquiry, field by field.

    Eight fields are read and nothing else. The upstream row also carries a
    guest block with name, email and phone, `source_text`, totals, and
    `upgraded_enquiry_id`; none of it is read, so none of it can leak.

    A row without a usable id or thread is dropped rather than guessed at: an
    enquiry whose thread cannot be addressed is one this feature cannot do
    anything with.
    """
    enquiry_id = row.get("id")
    thread_uid = row.get("thread_uid")

    if not isinstance(enquiry_id, int) or isinstance(enquiry_id, bool):
        return None

    if not isinstance(thread_uid, str) or not thread_uid:
        return None

    property_id = row.get("property_id")
    prop = PROPERTIES_BY_ID.get(property_id) if isinstance(property_id, int) else None

    source = row.get("source")
    arrival = row.get("arrival")
    departure = row.get("departure")
    is_replied = row.get("is_replied")
    created_at = row.get("created_at")

    return ResolvedEnquiry(
        enquiry_ref=enquiry_ref_for(enquiry_id),
        enquiry_id=enquiry_id,
        thread_uid=thread_uid,
        # The property name comes from our own closed allowlist, never from the
        # provider's row, for the same reason the booking path does it: the
        # console must show a name this deployment actually knows about.
        property_slug=prop.slug if prop else None,
        property_name=prop.display_name if prop else None,
        source=source if isinstance(source, str) else None,
        arrival=arrival if isinstance(arrival, str) else None,
        departure=departure if isinstance(departure, str) else None,
        is_replied=is_replied if isinstance(is_replied, bool) else None,
        created_at=created_at if isinstance(created_at, str) else None,
    )


def validate_limit(value: object) -> int:
    """Bound the response size. Raises rather than coercing a bad value."""
    if value is None:
        return DEFAULT_LIMIT

    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"limit must be an integer, got {type(value).__name__}.")

    if value < MIN_LIMIT or value > MAX_LIMIT:
        raise ValueError(
            f"limit must be between {MIN_LIMIT} and {MAX_LIMIT}, got {value}."
        )

    return value


class LodgifyEnquiries:
    """Reads open enquiries and their threads. It has no write method."""

    def __init__(self, client: LodgifyMessagingClient) -> None:
        self._client = client

    def pages(self) -> list[dict[str, Any]]:
        """Every open reservation row, walked by offset.

        `offset`/`limit` are the only paging parameters sent. Sending `page` or
        `size` here would be worse than useless: the provider ignores them, so
        every request would return the first page again and the walk would
        either loop or silently truncate.
        """
        rows: list[dict[str, Any]] = []

        for page in range(MAX_PAGES):
            payload = self._client.get(
                RESERVATION_PATH,
                {
                    "status": OPEN_STATUS,
                    "offset": str(page * PAGE_SIZE),
                    "limit": str(PAGE_SIZE),
                },
            )

            if not isinstance(payload, dict):
                raise LodgifyUnavailable(
                    "The provider returned enquiries in an unexpected shape."
                )

            items = payload.get("items")

            if not isinstance(items, list):
                raise LodgifyUnavailable(
                    "The provider returned enquiries in an unexpected shape."
                )

            rows.extend(row for row in items if isinstance(row, dict))

            # Two independent stop conditions, because either one alone has a
            # failure mode: a short page means the end regardless of what
            # `next` says, and an absent `next` means the end even when the
            # last page happened to be full.
            if len(items) < PAGE_SIZE or not payload.get("next"):
                break

        return rows

    def scan(self) -> list[ResolvedEnquiry]:
        """Open enquiries, newest first.

        The `type` test is the whole filter and it happens here, on the row we
        actually received -- `Booking` and `ClosedPeriod` are excluded by not
        matching, rather than by a provider parameter that is ignored.
        """
        enquiries: list[ResolvedEnquiry] = []

        for row in self.pages():
            if row.get("type") != ENQUIRY_TYPE:
                continue

            enquiry = read_enquiry(row)

            if enquiry is not None:
                enquiries.append(enquiry)

        return sorted(
            enquiries,
            key=lambda item: (item.created_at or "", item.enquiry_ref),
            reverse=True,
        )

    def open_page(self, limit: int | None = None) -> tuple[list[dict[str, Any]], int]:
        """Safe metadata for the open enquiries, bounded by `limit`, and the total.

        The total is how many open enquiries there actually are, not how many
        were returned. Twenty rows out of forty-seven and twenty out of twenty
        look identical on screen, and an operator who cannot tell them apart
        will believe they have seen the queue.

        One scan answers both. Counting separately would walk the provider's
        pages a second time for a number the first walk already had, and this
        surface reads live on every press.
        """
        count = validate_limit(limit)

        enquiries = self.scan()

        return [enquiry.summary() for enquiry in enquiries[:count]], len(enquiries)

    def list_open(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Safe metadata for the open enquiries, bounded by `limit`."""
        return self.open_page(limit)[0]

    def resolve(self, enquiry_ref: object) -> ResolvedEnquiry:
        """`enquiry_ref` -> the provider identifiers, by re-listing.

        No server-side state lives between a list and a draft. A fabricated ref
        matches no real enquiry and raises, so a caller cannot reach a thread it
        was never shown.

        Raises:
            ValueError: the ref is malformed or names no open enquiry.
        """
        if not is_well_formed_enquiry_ref(enquiry_ref):
            raise ValueError(UNKNOWN_ENQUIRY)

        for enquiry in self.scan():
            if enquiry.enquiry_ref == enquiry_ref:
                return enquiry

        raise ValueError(UNKNOWN_ENQUIRY)

    def read_thread(
        self,
        enquiry_ref: object,
    ) -> tuple[ResolvedEnquiry, tuple[ConversationMessage, ...]]:
        """One enquiry and its sanitized messages, oldest first.

        Message sanitization is the booking path's `read_messages`, reused
        rather than reimplemented: one place decides which message fields exist,
        so an enquiry thread cannot leak a field a booking thread drops.
        """
        enquiry = self.resolve(enquiry_ref)

        thread = self._client.get_thread(enquiry.thread_uid)

        return enquiry, read_messages(thread)
