"""npm adapter and the npm-registry command grammar that pnpm shares."""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import typer

from ..artifacts.trash import NATIVE_UNPUBLISH_NOTICE, WHOLE_PACKAGE_UNPUBLISH_HINT
from ..publishing import npm_publish_items
from ..runtime import tools
from .adapter import NativeAdapter
from .common import (
    arg_value,
    inject_npm_auth,
    nearest_named_files,
    positionals,
    remove_value_options,
)


if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..publishing import PublishItem
    from .common import PackageOperation, RegistryRoute


DistTagAction = Literal["add", "rm", "ls"]

# npm's own `dist-tag` subcommand spellings. Any other subcommand lists tags
# (`npm dist-tag` and `npm dist-tag PACKAGE` list) or is a usage error.
_DIST_TAG_ADD = frozenset({"add", "a", "set", "s"})
_DIST_TAG_REMOVE = frozenset({"rm", "r", "del", "d", "remove"})
_DEFAULT_TAG = "latest"


@dataclass(frozen=True)
class RegistryCommandGrammar:
    """How one npm-registry client spells the commands that change a registry."""

    tool: str
    # Options that take a separate value and may precede the command, so the
    # value is not mistaken for it (for example `npm --loglevel verbose publish`).
    value_options: frozenset[str]
    aliases: Mapping[str, str]
    # Where the client finds its default dist-tag. Empty means the client always
    # uses `latest` unless the command names another tag.
    tag_env: tuple[str, ...]
    honors_tag_option: bool
    # The phrase that explains a dist-tag spec without a version.
    unversioned_spec_note: str

    def positionals(self, argv: list[str]) -> list[str]:
        return positionals(argv, self.value_options)

    def command(self, argv: list[str]) -> str | None:
        """Return the command: the first positional argument, alias-resolved.

        Later positionals are package specs or command arguments, so a package
        named `unpublish` in `npm install unpublish` never changes the result.
        """
        found = self.positionals(argv)
        return self.aliases.get(found[0], found[0]) if found else None

    def dist_tag_action(self, argv: list[str]) -> DistTagAction | None:
        """Classify `dist-tag` by its subcommand; reads are everything else."""
        if self.command(argv) != "dist-tag":
            return None
        found = self.positionals(argv)
        subcommand = found[1] if len(found) > 1 else None
        if subcommand in _DIST_TAG_ADD:
            return "add"
        if subcommand in _DIST_TAG_REMOVE:
            return "rm"
        return "ls"

    def is_publish(self, argv: list[str]) -> bool:
        return self.command(argv) == "publish"

    def is_unpublish(self, argv: list[str]) -> bool:
        return self.command(argv) == "unpublish"

    def is_mutation(self, argv: list[str]) -> bool:
        # `dist-tag ls` reads from the download registry with a read-only
        # credential; `dist-tag add`/`rm` change tags on the publish registry.
        command = self.command(argv)
        if command == "dist-tag":
            return self.dist_tag_action(argv) != "ls"
        return command in {"unpublish", "deprecate", "undeprecate"}

    def writes(self, argv: list[str]) -> bool:
        """Whether the command talks to the upload registry."""
        return self.is_publish(argv) or self.is_mutation(argv)

    def operations(self, argv: list[str]) -> tuple[PackageOperation, ...]:
        if self.is_publish(argv):
            return ("publish",)
        if self.is_unpublish(argv):
            # Publish never implies deletion. The client reads the write
            # packument and sends the edited document to the upload host before
            # its delete, and those steps keep requiring publish.
            return ("read", "publish", "delete")
        if self.is_mutation(argv):
            return ("read", "publish")
        return ("read",)

    def dist_tag_confirmation(self, argv: list[str], env: Mapping[str, str]) -> str | None:
        """Return the question to confirm before a `dist-tag` change, if any.

        Moving `latest` changes what an install without a version resolves to,
        and removing a tag breaks installs by it unless an upstream supplies the
        same tag, so both are confirmed; other additions are not. The registry
        refuses a change to a tag or version an upstream defines with its own
        message.
        """
        action = self.dist_tag_action(argv)
        if action not in {"add", "rm"}:
            return None
        found = self.positionals(argv)
        spec = found[2] if len(found) > 2 else None
        explicit = found[3] if len(found) > 3 else None
        if spec is None or (action == "rm" and explicit is None):
            # The client refuses the command with its usage message before any request.
            return None
        if action == "rm":
            return (
                f"Remove {self.tool} dist-tag '{explicit}' from {spec}? Installs by this tag "
                "stop working unless an upstream supplies the same tag."
            )
        tag = (
            explicit
            or (arg_value(argv, "--tag") if self.honors_tag_option else None)
            or next((env[key] for key in self.tag_env if env.get(key)), None)
            or _DEFAULT_TAG
        )
        if tag != _DEFAULT_TAG:
            return None
        target = spec if spec_has_version(spec) else f"{spec} ({self.unversioned_spec_note})"
        return (
            f"Point {self.tool} dist-tag 'latest' at {target}? "
            f"`{self.tool} install` without a version or tag then installs it."
        )


