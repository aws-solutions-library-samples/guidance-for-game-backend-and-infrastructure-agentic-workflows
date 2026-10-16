"""A tiny, dependency-free GitHub REST client over ``urllib``.

The client is deliberately small and *injectable*: every network call goes
through a single ``transport`` callable so tests can supply a fake without any
monkeypatching. The default transport uses ``urllib.request``.

Only the endpoints the release workflow needs are exposed, and the mutating
ones (create release, upload asset, create tag object/ref, delete ref, delete
release) are segregated so a reader can audit the write surface at a glance.

The client never logs the token, never logs full URLs with query strings, and
never raises provider response bodies verbatim beyond a bounded, sanitized
excerpt.
"""

from __future__ import annotations

# Standard library
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

API_ROOT = "https://api.github.com"
_API_VERSION = "2022-11-28"
_USER_AGENT = "gbaw-release-tooling"
_MAX_ERROR_EXCERPT = 200
# Finite per-request timeout: a stalled call must not hold the release run (and
# its no-cancel concurrency group) for the whole job budget.
_HTTP_TIMEOUT_SECONDS = 30


class GitHubError(RuntimeError):
    """A sanitized GitHub API error. Never carries the token or raw payload."""

    def __init__(self, status: int, operation: str, excerpt: str = "") -> None:
        self.status = status
        self.operation = operation
        message = f"github api {operation} failed with status {status}"
        if excerpt:
            message = f"{message}: {excerpt}"
        super().__init__(message)


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8"))


@dataclass
class Request:
    method: str
    url: str
    headers: dict[str, str]
    data: bytes | None = None


def _sanitize_excerpt(body: bytes) -> str:
    try:
        text = body.decode("utf-8", "replace")
    except Exception:  # pragma: no cover - decode fallback
        return ""
    text = " ".join(text.split())
    return text[:_MAX_ERROR_EXCERPT]


