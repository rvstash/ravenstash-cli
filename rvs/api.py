"""Ravenstash API version selection, path builders, and wire-shape helpers.

Every API resource lives below one API generation and one owning group:
``/v0/platform/...`` for platform identity and sign-in, and ``/v0/{product}/...``
for product resources such as ``/v0/artifacts/...``. Command modules pass
group-relative paths to :func:`platform_path` or :func:`artifacts_path`; they
never spell the version or group segments themselves.
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import quote


API_VERSION = "v0"
API_STABILITY = "unstable"
PLATFORM_GROUP = "platform"
ARTIFACTS_PRODUCT = "artifacts"
API_ROUTE_RETIRED = "ApiRouteRetired"
COLLECTION_PAGE_LIMIT = 100

_GROUPS = frozenset({PLATFORM_GROUP, ARTIFACTS_PRODUCT})
_ROOT_RESOURCES = frozenset({"meta"})
_VERSION_SEGMENT = re.compile(r"v[0-9]+(?:(?:alpha|beta)[0-9]*)?")
_MINT_TOKEN_PATH = re.compile(
    rf"/{API_VERSION}/{ARTIFACTS_PRODUCT}/(?:repositories|remote-caches)/[^/]+/mint-token"
)


class ApiVersionMismatchError(RuntimeError):
    """The server identified a different wire-contract generation."""


def _relative(path: str) -> str:
    candidate = path.strip("/")
    first = candidate.split("/", 1)[0]
    if _VERSION_SEGMENT.fullmatch(first):
        raise ValueError("Ravenstash API resource paths must not embed an API version")
    if first in _GROUPS:
        raise ValueError(
            "Ravenstash API resource paths must not embed a platform or product segment; "
            "use platform_path or artifacts_path"
        )
    return candidate


def _grouped(group: str, path: str) -> str:
    candidate = _relative(path)
    if not candidate:
        raise ValueError(f"A {group} resource path is required")
    return f"/{API_VERSION}/{group}/{candidate}"


def api_path(path: str) -> str:
    """Return an ungrouped generation resource path such as ``/v0/meta``."""
    candidate = _relative(path)
    if candidate not in _ROOT_RESOURCES:
        raise ValueError("Only generation metadata is outside a platform or product group")
    return f"/{API_VERSION}/{candidate}"


def platform_path(path: str) -> str:
    """Return a platform (identity, accounts, sign-in) resource path."""
    return _grouped(PLATFORM_GROUP, path)


def artifacts_path(path: str) -> str:
    """Return an Artifacts product resource path."""
    return _grouped(ARTIFACTS_PRODUCT, path)


def is_path_segment(value: str) -> bool:
    """Whether ``value`` can be one path segment that URL normalization keeps.

    ``quote`` leaves ``.`` alone, and HTTP clients resolve the dot segments
    ``.`` and ``..``, so those (and the empty segment) would change the route.
    """
    return value not in {"", ".", ".."}


def segment(value: str) -> str:
    """Encode one caller-supplied path segment, such as a reference or digest."""
    if not is_path_segment(value):
        raise ValueError("Ravenstash API path segments cannot be empty, '.', or '..'")
    return quote(value, safe="")


def is_mint_token_path(path: str) -> bool:
    return _MINT_TOKEN_PATH.fullmatch(path) is not None


def repository_mint_token_path(repository_ref: str) -> str:
    return artifacts_path(f"repositories/{segment(repository_ref)}/mint-token")


def remote_cache_mint_token_path(remote_cache_ref: str) -> str:
    return artifacts_path(f"remote-caches/{segment(remote_cache_ref)}/mint-token")


def api_url(base_url: str, path: str) -> str:
    """Join a control-plane base URL with a path built by a builder above."""
    parts = path.split("/")
    grouped = len(parts) > 3 and parts[2] in _GROUPS and all(parts[3:])
    root_resource = len(parts) == 3 and parts[2] in _ROOT_RESOURCES
    if parts[:2] != ["", API_VERSION] or not (grouped or root_resource):
        raise ValueError(
            "Build Ravenstash API paths with api_path, platform_path, or artifacts_path"
        )
    return f"{base_url.rstrip('/')}{path}"


def validate_api_version(response: Any) -> None:
    reported = getattr(response, "headers", {}).get("Ravenstash-API-Version")
    if reported is not None and reported != API_VERSION:
        raise ApiVersionMismatchError(
            f"Ravenstash API returned version {reported!r}; rvs expects {API_VERSION!r}"
        )


def retry_after_seconds(headers: Any) -> float | None:
    """Parse a ``Retry-After`` header as delta seconds or an HTTP date."""
    raw = headers.get("Retry-After") if headers is not None else None
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        try:
            return max(0.0, (parsedate_to_datetime(raw) - datetime.now(UTC)).total_seconds())
        except TypeError, ValueError, OverflowError:
            return None


def route_deprecated(headers: Any) -> bool:
    """Return whether a response announces a deprecated route (RFC 9745).

    Only the header's presence matters, so a malformed value still counts.
    """
    return headers is not None and headers.get("Deprecation") is not None


def route_sunset_date(headers: Any) -> str | None:
    """Return the UTC date of a ``Sunset`` header (RFC 8594), if it parses."""
    raw = headers.get("Sunset") if headers is not None else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        sunset = parsedate_to_datetime(raw.strip())
        if sunset.tzinfo is None:
            sunset = sunset.replace(tzinfo=UTC)
        return sunset.astimezone(UTC).date().isoformat()
    except TypeError, ValueError, OverflowError, IndexError:
        return None


def collection_items(payload: Any) -> list[dict[str, Any]]:
    """Return the items of one ``Page{items, next_cursor}`` response."""
    return collection_page(payload)[0]


def collection_page(payload: Any) -> tuple[list[dict[str, Any]], str | None]:
    """Return the items and continuation cursor of one collection page."""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("Ravenstash API collection response is invalid")
    next_cursor = payload.get("next_cursor")
    if next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor):
        raise ValueError("Ravenstash API collection cursor is invalid")
    return payload["items"], next_cursor


class _PageReader(Protocol):
    def get(self, path: str, params: dict | None = None) -> Any: ...


# A listing that yields this many pages without ending is treated as broken.
_MAX_COLLECTION_PAGES = 10_000


@dataclass
class Collection:
    """Items read from a collection and the cursor where reading stopped."""

    items: list[dict[str, Any]] = field(default_factory=list)
    next_cursor: str | None = None


def read_collection(
    client: _PageReader,
    path: str,
    params: dict[str, Any] | None = None,
    *,
    max_items: int | None = None,
    cursor: str | None = None,
) -> Collection:
    """Read an API collection by following ``next_cursor``.

    Every page is requested with ``limit`` and, after the first, the cursor the
    previous page returned. Reading stops when the server returns a null cursor
    or, when ``max_items`` is set, once that many items were read; the returned
    ``next_cursor`` then resumes the collection after the last returned item.
    A repeated cursor or an unending listing raises ``ValueError``; request
    errors, including a rejected cursor, propagate unchanged.
    """
    if max_items is not None and max_items < 1:
        raise ValueError("max_items must be positive")
    query = {key: value for key, value in (params or {}).items() if value is not None}
    result = Collection(next_cursor=cursor)
    seen: set[str] = {cursor} if cursor is not None else set()
    for _ in range(_MAX_COLLECTION_PAGES):
        limit = COLLECTION_PAGE_LIMIT
        if max_items is not None:
            limit = min(limit, max_items - len(result.items))
        page_query = {**query, "limit": limit}
        if result.next_cursor is not None:
            page_query["cursor"] = result.next_cursor
        items, next_cursor = collection_page(client.get(path, params=page_query).json())
        if len(items) > limit:
            raise ValueError("Ravenstash API collection page exceeded the requested limit")
        result.items.extend(items)
        result.next_cursor = next_cursor
        if next_cursor is None or (max_items is not None and len(result.items) >= max_items):
            return result
        if next_cursor in seen:
            raise ValueError("Ravenstash API collection repeated a cursor")
        seen.add(next_cursor)
    raise ValueError("Ravenstash API collection did not end")


def collection_all(
    client: _PageReader, path: str, params: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Return every item of an API collection, following all pages."""
    return read_collection(client, path, params).items
