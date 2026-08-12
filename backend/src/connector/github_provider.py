"""GitHub provider adapter for the Source Control Connector (read-only).

This module implements the provider-agnostic :class:`SourceControlReader` contract
against the GitHub REST API. Provider *selection* no longer lives here: the adapter
self-registers with the provider-neutral registry at import time
(``registry.register("github", GitHubProvider)``) and the registry owns resolution via
``connector.registry.get_provider``. Importing this module is what makes ``"github"`` a
supported provider.

Design guarantees encoded here (see
``.kiro/specs/source-control-connector-readonly-split/design.md`` → GitHub adapter):

- **No local clone.** The container filesystem is read-only, so every operation is a
  direct call to the GitHub *Contents* REST endpoint. Nothing is written to disk.
- **Read-only.** The adapter exposes only ``get_file``/``get_files``; it holds no write
  method and attaches its credential only to read requests.
- **Per-request timeout.** Every HTTP call is bounded by
  ``config.connector.provider_timeout_seconds`` (sourced from
  ``GBAW_SCM_PROVIDER_TIMEOUT_SECONDS``).
- **Per-operation read-credential fetch.** The read credential is fetched fresh for each
  operation via ``get_secret(config.adapter.read_credential_secret_arn,
  source="secretsmanager")`` and placed in the ``Authorization`` header. It is **never**
  logged.
- **Typed error mapping**:
    - connection error / connect-timeout → :class:`ProviderUnavailableError`
    - read/write/pool timeout or other transport failure → :class:`ProviderTransientError`
    - HTTP 401 / 403 → :class:`ProviderAuthError` (never retried)
    - HTTP 5xx / 429 → :class:`ProviderTransientError` (retryable)

Only provider-agnostic dataclasses from ``connector.models`` and Python primitives cross
the method boundary; no GitHub-specific type escapes this layer.
"""

from __future__ import annotations

# Standard library
import base64
from typing import TYPE_CHECKING, Any

# Third-party packages
import httpx

# Local modules
from connector import registry
from connector.models import FileContent, FileFetchResult
from connector.provider import (
    OutboundRequest,
    ProviderAuth,
    ProviderAuthError,
    ProviderError,
    ProviderTransientError,
    ProviderUnavailableError,
    SourceControlReader,
)
from utils.secrets import get_secret

if TYPE_CHECKING:
    # Local modules
    from connector.config import AdapterConfig, SourceControlConfig

__all__ = ["GitHubProvider", "GitHubReadTokenAuth"]

# Public GitHub REST API host. GitHub Enterprise deployments would override this with a
# configured base URL; the connector defaults to the public host.
_DEFAULT_API_BASE_URL = "https://api.github.com"

# Pinned Accept header + API version keep responses stable across GitHub API changes.
_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-11-28"


class GitHubReadTokenAuth(ProviderAuth):
    """Token-based **read** :class:`ProviderAuth` for the GitHub adapter.

    Implements the provider-neutral credential-acquisition contract for a token-based
    Provider: it fetches the read credential fresh from AWS Secrets Manager at the
    configured ``read_credential_secret_arn`` and attaches it to the outbound read request
    as an ``Authorization: Bearer <token>`` header. The credential is **never** logged and
    a missing/empty credential fails closed as a :class:`ProviderAuthError` so the calling
    read is aborted without retry.

    Credential acquisition is owned here, in the adapter, rather than in the connector
    core: a future IAM-native adapter (e.g. CodeCommit/SigV4) satisfies the *same* contract
    by signing the request with the runtime role and never calling ``get_secret``. The
    credential is a provider-scoped, fine-grained read-only token and is attached only to
    read requests (there is no write request in the shipped package).
    """

    def __init__(self, config: AdapterConfig) -> None:
        # The single ARN-valued read-credential setting (AdapterConfig.read_credential_secret_arn).
        self._read_credential_secret_arn = config.read_credential_secret_arn

    def apply(self, request: OutboundRequest) -> None:
        """Fetch the read credential and set the ``Authorization`` header on ``request``.

        The credential is retrieved fresh per operation from Secrets Manager and placed in
        the ``Authorization`` header; it is never logged. A missing/empty credential fails
        closed as an authorization error.
        """
        token = get_secret(self._read_credential_secret_arn, source="secretsmanager")
        if not token:
            raise ProviderAuthError("Source-control read credential could not be retrieved from Secrets Manager")
        request.headers["Authorization"] = f"Bearer {token}"