class _NoCrossHostRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse any redirect that changes the host or downgrades the scheme.

    CPython's default redirect handler copies request headers -- including the
    ``Authorization`` bearer token -- onto the redirect target whatever its
    host or scheme, so a redirect from an API host to another host, or a
    downgrade from ``https`` to ``http`` on the same host, would carry the token
    off GitHub or onto a cleartext connection. This handler allows a redirect
    only when the target scheme and host both equal the original; otherwise it
    raises, naming the refused target so the error is actionable, and the token
    is never forwarded.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(newurl)
        if new.netloc and (new.netloc != old.netloc or new.scheme != old.scheme):
            raise urllib.error.HTTPError(
                req.full_url,
                code,
                f"refusing redirect to {new.scheme}://{new.netloc}",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# A module-level opener that refuses cross-host redirects, so the bearer token
# cannot follow a redirect to a different host on any request.
_OPENER = urllib.request.build_opener(_NoCrossHostRedirect())


def _urllib_transport(request: Request) -> Response:
    req = urllib.request.Request(request.url, data=request.data, method=request.method, headers=request.headers)
    try:
        with _OPENER.open(req, timeout=_HTTP_TIMEOUT_SECONDS) as handle:  # noqa: S310 - fixed https host
            return Response(
                status=handle.status,
                headers={k.lower(): v for k, v in handle.headers.items()},
                body=handle.read(),
            )
    except urllib.error.HTTPError as error:  # pragma: no cover - exercised via fakes
        body = error.read() or b""
        if not body:
            # A refused redirect (raised by _NoCrossHostRedirect) carries its
            # actionable reason in ``reason``/``msg`` but no body; surface it so
            # the sanitized error excerpt still names the refused target instead
            # of an opaque "status 302".
            reason = getattr(error, "reason", None) or getattr(error, "msg", None)
            if reason:
                body = str(reason).encode("utf-8", "replace")
        return Response(
            status=error.code,
            headers={k.lower(): v for k, v in (error.headers or {}).items()},
            body=body,
        )


@dataclass
class GitHubClient:
    owner: str
    repo: str
    token: str
    transport: Callable[[Request], Response] = field(default=_urllib_transport, repr=False)
    api_root: str = API_ROOT
    uploads_root: str = "https://uploads.github.com"

    # -- low level ---------------------------------------------------------

    def _headers(self, *, content_type: str | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": _API_VERSION,
            "User-Agent": _USER_AGENT,
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _repo_path(self, suffix: str) -> str:
        return f"{self.api_root}/repos/{self.owner}/{self.repo}{suffix}"

    def _call(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        body: Any = None,
        raw: bytes | None = None,
        content_type: str | None = None,
        expected: tuple[int, ...] = (200, 201),
        allow_404: bool = False,
    ) -> Response:
        data: bytes | None
        if raw is not None:
            data = raw
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            content_type = content_type or "application/json"
        else:
            data = None
        response = self.transport(
            Request(method=method, url=url, headers=self._headers(content_type=content_type), data=data)
        )
        if allow_404 and response.status == 404:
            return response
        if response.status not in expected:
            raise GitHubError(response.status, operation, _sanitize_excerpt(response.body))
        return response

    def _paginate(self, url: str, *, operation: str) -> list[Any]:
        items: list[Any] = []
        page = 1
        while True:
            sep = "&" if "?" in url else "?"
            page_url = f"{url}{sep}per_page=100&page={page}"
            response = self._call("GET", page_url, operation=operation, expected=(200,))
            batch = response.json() or []
            if not isinstance(batch, list):
                raise GitHubError(response.status, operation, "expected a list payload")
            items.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return items

    # -- read surface ------------------------------------------------------

    def get_branch_head_sha(self, branch: str) -> str:
        url = self._repo_path(f"/git/ref/heads/{urllib.parse.quote(branch)}")
        response = self._call("GET", url, operation="get-branch-ref", expected=(200,))
        payload = response.json() or {}
        return str(payload.get("object", {}).get("sha", ""))

    def get_ref(self, ref: str) -> dict[str, Any] | None:
        """``ref`` like ``tags/v0.1.0``. Returns None when absent (404)."""
        url = self._repo_path(f"/git/ref/{ref}")
        response = self._call("GET", url, operation="get-ref", expected=(200,), allow_404=True)
        if response.status == 404:
            return None
        return response.json() or {}

    def list_tags(self) -> list[str]:
        items = self._paginate(self._repo_path("/tags"), operation="list-tags")
        return [str(item.get("name", "")) for item in items if item.get("name")]

    def list_releases(self) -> list[dict[str, Any]]:
        return self._paginate(self._repo_path("/releases"), operation="list-releases")

    def list_check_runs(self, commit: str, *, check_name: str | None = None) -> list[dict[str, Any]]:
        """Return every check run for ``commit`` across all pages.

        ``GET /commits/{commit}/check-runs`` is paginated; a commit that has
        accumulated many runs for a context (e.g. a scheduled workflow that
        records a run on every weekday) can push an earlier real run off the
        default first page. Request ``per_page=100`` and follow ``page`` until a
        short page, so selection sees every run, not just the newest 30.
        """
        base = f"/commits/{commit}/check-runs?filter=latest"
        if check_name is not None:
            base += f"&check_name={urllib.parse.quote(check_name)}"
        runs: list[dict[str, Any]] = []
        page = 1
        while True:
            url = self._repo_path(f"{base}&per_page=100&page={page}")
            response = self._call("GET", url, operation="list-check-runs", expected=(200,))
            payload = response.json() or {}
            batch = payload.get("check_runs", [])
            if not isinstance(batch, list):
                return runs
            runs.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < 100:
                break
            page += 1
        return runs

    def get_branch_rules(self, branch: str) -> list[dict[str, Any]]:
        """Return the active rules for a branch (``GET /repos/{o}/{r}/rules/branches/{b}``).

        Needs only ``Metadata: read``. The response is a flat list of rule
        objects; the ``required_status_checks`` rule carries the authoritative
        set of required contexts and their integration ids.
        """
        url = self._repo_path(f"/rules/branches/{urllib.parse.quote(branch, safe='')}")
        items = self._paginate(url, operation="get-branch-rules")
        return [item for item in items if isinstance(item, dict)]

    def get_collaborator_permission(self, username: str) -> str:
        """Return a user's repository permission level (admin/write/read/none).

        Uses ``GET /repos/{o}/{r}/collaborators/{user}/permission`` (Metadata
        read). This is the precise test for "does this author maintain the
        repository?", unlike the broad ``author_association`` field.
        """
        url = self._repo_path(f"/collaborators/{urllib.parse.quote(username, safe='')}/permission")
        response = self._call("GET", url, operation="get-collaborator-permission", expected=(200,), allow_404=True)
        if response.status == 404:
            return "none"
        payload = response.json() or {}
        return str(payload.get("permission", "none"))

    def get_issue_comment(self, comment_id: int) -> dict[str, Any]:
        url = self._repo_path(f"/issues/comments/{int(comment_id)}")
        response = self._call("GET", url, operation="get-issue-comment", expected=(200,))
        return response.json() or {}

    def get_release(self, release_id: int) -> dict[str, Any]:
        """Return a single release payload (``GET /releases/{id}``).

        Used for the post-publish read-back. The payload carries ``immutable``
        (true once GitHub's immutable-releases setting is on and the release is
        published), ``tag_name``, ``draft``, and ``assets``. Needs only
        ``contents: read``.
        """
        url = self._repo_path(f"/releases/{int(release_id)}")
        response = self._call("GET", url, operation="get-release", expected=(200,))
        return response.json() or {}

    def list_release_assets(self, release_id: int) -> list[dict[str, Any]]:
        """Return every asset attached to a release across all pages.

        Each asset carries ``name`` and ``size`` and, on current GitHub, a
        ``digest`` of the form ``sha256:<hex>``. Needs only ``contents: read``.
        """
        return self._paginate(self._repo_path(f"/releases/{int(release_id)}/assets"), operation="list-release-assets")

    def get_tag_object(self, sha: str) -> dict[str, Any]:
        """Return an annotated tag object (``GET /git/tags/{sha}``).

        The payload's ``object`` names the commit the tag points at (``sha`` and
        ``type``), so the read-back can resolve tag ref -> tag object -> commit.
        Needs only ``contents: read``.
        """
        url = self._repo_path(f"/git/tags/{urllib.parse.quote(sha, safe='')}")
        response = self._call("GET", url, operation="get-tag-object", expected=(200,))
        return response.json() or {}

    def list_rulesets(self, *, target: str | None = None) -> list[dict[str, Any]]:
        """Return the repository's rulesets (``GET /repos/{o}/{r}/rulesets``).

        Needs only ``Metadata: read`` (the same permission the workflow token
        already carries for reading branch rules). ``target`` filters to a rule
        target such as ``tag``. The list payload summarizes each ruleset
        (``id``, ``name``, ``target``, ``enforcement``) without its ref-name
        conditions; :meth:`get_ruleset` reads a single ruleset's full
        ``conditions`` with the same ``Metadata: read`` permission.
        """
        suffix = "/rulesets"
        if target is not None:
            suffix += f"?targets={urllib.parse.quote(target, safe='')}"
        items = self._paginate(self._repo_path(suffix), operation="list-rulesets")
        return [item for item in items if isinstance(item, dict)]

    def get_ruleset(self, ruleset_id: int) -> dict[str, Any]:
        """Return one ruleset with its ``conditions`` (``GET /rulesets/{id}``).

        Needs only ``Metadata: read``. The payload carries
        ``conditions.ref_name.include`` / ``exclude`` (the tag patterns the
        ruleset targets) and ``enforcement``, so a caller can decide whether an
        active tag ruleset actually covers a ref such as ``refs/tags/v*``. Only
        ``bypass_actors`` is withheld from callers without write access, which
        this check does not need.
        """
        url = self._repo_path(f"/rulesets/{int(ruleset_id)}")
        response = self._call("GET", url, operation="get-ruleset", expected=(200,))
        return response.json() or {}

    def get_environment(self, name: str) -> dict[str, Any] | None:
        """Return the deployment environment payload, or None when absent (404).

        Read-only. Uses ``GET /repos/{owner}/{repo}/environments/{name}`` which
        needs only ``actions: read`` on the workflow token. The payload carries
        ``protection_rules`` (e.g. a ``required_reviewers`` rule, which also
        carries ``prevent_self_review``) and ``deployment_branch_policy``, which
        the caller inspects to decide whether publication may proceed.
        """
        url = self._repo_path(f"/environments/{urllib.parse.quote(name, safe='')}")
        response = self._call("GET", url, operation="get-environment", expected=(200,), allow_404=True)
        if response.status == 404:
            return None
        return response.json() or {}

    def list_deployment_branch_policies(self, name: str) -> list[dict[str, Any]]:
        """Return an environment's custom deployment branch policies.

        Uses ``GET /repos/{o}/{r}/environments/{name}/deployment-branch-policies``
        (``actions: read``). Each entry carries a ``name`` pattern a branch must
        match to deploy, so a caller can confirm the environment is limited to
        ``main``.
        """
        url = self._repo_path(f"/environments/{urllib.parse.quote(name, safe='')}/deployment-branch-policies")
        response = self._call("GET", url, operation="list-deployment-branch-policies", expected=(200,))
        payload = response.json() or {}
        policies = payload.get("branch_policies") if isinstance(payload, dict) else None
        if not isinstance(policies, list):
            return []
        return [p for p in policies if isinstance(p, dict)]

    # -- write surface (publication only) ---------------------------------

    def create_release(
        self,
        *,
        tag_name: str,
        target_commitish: str,
        name: str,
        body: str,
        draft: bool,
        prerelease: bool,
    ) -> dict[str, Any]:
        url = self._repo_path("/releases")
        response = self._call(
            "POST",
            url,
            operation="create-release",
            body={
                "tag_name": tag_name,
                "target_commitish": target_commitish,
                "name": name,
                "body": body,
                "draft": draft,
                "prerelease": prerelease,
                "generate_release_notes": False,
            },
            expected=(201,),
        )
        return response.json() or {}

    def update_release(
        self,
        release_id: int,
        *,
        draft: bool | None = None,
        prerelease: bool | None = None,
        make_latest: bool | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if draft is not None:
            payload["draft"] = draft
        if prerelease is not None:
            payload["prerelease"] = prerelease
        if make_latest is not None:
            # GitHub expects the string form; drafts/prereleases cannot be latest.
            payload["make_latest"] = "true" if make_latest else "false"
        url = self._repo_path(f"/releases/{int(release_id)}")
        response = self._call("PATCH", url, operation="update-release", body=payload, expected=(200,))
        return response.json() or {}

    def delete_release(self, release_id: int) -> None:
        url = self._repo_path(f"/releases/{int(release_id)}")
        self._call("DELETE", url, operation="delete-release", expected=(204,))

    def upload_release_asset(self, upload_url: str, *, name: str, data: bytes, content_type: str) -> dict[str, Any]:
        # ``upload_url`` from create_release is templated: strip the ``{?...}``.
        base = upload_url.split("{", 1)[0]
        # The upload host comes back from the API over TLS, but a bearer token
        # must never follow it to an unexpected host. Require the scheme and
        # host to equal the configured uploads origin before attaching the token.
        self._require_uploads_host(base)
        url = f"{base}?name={urllib.parse.quote(name)}"
        response = self._call(
            "POST",
            url,
            operation="upload-asset",
            raw=data,
            content_type=content_type,
            expected=(201,),
        )
        return response.json() or {}

    def _require_uploads_host(self, url: str) -> None:
        """Fail unless ``url`` is https on the configured uploads host.

        ``create_release`` returns the ``upload_url`` the asset POST targets.
        Although it arrives from the GitHub API over TLS, the request carries
        the bearer token, so the host is pinned to the configured uploads origin
        (``uploads.github.com`` by default) and the scheme to https. A mismatch
        raises before the token is attached, so a redirected or substituted host
        can never receive the token.
        """
        parsed = urllib.parse.urlsplit(url)
        expected = urllib.parse.urlsplit(self.uploads_root)
        if parsed.scheme != "https" or parsed.netloc != expected.netloc:
            raise GitHubError(0, "upload-asset", f"refusing asset upload to unexpected host: {parsed.netloc or 'none'}")

    def create_tag_object(
        self, *, tag: str, message: str, commit: str, tagger_name: str, tagger_email: str, date: str
    ) -> str:
        url = self._repo_path("/git/tags")
        response = self._call(
            "POST",
            url,
            operation="create-tag-object",
            body={
                "tag": tag,
                "message": message,
                "object": commit,
                "type": "commit",
                "tagger": {"name": tagger_name, "email": tagger_email, "date": date},
            },
            expected=(201,),
        )
        payload = response.json() or {}
        return str(payload.get("sha", ""))

    def create_ref(self, *, ref: str, sha: str) -> dict[str, Any]:
        url = self._repo_path("/git/refs")
        response = self._call(
            "POST",
            url,
            operation="create-ref",
            body={"ref": ref, "sha": sha},
            expected=(201,),
        )
        return response.json() or {}

    def delete_ref(self, ref: str) -> None:
        """``ref`` like ``tags/v0.1.0`` (no leading ``refs/``)."""
        url = self._repo_path(f"/git/refs/{ref}")
        self._call("DELETE", url, operation="delete-ref", expected=(204,))
