import annotationlib
from typing import TYPE_CHECKING

import pytest
from click import Context, unstyle
from rvs import config as cfg_mod
from rvs.cli import app
from typer.core import TyperGroup
from typer.main import get_command
from typer.testing import CliRunner


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


runner = CliRunner()
STAGING_API_URL = "https://control.example.test"


def _isolate_config(monkeypatch, tmp_path: Path, content: str = "") -> None:
    config_dir = tmp_path / ".rvs"
    config_dir.mkdir()
    config_file = config_dir / "config.toml"
    normalized = content or 'default_profile = "default"\n'
    if "schema_version" not in normalized:
        normalized = f"schema_version = 1\n{normalized}"
    config_file.write_text(normalized, encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_file)


def test_root_help_exposes_clean_public_command_surface() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in (
        "pip",
        "uv",
        "twine",
        "python",
        "npm",
        "mvn",
        "auth",
        "profile",
        "account",
        "status",
        "runtime",
        "art",
    ):
        assert command in result.output


def test_root_help_groups_commands_by_ecosystem() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    help_output = unstyle(result.output)
    expected_panels = (
        ("Python tools", ("pip", "python", "twine", "uv")),
        ("Node.js tools", ("npm",)),
        ("JVM tools", ("mvn",)),
        ("Container tools", ("docker", "oras")),
        ("Helm tools", ("helm",)),
        ("Artifact management", ("art",)),
        ("Account and configuration", ("account", "auth", "profile", "status")),
        ("Setup and maintenance", ("runtime", "update")),
    )

    for title, _ in expected_panels:
        assert title in help_output

    root_command = get_command(app)
    assert isinstance(root_command, TyperGroup)
    context = Context(root_command)
    ordered_commands = []
    for title, commands in expected_panels:
        for command_name in commands:
            command = root_command.get_command(context, command_name)
            assert command is not None
            assert getattr(command, "rich_help_panel", None) == title
            ordered_commands.append(command_name)

    visible_commands = [
        command_name
        for command_name in root_command.list_commands(context)
        if not getattr(root_command.get_command(context, command_name), "hidden", False)
    ]
    assert visible_commands == ordered_commands


def test_context_commands_are_separated_from_auth_help() -> None:
    auth_result = runner.invoke(app, ["auth", "--help"])
    profile_result = runner.invoke(app, ["profile", "--help"])
    account_result = runner.invoke(app, ["account", "--help"])

    assert auth_result.exit_code == 0
    assert "Manage named local CLI profiles" not in auth_result.output
    assert "login" in auth_result.output
    assert profile_result.exit_code == 0
    assert "current" in profile_result.output
    assert "use" in profile_result.output
    assert "switch" not in profile_result.output
    assert account_result.exit_code == 0
    assert "switch" in account_result.output
    assert not any(line.strip().startswith("│ use ") for line in account_result.output.splitlines())


def test_root_without_args_shows_help() -> None:
    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert "Ravenstash developer CLI" in result.output


def test_version_option_prints_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.output.startswith("Ravenstash CLI ")


@pytest.mark.parametrize("alias", ["art"])
def test_repo_commands_are_registered(alias: str) -> None:
    repo_result = runner.invoke(app, [alias, "repo", "--help"])

    assert repo_result.exit_code == 0
    for command in ("list", "create", "show"):
        assert command in repo_result.output


def test_artifact_commands_are_one_command_group() -> None:
    groups = [group.name for group in app.registered_groups]
    assert groups.count("art") == 1


def test_artifact_help_prints_no_deprecation_warning() -> None:
    result = runner.invoke(app, ["art", "--help"])
    assert result.exit_code == 0
    assert "DeprecationWarning" not in result.stderr


def test_repo_list_filters_by_format() -> None:
    result = runner.invoke(app, ["art", "repo", "list", "--help"])

    assert result.exit_code == 0
    assert "--format" in unstyle(result.output)


@pytest.mark.parametrize("command", ["docker", "helm", "oras"])
def test_container_wrappers_reject_unknown_rvs_options(command: str) -> None:
    result = runner.invoke(app, [command, "--rvs-unknown", "acme/packages", "version"])

    assert result.exit_code == 1
    assert "Unknown rvs option. Use --rvs-target for the target." in result.output


def _command_paths(command: object, prefix: str = "") -> Iterator[str]:
    yield prefix
    if isinstance(command, TyperGroup):
        context = Context(command)
        for name in command.list_commands(context):
            subcommand = command.get_command(context, name)
            assert subcommand is not None
            yield from _command_paths(subcommand, f"{prefix} {name}".strip())


def test_every_command_signature_resolves_at_runtime() -> None:
    # Annotations are evaluated lazily, and Typer evaluates each command's
    # signature when it builds the command tree. A name imported only for type
    # checking in a command signature would fail here instead of in the field.
    for group in app.registered_groups:
        assert group.typer_instance is not None
        for command in group.typer_instance.registered_commands:
            assert command.callback is not None
            annotationlib.get_annotations(command.callback, format=annotationlib.Format.VALUE)

    paths = set(_command_paths(get_command(app)))

    assert {"auth login", "art token mint", "status"} <= paths
    assert {"art package tag list", "art package tag set", "art package tag delete"} <= paths


def test_package_tag_commands_are_listed_in_workflow_order() -> None:
    package_result = runner.invoke(app, ["art", "package", "--help"])
    tag_result = runner.invoke(app, ["art", "package", "tag", "--help"])

    assert package_result.exit_code == 0
    assert tag_result.exit_code == 0
    assert "tag" in unstyle(package_result.output)
    group: object = get_command(app)
    for name in ("art", "package", "tag"):
        assert isinstance(group, TyperGroup)
        group = group.get_command(Context(group), name)
    assert isinstance(group, TyperGroup)
    assert group.list_commands(Context(group)) == ["list", "set", "delete"]
    assert "Package tags move, unlike OCI tags" in unstyle(
        runner.invoke(app, ["art", "package", "tag", "set", "--help"]).output
    )
