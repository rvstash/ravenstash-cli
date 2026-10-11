"""Signed APT update and explicit compatibility-channel upgrades."""

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, NoReturn

import httpx2 as httpx
import typer

from . import output
from .apt_channels import (
    channel_order,
    normalize_channel,
    normalize_minor_target,
    version_matches_channel,
)
from .identity import (
    APT_KEYRING_PATH,
    APT_REPOSITORY_URL,
    APT_SOURCE_PATH,
    NIX_FLAKE,
    PACKAGE_NAME,
    debian_package_name,
    homebrew_cask,
    winget_package,
)
from .installations import compatibility_channel, detect_portable_installation
from .portable_update import (
    UpdateError,
    apply_portable_update,
    check_manifest_freshness,
    download_verified_assets,
    fetch_channel_manifest,
    latest_for_channel,
    latest_for_minor,
    newer,
)


_APT_CACHE = Path("/usr/bin/apt-cache")
_APT_GET = Path("/usr/bin/apt-get")
_DPKG_QUERY = Path("/usr/bin/dpkg-query")
_DPKG_DEB = Path("/usr/bin/dpkg-deb")
_GPGV = Path("/usr/bin/gpgv")
_SUDO = Path("/usr/bin/sudo")
_APT_SOURCE = Path(APT_SOURCE_PATH)
_APT_KEYRING = Path(APT_KEYRING_PATH)
_CHANNELS_URL = f"{APT_REPOSITORY_URL}/channels.json"
_CHANNELS_SIGNATURE_URL = f"{_CHANNELS_URL}.gpg"
_CANDIDATE_PATTERN = re.compile(r"^(?P<base>[0-9]+\.[0-9]+\.[0-9]+)rc(?P<number>[1-9][0-9]*)$")
_SOURCE_PATTERN = re.compile(
    rf"^deb \[arch=(?:amd64|arm64) signed-by={re.escape(APT_KEYRING_PATH)}\] "
    rf"{re.escape(APT_REPOSITORY_URL)} (?P<channel>v(?:0|[1-9][0-9]*)) main$"
)


def _cli_version() -> str:
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return "dev"


def _safe_environment() -> dict[str, str]:
    return {
        "DEBIAN_FRONTEND": "noninteractive",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    }


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=_safe_environment(),
    )


def _root_command(command: list[str]) -> list[str]:
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and geteuid() == 0:
        return command
    if not _SUDO.is_file():
        output.fatal("Updating requires root privileges and /usr/bin/sudo is unavailable.")
    return [str(_SUDO), *command]


def _run_visible(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        _root_command(command),
        check=False,
        env=_safe_environment(),
    )


def _apt_versions() -> tuple[str | None, str | None]:
    if not _DPKG_QUERY.is_file() or not _APT_CACHE.is_file():
        return None, None
    installed_result = _run(
        [
            str(_DPKG_QUERY),
            "--show",
            "--showformat=${db:Status-Abbrev} ${Version}\\n",
            PACKAGE_NAME,
        ]
    )
    installed = None
    if installed_result.returncode == 0:
        status, separator, package_version = installed_result.stdout.strip().partition(" ")
        if separator and status == "ii":
            installed = package_version

    policy_result = _run([str(_APT_CACHE), "policy", PACKAGE_NAME])
    candidate = None
    if policy_result.returncode == 0:
        for line in policy_result.stdout.splitlines():
            label, separator, value = line.strip().partition(":")
            if separator and label == "Candidate" and value.strip() != "(none)":
                candidate = value.strip()
                break
    return installed, candidate


def _upgrade_available(installed: str, candidate: str) -> bool:
    result = _run(["/usr/bin/dpkg", "--compare-versions", candidate, "gt", installed])
    return result.returncode == 0


def _current_channel() -> str | None:
    try:
        source = _APT_SOURCE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    match = _SOURCE_PATTERN.fullmatch(source)
    if match is None:
        return None
    return normalize_channel(match.group("channel"))