def spec_has_version(spec: str) -> bool:
    """Whether `name@version` names a version; scoped names start with `@`."""
    name_and_version = spec[1:] if spec.startswith("@") else spec
    _, separator, version = name_and_version.partition("@")
    return bool(separator and version)


NPM_GRAMMAR = RegistryCommandGrammar(
    tool="npm",
    value_options=frozenset(
        {
            "--registry",
            "--userconfig",
            "--globalconfig",
            "--prefix",
            "-C",
            "--cache",
            "--loglevel",
            "--location",
            "-L",
            "--workspace",
            "-w",
            "--otp",
            "--tag",
            "--access",
            "--scope",
            "--auth-type",
        }
    ),
    aliases={"dist-tags": "dist-tag"},
    tag_env=("NPM_CONFIG_TAG", "npm_config_tag"),
    honors_tag_option=True,
    # A spec without a version is not a usage error: npm (npm-package-arg) reads
    # `demo`, `@scope/demo`, and `demo@` as the range `*` and sends that as the
    # tag's version, so the change is confirmed like any other.
    unversioned_spec_note="no version given; npm sends '*'",
)


def npmrc_files(env: Mapping[str, str], start: Path | None = None) -> list[Path]:
    """Return project, user, and global npmrc candidates in npm's lookup order."""
    user_config = env.get("NPM_CONFIG_USERCONFIG") or env.get("npm_config_userconfig")
    global_config = env.get("NPM_CONFIG_GLOBALCONFIG") or env.get("npm_config_globalconfig")
    candidates = nearest_named_files((".npmrc",), start)
    if user_config:
        candidates.append(Path(user_config).expanduser())
    candidates.append(Path.home() / ".npmrc")
    if global_config:
        candidates.append(Path(global_config).expanduser())
    return candidates


def write_empty_npmrc_files(env: dict[str, str], temp_dir: Path) -> None:
    user_npmrc = temp_dir / "user.npmrc"
    global_npmrc = temp_dir / "global.npmrc"
    user_npmrc.write_text("", encoding="utf-8")
    global_npmrc.write_text("", encoding="utf-8")
    env["NPM_CONFIG_USERCONFIG"] = str(user_npmrc)
    env["NPM_CONFIG_GLOBALCONFIG"] = str(global_npmrc)


class Npm(NativeAdapter):
    name = "npm"
    kind = "npm"
    env_prefixes = ("NPM_CONFIG_", "npm_config_")
    project_files = ("package-lock.json", "npm-shrinkwrap.json", "package.json", ".npmrc")
    grammar = NPM_GRAMMAR

    def command_prefix(self) -> list[str]:
        return [tools.npm()]

    def operations(self, argv: list[str]) -> tuple[PackageOperation, ...]:
        return self.grammar.operations(argv)

    def is_publishing(self, argv: list[str]) -> bool:
        return self.grammar.is_publish(argv)

    def publish_items(self, argv: list[str]) -> list[PublishItem]:
        return npm_publish_items(argv)

    def confirmation(self, argv: list[str], env: Mapping[str, str]) -> str | None:
        return self.grammar.dist_tag_confirmation(argv, env)

    def before_run(self, argv: list[str]) -> None:
        if self.grammar.is_unpublish(argv):
            typer.echo(NATIVE_UNPUBLISH_NOTICE, err=True)

    def after_failure(self, argv: list[str]) -> None:
        if self.grammar.is_unpublish(argv):
            typer.echo(WHOLE_PACKAGE_UNPUBLISH_HINT, err=True)

    def inject(
        self,
        cmd: list[str],
        native_arg_start: int,
        env: dict[str, str],
        route: RegistryRoute,
        temp_dir: Path,
        *,
        isolate: bool,
    ) -> None:
        registry_url = (
            route.npm_upload_registry_url
            if self.grammar.writes(cmd[native_arg_start:])
            else route.npm_registry_url
        )
        remove_value_options(cmd, {"--registry"}, native_arg_start)
        if len(cmd) > native_arg_start and not cmd[native_arg_start].startswith("-"):
            insert_at = native_arg_start + 1
            cmd[insert_at:insert_at] = ["--registry", registry_url]
        else:
            cmd.extend(["--registry", registry_url])
        env["NPM_CONFIG_REGISTRY"] = registry_url
        inject_npm_auth(env, [registry_url], route.package_token, "NPM_CONFIG_")
        if isolate:
            write_empty_npmrc_files(env, temp_dir)

    def config_files(self, argv: list[str], env: Mapping[str, str]) -> list[Path]:
        del argv
        return npmrc_files(env)

    def project_dir(self, argv: list[str]) -> Path:
        return Path(arg_value(argv, "--prefix") or Path.cwd()).resolve()
