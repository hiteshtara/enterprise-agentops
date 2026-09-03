"""Transport for Lodgify's messaging surface.

Deliberately separate from `LodgifyClient`, which stays read-only. This module
is the one place in the codebase that issues a Lodgify write, and it is written
to a different rulebook:

  * **It never retries.** The send endpoint has no idempotency key, so a retried
    POST can deliver a second real message to a real guest. There is no retry
    loop here, and no generic retry middleware may be wrapped around it.
  * **It classifies failure by whether the request could have taken effect**,
    not merely by whether it succeeded. A connection that was never established
    means nothing was sent; a read timeout means the message may already be on
    its way to a guest. Those are different outcomes and must never be merged.

Endpoints used, all documented and supported (docs/LODGIFY_API.md):

  GET  /v2/reservations/bookings
  GET  /v2/messaging/{threadGuid}
  POST /v1/reservation/booking/{id}/messages

No private `app.lodgify.com` endpoint, cookie or session token is used anywhere.
"""

import json
from typing import Any

import httpx

from app.connectors.lodgify.config import (
    BASE_URL,
    REQUEST_TIMEOUT_SECONDS,
    USER_AGENT,
)
from app.connectors.lodgify.errors import (
    LodgifySendAmbiguous,
    LodgifySendRefused,
    LodgifyUnavailable,
)

# The provider wants a JSON array and this content type specifically. Both are
# verified live -- see docs/LODGIFY_API.md section 10.
SEND_CONTENT_TYPE = "application/*+json"

# Pinned server-side, never model-supplied. `type` decides who the message
# appears to come from and `send_notification` decides whether anyone is told
# about it; neither belongs in a tool schema. See docs/LODGIFY_API.md section 18.
OWNER_MESSAGE_TYPE = "Owner"

SEND_NOTIFICATION = True

# Default page size only. Callers that need the archive pass the maximum and
# page explicitly.
#
# This constant used to carry the claim that the provider returns bookings
# "newest-first by update", and the Inbox was built on it. That claim is false:
# checked live against the whole account on 2026-09-03, the list is ordered by
# neither `created_at` nor `updated_at`, and no documented parameter sorts or
# filters it by message activity. Nothing may assume an order here again.
BOOKINGS_PAGE_SIZE = 50

MAX_BOOKINGS_PAGE_SIZE = 100

# The booking list is AgentGuard's only index of conversations, so it has to
# enumerate every booking that has a thread -- not a subset.
#
# `stayFilter` defaults to `Upcoming` upstream, and that default silently hid
# most of the account. Verified live 2026-09-03 against the real account:
#
#     default (unset)  145      <- what AgentGuard used to see
#     Upcoming         145
#     Current           12      <- guests in the property right now
#     Historic         908
#     All             1062
#
# A guest asking for a late checkout is, by definition, a `Current` stay. The
# most actionable conversations in the account were the ones the default
# excluded. Never send this endpoint an unfiltered request again.
STAY_FILTER_ALL = "All"

# What the Inbox actually enumerates. `All` is correct but not affordable: the
# archive is 1062 bookings, and reading a thread each -- the only way to learn a
# last-message time -- earns HTTP 429 from the provider. Verified live
# 2026-09-03: ~1050 thread reads rate-limit and fail closed to UNKNOWN, which
# silently *empties* the top of the Inbox rather than erroring. That is worse
# than the bug it was meant to fix.
#
# Current + Upcoming is 152 threads, reads clean in ~7s, and covers every guest
# who is in a property now or arriving later -- which is every conversation that
# can still need an answer. Historic stays are deliberately out; see
# `list_conversations` for what that costs.
INBOX_STAY_FILTERS = ("Current", "Upcoming")


