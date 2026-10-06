"""Decide which names and pages may reach the server through a visitor's browser.

DNS rebinding points a hostile page's own domain at this machine, so its requests arrive carrying that domain in Host. An open
server therefore answers only names public DNS cannot hand an attacker: IP addresses, single-label machine names, local-only
suffixes and Tailscale MagicDNS. A browser also names the page behind every write in Origin, and a write from another page is
refused, which stops a page that never rebinds from posting forms or uploads at the server.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlsplit

# Names under these suffixes resolve only on this machine, this network or a tailnet, never through public DNS.
LOCAL_SUFFIXES = (".localhost", ".local", ".lan", ".internal", ".home.arpa", ".ts.net")
_DEFAULT_PORTS = {"http": "80", "https": "443"}


@dataclass(frozen=True)
class AllowedHosts:
    """Extra names from ``ORB_ALLOWED_HOSTS``: exact names, ``.suffix`` entries, or ``*`` for any Host."""

    names: frozenset[str] = frozenset()
    suffixes: tuple[str, ...] = ()
    any_host: bool = False

    def lists(self, name: str) -> bool:
        """Whether *name* is listed by name or suffix. ``*`` vouches for Host names only, never for a page's Origin."""
        return name in self.names or any(name == s[1:] or name.endswith(s) for s in self.suffixes)


@lru_cache(maxsize=8)
def parse_allowed_hosts(raw: str) -> AllowedHosts:
    """Parse a comma- or space-separated list; ``*.example.com`` and ``.example.com`` both mean the domain and its subdomains."""
    names: set[str] = set()
    suffixes: list[str] = []
    any_host = False
    for entry in raw.replace(",", " ").split():
        if entry == "*":
            any_host = True
        elif entry.startswith(("*.", ".")):
            suffix = host_name(entry.lstrip("*"))
            if suffix:
                suffixes.append(f".{suffix.lstrip('.')}")
        elif name := host_name(entry):
            names.add(name)
    return AllowedHosts(frozenset(names), tuple(suffixes), any_host)


def host_name(netloc: str) -> str:
    """The lowercase name in a Host value or ``host[:port]``, without port, IPv6 brackets or a trailing dot."""
    value = netloc.strip().lower()
    if value.startswith("["):
        value = value[1 : value.find("]")] if "]" in value else value[1:]
    elif value.count(":") == 1:
        value = value.split(":", 1)[0]
    return value.rstrip(".")


def rebind_safe_host(host: str | None, allowed: AllowedHosts) -> bool:
    """Whether a request naming *host* cannot be a rebound page: no attacker can make public DNS answer for this name."""
    if not host:
        return True  # Browsers always send Host; a request without one comes from a tool, not a page.
    if allowed.any_host:
        return True
    name = host_name(host)
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return "." not in name or name.endswith(LOCAL_SUFFIXES) or allowed.lists(name)


def same_origin_write(
    origin: str | None, fetch_site: str | None, host: str | None, forwarded_host: str | None, allowed: AllowedHosts
) -> bool:
    """Whether a state-changing request came from a page served by this server.

    A reverse proxy may rewrite Host; ``X-Forwarded-Host`` then names what the browser asked for. A page cannot set that header
    on a cross-origin request without a CORS preflight, which this server never grants, so trusting it opens nothing.
    """
    if origin is None:
        # Every browser names the page behind a write; without Origin this is a script or tool unless the fetch metadata says otherwise.
        return fetch_site not in ("cross-site", "same-site")
    parts = urlsplit(origin)
    if parts.scheme not in _DEFAULT_PORTS or not parts.hostname:
        return False  # "null" (sandboxed frames, file pages) and anything that is not a web page.
    page = _without_default_port(parts.netloc.lower(), parts.scheme)
    forwarded = forwarded_host.split(",", 1)[0] if forwarded_host else None
    for target in (host, forwarded):
        if target and _without_default_port(target.strip().lower(), parts.scheme) == page:
            return True
    return allowed.lists(host_name(parts.netloc))


def _without_default_port(netloc: str, scheme: str) -> str:
    suffix = f":{_DEFAULT_PORTS[scheme]}"
    return netloc[: -len(suffix)] if netloc.endswith(suffix) else netloc