def _channel_manifest() -> dict[str, Any] | None:
    if not _APT_KEYRING.is_file() or not _GPGV.is_file():
        return None
    try:
        with httpx.Client(timeout=10.0, follow_redirects=False) as client:
            manifest_response = client.get(_CHANNELS_URL)
            signature_response = client.get(_CHANNELS_SIGNATURE_URL)
            manifest_response.raise_for_status()
            signature_response.raise_for_status()
        with tempfile.TemporaryDirectory(prefix="rvs-channels-") as directory:
            manifest_path = Path(directory) / "channels.json"
            signature_path = Path(directory) / "channels.json.gpg"
            manifest_path.write_bytes(manifest_response.content)
            signature_path.write_bytes(signature_response.content)
            verified = subprocess.run(
                [
                    str(_GPGV),
                    f"--keyring={_APT_KEYRING}",
                    str(signature_path),
                    str(manifest_path),
                ],
                check=False,
                capture_output=True,
                env=_safe_environment(),
            )
            if verified.returncode != 0:
                return None
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError, ValueError, httpx.HTTPError:
        return None

    if payload.get("schema") != 1 or not isinstance(payload.get("channels"), dict):
        return None
    try:
        recommended = normalize_channel(payload["recommended"])
        for name, channel_data in payload["channels"].items():
            normalized = normalize_channel(name)
            if normalized != name or not isinstance(channel_data, dict):
                return None
            if not version_matches_channel(channel_data.get("latest", ""), normalized):
                return None
            targets = channel_data.get("minor_targets")
            if not isinstance(targets, dict) or not targets:
                return None
            for selector, target in targets.items():
                if (
                    not isinstance(selector, str)
                    or not isinstance(target, str)
                    or normalize_minor_target(selector) != selector
                    or not target.startswith(f"{selector}.")
                    or not version_matches_channel(target, normalized)
                ):
                    return None
        if recommended not in payload["channels"]:
            return None
    except KeyError, TypeError, ValueError:
        return None
    try:
        check_manifest_freshness(payload)
    except UpdateError as exc:
        output.warn(f"Ignoring the release-channel manifest: {exc}.")
        return None
    return payload


def _refresh_ravenstash_source() -> bool:
    """Refresh APT metadata for the Ravenstash source only.

    Other sources keep their cached indexes, so a broken or slow third-party
    repository cannot block an rvs update. A missing Ravenstash source falls back
    to a full refresh, which reports the configuration problem through APT.
    """
    command = [str(_APT_GET), "update"]
    if _APT_SOURCE.is_file():
        command += [
            "-o",
            f"Dir::Etc::sourcelist={_APT_SOURCE}",
            "-o",
            "Dir::Etc::sourceparts=-",
            "-o",
            "APT::Get::List-Cleanup=0",
        ]
    return _run_visible(command).returncode == 0


def _channel_latest(manifest: dict[str, Any] | None, channel: str | None) -> str | None:
    if manifest is None or channel is None:
        return None
    try:
        return latest_for_channel(manifest, channel)
    except UpdateError, ValueError:
        return None


def _announce_new_channel(current_channel: str | None, manifest: dict[str, Any] | None) -> None:
    if current_channel is None or manifest is None:
        return
    recommended = manifest["recommended"]
    if channel_order(recommended) <= channel_order(current_channel):
        return
    latest = manifest["channels"][recommended]["latest"]
    output.info(
        f"rvs {latest} is available in release channel {recommended}. "
        f"Review the migration notes, then run: rvs update --to {recommended.removeprefix('v')}"
    )


def _requested_target(
    manifest: dict[str, Any], current_channel: str, requested: str | None
) -> tuple[str, str, str]:
    """Return channel, exact version, and a user-facing target label."""
    if requested is None:
        channel = current_channel
        return channel, latest_for_channel(manifest, channel), f"release channel {channel}"
    try:
        channel = normalize_channel(requested)
    except ValueError:
        selector = normalize_minor_target(requested)
        channel = normalize_channel(selector.partition(".")[0])
        if channel_order(channel) < channel_order(current_channel):
            raise ValueError("release-channel downgrades are not supported automatically") from None
        return channel, latest_for_minor(manifest, selector), f"minor release {selector}"
    if channel_order(channel) < channel_order(current_channel):
        raise ValueError("release-channel downgrades are not supported automatically")
    return channel, latest_for_channel(manifest, channel), f"release channel {channel}"


def _candidate_debian_version(candidate: str) -> str:
    match = _CANDIDATE_PATTERN.fullmatch(candidate)
    if match is None:
        output.fatal("Candidate versions must look like 0.15.1rc1.")
    return f"{match.group('base')}~rc{match.group('number')}"


def _candidate_architecture() -> str:
    architecture = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(platform.machine().lower())
    if architecture is None:
        output.fatal("Candidate packages are available only for Linux amd64 and arm64.")
    return architecture


