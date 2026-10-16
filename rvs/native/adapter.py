"""The per-tool contract that the native launcher drives.

Each wrapped package manager is one adapter: it classifies a command, previews a
publish, injects short-lived credentials, and names the files whose sources the
launcher compares with the selected target. The launcher owns everything else.
"""

from pathlib import Path
from typing import TYPE_CHECKING, ClassVar


if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..artifacts.targets import PackageFormat
    from ..publishing import PublishItem
    from .common import PackageOperation, RegistryRoute


# Help and version do not resolve packages and need no account or target.
NO_TARGET_ARGS = (["--version"], ["-V"], ["--help"], ["-h"], ["help"])


class NativeAdapter:
    name: ClassVar[str]
    kind: ClassVar[PackageFormat]
    # Environment variable prefixes whose values may name additional sources.
    env_prefixes: ClassVar[tuple[str, ...]] = ()
    # Project files searched upward from the project directory for other sources.
    project_files: ClassVar[tuple[str, ...]] = ()

    @property
    def advisory(self) -> str:
        """The `tool_advisories` entry for this client."""
        return self.name

    def command_prefix(self) -> list[str]:
        raise NotImplementedError

    def native_args(self, argv: list[str]) -> list[str]:
        """Return the arguments passed to the tool after its command prefix."""
        return argv

    def needs_target(self, native_args: list[str]) -> bool:
        return native_args not in NO_TARGET_ARGS

    def operations(self, argv: list[str]) -> tuple[PackageOperation, ...]:
        return ("publish",) if self.is_publishing(argv) else ("read",)

    def is_publishing(self, argv: list[str]) -> bool:
        del argv
        return False

    def publish_items(self, argv: list[str]) -> list[PublishItem]:
        raise NotImplementedError

    def confirmation(self, argv: list[str], env: Mapping[str, str]) -> str | None:
        """Return the question to confirm before a change that is not a publish."""
        del argv, env
        return None

    def preflight(self, argv: list[str], command: list[str]) -> None:
        """Print advisories that need the resolved executable, before the run."""
        del argv, command

    def before_run(self, argv: list[str]) -> None:
        del argv

    def after_failure(self, argv: list[str]) -> None:
        del argv

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
        raise NotImplementedError

    def config_files(self, argv: list[str], env: Mapping[str, str]) -> list[Path]:
        """Return native configuration files that may name sources."""
        del argv, env
        return []

    def project_dir(self, argv: list[str]) -> Path:
        del argv
        return Path.cwd()

    def extra_source_files(self, argv: list[str], base: Path) -> list[Path]:
        del argv, base
        return []

    def source_notes(self) -> tuple[str, ...]:
        """Extra warnings shown when sources outside the target were detected."""
        return ()

    def env_values(self, env: Mapping[str, str]) -> list[str]:
        return [
            value
            for key, value in env.items()
            if key.startswith(self.env_prefixes) and isinstance(value, str)
        ]

    def existing_config_files(self, argv: list[str], env: Mapping[str, str]) -> list[Path]:
        return [path for path in self.config_files(argv, env) if path.is_file()]
