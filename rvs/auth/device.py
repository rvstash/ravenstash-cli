"""Shared device authorization implementation for rvs auth login."""

import importlib.metadata
import math
import platform as platform_mod
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2 as httpx
from rich.live import Live

from .. import auth as auth_mod
from .. import config as cfg_mod
from .. import output
from ..api import api_url as endpoint_url
from ..api import (
    platform_path,
    retry_after_seconds,
    validate_api_version,
)
from ..artifacts.meta import sync_native_registries
from ..client import ApiRouteRetiredError, check_route_lifecycle
from ..identity import PACKAGE_NAME


_POLL_FRAMES = ("🔄", "🔃")
_HOUR_SECONDS = 60 * 60
_DAY_SECONDS = 24 * _HOUR_SECONDS
_MIN_DURATION_SECONDS = 12 * _HOUR_SECONDS
_MAX_DURATION_SECONDS = 180 * _DAY_SECONDS
_DURATION_RE = re.compile(
    r"^\s*(?P<amount>\d+)\s*(?P<unit>h|hour|hours|d|day|days|month|months)\s*$",
    re.IGNORECASE,
)


def _rvs_version() -> str:
    try:
        return importlib.metadata.version(PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return "dev"


def _rvs_user_agent() -> str:
    return f"rvs/{_rvs_version()}"


def _device_platform() -> str:
    return platform_mod.system().strip().lower() or "unknown"


def _resolve_api_url(profile: str, api_url: str | None) -> str:
    cfg = cfg_mod.load()
    existing_profile = cfg.profiles.get(profile)
    if api_url:
        try:
            return cfg_mod.validate_service_url(api_url, label=f"{profile} API URL")
        except ValueError as exc:
            output.fatal(str(exc))
    if existing_profile:
        return existing_profile.api_url.rstrip("/")
    return cfg_mod.profile_api_url(profile)


def _error_code(response: httpx.Response) -> str | None:
    try:
        payload: Any = response.json()
    except ValueError:
        return None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, str):
            return error
        detail = payload.get("detail")
        if isinstance(detail, dict):
            nested = detail.get("error")
            if isinstance(nested, str):
                return nested
    return None


def parse_duration_seconds(value: str) -> int:
    match = _DURATION_RE.match(value)
    if not match:
        raise ValueError("Use a duration like 12h, 3days, 1month, or 6months.")

    amount = int(match.group("amount"))
    unit = match.group("unit").lower()
    if unit in {"h", "hour", "hours"}:
        seconds = amount * _HOUR_SECONDS
    elif unit in {"d", "day", "days"}:
        seconds = amount * _DAY_SECONDS
    else:
        seconds = amount * 30 * _DAY_SECONDS

    if seconds < _MIN_DURATION_SECONDS or seconds > _MAX_DURATION_SECONDS:
        raise ValueError("Duration must be between 12 hours and 180 days.")
    return seconds


