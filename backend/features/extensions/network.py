"""The host-mediated HTTP client: the only way a flow reaches the network.

A package never opens a socket, names a proxy, or chooses a TLS setting. It
declares exact origins at install time, the user approves them individually,
and ``http.request`` hands a URL to this module -- which decides, for every
request and every redirect hop, whether that URL is one of the approved ones
and which IP address it is allowed to reach.

Four properties carry the security weight, and each is enforced per hop rather
than once per call:

* **The origin is re-derived from the URL, never trusted from the manifest.**
  A computed URL, a redirect, a relative ``Location`` -- all of them go through
  :func:`parse_url` and then through the granted-origin set. Compile-time
  derivation pins a *literal* origin when there is one; this is what gates the
  rest.
* **The address is validated and then pinned.** The hostname is resolved once,
  every returned address is checked against the policy the *origin* earned, and
  the connection is made to one pinned address with the original ``Host`` header
  and TLS SNI preserved. There is no second resolution for a rebinding attack to
  win, and a public-looking name that answers ``127.0.0.1`` is refused rather
  than followed.
* **Secrets are substituted here and scanned for on the way back.** A
  ``{"$secret": name}`` marker becomes a real value inside this module and
  nowhere else, so the interpreter never holds one. Before any response becomes
  a flow value, it is scanned for the exact configured byte sequences, which
  stops ordinary reflection of the literal value.
* **Everything is bounded.** Request body, response bytes after decompression,
  redirect count, and total wall time, all with the numbers in
  :mod:`.limits`.

``trust_env=False`` is not a default we happen to keep -- it is the line that
stops a machine-wide ``HTTPS_PROXY`` from becoming a package-visible egress
path Orb never validated.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .digest import canonical_json_bytes
from .errors import FlowCancelled, FlowError, PackageError
from .json_loader import load_json
from .limits import (
    HTTP_TIMEOUT_SECONDS,
    MAX_HTTP_REDIRECTS,
    MAX_HTTP_REQUEST_BODY_BYTES,
    MAX_HTTP_RESPONSE_BYTES,
)
from .values import assert_json_bounds

logger = logging.getLogger(__name__)

SECRET_KEY = "$secret"

_DEFAULT_PORTS = {"http": 80, "https": 443}

_BODYLESS_METHODS = frozenset({"GET", "DELETE"})

MAX_HEADER_VALUE_BYTES = 4096
"""One request header value, after secret substitution.

Small on purpose: a header is a credential or a content negotiation, never a
payload. Without this an unbounded header would be the way around the request
body cap."""

MAX_REQUEST_HEADERS = 16


# ── destinations ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Destination:
    """One parsed, policy-checked request target.

    ``origin`` is canonical (lowercased host, default port omitted, IPv6
    compressed and bracketed) so the grant comparison is on one spelling.
    ``https://api.example.com`` and ``https://API.example.com:443`` are the same
    origin by RFC 6454, and treating them as two would mean a package could be
    refused for capitalising its own declared host.
    """

    scheme: str
    host: str
    port: int
    origin: str
    url: str

    @property
    def authority(self) -> str:
        """The ``Host`` header value: bracketed for an IPv6 literal, and
        carrying the port only when it is not the scheme's default."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return host if self.port == _DEFAULT_PORTS[self.scheme] else f"{host}:{self.port}"


def canonical_origin(scheme: str, host: str, port: int | None) -> str:
    """The canonical ``scheme://host[:port]`` spelling of one origin."""
    scheme = scheme.lower()
    host = host.lower().strip("[]")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    resolved = port if port is not None else _DEFAULT_PORTS[scheme]
    return f"{scheme}://{host}" if resolved == _DEFAULT_PORTS[scheme] else f"{scheme}://{host}:{resolved}"


