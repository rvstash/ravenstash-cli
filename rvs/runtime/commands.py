"""`rvs runtime` command group."""

import os
import shutil
from pathlib import Path

import typer

from .. import output
from . import java as java_rt
from . import node as node_rt
from . import python as python_rt
from ._install import ENV_FILE, is_debian, runtime_platform, write_env_file, write_shim
from .selection import selected_version


app = typer.Typer(
    name="runtime",
    help="Install and manage local Python, Node.js, and Java runtimes.",
    no_args_is_help=True,
)

_KINDS = ("python", "node", "java")


def _finder(kind: str):
    return {
        "python": python_rt.find,
        "node": node_rt.find,
        "java": java_rt.find,
    }[kind]


def _lister(kind: str):
    return {
        "python": python_rt.list_installed,
        "node": node_rt.list_installed,
        "java": java_rt.list_installed,
    }[kind]


def _installer(kind: str):
    return {
        "python": python_rt.install,
        "node": node_rt.install,
        "java": java_rt.install,
    }[kind]


def _refresh_shims(kind: str, base: Path) -> None:
    """Refresh project-aware dynamic shims for an installed runtime."""
    windows = os.name == "nt"
    bin_dir = base if windows and kind in {"python", "node"} else base / "bin"
    if kind == "python":
        python3 = bin_dir / ("python.exe" if windows else "python3")
        if not python3.exists():
            candidates = sorted(bin_dir.glob("python3.*"))
            if candidates:
                python3 = candidates[0]
        if python3.exists():
            write_shim("python3", python3, runtime_kind="python")
            minor = ".".join(base.name.split(".")[:2])
            write_shim(
                f"python{minor}",
                python3,
                runtime_kind="python",
                version=minor,
            )
        return

    executables = {
        "node": ("node", "npm", "npx", "corepack"),
        "java": ("java", "javac", "jar", "javadoc"),
    }[kind]
    for executable in executables:
        target_name = executable
        if windows:
            target_name += ".cmd" if kind == "node" and executable in {"npm", "npx"} else ".exe"
        target = bin_dir / target_name
        if target.exists():
            write_shim(executable, target, runtime_kind=kind)


@app.command("install")
def install(
    runtime: str = typer.Argument(..., help="Runtime: python | node | java", metavar="RUNTIME"),
    version: str = typer.Argument(
        ...,
        help="Version to install. Examples: 3.12, 22, 21",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Remove an existing matching installation and download again.",
    ),
) -> None:
    """Install a runtime into the rvs-managed store under ~/.rvs/runtimes."""
    if runtime not in _KINDS:
        output.fatal(f"Unknown runtime '{runtime}'. Choose from: {', '.join(_KINDS)}")

    target = runtime_platform()
    if target.system == "linux" and target.libc != "musl" and not is_debian():
        output.warn(
            "This system does not appear to be Debian/Ubuntu based. "
            "Proceeding with portable Linux binaries."
        )

    if force:
        existing = _finder(runtime)(version)
        if existing:
            output.info(f"Removing {existing} ...")
            shutil.rmtree(existing)

    _installer(runtime)(version)


