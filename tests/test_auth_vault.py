import base64
import json
import socket
import sys
from typing import TYPE_CHECKING, Any

import pytest
from rvs import config as cfg_mod
from rvs.auth import vault


if TYPE_CHECKING:
    from pathlib import Path


# The encrypted vault and its session agent need POSIX sockets and file modes,
# and its cryptography dependency is not installed on Windows.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the encrypted vault is not available on Windows"
)

# A fixed key exercises the encrypted envelope without the deliberately slow
# passphrase derivation or a background agent process.
KEY = bytes(range(32))
SALT = bytes(16)


@pytest.fixture
def vault_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", tmp_path / ".rvs")
    return vault.path()


def _envelope(**overrides: Any) -> dict[str, Any]:
    payload = {
        "version": 1,
        "kdf": {"name": "argon2id", "iterations": 3, "lanes": 4, "memory_kib": 65536},
        "cipher": "aes-256-gcm",
        "salt": base64.b64encode(SALT).decode("ascii"),
    }
    payload.update(overrides)
    return payload


def test_vault_path_lives_in_the_rvs_config_directory(vault_file: Path, tmp_path: Path) -> None:
    assert vault_file == tmp_path / ".rvs" / "credentials.vault"
    assert vault.exists() is False


def test_initialize_refuses_to_overwrite_an_existing_vault(vault_file: Path) -> None:
    vault_file.parent.mkdir()
    vault_file.write_text("{}", encoding="utf-8")

    with pytest.raises(vault.VaultError, match=r"^Encrypted credential vault already exists at "):
        vault.initialize("correct horse battery staple")

    assert vault_file.read_text(encoding="utf-8") == "{}"


def test_encrypted_envelope_round_trips_without_plaintext_on_disk(vault_file: Path) -> None:
    vault._write_encrypted({"default": "vault-secret"}, KEY, SALT)

    payload = vault._read_envelope()

    assert "vault-secret" not in vault_file.read_text(encoding="utf-8")
    assert payload["kdf"]["name"] == "argon2id"
    assert vault._decode(payload, "salt", 16) == SALT
    assert vault._decrypt(payload, KEY) == {"default": "vault-secret"}


def test_decrypt_with_the_wrong_key_reports_incorrect_passphrase(vault_file: Path) -> None:
    vault._write_encrypted({"default": "vault-secret"}, KEY, SALT)

    with pytest.raises(vault.VaultError) as raised:
        vault._decrypt(vault._read_envelope(), bytes(32))

    assert str(raised.value) == "Incorrect vault passphrase or damaged vault."


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        (None, "Encrypted Ravenstash vault is not initialized."),
        ('{"version": 2}', "Unsupported encrypted credential-vault format."),
        ("[]", "Unsupported encrypted credential-vault format."),
    ],
)
def test_read_envelope_rejects_missing_or_unsupported_vault(
    vault_file: Path, contents: str | None, message: str
) -> None:
    if contents is not None:
        vault_file.parent.mkdir()
        vault_file.write_text(contents, encoding="utf-8")
        vault_file.chmod(0o600)

    with pytest.raises(vault.VaultError) as raised:
        vault._read_envelope()

    assert str(raised.value) == message


def test_read_envelope_rejects_invalid_json(vault_file: Path) -> None:
    vault_file.parent.mkdir()
    vault_file.write_text("{not json", encoding="utf-8")
    vault_file.chmod(0o600)

    with pytest.raises(vault.VaultError, match=r"^Could not read encrypted credential vault: "):
        vault._read_envelope()


def test_vault_file_must_not_be_readable_by_others(vault_file: Path) -> None:
    vault_file.parent.mkdir()
    vault_file.write_text("{}", encoding="utf-8")
    vault_file.chmod(0o644)

    with pytest.raises(vault.VaultError) as raised:
        vault._read_envelope()

    assert str(raised.value) == f"Vault {vault_file} has unsafe permissions; expected mode 0600."


def test_vault_file_must_not_be_a_symlink(vault_file: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.vault"
    target.write_text("{}", encoding="utf-8")
    vault_file.parent.mkdir()
    vault_file.symlink_to(target)

    with pytest.raises(vault.VaultError) as raised:
        vault._read_envelope()

    assert str(raised.value) == f"Vault {vault_file} must be a regular file."


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "Vault field 'salt' is missing or invalid."),
        ({"salt": "not base64!"}, "Vault field 'salt' is not valid base64."),
        (
            {"salt": base64.b64encode(b"short").decode()},
            "Vault field 'salt' has an invalid length.",
        ),
    ],
)
def test_decode_rejects_malformed_fields(payload: dict[str, Any], message: str) -> None:
    with pytest.raises(vault.VaultError) as raised:
        vault._decode(payload, "salt", 16)

    assert str(raised.value) == message


@pytest.mark.parametrize(
    ("kdf", "message"),
    [
        ({"name": "scrypt"}, "Unsupported vault key-derivation algorithm."),
        (
            {"name": "argon2id", "iterations": "many"},
            "Vault key-derivation parameters are invalid.",
        ),
        (
            {"name": "argon2id", "iterations": 3, "lanes": 4, "memory_kib": 1024},
            "Vault key-derivation parameters are outside supported bounds.",
        ),
    ],
)
def test_key_derivation_rejects_unsupported_parameters(kdf: dict[str, Any], message: str) -> None:
    with pytest.raises(vault.VaultError) as raised:
        vault._derive_key("correct horse battery staple", SALT, _envelope(kdf=kdf))

    assert str(raised.value) == message