def parse_url(raw: Any, *, what: str = "the request URL") -> Destination:
    """Parse and normalize one absolute http(s) URL, or raise :class:`FlowError`.

    Rejected outright: any non-``http``/``https`` scheme, userinfo, an empty
    host, a port outside 1-65535, and any control character or whitespace. The
    userinfo rejection matters more than it looks -- ``https://good.example@evil``
    reads as the granted origin to a human and resolves to the attacker's host,
    and it is exactly the shape a consent screen cannot defend against.
    """
    if not isinstance(raw, str) or not raw:
        raise FlowError(f"{what} is not a string")
    if len(raw) > 2048:
        raise FlowError(f"{what} is longer than 2048 characters")
    if any(character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in raw):
        raise FlowError(f"{what} contains whitespace or control characters")

    parts = urlsplit(raw)
    if parts.scheme not in _DEFAULT_PORTS:
        raise FlowError(f"{what} must use http or https")
    if "@" in parts.netloc:
        raise FlowError(f"{what} carries userinfo, which is never accepted")
    host = parts.hostname
    if not host:
        raise FlowError(f"{what} has no host")
    try:
        port = parts.port
    except ValueError:
        raise FlowError(f"{what} has an invalid port") from None
    port = port if port is not None else _DEFAULT_PORTS[parts.scheme]
    if not 1 <= port <= 65535:
        raise FlowError(f"{what} has a port outside 1-65535")
    if "*" in host:
        raise FlowError(f"{what} uses a wildcard host")

    origin = canonical_origin(parts.scheme, host, port)
    # Rebuild rather than pass the string through: the URL that reaches httpx is
    # the one this function validated, with the fragment (client-side only)
    # dropped and the authority in its canonical spelling.
    normalized = urlunsplit((parts.scheme, origin.split("://", 1)[1], parts.path or "/", parts.query, ""))
    return Destination(
        scheme=parts.scheme,
        host=host.lower().strip("[]"),
        port=port,
        origin=origin,
        url=normalized,
    )


def granted_origins(grants: Iterable[tuple[str, str | None]]) -> frozenset[str]:
    """The canonical origins a grant set approves for ``network.request``."""
    origins: set[str] = set()
    for capability, parameter in grants:
        if capability != "network.request" or not parameter:
            continue
        try:
            parsed = parse_url(
                parameter if "//" in parameter else f"https://{parameter}",
                what="a granted origin",
            )
        except FlowError:
            continue
        origins.add(parsed.origin)
    return frozenset(origins)


# ── address policy ───────────────────────────────────────────────────────────


def _effective_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address):
    """Unwrap IPv4-mapped and 6to4/Teredo IPv6 so the embedded v4 is judged.

    ``::ffff:127.0.0.1`` is loopback wearing an IPv6 spelling, and a policy that
    only looked at the outer address would call it global.
    """
    if isinstance(address, ipaddress.IPv6Address):
        for embedded in (address.ipv4_mapped, address.sixtofour):
            if embedded is not None:
                return embedded
        if address.teredo is not None:
            return address.teredo[1]
    return address


def address_rejection(raw: str, *, allow_local: bool) -> str | None:
    """Why *raw* may not be connected to, or ``None`` if it may.

    ``allow_local`` comes from the *origin the user approved*, not from the
    address: consenting to ``http://127.0.0.1:8188`` is consenting to a machine
    on your own network, and consenting to ``https://api.example.com`` is not --
    however that name happens to resolve today. That asymmetry is the whole
    DNS-rebinding defense, so it is a parameter here rather than a global.
    """
    try:
        address = _effective_address(ipaddress.ip_address(raw))
    except ValueError:
        return f"{raw!r} is not an IP address"
    if address.is_unspecified:
        return "the host resolves to an unspecified address"
    if address.is_multicast:
        return "the host resolves to a multicast address"
    # Order matters, and not for style. Python classifies ``::1`` and ``fe80::``
    # as *reserved* as well as loopback/link-local, so a reserved check placed
    # first would refuse the very addresses a confirmed-local origin exists to
    # reach. What survives this check is space that is unroutable regardless of
    # which network you are on -- 240.0.0.0/4 and the undelegated IPv6 blocks --
    # which no origin grant makes reachable.
    if address.is_reserved and not (address.is_loopback or address.is_link_local):
        return "the host resolves to a reserved address"
    if allow_local:
        return None
    if address.is_loopback:
        return "the host resolves to a loopback address, which needs a loopback origin grant"
    if address.is_link_local:
        return "the host resolves to a link-local address, which needs a local origin grant"
    if address.is_private:
        return "the host resolves to a private address, which needs a local origin grant"
    if not address.is_global:
        # Covers special-purpose ranges that are neither ``private`` nor
        # globally routable (notably carrier-grade NAT's 100.64.0.0/10).
        # A public-looking origin must not acquire LAN authority through one of
        # these less familiar address classes.
        return "the host resolves to a non-global address, which needs a local origin grant"
    return None


