"""Package registry URL helpers for Ravenstash native paths.

Ravenstash registry protocol routes are intentionally kind-specific:

    PyPI:  https://pypi.rvsta.sh/{native_path}/simple/
    npm:   https://npm.rvsta.sh/{native_path}/
    Maven: https://maven.rvsta.sh/{native_path}/

``native_path`` is the opaque host-less path the API returns with a minted
package credential (for example ``/in/ar_...``). rvs does not interpret it; it
only checks that it is a plain relative path before joining it to an exact
discovered service URL. No hostname labels are derived.
"""

import re
from urllib.parse import urlparse


# Unreserved URL characters plus the few the API uses in names; no "%", "?",
# "#", "\\", or whitespace, so a path can never change the host or the query.
_NATIVE_PATH_SEGMENT = re.compile(r"[A-Za-z0-9._~@+-]+")
_MAX_NATIVE_PATH_LENGTH = 512


def native_path(value: object) -> str:
    """Return a server-issued native path without its outer slashes, or raise.

    The path is opaque: any number of plain segments is accepted, but dot
    segments, empty segments, and characters that could escape the path are not.
    """
    if not isinstance(value, str) or not value or len(value) > _MAX_NATIVE_PATH_LENGTH:
        raise ValueError("Ravenstash returned an invalid native path.")
    candidate = value.removeprefix("/").removesuffix("/")
    segments = candidate.split("/")
    if not candidate or any(
        segment in {".", ".."} or _NATIVE_PATH_SEGMENT.fullmatch(segment) is None
        for segment in segments
    ):
        raise ValueError("Ravenstash returned an invalid native path.")
    return candidate


def native_base_url(service_url: str) -> str:
    return service_url.rstrip("/")


def npm_auth_token_key(registry_url: str) -> str:
    """Return npm's environment/config key for one exact registry URL."""
    parsed = urlparse(registry_url)
    path = parsed.path or "/"
    if not path.endswith("/"):
        path += "/"
    return f"//{parsed.netloc}{path}:_authToken"


def native_root_url(service_url: str, path: str) -> str:
    """Return the native root of *path* below one service URL, with a trailing slash."""
    return f"{native_base_url(service_url)}/{path}/"


class CanonicalRouter:
    """Current production routing for package repositories and private mirrors."""

    @staticmethod
    def pypi_index_url(download_url: str, path: str) -> str:
        return f"{native_root_url(download_url, path)}simple/"

    @staticmethod
    def pypi_upload_url(upload_url: str, path: str) -> str:
        return native_root_url(upload_url, path)

    @staticmethod
    def npm_registry_url(download_url: str, path: str) -> str:
        return native_root_url(download_url, path)

    @staticmethod
    def npm_upload_registry_url(upload_url: str, path: str) -> str:
        return native_root_url(upload_url, path)

    @staticmethod
    def maven_repo_url(download_url: str, path: str) -> str:
        return native_root_url(download_url, path)

    @staticmethod
    def maven_upload_url(upload_url: str, path: str) -> str:
        return native_root_url(upload_url, path)
