"""Docker credential-helper protocol for the ephemeral ``docker-credential-rvs`` broker.

Docker runs ``docker-credential-rvs <action>``, writes the server URL (``get``) or a
JSON payload (``store``/``erase``) to stdin, and reads the result or an error
message from stdout; a failure exits with status 1.
"""

import io
import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING, NamedTuple

import pytest
from rvs.oci import credential_helper
from rvs.oci.credential_helper import (
    BROKER_INVALID,
    BROKER_UNAVAILABLE,
    BROKER_UNREADABLE,
    BROKER_UNSAFE,
    CREDENTIALS_NOT_FOUND,
    READ_ONLY,
    UNSUPPORTED,
)


if TYPE_CHECKING:
    from pathlib import Path


REGISTRY = "registry.example.test"
USERNAME = "robot"
SECRET = "ephemeral-secret"


class HelperResult(NamedTuple):
    exit_code: int | str | None
    stdout: str
    stderr: str


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A private broker file with valid credentials for ``REGISTRY``."""
    path = tmp_path / "broker.json"
    _write_broker(path, {"server": REGISTRY, "username": USERNAME, "secret": SECRET})
    monkeypatch.setenv("RVS_OCI_CREDENTIAL_FILE", str(path))
    return path


def _write_broker(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def _run_helper(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *arguments: str,
    stdin: str = "",
) -> HelperResult:
    monkeypatch.setattr(sys, "argv", ["docker-credential-rvs", *arguments])
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    try:
        credential_helper.main()
    except SystemExit as exc:
        exit_code = exc.code
    else:
        exit_code = 0
    captured = capsys.readouterr()
    return HelperResult(exit_code, captured.out, captured.err)


def _assert_failed(result: HelperResult, message: str) -> None:
    # Docker reads the error message from stdout, not stderr.
    assert result == HelperResult(1, f"{message}\n", "")


# ── Successful protocol actions ───────────────────────────────────────────────


@pytest.mark.usefixtures("broker")
@pytest.mark.parametrize(
    "server_url",
    [REGISTRY, f"{REGISTRY}\n", f"https://{REGISTRY}", f"https://{REGISTRY.upper()}./v2/"],
)
def test_get_returns_compact_credentials_for_the_brokered_registry(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], server_url: str
) -> None:
    result = _run_helper(monkeypatch, capsys, "get", stdin=server_url)

    assert result == HelperResult(0, f'{{"Username":"{USERNAME}","Secret":"{SECRET}"}}\n', "")


@pytest.mark.usefixtures("broker")
def test_list_maps_the_brokered_server_to_its_username(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = _run_helper(monkeypatch, capsys, "list")

    assert result == HelperResult(0, f'{{"{REGISTRY}": "{USERNAME}"}}\n', "")
    assert SECRET not in result.stdout


# ── Refused actions and requests ──────────────────────────────────────────────


@pytest.mark.parametrize("operation", ["store", "erase"])
def test_store_and_erase_are_refused_without_touching_the_broker(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    broker: Path,
    operation: str,
) -> None:
    original = broker.read_bytes()
    request = json.dumps({"ServerURL": REGISTRY, "Username": "other", "Secret": "replacement"})

    result = _run_helper(monkeypatch, capsys, operation, stdin=request)

    _assert_failed(result, READ_ONLY)
    assert broker.read_bytes() == original


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        pytest.param(("store",), READ_ONLY, id="store"),
        pytest.param(("erase",), READ_ONLY, id="erase"),
        pytest.param(("version",), UNSUPPORTED, id="unsupported"),
    ],
)
def test_actions_without_a_broker_are_answered_from_the_action_alone(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: tuple[str, ...],
    message: str,
) -> None:
    monkeypatch.delenv("RVS_OCI_CREDENTIAL_FILE", raising=False)

    result = _run_helper(monkeypatch, capsys, *arguments, stdin=REGISTRY)

    _assert_failed(result, message)


@pytest.mark.usefixtures("broker")
@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param((), id="missing-action"),
        pytest.param(("",), id="empty-action"),
        pytest.param(("GET",), id="wrong-case"),
        pytest.param(("version",), id="version"),
    ],
)
def test_unsupported_actions_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: tuple[str, ...],
) -> None:
    result = _run_helper(monkeypatch, capsys, *arguments, stdin=REGISTRY)

    _assert_failed(result, UNSUPPORTED)


@pytest.mark.usefixtures("broker")
@pytest.mark.parametrize(
    "server_url",
    [
        pytest.param("", id="empty-request"),
        pytest.param("https:///", id="no-host"),
        pytest.param("other.example.test", id="other-host"),
        pytest.param(f"{REGISTRY}:5000", id="other-port"),
        pytest.param(f"{REGISTRY}.other.example.test", id="suffixed-host"),
        pytest.param(f"https://{REGISTRY}@other.example.test", id="userinfo-host"),
        pytest.param("https://[::1", id="unclosed-ipv6-literal"),
        pytest.param(f"{REGISTRY}:99999", id="port-out-of-range"),
    ],
)
def test_get_reports_not_found_for_any_other_registry(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], server_url: str
) -> None:
    # Docker treats this exact message as "no credentials" and continues.
    result = _run_helper(monkeypatch, capsys, "get", stdin=server_url)

    _assert_failed(result, CREDENTIALS_NOT_FOUND)


# ── Broker state ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("configured", [None, ""])
def test_missing_broker_configuration_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    configured: str | None,
) -> None:
    if configured is None:
        monkeypatch.delenv("RVS_OCI_CREDENTIAL_FILE", raising=False)
    else:
        monkeypatch.setenv("RVS_OCI_CREDENTIAL_FILE", configured)

    result = _run_helper(monkeypatch, capsys, "get", stdin=REGISTRY)

    _assert_failed(result, BROKER_UNAVAILABLE)


def test_removed_broker_reports_that_it_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], broker: Path
) -> None:
    # The wrapper deletes the broker when its native command exits.
    broker.unlink()

    result = _run_helper(monkeypatch, capsys, "get", stdin=REGISTRY)

    _assert_failed(result, BROKER_UNAVAILABLE)


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0,
    reason="needs POSIX permissions that apply to the current user",
)
def test_unreadable_broker_reports_neither_path_nor_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], broker: Path
) -> None:
    broker.chmod(0o000)

    result = _run_helper(monkeypatch, capsys, "get", stdin=REGISTRY)

    _assert_failed(result, BROKER_UNREADABLE)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not enforced on Windows")
@pytest.mark.parametrize("mode", [0o640, 0o604, 0o660, 0o644, 0o710], ids=oct)
def test_group_or_world_accessible_broker_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    broker: Path,
    mode: int,
) -> None:
    broker.chmod(mode)

    result = _run_helper(monkeypatch, capsys, "get", stdin=REGISTRY)

    _assert_failed(result, BROKER_UNSAFE)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b'{"server": ', id="truncated-json"),
        pytest.param(b"\xff", id="not-utf-8"),
    ],
)
def test_undecodable_broker_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    broker: Path,
    content: bytes,
) -> None:
    broker.write_bytes(content)

    result = _run_helper(monkeypatch, capsys, "get", stdin=REGISTRY)

    _assert_failed(result, BROKER_INVALID)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param([REGISTRY, USERNAME, SECRET], id="array"),
        pytest.param(SECRET, id="string"),
        pytest.param(None, id="null"),
        pytest.param({"server": REGISTRY, "username": USERNAME}, id="missing-secret"),
        pytest.param({"server": REGISTRY, "secret": SECRET}, id="missing-username"),
        pytest.param({"username": USERNAME, "secret": SECRET}, id="missing-server"),
        pytest.param({"server": REGISTRY, "username": "", "secret": SECRET}, id="empty-username"),
        pytest.param({"server": REGISTRY, "username": USERNAME, "secret": 42}, id="numeric-secret"),
        pytest.param(
            {"server": [REGISTRY], "username": USERNAME, "secret": SECRET}, id="list-server"
        ),
        pytest.param(
            {"server": "https://[::1", "username": USERNAME, "secret": SECRET},
            id="unparseable-server",
        ),
        pytest.param({"server": "https:///", "username": USERNAME, "secret": SECRET}, id="no-host"),
    ],
)
@pytest.mark.parametrize("operation", ["get", "list"])
def test_invalid_broker_payload_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    broker: Path,
    payload: object,
    operation: str,
) -> None:
    _write_broker(broker, payload)

    result = _run_helper(monkeypatch, capsys, operation, stdin=REGISTRY)

    _assert_failed(result, BROKER_INVALID)
    assert SECRET not in result.stdout


def test_helper_process_reports_refusal_without_a_traceback(broker: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "rvs.oci.credential_helper", "store"],
        input=json.dumps({"ServerURL": REGISTRY, "Username": USERNAME, "Secret": SECRET}),
        check=False,
        capture_output=True,
        text=True,
        env=os.environ | {"RVS_OCI_CREDENTIAL_FILE": str(broker)},
    )

    assert result.returncode == 1
    assert result.stdout == f"{READ_ONLY}\n"
    assert result.stderr == ""
