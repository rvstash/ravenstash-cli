"""Passphrase-encrypted local credential vault with a session-memory agent."""

import base64
import getpass
import hashlib
import json
import os
import socket
import stat
import struct
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


_FORMAT_VERSION = 1
_AAD = b"ravenstash-rvs-credential-vault-v1"
_KEY_BYTES = 32
_SALT_BYTES = 16
_NONCE_BYTES = 12
_ARGON2_ITERATIONS = 3
_ARGON2_LANES = 4
_ARGON2_MEMORY_KIB = 64 * 1024
_AGENT_IDLE_SECONDS = 8 * 60 * 60
_AGENT_START_TIMEOUT_SECONDS = 5.0
_MAX_REQUEST_BYTES = 1024 * 1024
_UNIX_SOCKET_PATH_MAX_BYTES = 103
MIN_PASSPHRASE_LENGTH = 8
RECOMMENDED_PASSPHRASE_LENGTH = 12


class VaultError(RuntimeError):
    """The encrypted credential vault could not complete an operation."""


class VaultLockedError(VaultError):
    """The vault needs an interactive unlock."""


def path() -> Path:
    from .. import config as cfg_mod

    return cfg_mod.CONFIG_DIR / "credentials.vault"


def exists() -> bool:
    return path().is_file()


def agent_running() -> bool:
    try:
        return _request({"operation": "ping"}).get("ok") is True
    except VaultError:
        return False


def initialize(passphrase: str) -> None:
    if os.name == "nt":
        raise VaultError("Use Windows Credential Manager through the OS keyring on Windows.")
    if exists():
        raise VaultError(f"Encrypted credential vault already exists at {path()}.")
    _validate_passphrase(passphrase)
    salt = os.urandom(_SALT_BYTES)
    key = _derive_key(passphrase, salt)
    _write_encrypted({}, key, salt)
    _start_agent(key)


def unlock(passphrase: str) -> None:
    if os.name == "nt":
        raise VaultError("Use Windows Credential Manager through the OS keyring on Windows.")
    payload = _read_envelope()
    salt = _decode(payload, "salt", _SALT_BYTES)
    key = _derive_key(passphrase, salt, payload)
    _decrypt(payload, key)
    _start_agent(key)


def unlock_interactive() -> None:
    if not _interactive_terminal_available():
        raise VaultLockedError(
            "The encrypted Ravenstash vault is locked. Run `rvs auth storage unlock` "
            "interactively or provide RVS_TOKEN."
        )
    try:
        passphrase = getpass.getpass("Ravenstash vault passphrase: ")
    except (EOFError, KeyboardInterrupt) as exc:
        raise VaultLockedError("Vault unlock was cancelled.") from exc
    unlock(passphrase)


def lock() -> bool:
    try:
        return _request({"operation": "lock"}).get("ok") is True
    except VaultError:
        return False


def get(account: str) -> str | None:
    response = _request_with_unlock({"operation": "get", "account": account})
    value = response.get("secret")
    if value is not None and not isinstance(value, str):
        raise VaultError("Credential agent returned an invalid secret value.")
    return value


def set(account: str, secret: str) -> None:
    _request_with_unlock({"operation": "set", "account": account, "secret": secret})


def delete(account: str) -> None:
    _request_with_unlock({"operation": "delete", "account": account})


def change_passphrase(old_passphrase: str, new_passphrase: str) -> None:
    _validate_passphrase(new_passphrase)
    payload = _read_envelope()
    old_salt = _decode(payload, "salt", _SALT_BYTES)
    old_key = _derive_key(old_passphrase, old_salt, payload)
    credentials = _decrypt(payload, old_key)
    new_salt = os.urandom(_SALT_BYTES)
    new_key = _derive_key(new_passphrase, new_salt)
    _write_encrypted(credentials, new_key, new_salt)
    lock()
    _start_agent(new_key)


def _interactive_terminal_available() -> bool:
    return bool(getattr(sys.stdin, "isatty", lambda: False)())


