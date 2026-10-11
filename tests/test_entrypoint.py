import importlib.metadata
import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
from rvs import cli, entrypoint
from rvs.config import ConfigError
from rvs.entrypoint import _version_requested


if TYPE_CHECKING:
    from pathlib import Path


def test_only_standalone_version_flags_take_the_fast_path() -> None:
    assert _version_requested(["--version"])
    assert _version_requested(["-V"])
    assert not _version_requested([])
    assert not _version_requested(["--json", "--version"])
    assert not _version_requested(["pip", "--version"])


def test_version_fast_path_does_not_import_cli() -> None:
    code = """
import sys
sys.argv = ["rvs", "--version"]
from rvs.entrypoint import main
main()
assert "rvs.cli" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("Ravenstash CLI ")


def test_newer_config_schema_is_reported_without_a_traceback(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text("schema_version = 999\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "rvs.entrypoint", "profile", "current"],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ | {"RVS_HOME": str(tmp_path)},
    )

    stderr = " ".join(result.stderr.split())
    assert result.returncode == 1
    assert result.stdout == ""
    assert "uses configuration schema 999, which is newer than this rvs release supports" in stderr
    assert "rvs update" in stderr
    assert "rvs profile delete --all" not in stderr
    assert "Traceback" not in stderr


def test_malformed_config_is_reported_without_a_traceback(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text("profiles = [\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "rvs.entrypoint", "profile", "current"],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ | {"RVS_HOME": str(tmp_path)},
    )

    stderr = " ".join(result.stderr.split())
    assert result.returncode == 1
    assert result.stdout == ""
    assert "could not read" in stderr
    assert "rvs profile delete --all" in stderr
    assert "Traceback" not in stderr


def test_retired_api_route_is_reported_without_a_traceback(monkeypatch, capsys) -> None:
    from rvs.client import ApiRouteRetiredError

    def retired_app() -> None:
        raise ApiRouteRetiredError

    monkeypatch.setattr(sys, "argv", ["rvs", "art", "repo", "list"])
    monkeypatch.setattr(cli, "app", retired_app)

    with pytest.raises(SystemExit) as exc_info:
        entrypoint.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    stderr = " ".join(captured.err.split())
    assert "This rvs release is no longer supported by the Ravenstash API." in stderr
    assert "Traceback" not in stderr


def _refuse_full_cli() -> None:
    raise AssertionError("the full CLI must not run")


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_flag_prints_the_installed_distribution_version(monkeypatch, capsys, flag) -> None:
    queried: list[str] = []

    def installed_version(distribution: str) -> str:
        queried.append(distribution)
        return "1.2.3"

    monkeypatch.setattr(importlib.metadata, "version", installed_version)
    monkeypatch.setattr(cli, "app", _refuse_full_cli)
    monkeypatch.setattr(sys, "argv", ["rvs", flag])

    entrypoint.main()

    assert queried == ["ravenstash-cli"]
    assert capsys.readouterr() == ("Ravenstash CLI 1.2.3\n", "")


def test_version_flag_reports_dev_when_the_distribution_is_not_installed(
    monkeypatch, capsys
) -> None:
    def missing_distribution(distribution: str) -> str:
        raise importlib.metadata.PackageNotFoundError(distribution)

    monkeypatch.setattr(importlib.metadata, "version", missing_distribution)
    monkeypatch.setattr(sys, "argv", ["rvs", "--version"])

    entrypoint.main()

    assert capsys.readouterr() == ("Ravenstash CLI dev\n", "")


def test_config_error_names_the_reset_command_without_a_traceback(monkeypatch, capsys) -> None:
    def unreadable_config() -> None:
        raise ConfigError("could not read the local configuration")

    monkeypatch.setattr(cli, "app", unreadable_config)
    monkeypatch.setattr(sys, "argv", ["rvs", "profile", "current"])

    with pytest.raises(SystemExit) as exc_info:
        entrypoint.main()

    assert exc_info.value.code == 1
    assert capsys.readouterr() == (
        "",
        "Error: could not read the local configuration. "
        "Run `rvs profile delete --all` to reset the local configuration.\n",
    )


@pytest.mark.parametrize(
    "program", ["/usr/local/bin/docker-credential-rvs", "docker-credential-rvs.exe"]
)
def test_docker_credential_alias_runs_the_credential_helper(
    monkeypatch, capsys, tmp_path: Path, program: str
) -> None:
    broker = tmp_path / "broker.json"
    broker.write_text(
        json.dumps({"server": "registry.example.test", "username": "robot", "secret": "s3cret"}),
        encoding="utf-8",
    )
    broker.chmod(0o600)
    monkeypatch.setenv("RVS_OCI_CREDENTIAL_FILE", str(broker))
    monkeypatch.setattr(cli, "app", _refuse_full_cli)
    monkeypatch.setattr(sys, "argv", [program, "list"])

    entrypoint.main()

    assert capsys.readouterr() == ('{"registry.example.test": "robot"}\n', "")
