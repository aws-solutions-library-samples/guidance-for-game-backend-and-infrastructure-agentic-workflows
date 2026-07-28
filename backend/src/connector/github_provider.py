"""GitHub provider adapter for the Source Control Connector.

This module implements the provider-agnostic :class:`SourceControlProvider` contract
against the GitHub REST API (Req 9.3), plus the :func:`get_provider` factory that selects
the concrete adapter from the validated :class:`ConnectorConfig` (Req 9.4, 9.6).

Design guarantees encoded here (see
``.kiro/specs/source-control-connector/design.md`` → GitHub adapter):

- **No local clone.** The container filesystem is read-only, so every operation is a
  direct call to the GitHub *Git Data* / *Contents* REST endpoints (get contents, get
  ref, create ref, create tree/commit, open pull request). Nothing is written to disk.
- **Per-request timeout.** Every HTTP call is bounded by
  ``config.provider_timeout_seconds`` (sourced from ``GBAW_SCM_PROVIDER_TIMEOUT_SECONDS``).
- **Per-operation credential fetch.** The SCM_Credential is fetched fresh for each public
  operation via ``get_secret(config.credential_secret_id, source="secretsmanager")`` and
  placed in the ``Authorization`` header. It is **never** logged (Req 4.7, 6.6).
- **Typed error mapping** (Req 10.1, 10.2, 10.4, 10.5):
    - connection error / connect-timeout → :class:`ProviderUnavailableError`
    - read/write/pool timeout or other transport failure → :class:`ProviderTransientError`
    - HTTP 401 / 403 → :class:`ProviderAuthError` (never retried)
    - HTTP 409 → :class:`ProviderConflictError` (no destructive resolution)
    - HTTP 5xx / 429 → :class:`ProviderTransientError` (retryable)

Only provider-agnostic dataclasses from ``connector.models`` and Python primitives cross
the method boundary; no GitHub-specific type escapes this layer (Req 9.1).
"""

# Standard library
from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

# Third-party packages
import httpx

# Local modules
from connector.models import (
    FileContent,
    FileFetchResult,
    ProposedFile,
    PullRequestResult,
)
from connector.provider import (
    ProviderAuthError,
    ProviderConflictError,
    ProviderError,
    ProviderTransientError,
    ProviderUnavailableError,
    SourceControlProvider,
    UnsupportedProviderError,
)
from utils.secrets import get_secret

if TYPE_CHECKING:
    # Local modules
    from connector.config import ConnectorConfig

__all__ = ["GitHubProvider", "get_provider"]

# Public GitHub REST API host. GitHub Enterprise deployments would override this with a
# configured base URL; the connector defaults to the public host.
_DEFAULT_API_BASE_URL = "https://api.github.com"

# Pinned Accept header + API version keep responses stable across GitHub API changes.
_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-11-28"

# Standard git file mode for a non-executable blob in a tree.
_BLOB_MODE = "100644"


