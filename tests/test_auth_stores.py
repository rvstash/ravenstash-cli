import base64
import os
import subprocess
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from rvs.auth import plaintext_store, stores, vault


if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.skipif(os.name == "nt", reason="pass is POSIX-only")
def test_pass_status_requires_initialized_store(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(stores.shutil, "which", lambda command: "/usr/bin/pass")
    monkeypatch.setattr(stores, "_password_store_dir", lambda: tmp_path)

    status = stores.pass_status()

    assert status.available is False
    assert "not initialized" in status.detail


@pytest.mark.skipif(os.name == "nt", reason="pass is POSIX-only")
def test_pass_status_accepts_initialized_store(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / ".gpg-id").write_text("developer@example.test\n", encoding="utf-8")
    monkeypatch.setattr(stores.shutil, "which", lambda command: "/usr/bin/pass")
    monkeypatch.setattr(stores, "_password_store_dir", lambda: tmp_path)

    status = stores.pass_status()

    assert status.available is True
    assert status.backend == "/usr/bin/pass"


def test_pass_set_sends_secret_only_over_stdin(monkeypatch) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(stores.subprocess, "run", fake_run)

    stores.pass_set("profile/with/slashes", "secret-token")

    command, kwargs = calls[0]
    assert command[:4] == ["pass", "insert", "--multiline", "--force"]
    assert "profile/with/slashes" not in command[-1]
    assert "secret-token" not in command
    assert kwargs["input"] == "secret-token"


def test_plaintext_store_requires_private_file_and_round_trips(monkeypatch, tmp_path: Path) -> None:
    from rvs import config as cfg_mod

    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", tmp_path / ".rvs")
    plaintext_store.initialize()
    plaintext_store.set("default", "plain-secret")

    assert plaintext_store.get("default") == "plain-secret"
    if os.name != "nt":
        assert plaintext_store.path().stat().st_mode & 0o777 == 0o600
    assert "plain-secret" in plaintext_store.path().read_text(encoding="utf-8")

    if os.name != "nt":
        plaintext_store.path().chmod(0o644)
        with pytest.raises(plaintext_store.PlaintextStoreError, match="unsafe permissions"):
            plaintext_store.get("default")


@pytest.mark.skipif(os.name == "nt", reason="session-agent vault is POSIX-only")
def test_encrypted_vault_round_trip_and_lock(monkeypatch, tmp_path: Path) -> None:
    from rvs import config as cfg_mod

    config_dir = tmp_path / ".rvs"
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(mode=0o700)
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setattr(vault, "_ARGON2_MEMORY_KIB", 8 * 1024)

    vault.initialize("correct horse battery staple")
    try:
        vault.set("default", "encrypted-secret")
        assert vault.get("default") == "encrypted-secret"
        assert "encrypted-secret" not in vault.path().read_text(encoding="utf-8")
        assert vault.path().stat().st_mode & 0o777 == 0o600
        assert vault.lock() is True
        monkeypatch.setattr(vault, "_interactive_terminal_available", lambda: False)
        with pytest.raises(vault.VaultLockedError, match="unlock"):
            vault.get("default")
        with pytest.raises(vault.VaultError, match="Incorrect vault passphrase"):
            vault.unlock("incorrect passphrase here")
        vault.unlock("correct horse battery staple")
        assert vault.get("default") == "encrypted-secret"
    finally:
        vault.lock()


def test_vault_accepts_eight_characters_and_rejects_shorter_passphrases() -> None:
    vault._validate_passphrase("12345678")

    with pytest.raises(vault.VaultError, match="at least 8 characters"):
        vault._validate_passphrase("1234567")


def test_vault_rejects_runtime_directory_with_oversized_socket_path(tmp_path: Path) -> None:
    runtime_dir = tmp_path / ("nested-" * 20)

    assert vault._socket_path_within_limit(runtime_dir) is False


class _MemoryKeyring:
    """An in-memory stand-in for the ``keyring`` package and its backend."""

    def __init__(self, priority: float = 5) -> None:
        self.priority = priority
        self.secrets: dict[tuple[str, str], str] = {}
        self.deleted: list[tuple[str, str]] = []

    def get_password(self, service: str, account: str) -> str | None:
        return self.secrets.get((service, account))

    def set_password(self, service: str, account: str, secret: str) -> None:
        self.secrets[service, account] = secret

    def delete_password(self, service: str, account: str) -> None:
        self.deleted.append((service, account))
        del self.secrets[service, account]


def _use_keyring(monkeypatch, backend: Any) -> None:
    module = SimpleNamespace(
        get_keyring=lambda: backend,
        get_password=backend.get_password,
        set_password=backend.set_password,
        delete_password=backend.delete_password,
    )
    monkeypatch.setitem(sys.modules, "keyring", module)


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> Any:
    return subprocess.CompletedProcess(["pass"], returncode, stdout, stderr)


def test_keyring_status_reports_recommended_backend(monkeypatch) -> None:
    _use_keyring(monkeypatch, _MemoryKeyring(priority=5))

    status = stores.system_keyring_status()

    assert status.available is True
    assert status.backend == f"{__name__}._MemoryKeyring"
    assert status.guidance == ""


def test_keyring_status_rejects_low_priority_backend(monkeypatch) -> None:
    _use_keyring(monkeypatch, _MemoryKeyring(priority=0))

    status = stores.system_keyring_status()

    assert status.available is False
    assert status.backend == "none"
    assert status.detail == "no recommended keyring backend was detected"
    assert "Start or unlock your desktop credential service" in status.guidance


def test_keyring_round_trip_and_delete_of_missing_entry(monkeypatch) -> None:
    backend = _MemoryKeyring()
    _use_keyring(monkeypatch, backend)

    stores.system_set("ravenstash", "default", "keyring-secret")
    assert stores.system_get("ravenstash", "default") == "keyring-secret"
    stores.system_delete("ravenstash", "default")
    stores.system_delete("ravenstash", "default")

    assert stores.system_get("ravenstash", "default") is None
    assert backend.deleted == [("ravenstash", "default")]


def test_keyring_failure_is_reported_without_the_secret(monkeypatch) -> None:
    backend = _MemoryKeyring()

    def locked(*_args: Any) -> None:
        raise RuntimeError("collection is locked")

    backend.set_password = locked
    _use_keyring(monkeypatch, backend)

    with pytest.raises(stores.StoreError) as raised:
        stores.system_set("ravenstash", "default", "keyring-secret")

    assert str(raised.value) == "OS keyring write failed: collection is locked"
    assert "keyring-secret" not in str(raised.value)


def test_password_store_dir_honors_configured_location(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PASSWORD_STORE_DIR", str(tmp_path / "custom-store"))
    assert stores._password_store_dir() == tmp_path / "custom-store"

    monkeypatch.delenv("PASSWORD_STORE_DIR")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert stores._password_store_dir() == tmp_path / ".password-store"


@pytest.mark.skipif(os.name == "nt", reason="pass is POSIX-only")
def test_pass_status_requires_pass_on_path(monkeypatch) -> None:
    monkeypatch.setattr(stores.shutil, "which", lambda command: None)

    status = stores.pass_status()

    assert status.available is False
    assert status.detail == "The pass executable is not installed or is not on PATH."


def test_pass_entry_hides_account_name_in_a_path_safe_encoding() -> None:
    entry = stores._pass_entry("team/ci")

    assert entry == "ravenstash/rvs/dGVhbS9jaQ"
    assert base64.urlsafe_b64decode(entry.rpartition("/")[2] + "==") == b"team/ci"


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (_completed(stdout="pass-secret\n"), "pass-secret"),
        (_completed(1, stderr="Error: entry is not in the password store.\n"), None),
    ],
)
def test_pass_get_returns_secret_or_none_for_missing_entry(
    monkeypatch, result: Any, expected: str | None
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        stores.subprocess, "run", lambda command, **_kwargs: calls.append(command) or result
    )

    assert stores.pass_get("default") == expected
    assert calls == [["pass", "show", stores._pass_entry("default")]]


