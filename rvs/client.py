"""HTTPX2-based Ravenstash API client.

All requests go through this module so that auth headers, base URL, and error
handling are consistent everywhere.

Usage
-----
    client = ApiClient.from_profile("default")
    repos = collection_all(client, artifacts_path("repositories"))

Resource paths are built with the group-aware builders in :mod:`rvs.api`,
which also follows collection pages.
"""

import importlib.metadata
import os
import random
import time
from typing import TYPE_CHECKING, Any

from . import auth as auth_mod
from . import config as cfg_mod
from .api import (
    API_ROUTE_RETIRED,
    ApiVersionMismatchError,
    api_url,
    is_mint_token_path,
    retry_after_seconds,
    route_deprecated,
    route_sunset_date,
    validate_api_version,
)
from .identity import PACKAGE_NAME


if TYPE_CHECKING:
    import httpx2 as httpx

    from .config import ProfileConfig


INVALID_CURSOR = "InvalidCursor"
MANIFEST_REFERENCED_BY_INDEX = "ManifestReferencedByIndex"
# Error codes whose message is the complete explanation, shown without the HTTP prefix.
_SELF_EXPLAINING_CODES = frozenset({MANIFEST_REFERENCED_BY_INDEX})

# Idempotent reads are retried when the server asks the client to back off.
_RETRYABLE_READ_STATUSES = frozenset({429, 503})
_READ_STATUS_ATTEMPTS = 3
_READ_STATUS_BACKOFF_SECONDS = (0.5, 2.0)
# A longer Retry-After is reported to the user instead of blocking the command.
MAX_RETRY_AFTER_SECONDS = 30.0

UPDATE_HINT = "run `rvs update` to check for a newer release"
API_ROUTE_RETIRED_MESSAGE = (
    f"This rvs release is no longer supported by the Ravenstash API. Update rvs: {UPDATE_HINT}."
)
_deprecation_warned = False


def rvs_user_agent() -> str:
    try:
        return f"rvs/{importlib.metadata.version(PACKAGE_NAME)}"
    except importlib.metadata.PackageNotFoundError:
        return "rvs/dev"


class ApiError(Exception):
    """Raised when the API returns a non-2xx response."""

    def __init__(self, status_code: int, detail: Any, *, retry_after: float | None = None) -> None:
        self.status_code = status_code
        self.detail = detail
        self.retry_after = retry_after
        # Server text is data: strip terminal control characters from the message.
        from .output import plain

        super().__init__(plain(self._message()))

    @property
    def code(self) -> str | None:
        code = self.detail.get("code") if isinstance(self.detail, dict) else None
        return code if isinstance(code, str) else None

    def _message(self) -> str:
        if self.status_code in _RETRYABLE_READ_STATUSES and self.retry_after is not None:
            return f"{self._http_message()} (retry after {self.retry_after:g}s)"
        if self.status_code == 410:
            return API_ROUTE_RETIRED_MESSAGE
        if isinstance(self.detail, dict) and self.detail.get("code") == "RepositoryTargetAmbiguous":
            matches = self.detail.get("matches")
            if isinstance(matches, list):
                choices = ", ".join(
                    str(item.get("account_ref"))
                    for item in matches
                    if isinstance(item, dict) and item.get("account_ref")
                )
                suffix = f" Choose an account with --account: {choices}." if choices else ""
                return f"Repository target is ambiguous.{suffix}"
        if isinstance(self.detail, dict) and self.detail.get("code") == "RepositoryTargetChanged":
            expected = self.detail.get("expected")
            current = self.detail.get("current")
            if isinstance(expected, dict) and isinstance(current, dict):
                expected_name = "/".join(
                    str(expected.get(key) or "?") for key in ("namespace_name", "repository_name")
                )
                current_name = "/".join(
                    str(current.get(key) or "?") for key in ("namespace_name", "repository_name")
                )
                repository_ref = current.get("repository_ref") or expected.get("repository_ref")
                stable = f"in/{repository_ref or '?'}"
                return (
                    "Repository target changed; no package operation was attempted. "
                    f"Expected {expected_name}, current {current_name}, ID-based target {stable}. "
                    "Use `rvs art select` to select the ID-based target or the repository "
                    "currently using the former name."
                )
        if self.code == INVALID_CURSOR:
            return (
                "The --cursor value is invalid or belongs to a different listing; "
                "run the command again without --cursor."
            )
        if self.code in _SELF_EXPLAINING_CODES:
            # These messages name the content and what to do instead, for example
            # the index to delete in place of one of its manifests.
            message = self.detail.get("message") if isinstance(self.detail, dict) else None
            if isinstance(message, str) and message:
                return message
        return self._http_message()

    def _http_message(self) -> str:
        """Render an API error envelope as ``HTTP status Code: message``."""
        if not isinstance(self.detail, dict):
            return f"HTTP {self.status_code}: {self.detail}"
        message = self.detail.get("message")
        if not isinstance(message, str) or not message:
            return f"HTTP {self.status_code}: {self.detail}"
        code = f" {self.code}" if self.code else ""
        errors = self.detail.get("errors")
        if isinstance(errors, list):
            problems = [
                f"{'.'.join(str(part) for part in item.get('location') or [])}: "
                f"{item.get('message')}"
                for item in errors[:3]
                if isinstance(item, dict)
            ]
            if problems:
                message = f"{message} ({'; '.join(problems)})"
        return f"HTTP {self.status_code}{code}: {message}"


