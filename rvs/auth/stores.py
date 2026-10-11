"""Secure credential-store adapters for desktop and headless environments."""

import base64
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import plaintext_store, vault


_COMMAND_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class StoreStatus:
    name: str
    available: bool
    backend: str
    detail: str
    guidance: str


class StoreError(RuntimeError):
    """A credential store could not complete a requested operation."""


def system_keyring_status() -> StoreStatus:
    try:
        import keyring

        backend = keyring.get_keyring()
        priority = backend.priority
        backend_name = f"{type(backend).__module__}.{type(backend).__name__}"
        if priority < 1:
            raise RuntimeError("no recommended keyring backend was detected")
    except Exception as exc:
        return StoreStatus(
            name="keyring",
            available=False,
            backend="none",
            detail=str(exc) or type(exc).__name__,
            guidance=(
                "Start or unlock your desktop credential service. Secret Service providers "
                "include GNOME Keyring, KWallet Secret Service, and KeePassXC integration."
            ),
        )
    return StoreStatus(
        name="keyring",
        available=True,
        backend=backend_name,
        detail="A recommended OS keyring backend is available.",
        guidance="",
    )


def system_get(service: str, account: str) -> str | None:
    try:
        import keyring

        return keyring.get_password(service, account)
    except Exception as exc:
        raise StoreError(f"OS keyring read failed: {exc}") from exc


def system_set(service: str, account: str, secret: str) -> None:
    try:
        import keyring

        keyring.set_password(service, account, secret)
    except Exception as exc:
        raise StoreError(f"OS keyring write failed: {exc}") from exc


def system_delete(service: str, account: str) -> None:
    try:
        import keyring

        if keyring.get_password(service, account) is None:
            return
        keyring.delete_password(service, account)
    except Exception as exc:
        raise StoreError(f"OS keyring delete failed: {exc}") from exc


def _password_store_dir() -> Path:
    configured = os.environ.get("PASSWORD_STORE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".password-store"


def pass_status() -> StoreStatus:
    if os.name == "nt":
        return StoreStatus(
            name="pass",
            available=False,
            backend="pass",
            detail="pass is a POSIX credential-store integration.",
            guidance="Use Windows Credential Manager through the OS keyring.",
        )
    executable = shutil.which("pass")
    if executable is None:
        return StoreStatus(
            name="pass",
            available=False,
            backend="pass",
            detail="The pass executable is not installed or is not on PATH.",
            guidance="Install pass and initialize it with `pass init <gpg-id>`.",
        )
    store_dir = _password_store_dir()
    if not store_dir.is_dir() or not (store_dir / ".gpg-id").is_file():
        return StoreStatus(
            name="pass",
            available=False,
            backend=executable,
            detail=f"The password store at {store_dir} is not initialized.",
            guidance="Initialize it with `pass init <gpg-id>`.",
        )
    return StoreStatus(
        name="pass",
        available=True,
        backend=executable,
        detail=f"An initialized pass store is available at {store_dir}.",
        guidance="",
    )


def _pass_entry(account: str) -> str:
    encoded = base64.urlsafe_b64encode(account.encode("utf-8")).decode("ascii").rstrip("=")
    return f"ravenstash/rvs/{encoded}"


def _run_pass(
    arguments: list[str], *, secret: str | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["pass", *arguments],
            input=secret,
            text=True,
            capture_output=True,
            check=False,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StoreError(f"pass could not be executed: {exc}") from exc


def pass_get(account: str) -> str | None:
    result = _run_pass(["show", _pass_entry(account)])
    if result.returncode != 0:
        if "not in the password store" in result.stderr.lower():
            return None
        raise StoreError(result.stderr.strip() or "pass could not read the credential")
    return result.stdout.rstrip("\n")


def pass_set(account: str, secret: str) -> None:
    result = _run_pass(["insert", "--multiline", "--force", _pass_entry(account)], secret=secret)
    if result.returncode != 0:
        raise StoreError(result.stderr.strip() or "pass could not store the credential")


def pass_delete(account: str) -> None:
    result = _run_pass(["rm", "--force", _pass_entry(account)])
    if result.returncode != 0 and "not in the password store" not in result.stderr.lower():
        raise StoreError(result.stderr.strip() or "pass could not delete the credential")


def vault_status() -> StoreStatus:
    if os.name == "nt":
        return StoreStatus(
            name="vault",
            available=False,
            backend="rvs encrypted vault",
            detail="The session-agent vault is unavailable on Windows.",
            guidance="Use Windows Credential Manager through the OS keyring.",
        )
    if not vault.exists():
        return StoreStatus(
            name="vault",
            available=False,
            backend="rvs encrypted vault",
            detail="The encrypted Ravenstash vault is not initialized.",
            guidance="Run `rvs auth storage setup` to initialize it.",
        )
    unlocked = vault.agent_running()
    return StoreStatus(
        name="vault",
        available=True,
        backend="rvs encrypted vault",
        detail=(
            "The encrypted Ravenstash vault is initialized and unlocked."
            if unlocked
            else "The encrypted Ravenstash vault is initialized and locked."
        ),
        guidance=("" if unlocked else "Run `rvs auth storage unlock` interactively."),
    )


def vault_get(account: str) -> str | None:
    try:
        return vault.get(account)
    except vault.VaultError as exc:
        raise StoreError(str(exc)) from exc


def vault_set(account: str, secret: str) -> None:
    try:
        vault.set(account, secret)
    except vault.VaultError as exc:
        raise StoreError(str(exc)) from exc


def vault_delete(account: str) -> None:
    try:
        vault.delete(account)
    except vault.VaultError as exc:
        raise StoreError(str(exc)) from exc


def plaintext_status() -> StoreStatus:
    if not plaintext_store.exists():
        return StoreStatus(
            name="plaintext",
            available=False,
            backend="rvs plaintext file",
            detail="The explicitly insecure plaintext credential file is not initialized.",
            guidance=("Run `rvs auth storage setup` only if encrypted storage cannot be used."),
        )
    return StoreStatus(
        name="plaintext",
        available=True,
        backend="rvs plaintext file",
        detail="WARNING: credentials are stored unencrypted in a user-readable file.",
        guidance="Prefer keyring, pass, or the encrypted Ravenstash vault.",
    )


def plaintext_get(account: str) -> str | None:
    try:
        return plaintext_store.get(account)
    except plaintext_store.PlaintextStoreError as exc:
        raise StoreError(str(exc)) from exc


def plaintext_set(account: str, secret: str) -> None:
    try:
        plaintext_store.set(account, secret)
    except plaintext_store.PlaintextStoreError as exc:
        raise StoreError(str(exc)) from exc


def plaintext_delete(account: str) -> None:
    try:
        plaintext_store.delete(account)
    except plaintext_store.PlaintextStoreError as exc:
        raise StoreError(str(exc)) from exc