class LodgifyMessagingClient:
    """Talks to Lodgify's messaging endpoints and returns raw provider payloads.

    Raw is deliberate: this layer does transport only. Sanitization happens one
    level up, in the inbox service, so there is exactly one place where fields
    are chosen and exactly one place to audit for leaks.

    The credential is supplied by a callable, resolved per call, and never
    stored on the instance, logged, or included in any return value.
    """

    def __init__(
        self,
        api_key_provider,
        transport: httpx.BaseTransport | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        base_url: str = BASE_URL,
    ) -> None:
        self._api_key_provider = api_key_provider
        self._transport = transport
        self._timeout = timeout
        self._base_url = base_url

    def headers(self) -> dict[str, str]:
        return {
            "X-ApiKey": self._api_key_provider(),
            "accept": "application/json",
            "User-Agent": USER_AGENT,
        }

    # -- reads -------------------------------------------------------------

    def get(self, path: str, params: dict[str, str] | None = None) -> Any:
        """One bounded GET. No retries: a slow provider must not stack calls."""
        try:
            with httpx.Client(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = client.get(path, params=params, headers=self.headers())

        except httpx.TimeoutException as exc:
            raise LodgifyUnavailable("The provider did not respond in time.") from exc

        except httpx.HTTPError as exc:
            # Deliberately excludes str(exc): a transport error can carry the
            # full request URL, and echoing provider internals is the habit
            # that leaks a credential later.
            raise LodgifyUnavailable("The provider could not be reached.") from exc

        if response.status_code >= 400:
            raise LodgifyUnavailable(
                f"The provider returned an error status ({response.status_code})."
            )

        try:
            return response.json()

        except ValueError as exc:
            raise LodgifyUnavailable(
                "The provider returned a response that could not be read."
            ) from exc

    def list_bookings(
        self,
        size: int = BOOKINGS_PAGE_SIZE,
        page: int = 1,
        stay_filter: str = STAY_FILTER_ALL,
    ) -> list[dict[str, Any]]:
        """Raw booking rows, one page, in whatever order the provider gives.

        The caller sanitizes. Every row carries guest contact details and
        financial fields, so nothing from here may be returned unfiltered.

        `stay_filter` is sent explicitly and defaults to every stay, because the
        provider's own default hides current and past ones -- see
        `STAY_FILTER_ALL`. Callers wanting a narrower slice must ask for it.

        The order is not specified and must not be relied on. Callers that care
        about recency page through everything and sort on a signal they have
        actually read.
        """
        payload = self.get(
            "/v2/reservations/bookings",
            {
                "page": str(max(page, 1)),
                "size": str(min(max(size, 1), MAX_BOOKINGS_PAGE_SIZE)),
                "includeCount": "false",
                "stayFilter": stay_filter,
            },
        )

        if not isinstance(payload, dict):
            raise LodgifyUnavailable(
                "The provider returned bookings in an unexpected shape."
            )

        items = payload.get("items")

        if not isinstance(items, list):
            raise LodgifyUnavailable(
                "The provider returned bookings in an unexpected shape."
            )

        return [row for row in items if isinstance(row, dict)]

    def get_thread(self, thread_uid: str) -> dict[str, Any]:
        """One raw conversation thread.

        Upstream returns `messages` newest-first and includes `guest_name` and
        `guest_email`; both facts are handled by the caller.
        """
        payload = self.get(f"/v2/messaging/{thread_uid}")

        if not isinstance(payload, dict):
            raise LodgifyUnavailable(
                "The provider returned a conversation in an unexpected shape."
            )

        return payload

    # -- the one write -----------------------------------------------------

    def post_message(
        self,
        booking_id: int,
        subject: str,
        message: str,
    ) -> None:
        """Send exactly one message. Never retried, under any circumstances.

        `type` and `send_notification` are pinned here rather than accepted from
        a caller, so no code path -- model, console or test -- can vary them.

        Returns None on success: the provider answers 200 with an empty body and
        no identifier, so there is nothing to return. The caller verifies by
        re-reading the thread (docs/LODGIFY_API.md section 16).

        Raises:
            LodgifySendRefused: the provider refused, or the connection was
                never established. Nothing was sent.
            LodgifySendAmbiguous: the request may already have taken effect.
                Never retry on this.
        """
        body = [
            {
                "subject": subject,
                "message": message,
                "type": OWNER_MESSAGE_TYPE,
                "send_notification": SEND_NOTIFICATION,
            }
        ]

        headers = {**self.headers(), "content-type": SEND_CONTENT_TYPE}

        try:
            with httpx.Client(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = client.post(
                    f"/v1/reservation/booking/{booking_id}/messages",
                    content=json.dumps(body).encode("utf-8"),
                    headers=headers,
                )

        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # The connection was never established, so the request cannot have
            # reached Lodgify. This is the one transport failure we can call
            # clean.
            raise LodgifySendRefused(
                "The provider could not be reached, so nothing was sent."
            ) from exc

        except httpx.HTTPError as exc:
            # Everything else -- read timeout, write error, pool timeout, a
            # broken response mid-flight -- means the request may already have
            # left the process and the message may already exist.
            raise LodgifySendAmbiguous(
                "The provider did not return a usable response."
            ) from exc

        if 400 <= response.status_code < 500:
            # The provider understood and rejected it. Nothing was created.
            raise LodgifySendRefused(
                f"The provider rejected the message ({response.status_code})."
            )

        if response.status_code >= 500:
            # A server error can follow partial processing, so this is not a
            # clean failure.
            raise LodgifySendAmbiguous(
                f"The provider returned a server error ({response.status_code})."
            )
