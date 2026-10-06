"""Public API for the optional access password that locks every route, and for which names and pages may reach the server."""

from __future__ import annotations

from .origins import AllowedHosts, host_name, parse_allowed_hosts, rebind_safe_host, same_origin_write
from .passwords import new_lock, password_matches, session_valid
from .throttle import LoginThrottle