class ApiRouteRetiredError(ApiError):
    """The API no longer serves a route this rvs release uses."""

    def __init__(self) -> None:
        super().__init__(410, {"code": API_ROUTE_RETIRED, "message": API_ROUTE_RETIRED_MESSAGE})


def deprecation_message(headers: Any) -> str:
    """Describe a deprecated-route response and how to update rvs."""
    sunset = route_sunset_date(headers)
    when = f"on {sunset}" if sunset else "soon"
    return (
        f"This rvs release uses an API that Ravenstash will retire {when}. "
        f"Update rvs: {UPDATE_HINT}."
    )


def check_route_lifecycle(response: Any) -> None:
    """Stop on a retired API route and warn once about a deprecated one.

    Every API response passes through here. Any ``410`` means this release
    can no longer use the route; a ``Deprecation`` header produces one stderr
    warning per process so machine-readable stdout stays untouched.
    """
    global _deprecation_warned
    if getattr(response, "status_code", None) == 410:
        raise ApiRouteRetiredError
    headers = getattr(response, "headers", None)
    if _deprecation_warned or not route_deprecated(headers):
        return
    _deprecation_warned = True
    from . import output

    output.warn(deprecation_message(headers))


def retry_rate_limited(
    resp: httpx.Response, send: Any, refresh: Any = lambda: False
) -> httpx.Response:
    """Resend a rate-limited or unavailable request, honoring ``Retry-After``.

    The session can expire while waiting, so a resent request that gets 401
    refreshes once and is sent again. Only requests that are safe to repeat,
    such as reads or requests carrying an idempotency key, may use this.
    """
    for attempt in range(_READ_STATUS_ATTEMPTS - 1):
        if resp.status_code not in _RETRYABLE_READ_STATUSES:
            break
        delay = retry_after_seconds(resp.headers)
        if delay is None:
            delay = _READ_STATUS_BACKOFF_SECONDS[attempt] + random.uniform(0, 0.1)
        if delay > MAX_RETRY_AFTER_SECONDS:
            break
        time.sleep(delay)
        resp = send()
        if resp.status_code == 401 and refresh():
            resp = send()
    return resp