def test_pass_errors_surface_pass_diagnostics(monkeypatch) -> None:
    monkeypatch.setattr(
        stores.subprocess,
        "run",
        lambda command, **_kwargs: _completed(2, stderr="gpg: decryption failed\n"),
    )

    with pytest.raises(stores.StoreError, match=r"^gpg: decryption failed$"):
        stores.pass_get("default")
    with pytest.raises(stores.StoreError, match=r"^gpg: decryption failed$"):
        stores.pass_delete("default")


def test_pass_delete_ignores_missing_entry(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        stores.subprocess,
        "run",
        lambda command, **_kwargs: (
            calls.append(command) or _completed(1, stderr="Error: x is not in the password store.")
        ),
    )

    stores.pass_delete("default")

    assert calls == [["pass", "rm", "--force", stores._pass_entry("default")]]


def test_pass_that_cannot_run_is_reported_without_the_secret(monkeypatch) -> None:
    def missing(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError("No such file or directory: 'pass'")

    monkeypatch.setattr(stores.subprocess, "run", missing)

    with pytest.raises(stores.StoreError) as raised:
        stores.pass_set("default", "pass-secret")

    assert str(raised.value) == ("pass could not be executed: No such file or directory: 'pass'")
    assert "pass-secret" not in str(raised.value)


@pytest.mark.skipif(os.name == "nt", reason="session-agent vault is POSIX-only")
@pytest.mark.parametrize(
    ("initialized", "unlocked", "available", "detail"),
    [
        (False, False, False, "The encrypted Ravenstash vault is not initialized."),
        (True, False, True, "The encrypted Ravenstash vault is initialized and locked."),
        (True, True, True, "The encrypted Ravenstash vault is initialized and unlocked."),
    ],
)
def test_vault_status_reflects_initialization_and_lock_state(
    monkeypatch, initialized: bool, unlocked: bool, available: bool, detail: str
) -> None:
    monkeypatch.setattr(stores.vault, "exists", lambda: initialized)
    monkeypatch.setattr(stores.vault, "agent_running", lambda: unlocked)

    status = stores.vault_status()

    assert status.available is available
    assert status.detail == detail


def test_vault_adapter_translates_vault_errors(monkeypatch) -> None:
    def locked(*_args: Any) -> None:
        raise vault.VaultLockedError("Encrypted Ravenstash vault is locked.")

    for operation in ("get", "set", "delete"):
        monkeypatch.setattr(stores.vault, operation, locked)

    with pytest.raises(stores.StoreError, match=r"^Encrypted Ravenstash vault is locked\.$"):
        stores.vault_get("default")
    with pytest.raises(stores.StoreError, match=r"^Encrypted Ravenstash vault is locked\.$"):
        stores.vault_set("default", "vault-secret")
    with pytest.raises(stores.StoreError, match=r"^Encrypted Ravenstash vault is locked\.$"):
        stores.vault_delete("default")


def test_plaintext_adapter_reports_status_and_round_trips(monkeypatch, tmp_path: Path) -> None:
    from rvs import config as cfg_mod

    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", tmp_path / ".rvs")
    assert stores.plaintext_status().available is False

    plaintext_store.initialize()
    stores.plaintext_set("default", "plain-secret")

    status = stores.plaintext_status()
    assert status.available is True
    assert status.detail.startswith("WARNING: credentials are stored unencrypted")
    assert stores.plaintext_get("default") == "plain-secret"
    stores.plaintext_delete("default")
    assert stores.plaintext_get("default") is None


def test_plaintext_adapter_translates_store_errors(monkeypatch) -> None:
    def unsafe(*_args: Any) -> None:
        raise plaintext_store.PlaintextStoreError("Credential file has unsafe permissions.")

    for operation in ("get", "set", "delete"):
        monkeypatch.setattr(stores.plaintext_store, operation, unsafe)

    for call in (
        lambda: stores.plaintext_get("default"),
        lambda: stores.plaintext_set("default", "plain-secret"),
        lambda: stores.plaintext_delete("default"),
    ):
        with pytest.raises(stores.StoreError, match=r"^Credential file has unsafe permissions\.$"):
            call()