def address_is_local(raw: str) -> bool:
    """Whether *raw* is non-global address space a local origin may name."""
    try:
        address = _effective_address(ipaddress.ip_address(raw))
    except ValueError:
        return False
    return not address.is_global


def origin_allows_local_addresses(destination: Destination) -> bool:
    """Whether the approved origin string itself names a local destination.

    This is deliberately narrower than the consent UI's ``is_weak_origin``
    warning predicate. Every plain-HTTP origin deserves a warning, but
    ``http://public.example`` must not thereby gain authority to resolve to
    loopback or a private address. Local authority comes only from a host that
    is visibly local in the approved origin: a local IP literal, localhost,
    mDNS/home-arpa name, or an unqualified LAN hostname.
    """
    host = destination.host.rstrip(".").lower()
    try:
        address = _effective_address(ipaddress.ip_address(host))
    except ValueError:
        return (
            host == "localhost"
            or host.endswith(".localhost")
            or host.endswith(".local")
            or host.endswith(".home.arpa")
            or "." not in host
        )
    return not address.is_global


async def resolve_destination(destination: Destination) -> str:
    """Resolve *destination* and return the one address the connection may use.

    Every address the resolver returns is checked, not only the one that gets
    pinned. A round-robin record mixing a public address with ``169.254.169.254``
    would otherwise be a coin flip, and the attacker gets to flip it as many
    times as they like.
    """
    allow_local = origin_allows_local_addresses(destination)
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(destination.host, destination.port, type=socket.SOCK_STREAM)
    except OSError:
        raise FlowError(f"the host of {destination.origin} could not be resolved") from None
    addresses = list(dict.fromkeys(str(info[4][0]) for info in infos if info[4]))
    if not addresses:
        raise FlowError(f"the host of {destination.origin} resolved to no addresses")
    for candidate in addresses:
        rejection = address_rejection(candidate, allow_local=allow_local)
        if rejection is not None:
            raise FlowError(rejection)
    return addresses[0]


# ── secret substitution ──────────────────────────────────────────────────────


def substitute_secrets(value: Any, secrets: Mapping[str, str], *, what: str) -> Any:
    """Replace ``{"$secret": name}`` markers with their configured values.

    Called only inside this module, with the resolved header/body value the
    interpreter produced. A marker whose secret has no stored value fails the
    request: sending the literal string ``$secret`` to an origin, or an empty
    ``Authorization``, is a worse outcome than a flow that stops and says the
    secret is unset.

    A list of parts concatenates, which is what makes ``["Bearer ", {"$secret":
    "token"}]`` expressible without a template -- and templates deliberately
    cannot carry a secret, because a template's output is an ordinary flow
    value.
    """
    if isinstance(value, dict):
        if set(value) == {SECRET_KEY}:
            name = value[SECRET_KEY]
            stored = secrets.get(name) if isinstance(name, str) else None
            if not stored:
                raise FlowError(f"{what} uses the secret {name!r}, which has no value set")
            return stored
        return {key: substitute_secrets(item, secrets, what=what) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute_secrets(item, secrets, what=what) for item in value]
    return value


def _header_text(value: Any, secrets: Mapping[str, str], *, name: str) -> str:
    """Render one header value, concatenating a list of literal/secret parts."""
    parts = value if isinstance(value, list) else [value]
    rendered: list[str] = []
    for part in parts:
        substituted = substitute_secrets(part, secrets, what=f"header {name!r}")
        if not isinstance(substituted, str):
            raise FlowError(f"header {name!r} must be a string or a list of strings and secrets")
        rendered.append(substituted)
    text = "".join(rendered)
    if len(text.encode("utf-8")) > MAX_HEADER_VALUE_BYTES:
        raise FlowError(f"header {name!r} is over the {MAX_HEADER_VALUE_BYTES} byte limit")
    if any(character in text for character in "\r\n\x00"):
        raise FlowError(f"header {name!r} contains a control character")
    return text


