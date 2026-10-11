from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
APPEND = ROOT / "packaging/repository/append_and_sign_apt.sh"
RECOVER = ROOT / "packaging/repository/recover_missing_apt_indexes.py"
VERIFY = ROOT / "packaging/repository/verify_apt.py"


def _run(command: list[str], *, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout


def _build_deb(root: Path, version: str, architecture: str) -> Path:
    package = root / f"package-{version}-{architecture}"
    (package / "DEBIAN").mkdir(parents=True)
    (package / "usr/bin").mkdir(parents=True)
    (package / "DEBIAN/control").write_text(
        "\n".join(
            (
                "Package: ravenstash-cli",
                f"Version: {version}",
                f"Architecture: {architecture}",
                "Section: utils",
                "Priority: optional",
                "Maintainer: Ravenstash Test <test@example.com>",
                "Description: repository test package",
                "",
            )
        ),
        encoding="utf-8",
    )
    executable = package / "usr/bin/rvs"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    output = root / f"ravenstash-cli_{version}_{architecture}.deb"
    _run(["dpkg-deb", "--build", str(package), str(output)])
    return output


def _exercise_multarch_append(root: Path) -> None:
    gpg_home = root / "gnupg"
    gpg_home.mkdir(mode=0o700)
    base_env = os.environ | {"GNUPGHOME": str(gpg_home)}
    _run(
        [
            "gpg",
            "--batch",
            "--passphrase",
            "",
            "--quick-generate-key",
            "Repository Test <test@example.com>",
            "rsa2048",
            "sign",
            "0",
        ],
        env=base_env,
    )
    listing = _run(["gpg", "--batch", "--with-colons", "--list-secret-keys"], env=base_env)
    fingerprint = next(
        line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")
    )
    keyring = root / "trusted.gpg"
    with keyring.open("wb") as stream:
        subprocess.run(
            ["gpg", "--batch", "--export", fingerprint],
            check=True,
            stdout=stream,
            env=base_env,
        )

    common_env = base_env | {
        "RAVENSTASH_CLI_APT_GPG_KEY_ID": fingerprint,
        "RAVENSTASH_CLI_APT_GPG_FINGERPRINT": fingerprint,
    }
    bootstrap = root / "bootstrap"
    bootstrap.mkdir()
    (bootstrap / "BOOTSTRAP").touch()
    current = root / "current"
    _run(
        [
            str(APPEND),
            str(bootstrap),
            str(current),
            str(keyring),
            str(_build_deb(root, "0.15.3", "amd64")),
        ],
        env=common_env | {"RAVENSTASH_CLI_APT_CHANNEL": "v0"},
    )
    amd64 = root / "amd64"
    _run(
        [
            str(APPEND),
            str(current),
            str(amd64),
            str(keyring),
            str(_build_deb(root, "0.16.0", "amd64")),
        ],
        env=common_env | {"RAVENSTASH_CLI_APT_CHANNEL": "v0"},
    )
    amd64_packages = (amd64 / "dists/v0/main/binary-amd64/Packages").read_text()
    assert "Version: 0.15.3" in amd64_packages
    assert "Version: 0.16.0" in amd64_packages
    multarch = root / "multarch"
    _run(
        [
            str(APPEND),
            str(amd64),
            str(multarch),
            str(keyring),
            str(_build_deb(root, "0.16.0", "arm64")),
        ],
        env=common_env | {"RAVENSTASH_CLI_APT_CHANNEL": "v0"},
    )

    assert "Architecture: arm64" in (multarch / "dists/v0/main/binary-arm64/Packages").read_text()

    for binary in (multarch / "dists").glob("*/main/binary-arm64"):
        shutil.rmtree(binary)
    _run(["python3", str(RECOVER), str(multarch), str(keyring)])
    _run(["python3", str(VERIFY), str(multarch), str(keyring)])
    assert "Architecture: arm64" in (multarch / "dists/v0/main/binary-arm64/Packages").read_text()


def _exercise_single_pass_multarch_append(root: Path) -> None:
    real_apt_ftparchive = shutil.which("apt-ftparchive")
    assert real_apt_ftparchive is not None
    counter = root / "apt-ftparchive-packages.count"
    wrapper_directory = root / "bin"
    wrapper_directory.mkdir()
    wrapper = wrapper_directory / "apt-ftparchive"
    wrapper.write_text(
        "#!/bin/sh\n"
        'for argument in "$@"; do\n'
        '  if [ "$argument" = packages ]; then printf x >> "$APT_COUNTER"; fi\n'
        "done\n"
        f'exec {shlex.quote(real_apt_ftparchive)} "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    gpg_home = root / "gnupg"
    gpg_home.mkdir(mode=0o700)
    base_env = os.environ | {
        "APT_COUNTER": str(counter),
        "GNUPGHOME": str(gpg_home),
        "PATH": f"{wrapper_directory}{os.pathsep}{os.environ['PATH']}",
    }
    _run(
        [
            "gpg",
            "--batch",
            "--passphrase",
            "",
            "--quick-generate-key",
            "Repository Test <test@example.com>",
            "rsa2048",
            "sign",
            "0",
        ],
        env=base_env,
    )
    listing = _run(["gpg", "--batch", "--with-colons", "--list-secret-keys"], env=base_env)
    fingerprint = next(
        line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")
    )
    keyring = root / "trusted.gpg"
    with keyring.open("wb") as stream:
        subprocess.run(
            ["gpg", "--batch", "--export", fingerprint],
            check=True,
            stdout=stream,
            env=base_env,
        )

    bootstrap = root / "bootstrap"
    bootstrap.mkdir()
    (bootstrap / "BOOTSTRAP").touch()
    repository = root / "repository"
    _run(
        [
            str(APPEND),
            str(bootstrap),
            str(repository),
            str(keyring),
            str(_build_deb(root, "0.15.3", "amd64")),
            str(_build_deb(root, "0.15.3", "arm64")),
        ],
        env=base_env
        | {
            "RAVENSTASH_CLI_APT_GPG_KEY_ID": fingerprint,
            "RAVENSTASH_CLI_APT_GPG_FINGERPRINT": fingerprint,
            "RAVENSTASH_CLI_APT_CHANNEL": "v0",
        },
    )
    _run(["python3", str(VERIFY), str(repository), str(keyring)])
    for architecture in ("amd64", "arm64"):
        packages = repository / f"dists/v0/main/binary-{architecture}/Packages"
        assert f"Architecture: {architecture}" in packages.read_text()
    assert counter.read_text() == "x"


class AppendMultiarchTests(unittest.TestCase):
    @unittest.skipIf(
        any(shutil.which(command) is None for command in ("apt-ftparchive", "dpkg-deb", "gpg")),
        "APT repository tooling is unavailable",
    )
    def test_new_architecture_keeps_older_minor_packages_in_rolling_channel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _exercise_multarch_append(Path(directory))

    @unittest.skipIf(
        any(shutil.which(command) is None for command in ("apt-ftparchive", "dpkg-deb", "gpg")),
        "APT repository tooling is unavailable",
    )
    def test_both_architectures_are_appended_in_one_generation_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _exercise_single_pass_multarch_append(Path(directory))


if __name__ == "__main__":
    unittest.main()