def _validate_passphrase(passphrase: str) -> None:
    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise VaultError(
            f"Vault passphrase must contain at least {MIN_PASSPHRASE_LENGTH} characters."
        )


def _derive_key(passphrase: str, salt: bytes, payload: dict[str, Any] | None = None) -> bytes:
    from cryptography.hazmat.primitives.kdf.argon2 import (  # pyright: ignore[reportMissingImports]
        Argon2id,
    )

    kdf_payload = payload.get("kdf", {}) if payload is not None else {}
    if payload is not None and kdf_payload.get("name") != "argon2id":
        raise VaultError("Unsupported vault key-derivation algorithm.")
    try:
        iterations = int(kdf_payload.get("iterations", _ARGON2_ITERATIONS))
        lanes = int(kdf_payload.get("lanes", _ARGON2_LANES))
        memory_kib = int(kdf_payload.get("memory_kib", _ARGON2_MEMORY_KIB))
    except (TypeError, ValueError) as exc:
        raise VaultError("Vault key-derivation parameters are invalid.") from exc
    if not (1 <= iterations <= 10 and 1 <= lanes <= 16 and 8 * 1024 <= memory_kib <= 1024 * 1024):
        raise VaultError("Vault key-derivation parameters are outside supported bounds.")
    return Argon2id(
        salt=salt,
        length=_KEY_BYTES,
        iterations=iterations,
        lanes=lanes,
        memory_cost=memory_kib,
    ).derive(passphrase.encode("utf-8"))


def _decode(payload: dict[str, Any], field: str, expected_length: int | None = None) -> bytes:
    value = payload.get(field)
    if not isinstance(value, str):
        raise VaultError(f"Vault field '{field}' is missing or invalid.")
    try:
        decoded = base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise VaultError(f"Vault field '{field}' is not valid base64.") from exc
    if expected_length is not None and len(decoded) != expected_length:
        raise VaultError(f"Vault field '{field}' has an invalid length.")
    return decoded


def _validate_file(file_path: Path) -> None:
    try:
        metadata = file_path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise VaultError(f"Vault {file_path} must be a regular file.")
    getuid = getattr(os, "getuid", None)
    if getuid is not None and metadata.st_uid != getuid():
        raise VaultError(f"Vault {file_path} is not owned by the current user.")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise VaultError(f"Vault {file_path} has unsafe permissions; expected mode 0600.")


