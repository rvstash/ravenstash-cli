"""Explicitly insecure credential storage for constrained local environments."""

import json
import os
import stat
import tempfile
from pathlib import Path


_FORMAT_VERSION = 1


class PlaintextStoreError(RuntimeError):
    """The plaintext credential file could not be used safely."""


def path() -> Path:
    from .. import config as cfg_mod

    return cfg_mod.CONFIG_DIR / "credentials.plaintext.json"


def exists() -> bool:
    return path().is_file()


def initialize() -> None:
    if exists():
        _read()
        return
    _write({})


def get(account: str) -> str | None:
    return _read().get(account)


def set(account: str, secret: str) -> None:
    credentials = _read()
    credentials[account] = secret
    _write(credentials)


def delete(account: str) -> None:
    credentials = _read()
    if credentials.pop(account, None) is not None:
        _write(credentials)


def _validate_file(file_path: Path) -> None:
    try:
        metadata = file_path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise PlaintextStoreError(f"Credential file {file_path} must be a regular file.")
    getuid = getattr(os, "getuid", None)
    if getuid is not None and metadata.st_uid != getuid():
        raise PlaintextStoreError(f"Credential file {file_path} is not owned by the current user.")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PlaintextStoreError(
            f"Credential file {file_path} has unsafe permissions; expected mode 0600."
        )


def _read() -> dict[str, str]:
    file_path = path()
    _validate_file(file_path)
    if not file_path.exists():
        return {}
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlaintextStoreError(f"Could not read plaintext credential file: {exc}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("version") != _FORMAT_VERSION
        or not isinstance(payload.get("credentials"), dict)
    ):
        raise PlaintextStoreError("Unsupported plaintext credential-file format.")
    credentials = payload["credentials"]
    if not all(
        isinstance(key, str) and isinstance(value, str) for key, value in credentials.items()
    ):
        raise PlaintextStoreError("Plaintext credential file contains invalid entries.")
    return dict(credentials)


def _write(credentials: dict[str, str]) -> None:
    file_path = path()
    parent = file_path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent.chmod(0o700)
    _validate_file(file_path)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=parent,
            prefix=".credentials.plaintext.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            if hasattr(os, "fchmod"):
                os.fchmod(temporary.fileno(), 0o600)
            json.dump(
                {"version": _FORMAT_VERSION, "credentials": credentials},
                temporary,
                sort_keys=True,
                separators=(",", ":"),
            )
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, file_path)
        file_path.chmod(0o600)
    except OSError as exc:
        raise PlaintextStoreError(f"Could not write plaintext credential file: {exc}") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
