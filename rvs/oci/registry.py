"""Canonical registry authority shared by OCI routing and credential lookup."""

from urllib.parse import urlsplit


def normalized_registry_host(value: str) -> str:
    candidate = value.strip()
    parsed = urlsplit(candidate if "://" in candidate else f"https://{candidate}")
    if not parsed.hostname:
        return ""
    host = parsed.hostname.rstrip(".").lower()
    if not host:
        return ""
    if ":" in host:
        host = f"[{host}]"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return host
