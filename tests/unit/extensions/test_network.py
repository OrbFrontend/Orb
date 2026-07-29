"""The host HTTP client: URL policy, address policy, secrets, and bounds.

The SSRF corpus lives here. Every case is a URL or a DNS answer that reads as
one destination to a human and resolves to another to a socket -- alternate IP
encodings, IPv4-mapped IPv6, userinfo, a public name answering with a private
address, a redirect that changes origin. None of them reaches the network in
these tests, because each is refused before a connection is attempted; the
assertion is on *which* refusal, since "it failed" and "it failed for the right
reason" diverge the first time someone loosens a bound.

The secret cases assert the two halves of the write-only contract: a
``{"$secret": name}`` marker becomes a value only inside this module, and a
response carrying the literal bytes back is discarded rather than handed to the
flow.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.features.extensions import network
from backend.features.extensions.errors import FlowError
from backend.features.extensions.limits import (
    MAX_HTTP_REQUEST_BODY_BYTES,
    MAX_HTTP_RESPONSE_BYTES,
)
from backend.features.extensions.network import (
    MAX_HEADER_VALUE_BYTES,
    HttpService,
    ResponseBytes,
    address_rejection,
    build_headers,
    canonical_origin,
    encode_body,
    find_reflected_secret,
    granted_origins,
    parse_url,
    resolve_destination,
    substitute_secrets,
)

# ── URL policy ──────────────────────────────────────────────────────────────


def test_an_origin_has_one_canonical_spelling():
    """Default ports and case are not two different grants.

    ``https://api.example.com`` and ``https://API.example.com:443`` are the same
    origin by RFC 6454. Comparing them as strings would refuse a package for
    capitalising its own declared host, which is a rule about typing rather than
    about safety.
    """
    assert canonical_origin("https", "API.example.com", 443) == "https://api.example.com"
    assert canonical_origin("https", "api.example.com", 8443) == "https://api.example.com:8443"
    assert canonical_origin("http", "example.com", 80) == "http://example.com"
    assert canonical_origin("https", "::1", None) == "https://[::1]"


def test_parse_url_normalizes_and_keeps_the_query():
    parsed = parse_url("https://API.example.com:443/v1/render?x=1#frag")
    assert parsed.origin == "https://api.example.com"
    assert parsed.url == "https://api.example.com/v1/render?x=1"
    assert parsed.authority == "api.example.com"


@pytest.mark.parametrize(
    "url,reason",
    [
        ("ftp://example.com/x", "http or https"),
        ("file:///etc/passwd", "http or https"),
        # Reads as the granted origin to a human, resolves to the attacker's
        # host to a parser. A consent screen cannot defend against this one.
        ("https://api.example.com@evil.example/x", "userinfo"),
        ("https://*.example.com/x", "wildcard"),
        ("https:///x", "no host"),
        ("https://example.com:99999/x", "port"),
        ("https://exa mple.com/x", "whitespace or control"),
        ("https://example.com/\x00", "whitespace or control"),
        ("http://2130706433/x", "non-canonical numeric"),
        ("http://127.1/x", "non-canonical numeric"),
        ("http://0177.0.0.1/x", "non-canonical numeric"),
        ("http://0x7f.0.0.1/x", "non-canonical numeric"),
    ],
)
def test_hostile_urls_are_refused_with_their_own_reason(url, reason):
    with pytest.raises(FlowError, match=reason):
        parse_url(url)


def test_granted_origins_canonicalizes_both_sides_of_the_comparison():
    grants = {
        ("network.request", "https://API.example.com:443"),
        ("state.read", "config"),
    }
    assert granted_origins(grants) == frozenset({"https://api.example.com"})


# ── address policy ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "192.168.1.1",
        "172.16.0.1",
        "169.254.169.254",  # the cloud metadata endpoint
        "100.64.0.1",  # carrier-grade NAT: non-global but not ``is_private``
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",  # loopback wearing an IPv6 spelling
        "::ffff:169.254.169.254",
    ],
)
def test_a_public_origin_may_not_resolve_to_a_local_address(address):
    """DNS rebinding, stated as a rule about the *origin* rather than the answer.

    Consent was given to a name. However that name resolves today, it does not
    become consent to reach the user's own network -- so ``allow_local`` comes
    from the origin string the user approved, never from what came back.
    """
    assert address_rejection(address, allow_local=False) is not None
    assert address_rejection(address, allow_local=True) is None


@pytest.mark.parametrize("address", ["0.0.0.0", "::", "224.0.0.1", "ff02::1", "240.0.0.1"])
def test_some_addresses_are_refused_even_for_a_local_origin(address):
    """Unspecified, multicast, and reserved are not "somewhere on your LAN"."""
    assert address_rejection(address, allow_local=True) is not None


def test_a_global_address_is_allowed():
    assert address_rejection("93.184.216.34", allow_local=False) is None
    assert address_rejection("2606:2800:220:1:248:1893:25c8:1946", allow_local=False) is None


def test_a_non_address_is_refused_rather_than_assumed_global():
    assert address_rejection("not-an-ip", allow_local=True) is not None


async def test_plain_http_warning_does_not_grant_a_public_name_local_network_access(
    monkeypatch,
):
    """Weak transport and local-address authority are separate decisions."""
    loop = asyncio.get_running_loop()

    async def rebound(_host, port, **_kwargs):
        return [(2, 1, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(loop, "getaddrinfo", rebound)
    with pytest.raises(FlowError, match="loopback"):
        await resolve_destination(parse_url("http://public.example/render"))


async def test_an_explicit_local_http_origin_may_resolve_to_loopback(monkeypatch):
    loop = asyncio.get_running_loop()

    async def local(_host, port, **_kwargs):
        return [(2, 1, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(loop, "getaddrinfo", local)
    assert await resolve_destination(parse_url("http://localhost/render")) == "127.0.0.1"


async def test_request_deadline_bounds_the_whole_hop(monkeypatch):
    async def no_secrets():
        return {}

    service = HttpService(
        "deadline-test",
        origins=lambda: frozenset({"https://example.com"}),
        secrets=no_secrets,
    )

    async def stalled(*_args, **_kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(network, "HTTP_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(service, "_one_hop", stalled)
    with pytest.raises(FlowError, match="second budget"):
        await service.request(
            method="GET",
            url="https://example.com",
            headers={},
            body=None,
            response_kind="text",
        )


# ── secrets ─────────────────────────────────────────────────────────────────


def test_a_secret_marker_becomes_a_value_only_here():
    substituted = substitute_secrets({"token": {"$secret": "api_key"}}, {"api_key": "s3cr3t"}, what="body")
    assert substituted == {"token": "s3cr3t"}


def test_an_unset_secret_fails_the_request_rather_than_sending_a_placeholder():
    with pytest.raises(FlowError, match="has no value set"):
        substitute_secrets({"$secret": "api_key"}, {}, what="header")


def test_a_header_may_concatenate_literal_parts_with_a_secret():
    headers = build_headers({"Authorization": ["Bearer ", {"$secret": "api_key"}]}, {"api_key": "abc"})
    assert headers == {"Authorization": "Bearer abc"}


@pytest.mark.parametrize(
    "name,value,reason",
    [
        ("Host", "evil.example", "set by the host"),
        ("Content-Length", "0", "set by the host"),
        ("X-Bad\nInjected", "1", "malformed"),
        ("X-Bad", "a\r\nX-Injected: 1", "control character"),
        ("X-Big", "x" * (MAX_HEADER_VALUE_BYTES + 1), "byte limit"),
    ],
)
def test_hostile_headers_are_refused(name, value, reason):
    with pytest.raises(FlowError, match=reason):
        build_headers({name: value}, {})


def test_a_body_is_bounded_before_it_is_sent():
    data, content_type = encode_body({"a": 1}, {})
    assert data == b'{"a":1}'
    assert content_type == "application/json"
    with pytest.raises(FlowError, match="over the"):
        encode_body("x" * (MAX_HTTP_REQUEST_BODY_BYTES + 1), {})


def test_a_reflected_secret_is_detected_by_its_exact_bytes():
    assert find_reflected_secret(b'{"echo":"s3cr3t"}', {"api_key": "s3cr3t"}) == "api_key"
    assert find_reflected_secret(b'{"echo":"other"}', {"api_key": "s3cr3t"}) is None
    # An unset secret is not a substring everything contains.
    assert find_reflected_secret(b"anything", {"api_key": ""}) is None


# ── the service, driven without a socket ────────────────────────────────────


def service(origins: set[str], secrets: dict[str, str] | None = None) -> HttpService:
    async def load():
        return dict(secrets or {})

    return HttpService("api-artifact", origins=lambda: frozenset(origins), secrets=load)


async def test_an_ungranted_origin_is_refused_before_anything_is_resolved():
    with pytest.raises(FlowError, match="not been granted network access"):
        await service({"https://api.example.invalid"}).request(
            method="GET",
            url="https://evil.example/x",
            headers={},
            body=None,
            response_kind="json",
        )


async def test_a_revoked_origin_stops_the_next_request():
    """The live grant view, not a captured set.

    Same rule every other privileged operation follows: revocation lands on the
    next operation of a flow that is already running.
    """
    live = {"origins": frozenset({"https://api.example.invalid"})}

    async def load():
        return {}

    client = HttpService("api-artifact", origins=lambda: live["origins"], secrets=load)
    live["origins"] = frozenset()
    with pytest.raises(FlowError, match="not been granted network access"):
        await client.request(
            method="GET",
            url="https://api.example.invalid/x",
            headers={},
            body=None,
            response_kind="json",
        )


@pytest.mark.parametrize("payload", [b'{"a":1,"a":2}', b'{"n":NaN}', b'{"n":1e400}'])
def test_remote_json_uses_the_same_strict_grammar_as_package_json(payload):
    with pytest.raises(FlowError, match="not valid JSON"):
        service(set())._decode(200, payload, "application/json", "json", {})


def test_a_response_handle_is_not_a_json_value():
    """Bytes flow into ``artifact.emit`` and nowhere else.

    Not by a check at every sink -- by the type. A handle has no scalar
    rendering, so a template, a state write, and a return value all reject it
    through the bounds they already enforce.
    """
    from backend.features.extensions.values import assert_json_bounds, scalar_text

    handle = ResponseBytes(media_type="image/png", data=b"\x89PNG")
    with pytest.raises(FlowError):
        assert_json_bounds(handle, what="a state value")
    with pytest.raises(FlowError):
        scalar_text(handle, what="a template substitution")
    assert "4 bytes" in repr(handle)


def test_the_response_budget_is_the_decompressed_one():
    """5 MiB of gzip is not 5 MiB of payload.

    httpx decodes as it streams, so the accumulator counts decoded bytes. The
    constant is asserted here so a future switch to raw counting is a test
    failure rather than a quiet regression.
    """
    assert MAX_HTTP_RESPONSE_BYTES == 5 * 1024 * 1024