def test_decrypt_rejects_unsupported_cipher() -> None:
    with pytest.raises(vault.VaultError) as raised:
        vault._decrypt(_envelope(cipher="chacha20"), KEY)

    assert str(raised.value) == "Unsupported vault encryption algorithm."


def test_agent_requests_manage_credentials_in_the_encrypted_file(vault_file: Path) -> None:
    vault._write_encrypted({}, KEY, SALT)

    assert vault._handle_request({"operation": "ping"}, KEY) == ({"ok": True}, False)
    assert vault._handle_request(
        {"operation": "set", "account": "default", "secret": "vault-secret"}, KEY
    ) == ({"ok": True}, False)
    assert vault._handle_request({"operation": "get", "account": "default"}, KEY) == (
        {"ok": True, "secret": "vault-secret"},
        False,
    )
    assert vault._handle_request({"operation": "delete", "account": "default"}, KEY) == (
        {"ok": True},
        False,
    )
    assert vault._decrypt(vault._read_envelope(), KEY) == {}
    assert vault._handle_request({"operation": "lock"}, KEY) == ({"ok": True}, True)


@pytest.mark.parametrize(
    ("request_payload", "message"),
    [
        (["get"], "Credential agent request must be an object."),
        ({"operation": "get"}, "Credential agent account is missing or invalid."),
        (
            {"operation": "set", "account": "default"},
            "Credential agent secret is missing or invalid.",
        ),
        (
            {"operation": "export", "account": "default"},
            "Credential agent operation is unsupported.",
        ),
    ],
)
def test_agent_rejects_malformed_requests(
    vault_file: Path, request_payload: Any, message: str
) -> None:
    vault._write_encrypted({}, KEY, SALT)

    with pytest.raises(vault.VaultError) as raised:
        vault._handle_request(request_payload, KEY)

    assert str(raised.value) == message


def test_get_set_and_delete_send_agent_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[dict[str, Any]] = []

    def fake_request(request: dict[str, Any]) -> dict[str, Any]:
        requests.append(request)
        return {"ok": True, "secret": "vault-secret"}

    monkeypatch.setattr(vault, "_request", fake_request)

    assert vault.get("default") == "vault-secret"
    vault.set("default", "vault-secret")
    vault.delete("default")

    assert requests == [
        {"operation": "get", "account": "default"},
        {"operation": "set", "account": "default", "secret": "vault-secret"},
        {"operation": "delete", "account": "default"},
    ]


def test_get_rejects_non_string_secret_from_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vault, "_request", lambda request: {"ok": True, "secret": 42})

    with pytest.raises(vault.VaultError) as raised:
        vault.get("default")

    assert str(raised.value) == "Credential agent returned an invalid secret value."


def test_locked_vault_unlocks_interactively_once_then_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses: list[Any] = [
        vault.VaultLockedError("Encrypted Ravenstash vault is locked."),
        {"ok": True, "secret": "vault-secret"},
    ]
    unlocked: list[str] = []

    def fake_request(_request: dict[str, Any]) -> dict[str, Any]:
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(vault, "_request", fake_request)
    monkeypatch.setattr(vault, "_interactive_terminal_available", lambda: True)
    monkeypatch.setattr(vault.getpass, "getpass", lambda prompt: "correct horse battery staple")
    monkeypatch.setattr(vault, "unlock", unlocked.append)

    assert vault.get("default") == "vault-secret"
    assert unlocked == ["correct horse battery staple"]


def test_locked_vault_without_terminal_explains_how_to_unlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(vault, "_interactive_terminal_available", lambda: False)

    with pytest.raises(vault.VaultLockedError) as raised:
        vault.unlock_interactive()

    assert str(raised.value) == (
        "The encrypted Ravenstash vault is locked. Run `rvs auth storage unlock` "
        "interactively or provide RVS_TOKEN."
    )


def test_cancelled_passphrase_prompt_keeps_vault_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    def cancelled(_prompt: str) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(vault, "_interactive_terminal_available", lambda: True)
    monkeypatch.setattr(vault.getpass, "getpass", cancelled)

    with pytest.raises(vault.VaultLockedError, match=r"^Vault unlock was cancelled\.$"):
        vault.unlock_interactive()


def test_agent_status_and_lock_report_false_when_agent_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreachable(_request: dict[str, Any]) -> dict[str, Any]:
        raise vault.VaultLockedError("Encrypted Ravenstash vault is locked.")

    monkeypatch.setattr(vault, "_request", unreachable)

    assert vault.agent_running() is False
    assert vault.lock() is False


def test_request_reports_locked_vault_when_no_agent_listens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(vault, "_socket_path", lambda: tmp_path / "missing.sock")

    with pytest.raises(vault.VaultLockedError) as raised:
        vault._request({"operation": "ping"})

    assert str(raised.value) == "Encrypted Ravenstash vault is locked."


def test_read_line_returns_one_message_and_rejects_incomplete_ones() -> None:
    reader, writer = socket.socketpair()
    with reader, writer:
        writer.sendall(json.dumps({"ok": True}).encode() + b"\nignored")
        assert vault._read_line(reader) == b'{"ok": true}'

    reader, writer = socket.socketpair()
    with reader, writer:
        writer.sendall(b'{"ok": true}')
        writer.shutdown(socket.SHUT_WR)
        with pytest.raises(vault.VaultError) as raised:
            vault._read_line(reader)

    assert str(raised.value) == (
        "Credential agent message exceeded its allowed size or was incomplete."
    )


def test_runtime_directory_prefers_private_xdg_runtime_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime_dir = tmp_path / "run"
    runtime_dir.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setattr(vault, "_socket_path_within_limit", lambda _directory: True)

    assert vault._runtime_directory() == runtime_dir