def _download_candidate(candidate: str, destination: Path) -> Path:
    architecture = _candidate_architecture()
    deb_name = debian_package_name(candidate, architecture)
    try:
        deb_path = download_verified_assets(candidate, {deb_name}, destination, candidate=True)[
            deb_name
        ]
    except UpdateError as exc:
        output.fatal(str(exc))

    metadata = _run(
        [
            str(_DPKG_DEB),
            "--show",
            "--showformat=${Package}\\n${Version}\\n${Architecture}\\n",
            str(deb_path),
        ]
    )
    expected_metadata = [PACKAGE_NAME, _candidate_debian_version(candidate), architecture]
    if metadata.returncode != 0 or metadata.stdout.splitlines() != expected_metadata:
        output.fatal("The candidate package metadata does not match the requested release.")
    return deb_path


def _update_candidate(
    candidate: str, *, apply: bool, yes: bool, installed_version: str | None = None
) -> None:
    debian_version = _candidate_debian_version(candidate)
    installed = installed_version
    if installed is None:
        installed, _apt_candidate = _apt_versions()
    if installed is None:
        output.fatal("Release candidates can only update an existing rvs APT installation.")
    if not _DPKG_DEB.is_file():
        output.fatal("Candidate verification requires /usr/bin/dpkg-deb.")
    if not _upgrade_available(installed, debian_version):
        output.fatal(f"Release candidate {candidate} is not newer than installed rvs {installed}.")

    with tempfile.TemporaryDirectory(prefix="rvs-candidate-") as directory:
        package = _download_candidate(candidate, Path(directory))
        output.info(
            f"Verified signed release candidate rvs {candidate} "
            f"(Debian version {debian_version}; installed: {installed})."
        )
        if not apply:
            output.info(f"Install it with: rvs update --candidate {candidate} --apply")
            return
        if not _APT_GET.is_file():
            output.fatal("Cannot update because /usr/bin/apt-get is unavailable.")
        if not yes:
            typer.confirm(f"Install signed rvs release candidate {candidate}?", abort=True)
        if _run_visible([str(_APT_GET), "install", "--yes", str(package)]).returncode != 0:
            _restore_apt_version(installed, candidate)
    output.success(f"Updated rvs to release candidate {candidate}.")


def _portable_installation():
    try:
        return detect_portable_installation(installed_version=_cli_version())
    except ValueError as exc:
        output.fatal(str(exc))


def _portable_update(*, to: str | None, candidate: str | None, apply: bool, yes: bool) -> None:
    installation = _portable_installation()
    if installation is None:
        _delegate_package_manager_update(to=to, candidate=candidate, apply=apply, yes=yes)
        return
    manifest: dict[str, Any] | None = None
    if candidate is not None:
        if _CANDIDATE_PATTERN.fullmatch(candidate) is None:
            output.fatal("Candidate versions must look like 0.15.1rc1.")
        target_version = candidate
        release_label = "release candidate"
    else:
        try:
            manifest = fetch_channel_manifest()
            _target_channel, target_version, release_label = _requested_target(
                manifest, installation.channel, to
            )
        except (UpdateError, ValueError) as exc:
            output.fatal(str(exc))
    if not newer(target_version, installation.version):
        if target_version == installation.version:
            output.success(
                f"Installed: rvs {installation.version}. Latest in {release_label}: "
                f"rvs {target_version}. You are up to date."
            )
            if manifest is not None and to is None:
                _announce_portable_channel(manifest, installation.channel)
            return
        output.fatal("The requested release is not newer than the installed portable version.")
    output.info(
        f"rvs {target_version} is available for {installation.target} "
        f"(installed: {installation.version}; {release_label})."
    )
    command = (
        f"rvs update --candidate {target_version} --apply"
        if candidate is not None
        else (f"rvs update --to {to} --apply" if to is not None else "rvs update --apply")
    )
    if not apply:
        output.info(f"Install it with: {command}")
        if manifest is not None and to is None:
            _announce_portable_channel(manifest, installation.channel)
        return
    if installation.scope == "system" and os.name != "nt" and os.geteuid() != 0:
        output.fatal(f"System-wide portable updates require root privileges. Run: sudo {command}")
    if not yes:
        typer.confirm(f"Install signed rvs {target_version} for {installation.target}?", abort=True)
    try:
        asynchronous = apply_portable_update(
            installation, target_version, candidate=candidate is not None
        )
    except (OSError, UpdateError) as exc:
        output.fatal(str(exc))
    if asynchronous:
        output.success(
            f"Verified and staged rvs {target_version}. It will activate after this command exits."
        )
    else:
        output.success(f"Updated rvs to {target_version}.")