@app.command("uninstall")
def uninstall(
    runtime: str = typer.Argument(..., help="Runtime: python | node | java", metavar="RUNTIME"),
    version: str = typer.Argument(..., help="Installed version or version prefix."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Remove a runtime from the rvs-managed store."""
    if runtime not in _KINDS:
        output.fatal(f"Unknown runtime '{runtime}'. Choose from: {', '.join(_KINDS)}")

    existing = _finder(runtime)(version)
    if not existing:
        output.fatal(f"No installed {runtime} runtime matches '{version}'.")
    if not yes:
        typer.confirm(f"Remove {runtime} runtime at {existing}?", abort=True)
    shutil.rmtree(existing)
    output.success(f"Removed {runtime} runtime '{version}'.")


@app.command("list")
def list_runtimes() -> None:
    """List runtimes installed by rvs."""
    rows: list[list[str]] = []
    for kind in _KINDS:
        for ver, path in _lister(kind)():
            rows.append([kind, ver, str(path)])

    if not rows:
        output.info("No runtimes installed. Run: rvs runtime install <runtime> <version>")
        return

    output.table(["Runtime", "Version", "Path"], rows, title="rvs-managed runtimes")


@app.command("which")
def which(
    runtime: str = typer.Argument(..., help="Runtime: python | node | java", metavar="RUNTIME"),
    version: str = typer.Argument("", help="Version prefix (optional)."),
    executable: str | None = typer.Option(
        None,
        "--executable",
        help="Executable name within the selected runtime (used by rvs shims).",
    ),
) -> None:
    """Print the binary path of an installed runtime."""
    if runtime not in _KINDS:
        output.fatal(f"Unknown runtime '{runtime}'. Choose from: {', '.join(_KINDS)}")

    windows = os.name == "nt"
    default_executable = {
        "python": "python.exe" if windows else "bin/python3",
        "node": "node.exe" if windows else "bin/node",
        "java": "bin/java.exe" if windows else "bin/java",
    }[runtime]
    requested_version = version or selected_version(runtime) or ""
    base = _finder(runtime)(requested_version)
    if base is None:
        output.fatal(
            f"No installed {runtime} matches '{requested_version}'.\n"
            f"  Install with: rvs runtime install {runtime} {requested_version or '<version>'}"
        )

    if executable:
        executable_name = executable
        if windows:
            if runtime == "node" and executable in {"npm", "npx"}:
                executable_name += ".cmd"
            elif not executable.lower().endswith((".exe", ".cmd")):
                executable_name += ".exe"
        executable_root = base if windows and runtime in {"python", "node"} else base / "bin"
        binary = executable_root / executable_name
    else:
        binary = base / default_executable
    output.value(str(binary) if binary.exists() else str(base), key="path")


@app.command("use")
def use(
    runtime: str = typer.Argument(..., help="Runtime: python | node | java", metavar="RUNTIME"),
    version: str = typer.Argument(..., help="Installed version or version prefix."),
) -> None:
    """Pin a runtime version for the current project."""
    if runtime not in _KINDS:
        output.fatal(f"Unknown runtime '{runtime}'. Choose from: {', '.join(_KINDS)}")

    base = _finder(runtime)(version)
    if base is None:
        output.fatal(
            f"No installed {runtime} matches '{version}'.\n"
            f"  Install with: rvs runtime install {runtime} {version}"
        )

    version_name = base.name
    marker = {
        "python": ".python-version",
        "node": ".node-version",
        "java": ".java-version",
    }[runtime]
    Path(marker).write_text(version_name + "\n", encoding="utf-8")
    _refresh_shims(runtime, base)
    output.success(f"Pinned {runtime} {version_name} in {marker}.")


@app.command("env")
def env() -> None:
    """Print the shell snippet that adds rvs runtime shims to PATH."""
    write_env_file()
    environment_file = ENV_FILE.with_name("env.ps1") if os.name == "nt" else ENV_FILE
    output.value(environment_file.read_text(encoding="utf-8").rstrip("\n"), key="configuration")


@app.command("setup-shell")
def setup_shell(
    shell: str | None = typer.Option(
        None,
        "--shell",
        help="Shell: bash | zsh | fish | powershell | pwsh. Auto-detected when omitted.",
    ),
) -> None:
    """Add rvs's runtime shims to the user's shell startup file."""
    for kind in _KINDS:
        base = _finder(kind)("")
        if base is not None:
            _refresh_shims(kind, base)
    write_env_file()
    source_line = '. "$HOME/.rvs/env"'
    detected_shell = shell or (
        "pwsh" if os.name == "nt" else Path(os.environ.get("SHELL", "")).name
    )

    if detected_shell in {"powershell", "pwsh"}:
        if os.name == "nt":
            profile_directory = "PowerShell" if detected_shell == "pwsh" else "WindowsPowerShell"
            profile = (
                Path.home() / "Documents" / profile_directory / "Microsoft.PowerShell_profile.ps1"
            )
        else:
            profile = Path.home() / ".config" / "powershell" / "Microsoft.PowerShell_profile.ps1"
        source_line = '. "$HOME/.rvs/env.ps1"'
        profile.parent.mkdir(parents=True, exist_ok=True)
        content = profile.read_text(encoding="utf-8") if profile.exists() else ""
        if source_line not in content:
            with profile.open("a", encoding="utf-8") as stream:
                stream.write(f"\n# rvs managed runtimes\n{source_line}\n")
            output.success(f"Added rvs environment to {profile}")
        else:
            output.info(f"Already configured in {profile}")
        output.info("Restart PowerShell or run: . $HOME/.rvs/env.ps1")
        return

    if detected_shell == "fish":
        fish_rc = Path.home() / ".config" / "fish" / "conf.d" / "rvs.fish"
        fish_rc.parent.mkdir(parents=True, exist_ok=True)
        fish_rc.write_text(
            '# rvs managed runtimes\nset -gx PATH "$HOME/.rvs/shims" $PATH\n',
            encoding="utf-8",
        )
        output.success(f"Written fish config to {fish_rc}")
        return

    if detected_shell == "zsh":
        rc_candidates = [Path.home() / ".zshrc"]
    elif detected_shell == "bash":
        rc_candidates = [Path.home() / ".bashrc", Path.home() / ".bash_profile"]
    else:
        rc_candidates = [
            p
            for p in (
                Path.home() / ".bashrc",
                Path.home() / ".bash_profile",
                Path.home() / ".zshrc",
                Path.home() / ".profile",
            )
            if p.exists()
        ] or [Path.home() / ".bashrc"]

    added: list[str] = []
    for rc in rc_candidates:
        content = rc.read_text(encoding="utf-8") if rc.exists() else ""
        if source_line in content:
            output.info(f"Already configured in {rc}")
            continue
        with rc.open("a", encoding="utf-8") as fh:
            fh.write(f"\n# rvs managed runtimes\n{source_line}\n")
        added.append(str(rc))

    if added:
        output.success(f"Added rvs env source to: {', '.join(added)}")
        output.info("Restart your shell or run: source ~/.rvs/env")
    else:
        output.info("Already configured; no changes made.")


@app.command("doctor")
def doctor() -> None:
    """Show local runtime manager status."""
    rows: list[list[str]] = []
    for kind, binary in (("python", "python3"), ("node", "node"), ("java", "java")):
        managed = _finder(kind)("")
        system = shutil.which(binary)
        rows.append(
            [
                kind,
                str(managed) if managed else "none",
                system or "not found",
            ]
        )
    output.table(["Runtime", "rvs-managed", "System PATH"], rows, title="Runtime status")
