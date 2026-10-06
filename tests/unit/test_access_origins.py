"""Which Host names an open server answers, and which pages may change its state."""

from __future__ import annotations

import pytest

from backend.features.access import parse_allowed_hosts, rebind_safe_host, same_origin_write

NONE = parse_allowed_hosts("")


@pytest.mark.parametrize(
    "host",
    [
        "localhost:8899",
        "127.0.0.1:8899",
        "192.168.1.20:8899",
        "[::1]:8899",
        "[fe80::1%en0]:8899",
        "100.101.102.103:8899",  # Tailscale address
        "mybox:8899",  # machine name or MagicDNS short name
        "mybox.tail1234.ts.net",  # MagicDNS name, including through `tailscale serve`
        "mybox.local:8899",
        "router.lan",
        "orb.localhost:8899",
        "MyBox.:8899",
        None,
    ],
)
def test_names_public_dns_cannot_hand_an_attacker_are_answered(host):
    assert rebind_safe_host(host, NONE)


@pytest.mark.parametrize("host", ["attacker.example:8899", "attacker.example", "evil.ts.net.attacker.example", ":8899"])
def test_public_names_are_not(host):
    assert not rebind_safe_host(host, NONE)


def test_allowed_hosts_add_names_suffixes_or_everything():
    allowed = parse_allowed_hosts("orb.example.com, *.home.example.net .lab.example.org")
    assert rebind_safe_host("ORB.example.com:443", allowed)
    assert rebind_safe_host("home.example.net", allowed)
    assert rebind_safe_host("pc.home.example.net", allowed)
    assert rebind_safe_host("x.lab.example.org", allowed)
    assert not rebind_safe_host("other.example.com", allowed)
    assert rebind_safe_host("anything.example", parse_allowed_hosts("*"))


@pytest.mark.parametrize(
    ("origin", "fetch_site", "host", "forwarded", "expected"),
    [
        ("http://192.168.1.20:8899", "same-origin", "192.168.1.20:8899", None, True),
        ("http://mybox:8899", None, "mybox:8899", None, True),
        ("https://mybox.tail1234.ts.net", None, "mybox.tail1234.ts.net:443", None, True),
        ("https://orb.example.com", None, "127.0.0.1:8899", "orb.example.com, 10.0.0.1", True),
        ("http://192.168.1.20:3000", "same-site", "192.168.1.20:8899", None, False),  # another app on this machine
        ("https://evil.example", "cross-site", "localhost:8899", None, False),
        ("null", "cross-site", "localhost:8899", None, False),
        ("file://", None, "localhost:8899", None, False),
        (None, None, "localhost:8899", None, True),  # a script or tool, not a page
        (None, "cross-site", "localhost:8899", None, False),
    ],
)
def test_only_this_servers_own_pages_may_write(origin, fetch_site, host, forwarded, expected):
    assert same_origin_write(origin, fetch_site, host, forwarded, NONE) is expected


def test_a_listed_name_vouches_for_its_pages_but_star_vouches_for_none():
    assert same_origin_write("https://orb.example.com", None, "127.0.0.1:8899", None, parse_allowed_hosts("orb.example.com"))
    assert not same_origin_write("https://evil.example", None, "127.0.0.1:8899", None, parse_allowed_hosts("*"))