_FORBIDDEN_HEADERS = frozenset({"host", "content-length", "connection", "transfer-encoding", "expect", "upgrade"})
"""Headers the transport owns. A package setting ``Host`` would be choosing a
different virtual host on the pinned address than the one the origin names."""


def build_headers(raw: Mapping[str, Any], secrets: Mapping[str, str]) -> dict[str, str]:
    """Validate, bound, and secret-substitute the package's request headers."""
    if len(raw) > MAX_REQUEST_HEADERS:
        raise FlowError(f"a request may carry at most {MAX_REQUEST_HEADERS} headers")
    headers: dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name or any(character in name for character in " \r\n\x00:"):
            raise FlowError("a request header name is malformed")
        if name.lower() in _FORBIDDEN_HEADERS:
            raise FlowError(f"header {name!r} is set by the host and cannot be overridden")
        headers[name] = _header_text(value, secrets, name=name)
    return headers


def encode_body(raw: Any, secrets: Mapping[str, str]) -> tuple[bytes, str]:
    """Encode a request body, returning ``(bytes, default content type)``.

    A string is sent verbatim; anything else is canonical JSON. The default
    content type is only a default -- an explicit package header wins, which is
    how a form-encoded or ``application/vnd.*`` body stays expressible.
    """
    substituted = substitute_secrets(raw, secrets, what="the request body")
    if isinstance(substituted, str):
        data, content_type = substituted.encode("utf-8"), "text/plain; charset=utf-8"
    else:
        try:
            data = canonical_json_bytes(substituted)
        except (TypeError, ValueError):
            raise FlowError("the request body is not encodable as JSON") from None
        content_type = "application/json"
    if len(data) > MAX_HTTP_REQUEST_BODY_BYTES:
        raise FlowError(f"the request body is {len(data)} bytes, over the {MAX_HTTP_REQUEST_BODY_BYTES} byte limit")
    return data, content_type


def find_reflected_secret(data: bytes, secrets: Mapping[str, str]) -> str | None:
    """The name of a configured secret that appears verbatim in *data*.

    Scanned before a response becomes a flow value, so the literal value cannot
    be read back out through state, the UI, a log, or a second request. It is
    not a claim that a granted origin cannot retain or transform what it was
    legitimately sent -- nothing Orb does can prevent that, and the consent copy
    says so.
    """
    for name, value in secrets.items():
        if value and value.encode("utf-8") in data:
            return name
    return None


# ── the client ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ResponseBytes:
    """An opaque handle over bounded response bytes.

    Deliberately not a JSON value: it has no scalar rendering, so a template
    substitution or a state write over it fails the ordinary way, and the only
    operation that declares it as an input is ``artifact.emit``. That is the
    design's "handles can flow only into operations that declare them" enforced
    by the type rather than by a check every sink has to remember.
    """

    media_type: str
    data: bytes

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<response bytes: {len(self.data)} bytes of {self.media_type}>"