class GitHubProvider(SourceControlReader):
    """`SourceControlReader` implemented against the GitHub REST API.

    The adapter holds only immutable configuration (timeout, base URL, and the secret id
    used to fetch the read credential per-operation). It stores no credential in memory
    beyond the lifetime of a single HTTP request.
    """

    def __init__(self, config: SourceControlConfig) -> None:
        # Credential acquisition is adapter-owned behind the neutral ProviderAuth contract:
        # GitHubReadTokenAuth fetches the secret at AdapterConfig.read_credential_secret_arn
        # and attaches the Authorization header per read operation. The per-request timeout
        # is neutral operational tuning (ConnectorConfig); the base URL is adapter-owned
        # (AdapterConfig).
        self._auth: ProviderAuth = GitHubReadTokenAuth(config.adapter)
        self._timeout = float(config.connector.provider_timeout_seconds)
        # provider_base_url is a real, validated config field (absolute https or None). When
        # unset the adapter falls back to the public GitHub API host.
        self._base_url = config.adapter.provider_base_url or _DEFAULT_API_BASE_URL

    # ------------------------------------------------------------------ read path

    def get_file(self, repo: str, branch: str, path: str) -> FileContent | None:
        """Return the file at ``path`` on ``branch`` of ``repo`` or ``None`` if absent.

        Uses the Contents endpoint; a 404 means the file does not exist and yields
        ``None`` without raising.
        """
        headers = self._auth_headers()
        response = self._request(
            "GET",
            f"/repos/{repo}/contents/{path}",
            headers,
            params={"ref": branch},
            allow_404=True,
        )
        if response.status_code == 404:
            return None

        payload = response.json()
        # A directory path returns a JSON array; treat that as "not a file".
        if isinstance(payload, list):
            return None

        content = self._decode_contents_payload(payload)
        return FileContent(path=path, content=content)

    def get_files(self, repo: str, branch: str, paths: list[str]) -> FileFetchResult:
        """Fetch multiple files, recording any missing paths.

        The credential is fetched once per operation and reused across the per-file
        requests. ``limit_exceeded`` is always ``False`` here; request-count limiting is
        enforced by the service layer before the provider is invoked.
        """
        headers = self._auth_headers()
        found: list[FileContent] = []
        missing: list[str] = []
        for path in paths:
            response = self._request(
                "GET",
                f"/repos/{repo}/contents/{path}",
                headers,
                params={"ref": branch},
                allow_404=True,
            )
            if response.status_code == 404:
                missing.append(path)
                continue
            payload = response.json()
            if isinstance(payload, list):
                missing.append(path)
                continue
            found.append(FileContent(path=path, content=self._decode_contents_payload(payload)))

        return FileFetchResult(
            files=tuple(found),
            missing=tuple(missing),
            limit_exceeded=False,
        )

    # -------------------------------------------------------------------- helpers

    def _auth_headers(self) -> dict[str, str]:
        """Build request headers, delegating credential acquisition to the ProviderAuth.

        The neutral, provider-agnostic base headers (Accept + API version) are set here;
        the read credential is acquired and attached by the adapter-owned
        :class:`GitHubReadTokenAuth` through the :class:`ProviderAuth` contract, which
        fetches the secret fresh per operation and never logs it. A missing/empty
        credential fails closed as a :class:`ProviderAuthError`.
        """
        request = OutboundRequest(
            headers={
                "Accept": _ACCEPT,
                "X-GitHub-Api-Version": _API_VERSION,
            }
        )
        self._auth.apply(request)
        return request.headers

    def _request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        allow_404: bool = False,
    ) -> httpx.Response:
        """Perform one HTTP call, mapping wire failures to typed exceptions.

        ``allow_404`` lets read callers treat a 404 as "absent" and inspect the returned
        response instead of raising. All other error classes map to the connector's typed
        exceptions so the service layer can react uniformly.
        """
        url = f"{self._base_url}{path}"
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.request(method, url, headers=headers, params=params, json=json)
        except httpx.ConnectTimeout as exc:
            raise ProviderUnavailableError(f"Provider connection timed out: {method} {path}") from exc
        except httpx.ConnectError as exc:
            raise ProviderUnavailableError(f"Provider is unreachable: {method} {path}") from exc
        except httpx.TimeoutException as exc:
            raise ProviderTransientError(f"Provider request timed out: {method} {path}") from exc
        except httpx.TransportError as exc:
            raise ProviderTransientError(f"Provider transport error: {method} {path}") from exc

        self._raise_for_status(response, method, path, allow_404=allow_404)
        return response

    @staticmethod
    def _raise_for_status(
        response: httpx.Response,
        method: str,
        path: str,
        *,
        allow_404: bool,
    ) -> None:
        """Map an HTTP status code to a typed provider exception.

        Success (2xx) and an allowed 404 return normally. Auth (401/403) is non-retryable;
        429 and 5xx are transient/retryable. Any other 4xx is a non-retryable provider
        error. Response bodies are not logged so no secret leaks.
        """
        status = response.status_code
        if 200 <= status < 300:
            return
        if status == 404 and allow_404:
            return
        if status in (401, 403):
            raise ProviderAuthError(f"Provider rejected the credential ({status}): {method} {path}")
        if status == 429 or 500 <= status < 600:
            raise ProviderTransientError(f"Provider temporarily unavailable ({status}): {method} {path}")
        raise ProviderError(f"Provider request failed ({status}): {method} {path}")

    @staticmethod
    def _decode_contents_payload(payload: dict[str, Any]) -> str:
        """Decode a Contents API response body into UTF-8 text.

        Only base64-encoded content is supported (the encoding GitHub returns for regular
        files). Anything else (e.g. very large files served as ``"none"``) is surfaced as
        a provider error rather than silently returning empty content.
        """
        if payload.get("encoding") == "base64":
            raw = base64.b64decode(payload.get("content", ""))
            return raw.decode("utf-8")
        raise ProviderError(f"Unsupported content encoding for '{payload.get('path', '<unknown>')}'")


# Self-register the bundled GitHub adapter with the provider-neutral registry so that
# importing this module makes "github" a supported provider. The registry owns provider
# selection now; there is no module-level get_provider factory here, and the
# provider-neutral core (service/config) never imports this module directly.
registry.register("github", GitHubProvider)
