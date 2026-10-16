"""pnpm adapter.

pnpm reads the same npm-registry settings as npm, but through a different set of
configuration channels depending on its major version:

- pnpm 10 and earlier read `npm_config_*` variables. pnpm 9 rewrites `_` to `-`
  in a whole credential key, and pnpm 10 does so in a key whose registry host
  has a port, so an `_authToken` variable for such a registry is lost.
- pnpm 11 and later ignore `npm_config_*` and read `pnpm_config_*` instead.
- Every version reads the npm user config file that `npm_config_userconfig`
  names, and pnpm 10 and earlier delegate `publish`, `dist-tag`, `deprecate`,
  and `unpublish` to the npm CLI, which reads the same variables and file.

The wrapper therefore sets the registry and credential in both variable
families and adds the credential to a temporary copy of the user config file.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from .. import output
from ..artifacts.routing import npm_auth_token_key
from ..artifacts.trash import NATIVE_UNPUBLISH_NOTICE, whole_package_unpublish_hint
from ..publishing import pnpm_publish_items
from ..runtime import tools
from ..tool_advisories import detected_version
from .adapter import NativeAdapter
from .common import arg_value, inject_npm_auth, nearest_named_files, remove_value_options
from .npm import RegistryCommandGrammar, npmrc_files


if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..publishing import PublishItem
    from .common import PackageOperation, RegistryRoute


PNPM_GRAMMAR = RegistryCommandGrammar(
    tool="pnpm",
    value_options=frozenset(
        {
            "--registry",
            "--filter",
            "-F",
            "--filter-prod",
            "--dir",
            "-C",
            "--loglevel",
            "--reporter",
            "--tag",
            "--access",
            "--otp",
            "--publish-branch",
            "--workspace-concurrency",
            "--store-dir",
            "--virtual-store-dir",
            "--modules-dir",
            "--lockfile-dir",
            "--cache-dir",
            "--global-dir",
        }
    ),
    aliases={"dist-tags": "dist-tag"},
    # pnpm 11 and later implement `dist-tag` natively and always default to
    # `latest`; only an explicit tag argument is trusted to name another tag.
    tag_env=(),
    honors_tag_option=False,
    unversioned_spec_note="no version given",
)

_ENV_FAMILIES = ("NPM_CONFIG_", "PNPM_CONFIG_")
_UNPUBLISH_HINT = whole_package_unpublish_hint("the registry refused the unpublish with status 405")


class Pnpm(NativeAdapter):
    name = "pnpm"
    kind = "npm"
    env_prefixes = ("NPM_CONFIG_", "npm_config_", "PNPM_CONFIG_", "pnpm_config_")
    project_files = ("pnpm-lock.yaml", "pnpm-workspace.yaml", "package.json", ".npmrc")
    grammar = PNPM_GRAMMAR

    def command_prefix(self) -> list[str]:
        return [tools.require("pnpm", install_kind="system")]

    def operations(self, argv: list[str]) -> tuple[PackageOperation, ...]:
        return self.grammar.operations(argv)

    def is_publishing(self, argv: list[str]) -> bool:
        return self.grammar.is_publish(argv)

    def publish_items(self, argv: list[str]) -> list[PublishItem]:
        found = self.grammar.positionals(argv)
        publish_index = argv.index(found[0]) if found else len(argv)
        return pnpm_publish_items(argv, publish_index)

    def confirmation(self, argv: list[str], env: Mapping[str, str]) -> str | None:
        return self.grammar.dist_tag_confirmation(argv, env)

    def preflight(self, argv: list[str], command: list[str]) -> None:
        pinned = _pinned_pnpm_version(self.project_dir(argv))
        if pinned is None:
            return
        running = detected_version("pnpm", tuple(command))
        if running is None or running == pinned:
            return
        output.warn(
            f"package.json pins pnpm {pinned}, but pnpm {running} is running. pnpm may "
            "download the pinned version from the selected Ravenstash repository, which "
            "works only when that repository has an npm upstream that provides pnpm. "
            "Run the pinned pnpm version to avoid the download."
        )

    def before_run(self, argv: list[str]) -> None:
        if self.grammar.is_unpublish(argv):
            typer.echo(NATIVE_UNPUBLISH_NOTICE, err=True)

    def after_failure(self, argv: list[str]) -> None:
        if self.grammar.is_unpublish(argv):
            typer.echo(_UNPUBLISH_HINT, err=True)

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
            route.npm_registry_url
            if not self.grammar.writes(cmd[native_arg_start:])
            else route.npm_upload_registry_url
        )
        # Both variable families outrank project `.npmrc` and `pnpm-workspace.yaml`
        # settings, so no `--registry` argument is added. Inserting one could land
        # among the arguments that `pnpm run` or `pnpm exec` pass to a script.
        remove_value_options(cmd, {"--registry"}, native_arg_start)
        user_config = _user_config(env)
        replaced = ["registry", "userconfig", *(["globalconfig"] if isolate else [])]
        for prefix in _ENV_FAMILIES:
            # A lower-case duplicate would compete with the upper-case value.
            for name in replaced:
                env.pop(f"{prefix.lower()}{name}", None)
            env[f"{prefix}REGISTRY"] = registry_url
            inject_npm_auth(env, [registry_url], route.package_token, prefix)
        auth_line = f"{npm_auth_token_key(registry_url)}={route.package_token}\n"
        preserved = "" if isolate else _without_key(user_config, npm_auth_token_key(registry_url))
        user_npmrc = temp_dir / "user.npmrc"
        user_npmrc.write_text(preserved + auth_line, encoding="utf-8")
        user_npmrc.chmod(0o600)
        env["NPM_CONFIG_USERCONFIG"] = str(user_npmrc)
        if isolate:
            global_npmrc = temp_dir / "global.npmrc"
            global_npmrc.write_text("", encoding="utf-8")
            env["NPM_CONFIG_GLOBALCONFIG"] = str(global_npmrc)

    def config_files(self, argv: list[str], env: Mapping[str, str]) -> list[Path]:
        config_home = Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        return [
            *npmrc_files(env, self.project_dir(argv)),
            # pnpm 10 and earlier keep global settings in `rc`; pnpm 11 and later
            # in `config.yaml`, with `pnpm login` credentials in `auth.ini`.
            config_home / "pnpm" / "rc",
            config_home / "pnpm" / "config.yaml",
            config_home / "pnpm" / "auth.ini",
        ]

    def project_dir(self, argv: list[str]) -> Path:
        return Path(arg_value(argv, "--dir", "-C") or Path.cwd()).resolve()


def _user_config(env: Mapping[str, str]) -> Path:
    configured = env.get("NPM_CONFIG_USERCONFIG") or env.get("npm_config_userconfig")
    return Path(configured).expanduser() if configured else Path.home() / ".npmrc"


def _without_key(path: Path, key: str) -> str:
    """Return the npmrc text without lines that set *key*; the injected line replaces them."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError, UnicodeError:
        return ""
    kept = [line for line in text.splitlines() if line.split("=", 1)[0].strip() != key]
    return "\n".join(kept) + "\n" if kept else ""


def _pinned_pnpm_version(project: Path) -> str | None:
    """Return the pnpm version pinned by the nearest package.json that pins a manager.

    A workspace package usually inherits the pin from the workspace root, so
    manifests without a `packageManager` field are skipped.
    """
    for path in nearest_named_files(("package.json",), project):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except OSError, UnicodeError, ValueError:
            return None
        pin = manifest.get("packageManager") if isinstance(manifest, dict) else None
        if not isinstance(pin, str):
            continue
        if not pin.startswith("pnpm@"):
            return None
        return pin.removeprefix("pnpm@").split("+", 1)[0] or None
    return None
