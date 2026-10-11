"""Lightweight process entry point for the CLI and credential helper."""

import sys
from pathlib import Path

from .identity import PACKAGE_NAME, PRODUCT_NAME


def _version_requested(arguments: list[str]) -> bool:
    return len(arguments) == 1 and arguments[0] in {"--version", "-V"}


def main() -> None:
    """Dispatch fast, standalone paths before importing the full CLI graph."""
    if Path(sys.argv[0]).stem == "docker-credential-rvs":
        from .oci.credential_helper import main as credential_helper_main

        credential_helper_main()
        return

    if _version_requested(sys.argv[1:]):
        from importlib.metadata import PackageNotFoundError, version

        try:
            release = version(PACKAGE_NAME)
        except PackageNotFoundError:
            release = "dev"
        print(f"{PRODUCT_NAME} {release}")
        return

    from .cli import app
    from .client import ApiRouteRetiredError
    from .config import ConfigError

    try:
        app()
    except ConfigError as exc:
        from . import output

        # The error names its own remedy: a reset, or an update for a newer schema.
        output.fatal(f"{exc}. {exc.hint}")
    except ApiRouteRetiredError as exc:
        # Raised from paths without their own handler, such as a session refresh.
        from . import output

        output.fatal(str(exc))


if __name__ == "__main__":
    main()