class HttpService:
    """One extension's bounded egress, bound to its live grants and secrets.

    Constructed per invocation by the adapter. ``origins`` is a callable rather
    than a set for the same reason the grant view is: revoking a network origin
    must stop the *next* request of a flow that is already running, and a
    captured set would let it finish reaching an origin the user just withdrew.
    """

    def __init__(
        self,
        extension_id: str,
        *,
        origins: Callable[[], frozenset[str]],
        secrets: Callable[[], Awaitable[Mapping[str, str]]],
        is_cancelled: Callable[[], bool] = lambda: False,
    ) -> None:
        self.extension_id = extension_id
        self._origins = origins
        self._secrets = secrets
        self._is_cancelled = is_cancelled

    def _check_cancelled(self) -> None:
        """Abandon the request when the owning turn or connection went away.

        Checked before each hop and between response chunks, so cancellation
        stops work that is *outstanding* rather than only work that has not
        started. The interpreter's own between-steps check would otherwise let a
        cancelled turn hold a socket open for the full 30-second budget.
        """
        if self._is_cancelled():
            raise FlowCancelled("the invocation was cancelled")

    async def request(
        self,
        *,
        method: str,
        url: Any,
        headers: Mapping[str, Any],
        body: Any,
        response_kind: str,
    ) -> dict[str, Any]:
        """Perform one bounded request, following validated redirects.

        Returns ``{"status": int, "body": <json | text | ResponseBytes>}``. A
        non-2xx status raises, so a flow that wants to handle one declares
        ``on_error: "continue"`` with a fallback -- the same shape every other
        recoverable step failure uses.
        """
        deadline = time.monotonic() + HTTP_TIMEOUT_SECONDS
        secrets = dict(await self._secrets())
        destination = self._authorized(parse_url(url))
        request_headers = build_headers(headers, secrets)
        payload: bytes | None = None
        if body is not None and method not in _BODYLESS_METHODS:
            payload, default_type = encode_body(body, secrets)
            if not any(name.lower() == "content-type" for name in request_headers):
                request_headers["Content-Type"] = default_type

        async with httpx.AsyncClient(
            # No environment proxies, no environment CA overrides, no netrc: the
            # egress path is exactly what this module built.
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(HTTP_TIMEOUT_SECONDS, connect=10.0),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
        ) as client:
            for hop in range(MAX_HTTP_REDIRECTS + 1):
                self._check_cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FlowError(f"the request exceeded its {HTTP_TIMEOUT_SECONDS:.0f} second budget")
                status, location, data, media_type = await self._one_hop(
                    client,
                    method=method,
                    destination=destination,
                    headers=request_headers,
                    payload=payload,
                    remaining=remaining,
                )
                if location is None:
                    return self._decode(status, data, media_type, response_kind, secrets)
                if hop >= MAX_HTTP_REDIRECTS:
                    raise FlowError(f"the request followed more than {MAX_HTTP_REDIRECTS} redirects")
                destination, method, payload, request_headers = self._redirect(
                    destination, location, status, method, payload, request_headers
                )

        raise FlowError("the request could not be completed")  # pragma: no cover - loop always returns or raises

    # ── internals ────────────────────────────────────────────────────────────

    def _authorized(self, destination: Destination) -> Destination:
        """Refuse a destination whose origin is not currently granted."""
        if destination.origin not in self._origins():
            raise FlowError(f"this extension has not been granted network access to {destination.origin}")
        return destination

    async def _one_hop(
        self,
        client: httpx.AsyncClient,
        *,
        method: str,
        destination: Destination,
        headers: Mapping[str, str],
        payload: bytes | None,
        remaining: float,
    ) -> tuple[int, str | None, bytes, str]:
        """Connect to one pinned address and read a bounded response.

        The URL handed to httpx carries the *address*; ``Host`` and
        ``sni_hostname`` carry the name. That split is what pins the connection
        without breaking virtual hosting or certificate verification -- the
        certificate is still checked against the hostname the origin names,
        because that is what SNI passes to the TLS handshake.
        """
        address = await resolve_destination(destination)
        literal = f"[{address}]" if ":" in address else address
        parts = urlsplit(destination.url)
        pinned = urlunsplit((parts.scheme, f"{literal}:{destination.port}", parts.path, parts.query, ""))

        request = client.build_request(
            method,
            pinned,
            headers={**headers, "Host": destination.authority},
            content=payload,
            timeout=httpx.Timeout(remaining, connect=min(10.0, remaining)),
            extensions={"sni_hostname": destination.host},
        )
        try:
            response = await client.send(request, stream=True)
        except httpx.HTTPError:
            # The exception text can carry the resolved address and the full
            # URL; neither belongs in a message a package's own error handler
            # (or an action's HTTP response) will read.
            raise FlowError(f"the request to {destination.origin} failed") from None
        try:
            if response.status_code in (301, 302, 303, 307, 308):
                await response.aclose()
                location = response.headers.get("location")
                if not location:
                    raise FlowError(f"{destination.origin} sent a redirect with no location")
                return response.status_code, location, b"", ""
            data = await self._read_bounded(response, destination)
        finally:
            await response.aclose()

        if not 200 <= response.status_code < 300:
            raise FlowError(f"{destination.origin} answered with HTTP {response.status_code}")
        return (
            response.status_code,
            None,
            data,
            response.headers.get("content-type", "").split(";")[0].strip(),
        )

    async def _read_bounded(self, response: httpx.Response, destination: Destination) -> bytes:
        """Accumulate the decompressed body, aborting past the byte budget.

        Decompressed, which is the number that matters: a 5 MiB cap on the
        compressed stream would admit a gigabyte of zeros. httpx decodes as it
        streams, so the check is at the streaming boundary and the process never
        holds the payload it is rejecting.
        """
        chunks: list[bytes] = []
        total = 0
        try:
            async for chunk in response.aiter_bytes():
                self._check_cancelled()
                total += len(chunk)
                if total > MAX_HTTP_RESPONSE_BYTES:
                    raise FlowError(f"the response from {destination.origin} exceeds the {MAX_HTTP_RESPONSE_BYTES} byte limit")
                chunks.append(chunk)
        except httpx.HTTPError:
            raise FlowError(f"the response from {destination.origin} could not be read") from None
        return b"".join(chunks)

    def _redirect(
        self,
        current: Destination,
        location: str,
        status: int,
        method: str,
        payload: bytes | None,
        headers: dict[str, str],
    ) -> tuple[Destination, str, bytes | None, dict[str, str]]:
        """Validate one redirect hop and decide what carries over.

        Cross-origin hops drop every package-supplied header. A redirect is the
        origin's choice, not the package's, so an ``Authorization`` header that
        followed one would deliver a secret to a host the consent screen never
        named -- and the taint scan would not help, because the leak is outbound.
        """
        target = urlsplit(location)
        absolute = location if target.scheme else urlunsplit(_join(urlsplit(current.url), target))
        destination = self._authorized(parse_url(absolute, what="the redirect target"))
        if status == 303 or (status in (301, 302) and method not in ("GET", "HEAD")):
            method, payload = "GET", None
        if destination.origin != current.origin:
            headers = {}
            payload = None if method in _BODYLESS_METHODS else payload
        return destination, method, payload, dict(headers)

    def _decode(
        self,
        status: int,
        data: bytes,
        media_type: str,
        response_kind: str,
        secrets: Mapping[str, str],
    ) -> dict[str, Any]:
        """Turn bounded bytes into the declared response shape.

        The reflection scan runs *before* the decode, on the raw bytes, so a
        secret cannot be smuggled back through an encoding the JSON parser would
        normalize away.
        """
        reflected = find_reflected_secret(data, secrets)
        if reflected is not None:
            raise FlowError(f"the response reflected the secret {reflected!r} and was discarded")
        if response_kind == "bytes":
            return {
                "status": status,
                "body": ResponseBytes(media_type=media_type or "application/octet-stream", data=data),
            }
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise FlowError("the response is not valid UTF-8") from None
        if response_kind == "text":
            assert_json_bounds(text, what="HTTP text response")
            return {"status": status, "body": text}
        try:
            decoded = load_json(data, what="HTTP JSON response", max_bytes=MAX_HTTP_RESPONSE_BYTES)
        except PackageError:
            raise FlowError("the response is not valid JSON") from None
        assert_json_bounds(decoded, what="HTTP JSON response")
        return {"status": status, "body": decoded}


def _join(base, target):
    """Resolve a relative ``Location`` against the current URL.

    Deliberately not ``urljoin``: this keeps the scheme and authority of the
    *validated* base rather than re-parsing a string an origin controls, and the
    result goes back through :func:`parse_url` anyway.
    """
    path = target.path
    if not path:
        path = base.path
    elif not path.startswith("/"):
        path = base.path.rsplit("/", 1)[0] + "/" + path
    return (base.scheme, base.netloc, path, target.query, "")


__all__ = [
    "Destination",
    "HttpService",
    "ResponseBytes",
    "address_is_local",
    "address_rejection",
    "build_headers",
    "canonical_origin",
    "encode_body",
    "find_reflected_secret",
    "granted_origins",
    "origin_allows_local_addresses",
    "parse_url",
    "resolve_destination",
    "substitute_secrets",
]
