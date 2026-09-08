"""The browser's preflight, which `TestClient` never sends.

`DELETE /pricing/session` shipped without `DELETE` in the CORS allow-list. Every
backend test passed -- `TestClient` calls the ASGI app directly and issues no
preflight -- and the End-session button was nonetheless dead in a real browser,
failing with `net::ERR_FAILED` before the request was ever made.

So these tests do what a browser does: send the `OPTIONS` preflight and check
the answer. They are the cheapest possible stand-in for opening the console,
and they would have caught it.

Every value is invented. Nothing in this file reaches PriceLabs.
"""

import pytest

ORIGIN = "http://localhost:5173"


def preflight(api, method: str, path: str, headers: str = "authorization"):
    return api.anonymous().options(
        path,
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": headers,
        },
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/pricing/session"),
        ("POST", "/pricing/session"),
        # The one that was missing.
        ("DELETE", "/pricing/session"),
        ("GET", "/vacancy/recommendations"),
        ("POST", "/vacancy/recommendations/submit"),
        ("GET", "/pricing/cleanup"),
        ("POST", "/pricing/cleanup/run"),
        ("GET", "/pricing/outcomes"),
        ("POST", "/auth/login"),
    ],
)
def test_the_console_can_preflight_every_verb_it_uses(api, method, path):
    response = preflight(api, method, path)

    assert response.status_code == 200, (
        f"{method} {path} fails CORS preflight, so the browser never sends it"
    )
    assert response.headers["access-control-allow-origin"] == ORIGIN


def test_ending_a_pricing_session_survives_the_preflight(api):
    """The reported failure, as its own case.

    A control the owner cannot click is not a control, however correct the
    handler behind it is.
    """
    assert preflight(api, "DELETE", "/pricing/session").status_code == 200


def test_the_custom_source_header_is_allowed(api):
    """`X-AgentGuard-Source` makes a request non-simple; without it in the
    allow-list a browser would drop every console call."""
    response = preflight(
        api,
        "POST",
        "/pricing/session",
        headers="authorization,content-type,x-agentguard-source",
    )

    assert response.status_code == 200

    allowed = response.headers["access-control-allow-headers"].lower()

    assert "x-agentguard-source" in allowed


def test_an_unknown_origin_is_still_refused(api):
    """The allow-list stays an explicit local-dev list, never a wildcard."""
    response = api.anonymous().options(
        "/pricing/session",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "DELETE",
        },
    )

    assert response.headers.get("access-control-allow-origin") != "*"
    assert response.headers.get("access-control-allow-origin") != (
        "https://evil.example"
    )


def test_a_verb_the_console_does_not_use_is_not_allowed(api):
    """The list is what the console needs, not everything HTTP offers."""
    assert preflight(api, "PUT", "/pricing/session").status_code != 200
    assert preflight(api, "PATCH", "/pricing/session").status_code != 200
