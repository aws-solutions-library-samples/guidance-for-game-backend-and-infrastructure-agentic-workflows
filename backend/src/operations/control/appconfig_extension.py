"""AppConfig Lambda extension localhost transport (issue #416, E4).

The AWS AppConfig Lambda extension runs as a sidecar inside the function
execution environment and serves the currently-cached configuration over a
localhost HTTP endpoint:

    http://localhost:<port>/applications/<application>/environments/<environment>/configurations/<profile>

:class:`AppConfigExtensionClient` is the concrete
:class:`~operations.control.kill_switch_gate.ConfigExtensionPort`: a thin,
bounded transport that builds that exact URL from frozen identifiers and issues
one bounded-timeout GET. It deliberately does *not* parse, validate, freshness-
check, cache, or fall back — all of that policy lives in
:class:`~operations.control.kill_switch_gate.KillSwitchGate`. A transport or
non-200 failure propagates so the gate fails closed.

The extension owns the poll/refresh/cache lifecycle; this client never adds its
own cache. Each :meth:`fetch_configuration` is a fresh read of whatever the
extension currently serves.
"""

from __future__ import annotations

# Standard library
import re
import urllib.request
from typing import Any, Callable
from urllib.parse import quote

# AppConfig application / environment / profile names are limited to a safe
# identifier alphabet; we re-validate before building the URL so a misconfigured
# or hostile identifier can never inject a path segment or escape the endpoint.
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# The maximum body the extension is allowed to return. The kill-switch document
# is tiny; a larger body is a misconfiguration and is refused rather than read
# unbounded.
_MAX_BODY_BYTES = 64 * 1024


def _default_opener(url: str, timeout: float) -> Any:
    # ``urlopen`` on a plain http://localhost URL to the in-environment sidecar.
    return urllib.request.urlopen(url, timeout=timeout)  # noqa: S310 - localhost sidecar only


class AppConfigExtensionClient:
    """Read raw kill-switch bytes from the AppConfig Lambda extension endpoint."""

    def __init__(
        self,
        *,
        application: str,
        environment: str,
        profile: str,
        port: int = 2772,
        timeout_s: float = 0.5,
        opener: Callable[[str, float], Any] | None = None,
    ) -> None:
        self._application = _require_identifier(application, "application")
        self._environment = _require_identifier(environment, "environment")
        self._profile = _require_identifier(profile, "profile")
        if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
            raise ValueError("port must be a valid TCP port")
        if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool) or timeout_s <= 0:
            raise ValueError("timeout_s must be a positive number of seconds")
        self._port = port
        self._timeout_s = float(timeout_s)
        self._opener = opener or _default_opener
        self._url = self._build_url()

    def fetch_configuration(self) -> bytes:
        """Issue one bounded GET and return the raw configuration bytes."""
        with self._opener(self._url, self._timeout_s) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = getattr(response, "code", 200)
            if status != 200:
                raise RuntimeError(f"AppConfig extension returned HTTP {status}")
            body = response.read(_MAX_BODY_BYTES + 1)
        if not isinstance(body, (bytes, bytearray)):
            raise RuntimeError("AppConfig extension returned a non-bytes body")
        if len(body) > _MAX_BODY_BYTES:
            raise RuntimeError("AppConfig extension body exceeds the maximum size")
        return bytes(body)

    def _build_url(self) -> str:
        # Every segment is a validated identifier; ``quote`` with an empty safe
        # set is defense in depth so no reserved character can survive.
        return (
            f"http://localhost:{self._port}"
            f"/applications/{quote(self._application, safe='')}"
            f"/environments/{quote(self._environment, safe='')}"
            f"/configurations/{quote(self._profile, safe='')}"
        )


def _require_identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a valid AppConfig identifier")
    return value