class GitHubProvider(SourceControlProvider):
    """`SourceControlProvider` implemented against the GitHub REST API.

    The adapter holds only immutable configuration (timeout, base URL, and the secret id
    used to fetch the credential per-operation). It stores no credential in memory beyond
    the lifetime of a single HTTP request.
    """

    def __init__(self, config: ConnectorConfig) -> None:
        self._credential_secret_id = config.credential_secret_id
        self._timeout = float(config.provider_timeout_seconds)
        self._base_url = getattr(config, "api_base_url", None) or _DEFAULT_API_BASE_URL

    # ------------------------------------------------------------------ read path

    def get_file(self, repo: str, branch: str, path: str) -> FileContent | None:
        """Return the file at ``path`` on ``branch`` of ``repo`` or ``None`` if absent.

        Uses the Contents endpoint; a 404 means the file does not exist and yields
        ``None`` without raising (Req 3.1).
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
        """Fetch multiple files, recording any missing paths without proposing (Req 3.4).

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

    def branch_exists(self, repo: str, branch: str) -> bool:
        """Return ``True`` if ``branch`` exists in ``repo`` (Req 2.8).

        Uses the Git Data get-ref endpoint; a 404 means the ref is absent.
        """
        headers = self._auth_headers()
        response = self._request(
            "GET",
            f"/repos/{repo}/git/ref/heads/{branch}",
            headers,
            allow_404=True,
        )
        return response.status_code != 404

    def latest_commit_sha(self, repo: str, branch: str) -> str:
        """Return the SHA the ``branch`` ref currently points at (Req 3.3)."""
        headers = self._auth_headers()
        response = self._request(
            "GET",
            f"/repos/{repo}/git/ref/heads/{branch}",
            headers,
        )
        payload = response.json()
        return self._extract_ref_sha(payload)

    # --------------------------------------------------------------- propose path

    def create_branch(self, repo: str, new_branch: str, from_sha: str) -> None:
        """Create ``new_branch`` at ``from_sha`` (Req 2.1).

        GitHub returns 422 for an already-existing ref; the service guarantees a unique
        Proposal_Branch name via ``branch_exists`` beforehand (Req 2.8), so this only
        creates a fresh ref.
        """
        headers = self._auth_headers()
        self._request(
            "POST",
            f"/repos/{repo}/git/refs",
            headers,
            json={"ref": f"refs/heads/{new_branch}", "sha": from_sha},
        )

    def commit_files(
        self,
        repo: str,
        branch: str,
        files: list[ProposedFile],
        message: str,
    ) -> str:
        """Commit ``files`` to ``branch`` as a single commit; return the commit SHA.

        Uses the Git Data API so the complete set of modified files lands in one atomic
        commit (Req 2.3): read the branch head, build a tree on top of the head's tree,
        create a commit with the head as parent, then fast-forward the ref.
        """
        headers = self._auth_headers()

        head_sha = self._extract_ref_sha(
            self._request("GET", f"/repos/{repo}/git/ref/heads/{branch}", headers).json()
        )
        base_commit = self._request(
            "GET", f"/repos/{repo}/git/commits/{head_sha}", headers
        ).json()
        base_tree_sha = base_commit["tree"]["sha"]

        tree_entries = [
            {
                "path": proposed.path,
                "mode": _BLOB_MODE,
                "type": "blob",
                "content": proposed.content,
            }
            for proposed in files
        ]
        new_tree = self._request(
            "POST",
            f"/repos/{repo}/git/trees",
            headers,
            json={"base_tree": base_tree_sha, "tree": tree_entries},
        ).json()

        new_commit = self._request(
            "POST",
            f"/repos/{repo}/git/commits",
            headers,
            json={
                "message": message,
                "tree": new_tree["sha"],
                "parents": [head_sha],
            },
        ).json()
        new_commit_sha = new_commit["sha"]

        self._request(
            "PATCH",
            f"/repos/{repo}/git/refs/heads/{branch}",
            headers,
            json={"sha": new_commit_sha, "force": False},
        )
        return new_commit_sha

    def open_pull_request(
        self,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> PullRequestResult:
        """Open exactly one pull request from ``head`` into ``base`` (Req 2.2, 2.6).

        The proposal is created unmerged and requires human review; the adapter exposes no
        merge/approve/close operation (Req 6.1).
        """
        headers = self._auth_headers()
        payload = self._request(
            "POST",
            f"/repos/{repo}/pulls",
            headers,
            json={"title": title, "head": head, "base": base, "body": body},
        ).json()
        return PullRequestResult(
            pull_request_id=str(payload.get("number", "")),
            pull_request_url=payload.get("html_url", ""),
        )

    # -------------------------------------------------------------------- helpers

    def _auth_headers(self) -> dict[str, str]:
        """Fetch the credential for this operation and build request headers.

        The credential is retrieved fresh per operation from Secrets Manager and placed in
        the ``Authorization`` header; it is never logged (Req 4.7, 6.6). A missing/empty
        credential fails closed as an authorization error (Req 10.2).
        """
        token = get_secret(self._credential_secret_id, source="secretsmanager")
        if not token:
            raise ProviderAuthError(
                "Source-control credential could not be retrieved from Secrets Manager"
            )
        return {
            "Authorization": f"Bearer {token}",
            "Accept": _ACCEPT,
            "X-GitHub-Api-Version": _API_VERSION,
        }

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
        exceptions so the service layer can react uniformly (Req 10.1, 10.2, 10.4, 10.5).
        """
        url = f"{self._base_url}{path}"
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.request(
                    method, url, headers=headers, params=params, json=json
                )
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

        Success (2xx) and an allowed 404 return normally. Auth (401/403) and conflict
        (409) are non-retryable; 429 and 5xx are transient/retryable. Any other 4xx is a
        non-retryable provider error. Response bodies are not logged so no secret leaks.
        """
        status = response.status_code
        if 200 <= status < 300:
            return
        if status == 404 and allow_404:
            return
        if status in (401, 403):
            raise ProviderAuthError(
                f"Provider rejected the credential ({status}): {method} {path}"
            )
        if status == 409:
            raise ProviderConflictError(f"Provider reported a conflict (409): {method} {path}")
        if status == 429 or 500 <= status < 600:
            raise ProviderTransientError(
                f"Provider temporarily unavailable ({status}): {method} {path}"
            )
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
        raise ProviderError(
            f"Unsupported content encoding for '{payload.get('path', '<unknown>')}'"
        )

    @staticmethod
    def _extract_ref_sha(payload: dict[str, Any]) -> str:
        """Pull the commit SHA out of a Git Data ref response."""
        return payload["object"]["sha"]


def get_provider(config: ConnectorConfig) -> SourceControlProvider:
    """Return the provider adapter for the configured provider (Req 9.4).

    Only ``"github"`` is implemented today. Any other provider name raises
    :class:`UnsupportedProviderError`, which the config loader catches so the connector
    stays disabled and read-only (Req 9.6).
    """
    if config.provider == "github":
        return GitHubProvider(config)
    raise UnsupportedProviderError(config.provider)