def _announce_portable_channel(manifest: dict[str, Any], current: str) -> None:
    recommended = manifest["recommended"]
    if channel_order(recommended) <= channel_order(current):
        return
    latest = manifest["channels"][recommended]["latest"]
    output.info(
        f"rvs {latest} is available in release channel {recommended}. Review the migration "
        f"notes, then run: rvs update --to {recommended.removeprefix('v')}"
    )


def _is_nix_application_executable(value: str) -> bool:
    normalized = value.lower().replace("\\", "/")
    return re.search(rf"/nix/store/[a-z0-9]+-{PACKAGE_NAME}-env/", normalized) is not None


def _managed_method() -> str | None:
    executable = Path(sys.executable).resolve()
    value = str(executable).lower().replace("\\", "/")
    if _is_nix_application_executable(value):
        return "nix"
    if not getattr(sys, "frozen", False):
        return None
    if "/cellar/" in value or "/caskroom/" in value:
        return "homebrew"
    if os.name == "nt" and "/microsoft/winget/packages/" in value:
        return "winget"
    return None


def _delegate_package_manager_update(
    *, to: str | None, candidate: str | None, apply: bool, yes: bool
) -> None:
    method = _managed_method()
    installed = _cli_version()
    if method is None:
        output.info(f"Ravenstash CLI {installed} is not a recognized managed installation.")
        output.info(
            "Install a managed release with: curl -fsSL https://ravenstash.com/install.sh | bash"
        )
        return
    if candidate is not None:
        output.fatal(f"Release candidates are not installed through {method}.")
    try:
        manifest = fetch_channel_manifest()
        current = compatibility_channel(installed)
        target, target_version, target_label = _requested_target(manifest, current, to)
    except (UpdateError, ValueError) as exc:
        output.fatal(str(exc))
    minor_selected = to is not None and "." in to.removeprefix("v")
    if minor_selected and method in {"homebrew", "winget"}:
        output.fatal(
            f"{method} cannot select an exact minor release without changing package state. "
            "Use the signed portable installer for this one-shot target."
        )
    if not newer(target_version, installed):
        if target_version == installed:
            output.success(
                f"Installed: rvs {installed}. Latest in {target_label}: rvs {target_version}."
            )
            if to is None:
                _announce_portable_channel(manifest, current)
            return
        output.fatal("The requested release is not newer than the installed managed version.")
    commands = {
        "homebrew": ["brew", "upgrade", homebrew_cask(target)],
        "winget": ["winget", "upgrade", "--exact", "--id", winget_package(target)],
    }
    command = commands.get(method)
    output.info(f"rvs {target_version} is available through {method} (installed: {installed}).")
    if not apply:
        if method == "nix":
            apply_command = (
                f"rvs update --to {to} --apply" if to is not None else "rvs update --apply"
            )
            output.info(
                f"Install it with: {apply_command} "
                f"(replaces the pinned Nix profile with tag v{target_version})"
            )
        elif target != current:
            output.info(
                f"Install it with: rvs update --to {target.removeprefix('v')} --apply "
                f"(replaces the {current} {method} package)"
            )
        else:
            assert command is not None
            output.info(f"Install it with: {' '.join(command)}")
        if to is None:
            _announce_portable_channel(manifest, current)
        return
    if not yes:
        typer.confirm(f"Install rvs {target_version} through {method}?", abort=True)
    if method == "nix":
        _replace_nix_profile(installed, target_version)
        return
    if target != current:
        _replace_managed_series(method, current, target)
        return
    assert command is not None
    executable = shutil.which(command[0])
    if executable is None:
        output.fatal(f"Cannot update because {command[0]} is unavailable.")
    result = subprocess.run([executable, *command[1:]], check=False)
    if result.returncode != 0:
        output.fatal(f"{method} could not install the rvs update.")
    output.success(f"{method} updated the rvs package; open a new shell and run rvs --version.")