def _read_envelope() -> dict[str, Any]:
    file_path = path()
    _validate_file(file_path)
    if not file_path.exists():
        raise VaultError("Encrypted Ravenstash vault is not initialized.")
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VaultError(f"Could not read encrypted credential vault: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("version") != _FORMAT_VERSION:
        raise VaultError("Unsupported encrypted credential-vault format.")
    return payload


def _decrypt(payload: dict[str, Any], key: bytes) -> dict[str, str]:
    from cryptography.exceptions import InvalidTag  # pyright: ignore[reportMissingImports]
    from cryptography.hazmat.primitives.ciphers.aead import (  # pyright: ignore[reportMissingImports]
        AESGCM,
    )

    if payload.get("cipher") != "aes-256-gcm":
        raise VaultError("Unsupported vault encryption algorithm.")
    nonce = _decode(payload, "nonce", _NONCE_BYTES)
    ciphertext = _decode(payload, "ciphertext")
    try:
        cleartext = AESGCM(key).decrypt(nonce, ciphertext, _AAD)
        decoded = json.loads(cleartext)
    except InvalidTag as exc:
        raise VaultError("Incorrect vault passphrase or damaged vault.") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VaultError("Encrypted vault payload is invalid.") from exc
    if not isinstance(decoded, dict) or not all(
        isinstance(account, str) and isinstance(secret, str) for account, secret in decoded.items()
    ):
        raise VaultError("Encrypted vault contains invalid credential entries.")
    return decoded


def _write_encrypted(credentials: dict[str, str], key: bytes, salt: bytes) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import (  # pyright: ignore[reportMissingImports]
        AESGCM,
    )

    file_path = path()
    parent = file_path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent.chmod(0o700)
    _validate_file(file_path)
    nonce = os.urandom(_NONCE_BYTES)
    cleartext = json.dumps(credentials, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, cleartext, _AAD)
    payload = {
        "version": _FORMAT_VERSION,
        "kdf": {
            "name": "argon2id",
            "iterations": _ARGON2_ITERATIONS,
            "lanes": _ARGON2_LANES,
            "memory_kib": _ARGON2_MEMORY_KIB,
        },
        "cipher": "aes-256-gcm",
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=parent,
            prefix=".credentials.vault.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            if hasattr(os, "fchmod"):
                os.fchmod(temporary.fileno(), 0o600)
            json.dump(payload, temporary, sort_keys=True, separators=(",", ":"))
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, file_path)
        file_path.chmod(0o600)
    except OSError as exc:
        raise VaultError(f"Could not write encrypted credential vault: {exc}") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _runtime_directory() -> Path:
    configured = os.environ.get("XDG_RUNTIME_DIR")
    candidates = [Path(configured)] if configured and Path(configured).is_absolute() else []
    getuid = getattr(os, "getuid", None)
    if getuid is not None:
        uid = getuid()
        candidates.append(Path("/run/user") / str(uid))
        candidates.append(Path("/tmp") / f"rvs-{uid}")
    candidates.append(path().parent / "run")
    for candidate in candidates:
        if not _socket_path_within_limit(candidate):
            continue
        try:
            candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
            metadata = candidate.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            continue
        if getuid is not None and metadata.st_uid != getuid():
            continue
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            continue
        return candidate
    raise VaultError("No private runtime directory is available for the vault agent.")


def _socket_path_within_limit(runtime_directory: Path) -> bool:
    placeholder = runtime_directory / "rvs" / f"vault-{'0' * 16}.sock"
    return len(os.fsencode(placeholder)) <= _UNIX_SOCKET_PATH_MAX_BYTES


def _socket_path() -> Path:
    identity = hashlib.sha256(str(path().parent.resolve()).encode("utf-8")).hexdigest()[:16]
    agent_dir = _runtime_directory() / "rvs"
    agent_dir.mkdir(mode=0o700, exist_ok=True)
    agent_dir.chmod(0o700)
    return agent_dir / f"vault-{identity}.sock"


def _request_with_unlock(request: dict[str, Any]) -> dict[str, Any]:
    try:
        return _request(request)
    except VaultLockedError:
        unlock_interactive()
        return _request(request)


def _request(request: dict[str, Any]) -> dict[str, Any]:
    socket_path = _socket_path()
    unix_family = getattr(socket, "AF_UNIX", None)
    if unix_family is None:
        raise VaultLockedError("The encrypted vault requires Unix-domain sockets.")
    client = socket.socket(unix_family, socket.SOCK_STREAM)
    client.settimeout(5.0)
    try:
        client.connect(str(socket_path))
        encoded = json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n"
        client.sendall(encoded)
        response_bytes = _read_line(client)
    except OSError as exc:
        raise VaultLockedError("Encrypted Ravenstash vault is locked.") from exc
    finally:
        client.close()
    try:
        response = json.loads(response_bytes)
    except json.JSONDecodeError as exc:
        raise VaultError("Credential agent returned an invalid response.") from exc
    if not isinstance(response, dict):
        raise VaultError("Credential agent returned an invalid response.")
    if response.get("ok") is not True:
        raise VaultError(str(response.get("error") or "Credential agent request failed."))
    return response


def _read_line(connection: socket.socket) -> bytes:
    data = bytearray()
    while len(data) <= _MAX_REQUEST_BYTES:
        chunk = connection.recv(min(65536, _MAX_REQUEST_BYTES + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        newline = data.find(b"\n")
        if newline >= 0:
            return bytes(data[:newline])
    raise VaultError("Credential agent message exceeded its allowed size or was incomplete.")


def _start_agent(key: bytes) -> None:
    if agent_running():
        return
    socket_path = _socket_path()
    try:
        socket_path.unlink(missing_ok=True)
    except OSError as exc:
        raise VaultError(f"Could not clear stale vault-agent socket: {exc}") from exc
    fork = getattr(os, "fork", None)
    setsid = getattr(os, "setsid", None)
    if fork is None or setsid is None:
        raise VaultError("The encrypted vault agent requires a POSIX process environment.")
    try:
        first_pid = fork()
    except (AttributeError, OSError) as exc:
        raise VaultError("The encrypted vault agent requires a POSIX process environment.") from exc
    if first_pid == 0:
        try:
            setsid()
            second_pid = fork()
            if second_pid > 0:
                os._exit(0)
            with (
                open(os.devnull, "rb", buffering=0) as null_in,
                open(os.devnull, "ab", buffering=0) as null_out,
            ):
                os.dup2(null_in.fileno(), 0)
                os.dup2(null_out.fileno(), 1)
                os.dup2(null_out.fileno(), 2)
            _serve(key, socket_path)
        finally:
            os._exit(0)
    os.waitpid(first_pid, 0)
    deadline = time.monotonic() + _AGENT_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if agent_running():
            return
        time.sleep(0.05)
    raise VaultError("Encrypted vault agent did not start.")


def _serve(key: bytes, socket_path: Path) -> None:
    unix_family = getattr(socket, "AF_UNIX", None)
    if unix_family is None:
        raise VaultError("The encrypted vault agent requires Unix-domain sockets.")
    server = socket.socket(unix_family, socket.SOCK_STREAM)
    try:
        server.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        server.listen(8)
        server.settimeout(30.0)
        last_activity = time.monotonic()
        should_stop = False
        while not should_stop and time.monotonic() - last_activity < _AGENT_IDLE_SECONDS:
            try:
                connection, _ = server.accept()
            except TimeoutError:
                continue
            with connection:
                if not _peer_is_current_user(connection):
                    continue
                try:
                    request = json.loads(_read_line(connection))
                    response, should_stop = _handle_request(request, key)
                except Exception as exc:
                    response = {"ok": False, "error": str(exc)}
                connection.sendall(
                    json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n"
                )
                last_activity = time.monotonic()
    finally:
        server.close()
        socket_path.unlink(missing_ok=True)


def _peer_is_current_user(connection: socket.socket) -> bool:
    peer_credentials_option = getattr(socket, "SO_PEERCRED", None)
    getuid = getattr(os, "getuid", None)
    if peer_credentials_option is None or getuid is None:
        return True
    credentials = connection.getsockopt(socket.SOL_SOCKET, peer_credentials_option, 12)
    _pid, uid, _gid = struct.unpack("3i", credentials)
    return uid == getuid()


def _handle_request(request: Any, key: bytes) -> tuple[dict[str, Any], bool]:
    if not isinstance(request, dict):
        raise VaultError("Credential agent request must be an object.")
    operation = request.get("operation")
    if operation == "ping":
        return {"ok": True}, False
    if operation == "lock":
        return {"ok": True}, True
    account = request.get("account")
    if not isinstance(account, str) or not account:
        raise VaultError("Credential agent account is missing or invalid.")
    payload = _read_envelope()
    credentials = _decrypt(payload, key)
    if operation == "get":
        return {"ok": True, "secret": credentials.get(account)}, False
    if operation == "set":
        secret = request.get("secret")
        if not isinstance(secret, str):
            raise VaultError("Credential agent secret is missing or invalid.")
        credentials[account] = secret
        salt = _decode(payload, "salt", _SALT_BYTES)
        _write_encrypted(credentials, key, salt)
        return {"ok": True}, False
    if operation == "delete":
        if credentials.pop(account, None) is not None:
            salt = _decode(payload, "salt", _SALT_BYTES)
            _write_encrypted(credentials, key, salt)
        return {"ok": True}, False
    raise VaultError("Credential agent operation is unsupported.")