def _store_expiring_credential(
    *,
    profile: str,
    api_url: str,
    token: str,
    refresh_token: str,
    account_ref: str | None,
    expires_in: int,
    refresh_expires_in: int,
    credential_store: str | None = None,
) -> None:
    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
    refresh_expires_at = datetime.now(UTC) + timedelta(seconds=refresh_expires_in)
    try:
        if credential_store is None:
            auth_mod.set_token(profile, token)
            auth_mod.set_refresh_token(profile, refresh_token)
        else:
            auth_mod.set_token(profile, token, credential_store)
            auth_mod.set_refresh_token(profile, refresh_token, credential_store)
        cfg_mod.set_profile_metadata(
            profile,
            api_url=api_url,
            account_ref=account_ref,
            credential_store=credential_store,
            credential_type=auth_mod.EXPIRING_CREDENTIAL_TYPE,
            credential_api_url=api_url,
            expires_at=expires_at.isoformat(),
            refresh_expires_at=refresh_expires_at.isoformat(),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        if credential_store is not None:
            auth_mod.delete_token_from_store(profile, credential_store)
        raise RuntimeError(str(exc)) from exc


def _print_browser_prompt(*, no_browser: bool) -> None:
    if no_browser:
        return

    output.info("Open the link in your browser, or CTRL+CLICK it from your terminal.")


class _AuthorizationPollDisplay:
    def __init__(self) -> None:
        self._enabled = output.console.is_terminal
        self._live: Live | None = None
        self._frame = 0

    def __enter__(self) -> _AuthorizationPollDisplay:
        if self._enabled:
            self._live = Live(
                self._render(),
                console=output.console,
                refresh_per_second=10,
                transient=True,
            )
            self._live.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.stop()

    def stop(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    def sleep(self, seconds: int, *, tick: Any | None = None) -> None:
        if not self._enabled:
            if tick is not None:
                tick()
            time.sleep(seconds)
            return

        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if tick is not None:
                tick()
            self._refresh()
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

    def _refresh(self) -> None:
        if self._live is None:
            return
        self._frame += 1
        self._live.update(self._render())

    def _render(self) -> str:
        frame = _POLL_FRAMES[self._frame % len(_POLL_FRAMES)]
        return f"[cyan]{frame}[/] device authorization in progress via UI"


def perform_device_login(
    *,
    profile: str,
    api_url: str | None,
    no_browser: bool,
    duration: str | None = None,
    credential_store: str | None = None,
) -> None:
    resolved_api_url = _resolve_api_url(profile, api_url)
    if api_url is None and (ignored := cfg_mod.ignored_endpoint_overrides(profile)):
        verb = "is" if len(ignored) == 1 else "are"
        output.warn(
            f"{' and '.join(ignored)} {verb} ignored because profile '{profile}' is already "
            f"saved and uses {resolved_api_url}. Log in with a new profile name, or delete "
            "this profile first, to use another Ravenstash API."
        )
    previous_refresh_token: str | None = None
    # A replaced session is revoked where it was issued.
    previous_refresh_api_url = cfg_mod.stored_credential_api_url(profile) or resolved_api_url
    if auth_mod.has_active_expiring_session(profile):
        previous_refresh_token = auth_mod.get_refresh_token(profile)
        output.info(f"Profile '{profile}' is already authenticated; replacing it.")

    requested_duration_seconds: int | None = None
    if duration:
        try:
            requested_duration_seconds = parse_duration_seconds(duration)
        except ValueError as exc:
            output.fatal(str(exc))

    try:
        with httpx.Client(timeout=15.0) as client:
            rvs_version = _rvs_version()
            request_payload: dict[str, Any] = {
                "client_name": "rvs CLI",
                "client_version": rvs_version,
                "platform": _device_platform(),
            }
            if requested_duration_seconds is not None:
                request_payload["requested_duration_seconds"] = requested_duration_seconds
            create_resp = client.post(
                endpoint_url(resolved_api_url, platform_path("auth/device/code")),
                headers={"User-Agent": f"rvs/{rvs_version}"},
                json=request_payload,
            )
            check_route_lifecycle(create_resp)
            validate_api_version(create_resp)
            create_resp.raise_for_status()
            session = create_resp.json()
    except Exception as exc:
        output.fatal(f"Could not start device login: {exc}")

    user_code = session["user_code"]
    approval_url = session.get("verification_uri_complete") or session["verification_uri"]
    output.info(f"Device code: {user_code}")
    output.info(f"Open this URL to approve the device: {approval_url}")
    _print_browser_prompt(no_browser=no_browser)

    device_code = session["device_code"]
    interval = max(int(session.get("interval") or 5), 1)
    deadline = time.monotonic() + max(int(session.get("expires_in") or 600), 1)

    try:
        with httpx.Client(timeout=15.0) as client, _AuthorizationPollDisplay() as poll_display:
            while time.monotonic() < deadline:
                poll_resp = client.post(
                    endpoint_url(resolved_api_url, platform_path("auth/device/token")),
                    headers={"User-Agent": _rvs_user_agent()},
                    json={"device_code": device_code},
                )
                check_route_lifecycle(poll_resp)
                validate_api_version(poll_resp)
                if poll_resp.is_success:
                    payload = poll_resp.json()
                    try:
                        _store_expiring_credential(
                            profile=profile,
                            api_url=resolved_api_url,
                            token=payload["access_token"],
                            refresh_token=payload["refresh_token"],
                            account_ref=payload.get("account_ref"),
                            expires_in=int(payload.get("expires_in") or 0),
                            refresh_expires_in=int(payload.get("refresh_expires_in") or 0),
                            credential_store=credential_store,
                        )
                    except RuntimeError as exc:
                        auth_mod.discard_device_refresh_token(
                            resolved_api_url,
                            payload["refresh_token"],
                        )
                        output.fatal(f"Login approved, but credentials could not be stored: {exc}")
                    try:
                        if previous_refresh_token:
                            auth_mod.revoke_device_refresh_token(
                                previous_refresh_api_url,
                                previous_refresh_token,
                            )
                        discovery = sync_native_registries(
                            profile, resolved_api_url, http_client=client
                        )
                    except ApiRouteRetiredError as exc:
                        # The new credential is stored; report that before the
                        # update message rather than calling the login failed.
                        poll_display.stop()
                        output.success(
                            f"Authenticated profile '{profile}' with an expiring credential."
                        )
                        output.fatal(str(exc))
                    poll_display.stop()
                    if discovery == "failed":
                        output.warn(
                            "Could not refresh native registry endpoints; package tools "
                            "keep the profile's stored endpoints until the next refresh."
                        )
                    output.success(
                        f"Authenticated profile '{profile}' with an expiring credential."
                    )
                    return

                error = _error_code(poll_resp)
                if error == "authorization_pending":
                    poll_display.sleep(interval)
                    continue
                if error == "slow_down":
                    interval += 5
                    poll_display.sleep(interval)
                    continue
                if poll_resp.status_code in (429, 503):
                    # Back off once, as long as the server asks, without outliving
                    # the session; later polls return to the normal interval.
                    retry_after = retry_after_seconds(poll_resp.headers)
                    wait = (
                        max(interval, math.ceil(retry_after))
                        if retry_after is not None
                        else interval + 5
                    )
                    remaining = math.ceil(deadline - time.monotonic())
                    poll_display.sleep(max(1, min(wait, remaining)))
                    continue
                if error == "access_denied":
                    output.fatal("Login was denied.")
                if error == "expired_token":
                    output.fatal("Login session expired before approval.")

                poll_resp.raise_for_status()
    except KeyboardInterrupt:
        output.fatal("Login cancelled.")
    except Exception as exc:
        output.fatal(f"Login failed: {exc}")

    output.fatal("Login session expired before approval.")
