"""Ephemeral, read-only Docker credential helper for Ravenstash OCI.

It follows the Docker credential-helper protocol: Docker runs
``docker-credential-rvs <action>`` with the request on stdin and reads the
result, or an error message, from stdout; a failure exits with status 1. For any
registry other than the one the current ``rvs`` command brokers, ``get``
answers the protocol's not-found message, so Docker continues without
credentials instead of failing.
"""

import json
import os
import sys
from pathlib import Path

from .registry import normalized_registry_host


# Docker recognizes this exact message as "no credentials for this server".
CREDENTIALS_NOT_FOUND = "credentials not found in native keychain"
BROKER_UNAVAILABLE = "Ravenstash OCI credential broker is unavailable"
BROKER_UNREADABLE = "Ravenstash OCI credential broker could not be read"
BROKER_UNSAFE = "Ravenstash OCI credential broker permissions are unsafe"
BROKER_INVALID = "Ravenstash OCI credential broker is invalid"
READ_ONLY = "the ephemeral Ravenstash helper is read-only"
UNSUPPORTED = "unsupported credential-helper operation"


class HelperError(Exception):
    """A protocol failure reported to Docker with exit status 1."""


def _read_broker(path: Path) -> object:
    try:
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise HelperError(BROKER_UNSAFE)
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        # The broker is removed when the rvs command that created it exits.
        raise HelperError(BROKER_UNAVAILABLE) from exc
    except OSError as exc:
        raise HelperError(BROKER_UNREADABLE) from exc
    except ValueError as exc:
        raise HelperError(BROKER_INVALID) from exc


def _text(payload: dict[object, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise HelperError(BROKER_INVALID)
    return value


def _broker() -> tuple[str, str, str]:
    """Return the brokered registry host, username, and secret."""
    path_value = os.environ.get("RVS_OCI_CREDENTIAL_FILE")
    if not path_value:
        raise HelperError(BROKER_UNAVAILABLE)
    payload = _read_broker(Path(path_value))
    if not isinstance(payload, dict):
        raise HelperError(BROKER_INVALID)
    server, username, secret = (_text(payload, key) for key in ("server", "username", "secret"))
    try:
        host = normalized_registry_host(server)
    except ValueError as exc:
        raise HelperError(BROKER_INVALID) from exc
    if not host:
        raise HelperError(BROKER_INVALID)
    return host, username, secret


def _requested_host(request: str) -> str:
    try:
        return normalized_registry_host(request)
    except ValueError:
        return ""


def main() -> None:
    operation = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if operation in {"store", "erase"}:
            raise HelperError(READ_ONLY)
        if operation not in {"get", "list"}:
            raise HelperError(UNSUPPORTED)
        host, username, secret = _broker()
        if operation == "list":
            print(json.dumps({host: username}))
            return
        if _requested_host(sys.stdin.read()) != host:
            raise HelperError(CREDENTIALS_NOT_FOUND)
        print(json.dumps({"Username": username, "Secret": secret}, separators=(",", ":")))
    except HelperError as exc:
        print(exc)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