def _replace_managed_series(method: str, current: str, target: str) -> None:
    packages = {
        "homebrew": (homebrew_cask(current), homebrew_cask(target)),
        "winget": (winget_package(current), winget_package(target)),
    }
    old_package, new_package = packages[method]
    executable_name = "brew" if method == "homebrew" else "winget"
    executable = shutil.which(executable_name)
    if executable is None:
        output.fatal(f"Cannot update because {executable_name} is unavailable.")
    exact = ["--exact", "--id"] if method == "winget" else []
    removed = subprocess.run([executable, "uninstall", *exact, old_package], check=False)
    if removed.returncode != 0:
        output.fatal(f"{method} could not remove the existing {old_package} package.")
    install_result = subprocess.run([executable, "install", *exact, new_package], check=False)
    if install_result.returncode == 0:
        output.success(
            f"{method} moved rvs from {current} to {target}; "
            "open a new shell and run rvs --version."
        )
        return
    output.warn(f"{method} could not install {new_package}; restoring {old_package}.")
    restored = subprocess.run([executable, "install", *exact, old_package], check=False)
    if restored.returncode != 0:
        restore_command = " ".join([executable_name, "install", *exact, old_package])
        output.fatal(
            f"{method} series migration and rollback both failed. "
            f"Restore the package with: {restore_command}"
        )
    output.fatal(f"{method} could not move rvs to {target}; the {current} package was restored.")


def _replace_nix_profile(installed: str, target: str) -> None:
    """Replace a tag-pinned Nix profile element and restore it on failure."""

    executable = shutil.which("nix")
    if executable is None:
        output.fatal("Cannot update because nix is unavailable.")
    removed = subprocess.run([executable, "profile", "remove", PACKAGE_NAME], check=False)
    if removed.returncode != 0:
        output.fatal(f"Nix could not remove the existing {PACKAGE_NAME} profile element.")
    repository = NIX_FLAKE
    installed_result = subprocess.run(
        [executable, "profile", "install", f"{repository}/v{target}"], check=False
    )
    if installed_result.returncode == 0:
        output.success(
            f"Nix updated the rvs profile to {target}; open a new shell and run rvs --version."
        )
        return
    output.warn(f"Nix could not install rvs {target}; restoring rvs {installed}.")
    restored = subprocess.run(
        [executable, "profile", "install", f"{repository}/v{installed}"], check=False
    )
    if restored.returncode != 0:
        output.fatal(
            "Nix update and rollback both failed. Restore the profile with: "
            f"nix profile install {repository}/v{installed}"
        )
    output.fatal(f"Nix could not install rvs {target}; rvs {installed} was restored.")


def update(
    to: str | None = typer.Option(
        None,
        "--to",
        help="Select a release channel (0/v0) or latest patch in a minor line (0.15).",
    ),
    candidate: str | None = typer.Option(
        None,
        "--candidate",
        help="Verify a signed GitHub release candidate; install with --apply.",
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Download and install the selected update.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Do not ask for confirmation when used with --apply.",
    ),
) -> None:
    """Check for a Ravenstash CLI update, or install it with --apply."""
    if yes and not apply:
        output.fatal("--yes requires --apply.")
    if candidate is not None and to is not None:
        output.fatal("--candidate and --to cannot be used together.")
    if candidate is not None:
        if _CANDIDATE_PATTERN.fullmatch(candidate) is None:
            output.fatal("Candidate versions must look like 0.15.1rc1.")
        installed, _apt_candidate = _apt_versions()
        if installed is not None:
            _update_candidate(candidate, apply=apply, yes=yes, installed_version=installed)
        else:
            _portable_update(to=None, candidate=candidate, apply=apply, yes=yes)
        return
    if to is not None:
        installed, _apt_candidate = _apt_versions()
        if installed is not None:
            _update_series(to, apply=apply, yes=yes, installed_version=installed)
        else:
            _portable_update(to=to, candidate=None, apply=apply, yes=yes)
        return
    installed, candidate = _apt_versions()
    if installed is None:
        _portable_update(to=None, candidate=None, apply=apply, yes=yes)
        return

    current_channel = _current_channel()
    # The signed channel manifest is current even when the local APT indexes are
    # not; APT stays the authority for what is installed.
    manifest = _channel_manifest() if current_channel else None
    latest = _channel_latest(manifest, current_channel)
    target = latest or candidate
    if target is None:
        output.fatal(
            "APT has no rvs candidate. Check that the Ravenstash source is configured, "
            "then run `sudo apt-get update`."
        )

    if not _upgrade_available(installed, target):
        if current_channel:
            output.success(
                f"Installed: rvs {installed}. Latest in release channel "
                f"{current_channel}: rvs {target}. You are up to date."
            )
        else:
            output.success(
                f"Installed: rvs {installed}. Latest available: rvs {target}. You are up to date."
            )
        if latest is None:
            output.info(
                "This check used APT's local package list because the signed release "
                "channel could not be read. Run `sudo apt-get update` to refresh it."
            )
        _announce_new_channel(current_channel, manifest)
        return

    if current_channel and not version_matches_channel(target, current_channel):
        output.fatal(
            f"APT candidate {target} does not belong to configured release channel "
            f"{current_channel}."
        )
    output.info(f"rvs {target} is available (installed: {installed}).")
    if not apply:
        output.info("Install it with: rvs update --apply")
        _announce_new_channel(current_channel, manifest)
        return
    if not _APT_GET.is_file():
        output.fatal("Cannot update because /usr/bin/apt-get is unavailable.")
    if not yes:
        typer.confirm(f"Install signed rvs package {target}?", abort=True)

    if not _refresh_ravenstash_source():
        output.fatal("APT metadata refresh for the Ravenstash source failed.")
    refreshed_installed, refreshed_candidate = _apt_versions()
    if refreshed_installed != installed:
        output.fatal("The installed package changed during refresh; run rvs update again.")
    if refreshed_candidate is None or _upgrade_available(refreshed_candidate, target):
        output.fatal(
            f"APT does not offer rvs {target} after refreshing the Ravenstash source. "
            f"Check for an APT pin or hold on {PACKAGE_NAME}, or retry later if you use a mirror."
        )
    if refreshed_candidate != target:
        output.fatal(
            f"APT now offers rvs {refreshed_candidate} instead of {target}; "
            "run rvs update again to review it."
        )
    if (
        _run_visible(
            [str(_APT_GET), "install", "--only-upgrade", "--yes", f"{PACKAGE_NAME}={target}"]
        ).returncode
        != 0
    ):
        _restore_apt_version(installed, target)
    output.success(f"Updated rvs to {target}.")


