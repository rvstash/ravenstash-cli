"""Unauthenticated Artifacts discovery of native registry endpoints.

The platform sign-in responses carry no product data. After a device login and
after each access-token refresh, rvs reads ``GET /v0/artifacts/meta`` and
updates the profile's stored native-registry projection when it changed. A
failed discovery keeps the stored projection and never fails the command that
triggered it, unless the API reports that this rvs release is no longer
supported.
"""

import logging
from typing import Literal

import httpx2 as httpx

from .. import config as cfg_mod
from ..api import api_url, artifacts_path, validate_api_version
from ..client import ApiRouteRetiredError, check_route_lifecycle, rvs_user_agent


logger = logging.getLogger(__name__)

_DISCOVERY_TIMEOUT_SECONDS = 5.0


DiscoveryResult = Literal["changed", "unchanged", "failed"]


def sync_native_registries(
    profile: str,
    base_url: str,
    *,
    http_client: httpx.Client | None = None,
) -> DiscoveryResult:
    """Refresh *profile*'s stored native registries and report the outcome."""
    url = api_url(base_url, artifacts_path("meta"))
    headers = {"Accept": "application/json", "User-Agent": rvs_user_agent()}
    try:
        if http_client is None:
            with httpx.Client(timeout=_DISCOVERY_TIMEOUT_SECONDS) as client:
                response = client.get(url, headers=headers)
        else:
            response = http_client.get(url, headers=headers, timeout=_DISCOVERY_TIMEOUT_SECONDS)
        check_route_lifecycle(response)
        validate_api_version(response)
        if not response.is_success:
            logger.info(
                "Artifacts discovery failed for profile %s: HTTP %s", profile, response.status_code
            )
            return "failed"
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Artifacts discovery response is invalid")
        registries = payload["native_registries"]
        current = cfg_mod.load().profiles.get(profile)
        if current is None:
            return "unchanged"
        discovered = cfg_mod.parse_native_registries(
            registries,
            profile_name=profile,
            repository_domain=current.repository_domain,
        )
        if current.native_registries == discovered:
            return "unchanged"
        cfg_mod.set_profile_metadata(profile, native_registries=registries)
    except ApiRouteRetiredError:
        raise
    except Exception:  # Discovery is advisory; it must never fail the triggering command.
        logger.info(
            "Artifacts discovery failed for profile %s; keeping stored endpoints",
            profile,
            exc_info=True,
        )
        return "failed"
    return "changed"
