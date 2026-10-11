"""Credential storage for rvs.

Device-login credentials are stored in a selected provider: an OS keyring,
``pass``, the passphrase-encrypted Ravenstash vault, or an explicitly accepted
plaintext file. Automation credentials must be supplied through ``RVS_TOKEN``.

Public API
----------
get_token(profile) -> str | None
set_token(profile, token) -> None
delete_token(profile) -> None
token_source(profile) -> str | None
"""

import hashlib
import importlib
import importlib.metadata
import logging
import os
import platform as platform_mod
import secrets
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import IO, TYPE_CHECKING, Any

import httpx2 as httpx

from ..api import (
    ApiVersionMismatchError,
    platform_path,
    validate_api_version,
)
from ..api import (
    api_url as endpoint_url,
)
from ..identity import PACKAGE_NAME
from . import stores
from .token_format import validate_public_token


def _optional_module(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


fcntl = _optional_module("fcntl")
msvcrt = _optional_module("msvcrt")


if TYPE_CHECKING:
    from collections.abc import Iterator


logger = logging.getLogger(__name__)

_SERVICE = "rvs"
_REFRESH_SUFFIX = ":refresh"
_REFRESH_SKEW_SECONDS = 60
EXPIRING_CREDENTIAL_TYPE = "expiring"
_CREDENTIAL_STORES = {"auto", "keyring", "pass", "vault", "plaintext"}


class NoCredentialStoreError(RuntimeError):
    """No configured credential store can be used without first-time setup."""


def credential_origin_error(profile: str) -> str | None:
    """Explain why *profile*'s stored credential must not be sent, if it must not.

    Device credentials are bound to the Ravenstash API that issued them. They are never
    sent to another origin, even when the profile's API URL was changed.
    """
    from .. import config as cfg_mod

    p = cfg_mod.load().profiles.get(profile)
    if p is None or p.credential_origin_matches():
        return None
    issuer = cfg_mod.api_origin(p.credential_origin_url)
    target = cfg_mod.api_origin(p.api_url)
    return (
        f"The stored login for profile '{profile}' was issued by {issuer}, not {target}; "
        f"rvs will not send it to {target}. Run `rvs auth login --profile {profile}` "
        f"to log in to {target}."
    )


def is_refreshable_credential_type(credential_type: str | None) -> bool:
    """Return whether *credential_type* can use device refresh tokens."""
    return credential_type == EXPIRING_CREDENTIAL_TYPE


def display_credential_type(credential_type: str | None) -> str | None:
    """Return the user-facing label for a stored credential type."""
    return credential_type


def _rvs_user_agent() -> str:
    try:
        return f"rvs/{importlib.metadata.version(PACKAGE_NAME)}"
    except importlib.metadata.PackageNotFoundError:
        return "rvs/dev"


def _device_platform() -> str:
    return platform_mod.system().strip().lower() or "unknown"


# ── keyring helpers ───────────────────────────────────────────────────────────


def _keyring_available() -> bool:
    return stores.system_keyring_status().available


def _kr_get(profile: str) -> str | None:
    try:
        return stores.system_get(_SERVICE, profile)
    except stores.StoreError:
        return None


def _kr_set(profile: str, token: str) -> None:
    try:
        stores.system_set(_SERVICE, profile, token)
    except stores.StoreError as exc:
        msg = (
            "No usable OS keyring is available. Use RVS_TOKEN for CI/headless "
            "runs or configure a keyring before storing local credentials."
        )
        raise RuntimeError(msg) from exc


def _kr_delete(profile: str) -> None:
    stores.system_delete(_SERVICE, profile)


def credential_store_statuses() -> list[stores.StoreStatus]:
    """Return non-secret diagnostics for supported local credential stores."""
    return [
        stores.system_keyring_status(),
        stores.pass_status(),
        stores.vault_status(),
        stores.plaintext_status(),
    ]


def _credential_store_preference(profile: str, requested: str | None = None) -> str:
    from .. import config as cfg_mod

    value = requested or os.environ.get("RVS_CREDENTIAL_STORE")
    if value is None:
        cfg = cfg_mod.load()
        profile_config = cfg.profiles.get(profile)
        value = (
            profile_config.credential_store
            if profile_config is not None and profile_config.credential_store
            else cfg.credential_store
        )
    if not isinstance(value, str):
        raise RuntimeError("Credential store must be auto, keyring, pass, vault, or plaintext.")
    normalized = value.strip().lower()
    if normalized not in _CREDENTIAL_STORES:
        raise RuntimeError("Credential store must be auto, keyring, pass, vault, or plaintext.")
    return normalized


def credential_store_preference(profile: str, requested: str | None = None) -> str:
    """Return the effective configured provider name without probing it."""
    return _credential_store_preference(profile, requested)


def _store_available(name: str) -> bool:
    if name == "keyring":
        return _keyring_available()
    if name == "pass":
        return stores.pass_status().available
    if name == "vault":
        return stores.vault_status().available
    if name == "plaintext":
        return stores.plaintext_status().available
    return False


def _store_candidates(preference: str) -> tuple[str, ...]:
    if preference == "auto":
        # Plaintext is never selected implicitly. It is usable only after the
        # user explicitly configures it globally, per profile, or per login.
        return ("keyring", "pass", "vault")
    return (preference,)


def selected_credential_store(
    profile: str,
    requested: str | None = None,
    *,
    required: bool = False,
) -> str | None:
    """Resolve the configured or automatically detected credential store."""
    preference = _credential_store_preference(profile, requested)
    candidates = _store_candidates(preference)
    for candidate in candidates:
        if _store_available(candidate):
            return candidate
    if not required:
        return None

    statuses = {status.name: status for status in credential_store_statuses()}
    details = "; ".join(
        f"{candidate}: {statuses[candidate].detail.rstrip('.')}" for candidate in candidates
    )
    raise NoCredentialStoreError(
        "No usable credential store is available. "
        f"{details}. Run `rvs auth storage setup` interactively, configure an existing "
        "store, or use RVS_TOKEN for CI/headless runs."
    )


def _store_get(name: str, account: str, *, strict: bool = False) -> str | None:
    if name == "keyring":
        if strict:
            try:
                return stores.system_get(_SERVICE, account)
            except stores.StoreError as exc:
                raise RuntimeError(str(exc)) from exc
        return _kr_get(account)
    try:
        if name == "pass":
            return stores.pass_get(account)
        if name == "vault":
            return stores.vault_get(account)
        return stores.plaintext_get(account)
    except stores.StoreError as exc:
        if strict:
            raise RuntimeError(str(exc)) from exc
        return None


def _store_set(name: str, account: str, token: str) -> None:
    if name == "keyring":
        _kr_set(account, token)
        return
    try:
        if name == "pass":
            stores.pass_set(account, token)
        elif name == "vault":
            stores.vault_set(account, token)
        else:
            stores.plaintext_set(account, token)
    except stores.StoreError as exc:
        raise RuntimeError(f"Could not store credentials with {name}: {exc}") from exc


def _store_delete(name: str, account: str, *, strict: bool = False) -> None:
    try:
        if name == "keyring":
            _kr_delete(account)
        elif name == "pass":
            stores.pass_delete(account)
        elif name == "vault":
            stores.vault_delete(account)
        else:
            stores.plaintext_delete(account)
    except (OSError, stores.StoreError) as exc:
        if strict:
            raise RuntimeError(str(exc)) from exc
        return


def preflight_credential_store(profile: str, requested: str | None = None) -> str:
    """Verify candidate stores with a disposable round trip before device auth."""
    preference = _credential_store_preference(profile, requested)
    candidates = _store_candidates(preference)
    failures: list[str] = []
    available = False
    for store in candidates:
        if not _store_available(store):
            continue
        available = True
        try:
            _preflight_store(store)
        except RuntimeError as exc:
            failures.append(f"{store}: {exc}")
            continue
        return store
    if not available:
        selected_credential_store(profile, requested, required=True)
    if preference == "auto":
        raise NoCredentialStoreError(
            "No automatically detected credential store passed its write/read/delete check. "
            + "; ".join(failures)
            + ". Run `rvs auth storage setup` interactively or use RVS_TOKEN for "
            "CI/headless runs."
        )
    raise RuntimeError(
        "No credential store passed its write/read/delete check. " + "; ".join(failures)
    )


def _preflight_store(store: str) -> None:
    account = f"__probe__:{secrets.token_urlsafe(12)}"
    probe = secrets.token_urlsafe(24)
    failure: RuntimeError | None = None
    try:
        _store_set(store, account, probe)
        if _store_get(store, account, strict=True) != probe:
            raise RuntimeError("the credential store did not return the probe value")
    except RuntimeError as exc:
        failure = exc
    try:
        _store_delete(store, account, strict=True)
    except RuntimeError as cleanup_error:
        if failure is not None:
            raise RuntimeError(
                f"{failure}; probe cleanup also failed: {cleanup_error}"
            ) from failure
        raise RuntimeError(f"probe cleanup failed: {cleanup_error}") from cleanup_error
    if failure is not None:
        raise RuntimeError(str(failure)) from failure


def _refresh_profile(profile: str) -> str:
    return f"{profile}{_REFRESH_SUFFIX}"


@contextmanager
def _profile_refresh_lock(profile: str) -> Iterator[None]:
    """Serialize refresh-token rotation across processes for one profile."""
    from .. import config as cfg_mod

    cfg_mod.CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    cfg_mod.CONFIG_DIR.chmod(0o700)
    digest = hashlib.sha256(profile.encode("utf-8")).hexdigest()[:24]
    lock_path = cfg_mod.CONFIG_DIR / f".refresh-{digest}.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    lock_file: IO[bytes] | None = None
    try:
        os.chmod(lock_path, 0o600)
        lock_file = os.fdopen(descriptor, "rb", closefd=True)
        descriptor = -1
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:
            if lock_path.stat().st_size == 0:
                lock_file.close()
                with lock_path.open("wb") as seed:
                    seed.write(b"\0")
                lock_file = lock_path.open("rb")
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        yield
    finally:
        if lock_file is not None:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            lock_file.close()
        elif descriptor >= 0:
            os.close(descriptor)


def _stored_profile_expired(profile: str) -> bool:
    from .. import config as cfg_mod

    p = cfg_mod.load().profiles.get(profile)
    if not p or not is_refreshable_credential_type(p.credential_type) or not p.expires_at:
        return False
    try:
        expires_at = datetime.fromisoformat(p.expires_at)
    except ValueError:
        return False
    now = datetime.now(expires_at.tzinfo or UTC)
    return expires_at <= now + timedelta(seconds=_REFRESH_SKEW_SECONDS)


def _stored_refresh_token_expired(profile: str) -> bool:
    from .. import config as cfg_mod

    p = cfg_mod.load().profiles.get(profile)
    if not p or not is_refreshable_credential_type(p.credential_type) or not p.refresh_expires_at:
        return False
    try:
        expires_at = datetime.fromisoformat(p.refresh_expires_at)
    except ValueError:
        return False
    now = datetime.now(expires_at.tzinfo or UTC)
    return expires_at <= now


def get_refresh_token(profile: str) -> str | None:
    """Return the stored device refresh token for *profile*, if present."""
    store = selected_credential_store(profile)
    if store is None:
        return None
    return _store_get(store, _refresh_profile(profile))


def has_active_expiring_session(profile: str) -> bool:
    """Return whether *profile* appears to have a reusable expiring login."""
    from .. import config as cfg_mod

    p = cfg_mod.load().profiles.get(profile)
    if not p or not is_refreshable_credential_type(p.credential_type):
        return False
    if _stored_refresh_token_expired(profile):
        return False
    return get_refresh_token(profile) is not None


def revoke_stored_refresh_token(profile: str) -> bool | None:
    """Best-effort revocation of *profile*'s stored refresh token on its issuer.

    Returns ``None`` when there is nothing to revoke, otherwise whether the
    server accepted the revocation. Local credentials are left untouched.
    """
    from .. import config as cfg_mod

    try:
        p = cfg_mod.load().profiles.get(profile)
        if p is None or not is_refreshable_credential_type(p.credential_type):
            return None
        if _stored_refresh_token_expired(profile):
            return None
        refresh_token = get_refresh_token(profile)
    except OSError, RuntimeError, ValueError:
        logger.info("Cannot read the stored refresh token for profile %s", profile)
        return False
    if not refresh_token:
        return None
    return revoke_device_refresh_token(p.credential_origin_url, refresh_token)


def revoke_device_refresh_token(api_url: str, refresh_token: str) -> bool:
    """Best-effort server-side revocation for a stored device refresh token.

    A retired route raises :class:`~rvs.client.ApiRouteRetiredError` so the
    caller can tell the user to update rvs instead of reporting a plain failure.
    """
    from ..client import check_route_lifecycle

    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.post(
                endpoint_url(api_url, platform_path("auth/device/revoke")),
                headers={"User-Agent": _rvs_user_agent()},
                json={"refresh_token": refresh_token},
            )
            check_route_lifecycle(response)
            validate_api_version(response)
    except httpx.HTTPError, ApiVersionMismatchError:
        logger.info("Device refresh token revocation request failed")
        return False
    return response.is_success


def discard_device_refresh_token(api_url: str, refresh_token: str) -> None:
    """Revoke a refresh token while cleaning up after a failed credential write.

    The cleanup must finish even when this rvs release is no longer supported,
    so a retired revocation route is only logged here.
    """
    from ..client import ApiRouteRetiredError

    try:
        revoke_device_refresh_token(api_url, refresh_token)
    except ApiRouteRetiredError:
        logger.info("Device refresh token revocation route is retired")


def refresh_expiring_credential(
    profile: str,
    *,
    stale_access_token: str | None = None,
    http_client: httpx.Client | None = None,
) -> str | None:
    """Refresh an expired expiring device credential for *profile*.

    A retired refresh route raises :class:`~rvs.client.ApiRouteRetiredError`.
    """
    from .. import config as cfg_mod
    from ..client import check_route_lifecycle, retry_rate_limited

    initial_store = selected_credential_store(profile)
    initial_refresh_token = (
        _store_get(initial_store, _refresh_profile(profile)) if initial_store else None
    )

    with _profile_refresh_lock(profile):
        store = selected_credential_store(profile)
        if store is None:
            return None

        cfg = cfg_mod.load()
        p = cfg.profiles.get(profile)
        if not p or not is_refreshable_credential_type(p.credential_type):
            return None
        if not p.credential_origin_matches():
            # Never send a refresh token to an API other than the one that issued it.
            logger.info("Refusing to refresh profile %s against another API origin", profile)
            return None
        issuer_api_url = p.credential_origin_url

        refresh_token = _store_get(store, _refresh_profile(profile))
        if not refresh_token:
            return None
        current_access_token = _store_get(store, profile)
        refresh_was_rotated = (
            initial_refresh_token is not None and refresh_token != initial_refresh_token
        )
        access_was_replaced = (
            stale_access_token is not None
            and current_access_token is not None
            and current_access_token != stale_access_token
        )
        if current_access_token is not None and (refresh_was_rotated or access_was_replaced):
            return current_access_token

        from .refresh_operation import pending_refresh_operation

        try:
            operation_id, pending_operation = pending_refresh_operation(
                cfg_mod.CONFIG_DIR, profile, refresh_token
            )
        except OSError:
            logger.info("Cannot persist refresh operation for profile %s", profile)
            return None
        request = {
            "operation_id": operation_id,
            "refresh_token": refresh_token,
            "platform": _device_platform(),
        }
        refresh_url = endpoint_url(issuer_api_url, platform_path("auth/device/refresh"))
        headers = {"User-Agent": _rvs_user_agent()}

        def send() -> httpx.Response:
            if http_client is None:
                with httpx.Client(timeout=15.0) as client:
                    return client.post(refresh_url, headers=headers, json=request)
            return http_client.post(refresh_url, headers=headers, json=request, timeout=15.0)

        try:
            # The operation ID makes a resent refresh safe after 429 or 503.
            response = retry_rate_limited(send(), send)
            check_route_lifecycle(response)
            validate_api_version(response)
        except httpx.HTTPError, ApiVersionMismatchError:
            logger.info("Device credential refresh failed for profile %s", profile)
            return None

        if not response.is_success:
            logger.info(
                "Device credential refresh rejected for profile %s: HTTP %s",
                profile,
                response.status_code,
            )
            if response.status_code in {400, 401, 403, 409}:
                delete_token(profile)
                pending_operation.unlink(missing_ok=True)
            return None

        payload = response.json()
        access_token = payload.get("access_token")
        new_refresh_token = payload.get("refresh_token")
        expires_in = int(payload.get("expires_in") or 0)
        refresh_expires_in = int(payload.get("refresh_expires_in") or 0)
        if not isinstance(access_token, str) or not isinstance(new_refresh_token, str):
            delete_token(profile)
            return None

        try:
            # Store the rotated refresh token first. A process interruption can
            # then recover with it instead of retaining the invalidated token.
            _store_set(store, _refresh_profile(profile), new_refresh_token)
            _store_set(store, profile, access_token)
            expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
            refresh_expires_at = datetime.now(UTC) + timedelta(seconds=refresh_expires_in)
            cfg_mod.set_profile_metadata(
                profile,
                account_ref=payload.get("account_ref"),
                credential_store=store,
                credential_type=EXPIRING_CREDENTIAL_TYPE,
                credential_api_url=issuer_api_url,
                expires_at=expires_at.isoformat(),
                refresh_expires_at=refresh_expires_at.isoformat(),
            )
            pending_operation.unlink(missing_ok=True)
        except OSError, RuntimeError, ValueError:
            # A rotated pair is useful only when both secrets and its metadata are
            # durable. Revoke and remove a partial pair rather than leaving an
            # access token whose refresh state is ambiguous.
            discard_device_refresh_token(issuer_api_url, new_refresh_token)
            delete_token_from_store(profile, store)
            cfg_mod.clear_profile_credential_metadata(profile)
            return None
        refreshed_api_url = issuer_api_url

    # Product discovery is outside the refresh lock and never fails the refresh,
    # unless the API reports that this rvs release is no longer supported.
    from ..artifacts.meta import sync_native_registries

    sync_native_registries(profile, refreshed_api_url, http_client=http_client)
    return access_token


# ── Public API ────────────────────────────────────────────────────────────────


def get_token(profile: str, *, refresh: bool = True) -> str | None:
    """Return the effective credential for *profile*.

    RVS_TOKEN always wins and is intended for PAT/M2M automation. Local stored
    credentials are short-lived CLI JWTs from the device login flow. Callers may
    defer refresh so it can share their resource-request connection.
    """
    if "RVS_TOKEN" in os.environ:
        try:
            return validate_public_token(os.environ["RVS_TOKEN"])
        except ValueError:
            from .. import output

            output.fatal(
                "RVS_TOKEN must contain a current rvs_ust, rvs_uot, or rvs_oat automation credential. Clear it to use your stored login session."
            )
    expired = _stored_profile_expired(profile)
    if expired and refresh:
        return refresh_expiring_credential(profile)
    store = selected_credential_store(profile)
    if store is not None:
        token = _store_get(store, profile)
        if token:
            return token
        if refresh:
            return refresh_expiring_credential(profile)
    return None


def credential_needs_refresh(profile: str) -> bool:
    """Return whether a stored expiring credential should refresh before use."""
    return "RVS_TOKEN" not in os.environ and _stored_profile_expired(profile)


def token_source(profile: str) -> str | None:
    """Return where the effective credential for *profile* comes from."""
    if "RVS_TOKEN" in os.environ:
        return "RVS_TOKEN"
    if _stored_profile_expired(profile):
        return None
    store = selected_credential_store(profile)
    if store is not None and _store_get(store, profile):
        return store
    return None


def set_token(profile: str, token: str, credential_store: str | None = None) -> None:
    """Persist *token* for *profile* in the selected secure store."""
    store = selected_credential_store(profile, credential_store, required=True)
    assert store is not None
    _store_set(store, profile, token)


def set_refresh_token(profile: str, token: str, credential_store: str | None = None) -> None:
    """Persist the device refresh token for *profile*."""
    store = selected_credential_store(profile, credential_store, required=True)
    assert store is not None
    _store_set(store, _refresh_profile(profile), token)


def delete_token_from_store(profile: str, credential_store: str) -> None:
    """Remove both device credentials from one explicitly selected store."""
    if credential_store not in _CREDENTIAL_STORES - {"auto"}:
        raise ValueError("Credential store is invalid")
    _store_delete(credential_store, profile)
    _store_delete(credential_store, _refresh_profile(profile))


def delete_token_from_all_stores(profile: str) -> None:
    """Remove a profile's credentials without consulting local config."""
    for credential_store in sorted(_CREDENTIAL_STORES - {"auto"}):
        delete_token_from_store(profile, credential_store)


def delete_token(profile: str) -> None:
    """Remove stored credentials for *profile*."""
    preference = _credential_store_preference(profile)
    candidates = _store_candidates(preference)
    for store in candidates:
        delete_token_from_store(profile, store)
    from .. import config as cfg_mod

    cfg_mod.clear_profile_credential_metadata(profile)