class ApiClient:
    """Thin wrapper around the versioned Ravenstash API."""

    def __init__(
        self,
        api_url: str,
        token: str,
        timeout: float = 30.0,
        profile: str | None = None,
        allow_refresh: bool = True,
        refresh_before_request: bool = False,
    ) -> None:
        self._base = api_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._profile = profile
        self._allow_refresh = allow_refresh
        self._refresh_before_request = refresh_before_request

    @classmethod
    def from_profile(cls, profile: str | None = None) -> ApiClient:
        cfg = cfg_mod.load()
        profile_name = profile or cfg_mod.current_profile_name(cfg)
        p: ProfileConfig = cfg.active_profile(profile_name)
        from . import output

        ignored = cfg_mod.ignored_endpoint_overrides(profile_name, cfg)
        if ignored:
            verb = "is" if len(ignored) == 1 else "are"
            output.warn(
                f"{' and '.join(ignored)} {verb} ignored because profile '{profile_name}' is "
                "already saved; environment endpoint overrides only seed a new profile."
            )
        if "RVS_TOKEN" not in os.environ and (
            origin_error := auth_mod.credential_origin_error(profile_name)
        ):
            output.fatal(origin_error)
        refresh_before_request = auth_mod.credential_needs_refresh(profile_name)
        token = (
            auth_mod.get_token(profile_name, refresh=False)
            if refresh_before_request
            else auth_mod.get_token(profile_name)
        )
        if not token and refresh_before_request:
            token = auth_mod.get_token(profile_name)
            refresh_before_request = False
        if not token:
            output.fatal(
                f"No token for profile '{profile_name}'. Run: rvs auth login --profile {profile_name}"
            )
        return cls(
            api_url=p.api_url,
            token=token,  # type: ignore[arg-type]
            profile=profile_name,
            allow_refresh=auth_mod.token_source(profile_name) != "RVS_TOKEN",
            refresh_before_request=refresh_before_request,
        )

    # ── internal ──────────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "User-Agent": rvs_user_agent(),
        }

    def _url(self, path: str) -> str:
        return api_url(self._base, path)

    def _raise(self, resp: httpx.Response) -> None:
        if resp.is_success:
            return
        try:
            payload = resp.json()
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                detail = {"code": error.get("code"), "message": error.get("message")}
                if isinstance(error.get("details"), dict):
                    detail.update(error["details"])
            else:
                detail = payload.get("detail", resp.text)
        except Exception:
            detail = resp.text or resp.reason_phrase
        raise ApiError(resp.status_code, detail, retry_after=retry_after_seconds(resp.headers))

    def _validate_contract(self, resp: httpx.Response) -> None:
        try:
            validate_api_version(resp)
        except ApiVersionMismatchError as exc:
            raise ApiError(502, str(exc)) from exc

    def _refresh(self, http_client: httpx.Client) -> bool:
        if not self._profile or not self._allow_refresh:
            return False
        token = auth_mod.refresh_expiring_credential(
            self._profile,
            stale_access_token=self._token,
            http_client=http_client,
        )
        if not token:
            return False
        self._token = token
        return True

    def _request(
        self,
        method: str,
        path: str,
        *,
        retry: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        import httpx2 as httpx

        idempotent_read = method.upper() == "GET"
        transport_attempts = 3 if idempotent_read else 1
        with httpx.Client(timeout=self._timeout) as hx:

            def send() -> httpx.Response:
                for attempt in range(transport_attempts):
                    try:
                        return hx.request(
                            method,
                            self._url(path),
                            headers=self._headers(),
                            **kwargs,
                        )
                    except httpx.TransportError:
                        if attempt + 1 == transport_attempts:
                            raise
                        time.sleep((0.25, 1.0)[attempt])
                raise RuntimeError("HTTP transport attempt limit exceeded")

            if self._refresh_before_request:
                if not self._refresh(hx):
                    raise ApiError(401, "Could not refresh the expired CLI session")
                self._refresh_before_request = False
            resp = send()
            if resp.status_code == 401 and retry and self._refresh(hx):
                resp = send()
            if idempotent_read:
                resp = retry_rate_limited(resp, send, lambda: retry and self._refresh(hx))
        check_route_lifecycle(resp)
        self._raise(resp)
        self._validate_contract(resp)
        return resp

    # ── request methods ───────────────────────────────────────────────────────

    def get(self, path: str, params: dict | None = None) -> httpx.Response:
        return self._request("GET", path, params=params)

    def put(self, path: str, json: Any = None, params: dict | None = None) -> httpx.Response:
        return self._request("PUT", path, json=json, params=params)

    def post(self, path: str, json: Any = None, **kwargs: Any) -> httpx.Response:
        return self._request("POST", path, json=json, **kwargs)

    def issue_native(self, path: str, payload: dict) -> httpx.Response:
        """Bounded retries only for temporary issuance, never arbitrary mutations."""
        import httpx2 as httpx

        if not is_mint_token_path(path):
            raise ValueError(
                "Native issuance requires a repository or remote-cache mint-token path"
            )
        deadline = time.monotonic() + 30
        for attempt in range(3):
            retry_after = None
            try:
                return self.post(
                    path,
                    json=payload,
                    retry=False,
                    timeout=min(5.0, max(0.01, (deadline - time.monotonic()) / 4)),
                )
            except ApiError as exc:
                if exc.status_code not in {429, 502, 503, 504} or attempt == 2:
                    raise
                failure: Exception = exc
                retry_after = exc.retry_after
            except httpx.TransportError as exc:
                if attempt == 2:
                    raise
                failure = exc
            delay = (0.25, 1.0)[attempt] + random.uniform(0, 0.1)
            if retry_after is not None:
                delay = max(delay, retry_after)
            if time.monotonic() + delay >= deadline:
                raise failure
            time.sleep(delay)
        raise RuntimeError("Native issuance retry limit exceeded")

    def patch(
        self,
        path: str,
        json: Any = None,
        params: dict | None = None,
    ) -> httpx.Response:
        return self._request("PATCH", path, json=json, params=params)

    def delete(self, path: str, params: dict | None = None) -> httpx.Response:
        return self._request("DELETE", path, params=params)