def _update_series(
    to: str, *, apply: bool, yes: bool, installed_version: str | None = None
) -> None:
    """Install a selected rolling channel or exact minor-line target."""
    installed = installed_version
    if installed is None:
        installed, _candidate = _apt_versions()
    current = _current_channel()
    if installed is None or current is None:
        output.fatal("rvs is not managed by a recognized Ravenstash APT source.")
    manifest = _channel_manifest()
    if manifest is None:
        output.fatal("Cannot authenticate the Ravenstash release-channel manifest.")
    try:
        _target, target_version, target_label = _requested_target(manifest, current, to)
    except (UpdateError, ValueError) as exc:
        output.fatal(str(exc))
    if not _upgrade_available(installed, target_version):
        output.fatal("The target release is not newer than the installed package.")
    output.info(f"Target: {target_label}, rvs {target_version}")
    if not apply:
        output.info(f"Install it with: rvs update --to {to} --apply")
        return
    if not yes:
        typer.confirm(f"Install rvs {target_version} from {target_label}?", abort=True)
    if not _refresh_ravenstash_source():
        output.fatal("APT metadata refresh for the Ravenstash source failed.")

    installed_after_refresh, _candidate = _apt_versions()
    if installed_after_refresh != installed:
        output.fatal("The installed package changed during refresh; run rvs update again.")
    if (
        _run_visible(
            [
                str(_APT_GET),
                "install",
                "--only-upgrade",
                "--yes",
                f"{PACKAGE_NAME}={target_version}",
            ]
        ).returncode
        != 0
    ):
        _restore_apt_version(installed, target_version)
    output.success(f"Updated rvs to {target_version} from {target_label}.")


def _restore_apt_version(installed: str, target: str) -> NoReturn:
    """Report a failed exact-version install after restoring the prior package."""

    current, _candidate = _apt_versions()
    if current == installed:
        output.fatal(f"APT could not install rvs {target}; rvs {installed} is still installed.")
    if "~" in installed:
        # Release candidates and test builds never enter an APT suite.
        output.fatal(
            f"APT could not install rvs {target}; rvs {installed} is not published to APT "
            "and was not restored."
        )
    output.warn(f"APT could not install rvs {target}; restoring rvs {installed}.")
    package = f"{PACKAGE_NAME}={installed}"
    restore = [str(_APT_GET), "install", "--yes", "--allow-downgrades", package]
    if _run_visible(restore).returncode != 0:
        output.fatal(
            f"APT could not install rvs {target}, and restoring rvs {installed} also failed. "
            f"Restore the package with: sudo apt-get install --allow-downgrades {package}"
        )
    output.fatal(f"APT could not install rvs {target}; rvs {installed} was restored.")
