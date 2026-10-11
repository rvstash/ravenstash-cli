"""Keep every published name in agreement with the release identity.

``rvs/identity.py`` is the runtime source of the names under which the CLI is
published, installed, and updated, and ``packaging/identity.env`` is the build
and release source. These tests fail when the two disagree, when a standalone
installer, a workflow literal, a generated manifest, or a documented link drifts
from them, or when ``rvs`` is used as a package, asset, or storage name.

``rvs`` is the name of the command, the Python package, and the user-facing
configuration. ``ALLOWED`` lists those forms with the reason for each one.
"""

import ast
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

import pytest
import yaml
from rvs import entrypoint, identity, update


ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
INSTALLER = ROOT / "packaging" / "install.sh"
WINDOWS_INSTALLER = ROOT / "packaging" / "install.ps1"
IDENTITY_ENV = ROOT / "packaging" / "identity.env"

# The command name, spelled once so that the naming patterns below never match
# their own source. It is the name of the command, never of a distribution.
CMD = "rvs"
CMD_ENV = CMD.upper()

# packaging/identity.env key -> the value rvs/identity.py holds for the same name.
BUILD_IDENTITY = {
    "RAVENSTASH_CLI_REPOSITORY": identity.REPOSITORY,
    "RAVENSTASH_CLI_PACKAGE": identity.PACKAGE_NAME,
    "RAVENSTASH_CLI_ASSET_STEM": identity.ASSET_STEM,
    "RAVENSTASH_CLI_NAMESPACE": identity.DISTRIBUTION_NAMESPACE,
    "RAVENSTASH_CLI_RELEASES_URL": identity.RELEASES_URL,
    "RAVENSTASH_CLI_APT_URL": identity.APT_REPOSITORY_URL,
    "RAVENSTASH_CLI_CHANNELS_URL": identity.CHANNELS_URL,
    "RAVENSTASH_CLI_SIGNING_KEY": identity.SIGNING_KEY_NAME,
    "RAVENSTASH_CLI_KEYRING": identity.KEYRING_NAME,
    "RAVENSTASH_CLI_SOURCE_LIST": identity.SOURCE_LIST_NAME,
    "RAVENSTASH_CLI_RECEIPT": identity.RECEIPT_NAME,
    "RAVENSTASH_CLI_HOMEBREW_CASK": identity.HOMEBREW_CASK,
    "RAVENSTASH_CLI_WINGET_PACKAGE": identity.WINGET_PACKAGE,
}
# Build-only names with no runtime counterpart.
BUILD_ONLY_IDENTITY = {"RAVENSTASH_CLI_STORAGE_BUCKET": "ravenstash-cli-releases"}

# Inputs of the build and release scripts. They share the identity prefix but
# are parameters of one invocation, so identity.env must never define them.
BUILD_INPUTS = frozenset(
    {
        "RAVENSTASH_CLI_VERSION",
        "RAVENSTASH_CLI_ARCH",
        "RAVENSTASH_CLI_BUILDER_IMAGE",
        "RAVENSTASH_CLI_CODESIGN_IDENTITY",
        "RAVENSTASH_CLI_CERTIFICATION_BUNDLE",
        "RAVENSTASH_CLI_APT_CHANNEL",
        "RAVENSTASH_CLI_APT_COMPONENT",
        "RAVENSTASH_CLI_APT_VALID_DAYS",
        "RAVENSTASH_CLI_APT_REFRESH_ALL",
        "RAVENSTASH_CLI_APT_GPG_KEY_ID",
        "RAVENSTASH_CLI_APT_GPG_FINGERPRINT",
        "RAVENSTASH_CLI_APT_GPG_PASSPHRASE_FILE",
    }
)
SIGNING_SECRETS = frozenset(
    {"RAVENSTASH_CLI_APT_GPG_PRIVATE_KEY", "RAVENSTASH_CLI_APT_GPG_PASSPHRASE"}
)
STORAGE_SECRETS = frozenset(
    {"R2_APT_ACCESS_KEY_ID", "R2_APT_SECRET_ACCESS_KEY", "R2_APT_ACCOUNT_ID"}
)
DELIVERY_SECRETS = frozenset({"CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"})
PUBLISHING_VARIABLE = "RAVENSTASH_CLI_PUBLISHING"

COMMANDS = ("rvs", "ravenstash", "docker-credential-rvs")
RELEASE_TARGETS = (
    "linux-amd64",
    "linux-arm64",
    "linux-musl-amd64",
    "linux-musl-arm64",
    "macos-amd64",
    "macos-arm64",
    "windows-amd64",
    "windows-arm64",
)
LOAD_IDENTITY = "grep -E '^[A-Z_]+=' packaging/identity.env >> \"$GITHUB_ENV\""


def _build_identity() -> dict[str, str]:
    lines = IDENTITY_ENV.read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _workflow_names() -> list[str]:
    return sorted(path.name for path in WORKFLOWS.glob("*.yml"))


# ── The two sources ───────────────────────────────────────────────────────────


def test_build_identity_matches_the_runtime_identity() -> None:
    assert _build_identity() == BUILD_IDENTITY | BUILD_ONLY_IDENTITY


def test_identity_values_follow_the_naming_contract() -> None:
    assert identity.PRODUCT_NAME == "Ravenstash CLI"
    assert identity.REPOSITORY == "rvstash/ravenstash-cli"
    assert identity.RELEASE_API == "https://api.github.com/repos/rvstash/ravenstash-cli"
    assert identity.RELEASE_DOWNLOAD_ROOT == (
        "https://github.com/rvstash/ravenstash-cli/releases/download"
    )
    assert identity.NIX_FLAKE == "github:rvstash/ravenstash-cli"
    assert identity.PACKAGE_NAME == identity.ASSET_STEM == "ravenstash-cli"
    assert identity.APT_REPOSITORY_URL == "https://releases.ravenstash.com/ravenstash/apt"
    assert identity.CHANNELS_URL == "https://releases.ravenstash.com/ravenstash/channels.json"
    assert identity.APT_KEYRING_PATH == "/etc/apt/keyrings/ravenstash-cli.gpg"
    assert identity.APT_SOURCE_PATH == "/etc/apt/sources.list.d/ravenstash-cli.list"
    assert identity.SIGNING_KEY_NAME == "ravenstash-cli.asc"
    assert identity.RECEIPT_NAME == "ravenstash-cli-install.json"
    assert identity.homebrew_cask("v0") == "ravenstash-cli@0"
    assert identity.winget_package("v0") == "Ravenstash.RavenstashCLI.v0"


def test_release_asset_names_follow_the_naming_contract() -> None:
    assert identity.debian_package_name("0.15.0", "amd64") == "ravenstash-cli_0.15.0_amd64.deb"
    assert identity.archive_root("0.15.0", "linux-musl-arm64") == (
        "ravenstash-cli-v0.15.0-linux-musl-arm64"
    )
    assert identity.archive_name("0.15.0", "macos-arm64") == (
        "ravenstash-cli-v0.15.0-macos-arm64.tar.gz"
    )
    assert identity.archive_name("0.15.0", "windows-amd64") == (
        "ravenstash-cli-v0.15.0-windows-amd64.zip"
    )
    assert identity.checksums_name("0.15.0rc1") == "ravenstash-cli-v0.15.0rc1-checksums.txt"


def test_build_identity_is_plain_key_value_text() -> None:
    for line in IDENTITY_ENV.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            # Bash, Python, and the workflow environment file must all read the
            # same value, so nothing may need quoting or expansion.
            assert re.fullmatch(r"RAVENSTASH_CLI_[A-Z_]+=[A-Za-z0-9._:/-]+", line), line
    assert b"\r" not in IDENTITY_ENV.read_bytes()
    assert "packaging/identity.env text eol=lf" in _read(".gitattributes").splitlines()
    assert not BUILD_INPUTS & _build_identity().keys()
    assert not (SIGNING_SECRETS | {PUBLISHING_VARIABLE}) & _build_identity().keys()


def test_every_reader_of_the_build_identity_sees_the_same_values() -> None:
    expected = _build_identity()
    readers = {
        "packaging/scripts": "import release_identity; values = release_identity.load()",
        "packaging/repository": "import channel_policy; values = channel_policy.IDENTITY",
    }
    for directory, statement in readers.items():
        printed = subprocess.run(
            [sys.executable, "-c", f"import json; {statement}; print(json.dumps(values))"],
            check=True,
            capture_output=True,
            text=True,
            cwd=ROOT / directory,
        ).stdout
        assert json.loads(printed) == expected, directory


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None, reason="the packaging scripts are POSIX-only"
)
def test_packaging_shell_scripts_source_the_build_identity() -> None:
    printed = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1" && printf "%s\\n" "${!RAVENSTASH_CLI_@}"',
            "bash",
            IDENTITY_ENV,
        ],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"]},
    ).stdout
    assert set(printed.split()) == _build_identity().keys()

    sources = {
        "packaging/scripts/common.sh": 'source "$ROOT/packaging/identity.env"',
        "packaging/repository/append_and_sign_apt.sh": 'source "$script_dir/../identity.env"',
        "packaging/repository/prepare_keyring.sh": 'source "$script_dir/../identity.env"',
        "packaging/repository/publish_apt.sh": '/../identity.env"',
    }
    for path, statement in sources.items():
        assert statement in _read(path), path
    for script in sorted((ROOT / "packaging" / "scripts").glob("*.sh")):
        if script.name not in {"common.sh", "postinstall.sh"}:
            assert "/common.sh" in script.read_text(encoding="utf-8"), script.name


def test_only_the_identity_module_spells_the_runtime_identity() -> None:
    spelled = re.compile(
        "|".join(
            re.escape(value)
            for value in (
                identity.REPOSITORY,
                identity.PACKAGE_NAME,
                "releases.ravenstash.com",
                identity.WINGET_PACKAGE,
                "/etc/apt/",
            )
        )
    )
    offenders = [
        f"{path.relative_to(ROOT).as_posix()}:{number}"
        for path in sorted((ROOT / "rvs").rglob("*.py"))
        if path.relative_to(ROOT).as_posix() != "rvs/identity.py"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if spelled.search(line)
    ]

    assert offenders == []


def test_entry_point_takes_its_identity_from_the_identity_module(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The identity module imports nothing, so the --version fast path can afford it.
    assert not [
        node
        for node in ast.walk(ast.parse(_read("rvs/identity.py")))
        if isinstance(node, ast.Import | ast.ImportFrom)
    ]
    assert entrypoint.PACKAGE_NAME is identity.PACKAGE_NAME
    assert entrypoint.PRODUCT_NAME is identity.PRODUCT_NAME
    assert identity.PRODUCT_NAME not in _read("rvs/entrypoint.py")

    queried: list[str] = []
    monkeypatch.setattr(entrypoint, "PACKAGE_NAME", "example-distribution")
    monkeypatch.setattr(entrypoint, "PRODUCT_NAME", "Example Product")
    monkeypatch.setattr(
        importlib.metadata, "version", lambda distribution: queried.append(distribution) or "1.2.3"
    )
    monkeypatch.setattr(sys, "argv", ["rvs", "--version"])

    entrypoint.main()

    assert queried == ["example-distribution"]
    assert capsys.readouterr() == ("Example Product 1.2.3\n", "")


def test_python_project_carries_the_identity() -> None:
    project = tomllib.loads(_read("pyproject.toml"))

    assert project["project"]["name"] == identity.PACKAGE_NAME
    assert project["project"]["urls"]["Repository"] == identity.REPOSITORY_URL
    assert project["project"]["urls"]["Issues"] == f"{identity.REPOSITORY_URL}/issues"
    assert set(project["project"]["scripts"]) == set(COMMANDS)
    assert project["tool"]["setuptools"]["package-data"] == {
        "rvs.resources": [identity.SIGNING_KEY_NAME]
    }
    assert (ROOT / "rvs" / "resources" / identity.SIGNING_KEY_NAME).is_file()
    assert (ROOT / "packaging/repository/keys" / identity.SIGNING_KEY_NAME).is_file()
    assert sorted(path.name for path in (ROOT / "packaging/repository/keys").glob("*.asc")) == [
        identity.SIGNING_KEY_NAME
    ]


def test_frozen_bundle_ships_the_identity_resources() -> None:
    spec = _read("packaging/pyinstaller/rvs.spec")

    assert f'ROOT / "rvs" / "resources" / "{identity.SIGNING_KEY_NAME}"' in spec
    assert f'    "{identity.PACKAGE_NAME}",\n' in spec
    assert 'os.environ.get("RAVENSTASH_CLI_CODESIGN_IDENTITY")' in spec


# ── Standalone installers ─────────────────────────────────────────────────────


def _shell_constants(source: str) -> dict[str, str]:
    """Resolve the ``readonly name="value"`` literals at the top of a script."""
    constants: dict[str, str] = {}
    for name, value in re.findall(r'^readonly ([a-z_]+)="([^"]*)"$', source, flags=re.MULTILINE):
        constants[name] = re.sub(
            r"\$\{([a-z_]+)\}", lambda match: constants.get(match[1], match[0]), value
        )
    return constants


def _powershell_constants(source: str) -> dict[str, str]:
    return dict(re.findall(r'^\$([A-Za-z]+) = "([^"$]*)"$', source, flags=re.MULTILINE))


def test_shell_installer_embeds_the_identity() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    constants = _shell_constants(source)

    assert constants["repository_url"] == identity.APT_REPOSITORY_URL
    assert constants["github_repository"] == identity.REPOSITORY
    assert constants["github_release_url"] == identity.RELEASE_DOWNLOAD_ROOT
    assert constants["package_name"] == identity.PACKAGE_NAME
    assert constants["asset_stem"] == identity.ASSET_STEM
    assert constants["keyring_name"] == identity.KEYRING_NAME
    assert constants["signing_key_url"] == f"{identity.APT_REPOSITORY_URL}/{identity.KEYRING_NAME}"
    assert constants["keyring_path"] == identity.APT_KEYRING_PATH
    assert constants["source_path"] == identity.APT_SOURCE_PATH
    assert constants["receipt_name"] == identity.RECEIPT_NAME
    # Every other mention goes through those constants.
    assert source.count(identity.REPOSITORY) == 1
    assert source.count("releases.ravenstash.com") == 1
    assert "https://api.github.com/repos/${github_repository}/releases/tags/v" in source
    assert "nix profile install github:${github_repository}/v${release_version}" in source
    assert 'apt-get install -y -qq "$package_name"' in source
    assert '"${asset_stem}-v${selected_release_version}-${system_name}-${architecture}"' in source
    assert 'local archive_name="${archive_prefix}.tar.gz"' in source
    assert 'local checksums_name="${asset_stem}-v${selected_release_version}-checksums.txt"' in (
        source
    )
    assert '--output "${archive_root}/${receipt_name}"' in source
    assert f'"${{RVS_INSTALL_ROOT:-${{HOME}}/.local/share/{identity.PACKAGE_NAME}}}"' in source
    assert f'"${{RVS_INSTALL_ROOT:-/opt/{identity.PACKAGE_NAME}}}"' in source
    for command in COMMANDS:
        assert f'"${{install_root}}/current/{command}" "${{bin_directory}}/{command}"' in source


def test_shell_installer_writes_the_apt_source_the_updater_recognizes() -> None:
    constants = _shell_constants(INSTALLER.read_text(encoding="utf-8"))
    template = re.search(
        r'^readonly expected_source="(.+)"$', INSTALLER.read_text(encoding="utf-8"), re.MULTILINE
    )
    assert template is not None

    for architecture in ("amd64", "arm64"):
        line = re.sub(
            r"\$\{([a-z_]+)\}",
            lambda match, architecture=architecture: (
                constants | {"package_architecture": architecture}
            )[match[1]],
            template[1],
        )
        recognized = update._SOURCE_PATTERN.fullmatch(line)
        assert recognized is not None, line
        assert recognized["channel"] == constants["compatibility_channel"]


def test_windows_installer_embeds_the_identity() -> None:
    source = WINDOWS_INSTALLER.read_text(encoding="utf-8")
    constants = _powershell_constants(source)

    assert constants["Repository"] == identity.REPOSITORY
    assert constants["AssetStem"] == identity.ASSET_STEM
    assert constants["ReceiptName"] == identity.RECEIPT_NAME
    assert source.count(identity.REPOSITORY) == 1
    assert '"https://github.com/$Repository/releases/download/v$ReleaseVersion"' in source
    assert '"https://api.github.com/repos/$Repository/releases/tags/v$ReleaseVersion"' in source
    assert '$archiveRoot = "$AssetStem-v$ReleaseVersion-windows-$architecture"' in source
    assert '$archiveName = "$archiveRoot.zip"' in source
    assert '$checksumsName = "$AssetStem-v$ReleaseVersion-checksums.txt"' in source
    assert "Join-Path $temporary $archiveRoot" in source
    assert source.count(f'"Ravenstash\\{identity.PACKAGE_NAME}"') == 2
    assert source.count("$ReceiptName") == 3
    for command in COMMANDS:
        assert f'"{command}.exe"' in source


def _installer_floor() -> str:
    """Return the arithmetic test that guards ``RVS_INSTALL_VERSION``."""
    source = INSTALLER.read_text(encoding="utf-8")
    guard = re.search(
        r"^  (\(\( BASH_REMATCH.+ \)\)) \\\n    \|\| fail \"(RVS_INSTALL_VERSION must be [^\"]+)\"$",
        source,
        re.MULTILINE,
    )
    assert guard is not None
    assert guard[2].startswith("RVS_INSTALL_VERSION must be 0.15.0 or later")
    return guard[1]


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None, reason="the shell installer is POSIX-only"
)
@pytest.mark.parametrize(
    ("version", "accepted"),
    [
        ("0.15.0", True),
        ("0.15.0rc1", True),
        ("0.15.7", True),
        ("0.16.0", True),
        ("1.0.0", True),
        ("0.14.99", False),
        ("0.1.0", False),
        ("0.9.0", False),
    ],
)
def test_shell_installer_selects_no_version_before_the_first_release(
    version: str, accepted: bool
) -> None:
    pattern = r"^([0-9]+)\.([0-9]+)\.([0-9]+)(rc[1-9][0-9]*)?$"
    result = subprocess.run(
        ["bash", "-c", f'[[ "$1" =~ {pattern} ]] && {_installer_floor()}', "bash", version],
        check=False,
    )

    assert (result.returncode == 0) is accepted


@pytest.mark.skipif(os.name == "nt", reason="the shell installer is POSIX-only")
def test_shell_installer_explains_the_first_release_floor() -> None:
    result = subprocess.run(
        ["bash", str(INSTALLER)],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ | {"RVS_INSTALL_METHOD": "portable", "RVS_INSTALL_VERSION": "0.14.99"},
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        "rvs installer: error: RVS_INSTALL_VERSION must be 0.15.0 or later; "
        "0.15.0 is the first published release\n"
    )


def test_installers_keep_the_token_only_as_optional_github_authentication() -> None:
    for installer in (INSTALLER, WINDOWS_INSTALLER):
        source = installer.read_text(encoding="utf-8")
        assert "RVS_GITHUB_TOKEN" in source
        assert "private" not in source.lower()
    assert "`RVS_GITHUB_TOKEN` is optional and can raise GitHub API limits" in _read(
        "docs/linux-compatibility.md"
    )


# ── Debian package, archives, and release assets ──────────────────────────────


def test_debian_build_script_names_everything_from_the_identity() -> None:
    script = _read("packaging/scripts/build-deb.sh")
    control = script.split('cat > "$STAGING/DEBIAN/control" <<EOF\n', 1)[1].split("\nEOF\n", 1)[0]
    fields = dict(line.split(": ", 1) for line in control.splitlines() if not line.startswith(" "))

    assert 'PACKAGE="$RAVENSTASH_CLI_PACKAGE"' in script
    assert fields["Package"] == "${PACKAGE}"
    assert set(fields) == {
        "Package",
        "Version",
        "Architecture",
        "Maintainer",
        "Depends",
        "Section",
        "Priority",
        "Homepage",
        "Description",
    }
    assert '"dist/packages/${PACKAGE}_${VERSION}_${ARCH}.deb"' in script
    assert identity.PACKAGE_NAME not in script


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("dpkg-deb") is None, reason="dpkg-deb is unavailable"
)
def test_debian_package_carries_the_identity(tmp_path: Path) -> None:
    source = tmp_path / "source"
    scripts = source / "packaging" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("common.sh", "build-deb.sh", "postinstall.sh"):
        shutil.copy2(ROOT / "packaging" / "scripts" / name, scripts / name)
    shutil.copy2(IDENTITY_ENV, source / "packaging" / "identity.env")
    for document in ("README.md", "LICENSE", "NOTICE"):
        shutil.copy2(ROOT / document, source / document)
    frozen = source / "dist" / "pyinstaller" / "rvs"
    (frozen / "_internal").mkdir(parents=True)
    (frozen / "_internal" / "base_library.zip").write_bytes(b"library")
    (frozen / "rvs").write_text("#!/bin/sh\n", encoding="utf-8")
    (frozen / "rvs").chmod(0o755)
    for alias in ("ravenstash", "docker-credential-rvs"):
        (frozen / alias).symlink_to("rvs")

    built = subprocess.run(
        ["bash", "packaging/scripts/build-deb.sh"],
        check=False,
        capture_output=True,
        text=True,
        cwd=source,
        env=os.environ
        | {
            "RAVENSTASH_CLI_VERSION": "0.15.0rc1",
            "RAVENSTASH_CLI_ARCH": "arm64",
            "SOURCE_DATE_EPOCH": "0",
        },
    )
    assert built.returncode == 0, built.stderr

    package = source / "dist" / "packages" / identity.debian_package_name("0.15.0rc1", "arm64")
    assert [path.name for path in package.parent.iterdir()] == [package.name]
    control = subprocess.run(
        ["dpkg-deb", "--field", str(package)], check=True, capture_output=True, text=True
    ).stdout
    fields = dict(line.split(": ", 1) for line in control.splitlines() if not line.startswith(" "))
    assert fields["Package"] == identity.PACKAGE_NAME
    assert fields["Version"] == "0.15.0~rc1"
    assert fields["Architecture"] == "arm64"
    # The package stands alone: it declares no relation to any other package.
    assert not {"Provides", "Replaces", "Conflicts", "Breaks", "Pre-Depends"} & fields.keys()

    extracted = tmp_path / "extracted"
    subprocess.run(["dpkg-deb", "--extract", str(package), str(extracted)], check=True)
    payload = f"usr/lib/{identity.PACKAGE_NAME}"
    assert sorted(path.name for path in (extracted / "usr").iterdir()) == ["bin", "lib", "share"]
    assert [path.name for path in (extracted / "usr/lib").iterdir()] == [identity.PACKAGE_NAME]
    assert (extracted / payload / "rvs").is_file()
    assert sorted(path.name for path in (extracted / "usr/bin").iterdir()) == sorted(COMMANDS)
    assert os.readlink(extracted / "usr/bin/rvs") == f"../lib/{identity.PACKAGE_NAME}/rvs"
    assert os.readlink(extracted / "usr/bin/ravenstash") == f"../lib/{identity.PACKAGE_NAME}/rvs"
    assert os.readlink(extracted / "usr/bin/docker-credential-rvs") == (
        f"../lib/{identity.PACKAGE_NAME}/docker-credential-rvs"
    )
    documents = extracted / "usr/share/doc" / identity.PACKAGE_NAME
    assert [path.name for path in (extracted / "usr/share/doc").iterdir()] == [
        identity.PACKAGE_NAME
    ]
    assert sorted(path.name for path in documents.iterdir()) == ["LICENSE", "NOTICE", "README.md"]


def test_build_scripts_name_archives_and_assets_from_the_identity() -> None:
    tarball = _read("packaging/scripts/build-tarball.sh")
    artifacts = _read("packaging/scripts/build-release-artifacts.sh")
    portable = _read("packaging/scripts/build_portable.py")
    smoke = _read("packaging/scripts/smoke_portable.py")
    builder = _read("packaging/scripts/build-in-ubuntu20.sh")
    repository = _read("packaging/scripts/update-apt-repo.sh")
    append = _read("packaging/repository/append_and_sign_apt.sh")

    assert 'ARCHIVE_ROOT="${RAVENSTASH_CLI_ASSET_STEM}-v${VERSION}-linux-${ARCH}"' in tarball
    assert 'ARCHIVE="dist/release/${ARCHIVE_ROOT}.tar.gz"' in tarball
    assert 'STEM="$RAVENSTASH_CLI_ASSET_STEM"' in artifacts
    assert 'cp "dist/packages/${RAVENSTASH_CLI_PACKAGE}_${VERSION}_${ARCH}.deb" dist/release/' in (
        artifacts
    )
    assert 'install -m 0755 packaging/install.sh "dist/release/${STEM}-install.sh"' in artifacts
    assert '"dist/release/${STEM}-v${VERSION}-sbom.cdx.json"' in artifacts
    assert artifacts.count('"${STEM}-v${VERSION}-checksums.txt"') == 2
    assert 'archive_root = f"{ASSET_STEM}-v{version}-{system}-{architecture}"' in portable
    assert 'rf"{re.escape(ASSET_STEM)}-v(?P<version>.+)-"' in smoke
    assert (
        'BUILDER_IMAGE="${RAVENSTASH_CLI_BUILDER_IMAGE:-${RAVENSTASH_CLI_PACKAGE}-builder-ubuntu20}"'
        in builder
    )
    assert 'POOL_DIR="${APT_REPO_DIR}/pool/${COMPONENT}/${PACKAGE:0:1}/${PACKAGE}"' in repository
    assert 'pool="$output_repository/pool/main/${package:0:1}/$package"' in append
    assert 'test "$package_name" = "$package"' in append
    for script in (tarball, artifacts, portable, smoke, builder, repository, append):
        assert identity.PACKAGE_NAME not in script
    for script in sorted((ROOT / "packaging").glob("*/*.py")):
        assert identity.PACKAGE_NAME not in script.read_text(encoding="utf-8"), script.name


def test_package_manager_manifests_carry_the_identity(tmp_path: Path) -> None:
    version = "0.15.0"
    release = tmp_path / "release"
    release.mkdir()
    for target in ("macos-amd64", "macos-arm64", "windows-amd64", "windows-arm64"):
        (release / identity.archive_name(version, target)).write_bytes(target.encode())

    subprocess.run(
        [
            sys.executable,
            str(ROOT / "packaging/scripts/generate_package_manifests.py"),
            str(release),
            version,
            identity.REPOSITORY,
        ],
        check=True,
    )

    cask = identity.homebrew_cask("v0")
    winget = identity.winget_package("v0")
    with tarfile.open(release / f"{identity.ASSET_STEM}-v{version}-package-manifests.tar.gz") as (
        package
    ):
        manifests = {
            member.name: package.extractfile(member).read().decode()  # type: ignore[union-attr]
            for member in package.getmembers()
            if member.isfile()
        }
    assert sorted(manifests) == [
        f"package-manifests/homebrew/{cask}.rb",
        f"package-manifests/winget/{winget}.installer.yaml",
        f"package-manifests/winget/{winget}.locale.en-US.yaml",
        f"package-manifests/winget/{winget}.yaml",
    ]
    cask_text = manifests[f"package-manifests/homebrew/{cask}.rb"]
    assert cask_text.startswith(f'cask "{cask}" do\n')
    assert f'name "{identity.PRODUCT_NAME}"' in cask_text
    downloads = f"{identity.RELEASE_DOWNLOAD_ROOT}/v{version}"
    for target in ("macos-amd64", "macos-arm64"):
        assert f'url "{downloads}/{identity.archive_name(version, target)}"' in cask_text
    for command in COMMANDS:
        assert f'binary "{identity.ASSET_STEM}-v#{{version}}-macos-#{{arch}}/{command}"' in (
            cask_text
        )
    for suffix in ("", ".installer", ".locale.en-US"):
        manifest = yaml.safe_load(manifests[f"package-manifests/winget/{winget}{suffix}.yaml"])
        assert manifest["PackageIdentifier"] == winget
    installer = yaml.safe_load(manifests[f"package-manifests/winget/{winget}.installer.yaml"])
    assert [entry["InstallerUrl"] for entry in installer["Installers"]] == [
        f"{downloads}/{identity.archive_name(version, target)}"
        for target in ("windows-amd64", "windows-arm64")
    ]
    assert [
        nested["RelativeFilePath"] for nested in installer["Installers"][0]["NestedInstallerFiles"]
    ] == [
        f"{identity.archive_root(version, 'windows-amd64')}\\{command}.exe" for command in COMMANDS
    ]
    locale = yaml.safe_load(manifests[f"package-manifests/winget/{winget}.locale.en-US.yaml"])
    assert locale["PackageName"] == identity.PRODUCT_NAME


def test_nix_flake_exposes_the_package_under_the_identity() -> None:
    flake = _read("flake.nix")
    package = identity.PACKAGE_NAME

    assert f"          {package} = (mkApplication {{" in flake
    assert f"          default = {package};\n          inherit {package};\n" in flake
    assert f"          inherit (self.packages.${{system}}) {package};\n" in flake
    assert f"package = pythonSet.{package};" in flake
    assert f'mkVirtualEnv "{package}-env"' in flake
    assert re.search(rf"\b(?:inherit|default =) {CMD}\b|^\s*{CMD} = ", flake, re.MULTILINE) is None
    # The updater recognizes a Nix installation by that environment name.
    assert update._is_nix_application_executable(
        f"/nix/store/0123456789abcdfghijklmnpqrsvwxyz-{package}-env/bin/python3.14"
    )


# ── Workflows ─────────────────────────────────────────────────────────────────


def _jobs(name: str) -> dict[str, dict]:
    return _workflow(name)["jobs"]


def _uses_identity(step: dict) -> bool:
    return any(re.search(rf"\b{key}\b", json.dumps(step)) for key in _build_identity())


@pytest.mark.parametrize("name", _workflow_names())
def test_workflow_jobs_load_the_identity_before_using_it(name: str) -> None:
    for job_name, job in _jobs(name).items():
        steps = job.get("steps", [])
        users = [index for index, step in enumerate(steps) if _uses_identity(step)]
        loaders = [
            index for index, step in enumerate(steps) if step.get("name") == "Load release identity"
        ]
        if not users:
            assert loaders == [], f"{name}:{job_name} loads an identity it never uses"
            continue
        assert len(loaders) == 1, f"{name}:{job_name}"
        assert steps[loaders[0]]["run"] == LOAD_IDENTITY
        assert loaders[0] < users[0], f"{name}:{job_name}"
        assert any(
            step.get("uses", "").startswith("actions/checkout@") for step in steps[: loaders[0]]
        ), f"{name}:{job_name} loads the identity before checking it out"


@pytest.mark.parametrize("name", _workflow_names())
def test_workflows_use_only_known_identity_variables(name: str) -> None:
    known = _build_identity().keys() | BUILD_INPUTS | SIGNING_SECRETS | {PUBLISHING_VARIABLE}
    text = (WORKFLOWS / name).read_text(encoding="utf-8")

    assert set(re.findall(r"\bRAVENSTASH_CLI_[A-Z_]+\b", text)) <= known
    assert re.search(rf"\b{CMD_ENV}_(?:APT|VERSION|ARCH|BUILDER|CERTIFICATION|CODESIGN)", text) is (
        None
    )


def test_workflows_reference_only_the_documented_secrets_and_environments() -> None:
    secrets = {
        name: set(re.findall(r"\bsecrets\.([A-Za-z0-9_]+)", (WORKFLOWS / name).read_text("utf-8")))
        for name in _workflow_names()
    }
    variables = {
        name: set(re.findall(r"\bvars\.([A-Za-z0-9_]+)", (WORKFLOWS / name).read_text("utf-8")))
        for name in _workflow_names()
    }
    environments = {
        name: {job["environment"] for job in _jobs(name).values() if "environment" in job}
        for name in _workflow_names()
    }

    assert {name: found for name, found in secrets.items() if found} == {
        "refresh-apt.yml": SIGNING_SECRETS | STORAGE_SECRETS,
        "release.yml": SIGNING_SECRETS | STORAGE_SECRETS | DELIVERY_SECRETS,
    }
    assert {name: found for name, found in variables.items() if found} == {
        "refresh-apt.yml": {PUBLISHING_VARIABLE},
    }
    assert {name: found for name, found in environments.items() if found} == {
        "refresh-apt.yml": {"apt-signing", "apt-storage"},
        "release.yml": {"apt-signing", "apt-storage", "installer-delivery"},
    }
    # The signing secrets reach a step under their own names.
    for name in ("refresh-apt.yml", "release.yml"):
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        for secret in SIGNING_SECRETS:
            assert f"{secret}: ${{{{ secrets.{secret} }}}}" in text


def _literal_names(text: str) -> set[str]:
    """Collect every literal that starts with the package name."""
    return set(re.findall(rf"{re.escape(identity.PACKAGE_NAME)}(?:[-_][A-Za-z0-9]+)*[-_]?", text))


def test_workflow_literals_are_the_pinned_identity_names() -> None:
    package = identity.PACKAGE_NAME
    stem = identity.ASSET_STEM
    # Only places that are evaluated before any step can load the identity:
    # concurrency groups, reusable-workflow inputs, and a job with no checkout.
    expected = {
        "_build-release-target.yml": {f"{package}-target"},
        "ci.yml": set(),
        "platform-certification.yml": set(),
        "refresh-apt.yml": {f"{package}-release"},
        "release.yml": {f"{package}-release", f"{package}_", f"{stem}-v"},
        "test-build.yml": {
            f"{package}-test-target",
            f"{package}-test-target-",
            f"{package}-test-build-",
            f"{package}_",
            f"{stem}-v",
        },
    }

    assert {
        name: _literal_names((WORKFLOWS / name).read_text(encoding="utf-8"))
        for name in _workflow_names()
    } == expected
    for name in _workflow_names():
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        assert "releases.ravenstash.com" not in text
        assert f"/{identity.DISTRIBUTION_NAMESPACE}/" not in text
        assert BUILD_ONLY_IDENTITY["RAVENSTASH_CLI_STORAGE_BUCKET"] not in text
        assert set(re.findall(r"s3://[^\s\"]+", text)) <= {
            "s3://${RAVENSTASH_CLI_STORAGE_BUCKET}/${RAVENSTASH_CLI_NAMESPACE}/apt/",
            "s3://${RAVENSTASH_CLI_STORAGE_BUCKET}/${RAVENSTASH_CLI_NAMESPACE}",
        }


def test_release_and_refresh_share_one_publication_lock() -> None:
    group = f"{identity.PACKAGE_NAME}-release"

    for name in ("release.yml", "refresh-apt.yml"):
        assert _workflow(name)["concurrency"] == {"group": group, "cancel-in-progress": False}
    assert identity.PRODUCT_NAME in _workflow("release.yml")["run-name"]


def _expected_build_artifacts(version: str) -> dict[str, list[str]]:
    paths = {
        f"linux-glibc-{architecture}": [
            f"dist/release/{identity.debian_package_name(version, architecture)}",
            f"dist/release/{identity.archive_name(version, f'linux-{architecture}')}",
        ]
        for architecture in ("amd64", "arm64")
    }
    for target in RELEASE_TARGETS[2:]:
        paths[target] = [f"dist/release/{identity.archive_name(version, target)}"]
    return paths


@pytest.mark.parametrize(
    ("name", "version", "prefix"),
    [
        ("release.yml", "${{ inputs.version }}", None),
        ("test-build.yml", "${{ needs.prepare.outputs.version }}", "test-target"),
    ],
)
def test_build_jobs_hand_off_the_identity_named_artifacts(
    name: str, version: str, prefix: str | None
) -> None:
    builds = {
        job_name: job["with"]
        for job_name, job in _jobs(name).items()
        if job.get("uses") == "./.github/workflows/_build-release-target.yml"
    }

    assert {
        job_name: [path.strip() for path in inputs["artifact_paths"].splitlines()]
        for job_name, inputs in builds.items()
    } == _expected_build_artifacts(version)
    assert {inputs["target"] for inputs in builds.values()} == set(builds)
    if prefix is None:
        assert all("artifact_prefix" not in inputs for inputs in builds.values())
    else:
        assert {inputs["artifact_prefix"] for inputs in builds.values()} == {
            f"{identity.PACKAGE_NAME}-{prefix}"
        }


def test_reusable_builder_names_its_handoffs_from_the_identity() -> None:
    builder = _workflow("_build-release-target.yml")
    inputs = builder[True]["workflow_call"]["inputs"]
    steps = builder["jobs"]["build"]["steps"]
    text = (WORKFLOWS / "_build-release-target.yml").read_text(encoding="utf-8")

    assert inputs["artifact_prefix"]["default"] == f"{identity.PACKAGE_NAME}-target"
    assert steps[-1]["with"]["path"] == (
        "dist/release/${{ env.RAVENSTASH_CLI_ASSET_STEM }}-v${{ inputs.version }}-sbom.cdx.json"
    )
    assert "/release/${RAVENSTASH_CLI_PACKAGE}_*_${ARCHITECTURE}.deb" in text
    assert 'test -e "/usr/lib/${RAVENSTASH_CLI_PACKAGE}/_internal/libpython3.14.so.1.0"' in text
    assert (
        text.count('bundle="build/release/${RAVENSTASH_CLI_ASSET_STEM}-v${VERSION}-${TARGET}"') == 2
    )
    # A container sees the identity only when the step passes it through.
    assert text.count("--env RAVENSTASH_CLI_PACKAGE \\") == 1
    assert text.count("--env RAVENSTASH_CLI_ASSET_STEM \\") == 1


def test_release_inventory_lists_every_asset_under_its_identity_name() -> None:
    version = "0.15.0"
    assemble = next(
        step["run"]
        for step in _jobs("release.yml")["assemble"]["steps"]
        if step.get("name") == "Assemble a collision-free release inventory"
    )
    listed = assemble.split("expected=(\n", 1)[1].split("\n)\n", 1)[0].split()
    names = [
        entry.strip('"')
        .replace("${package}", identity.PACKAGE_NAME)
        .replace("${stem}", identity.ASSET_STEM)
        .replace("${VERSION}", version)
        for entry in listed
    ]

    assert 'package="$RAVENSTASH_CLI_PACKAGE"\nstem="$RAVENSTASH_CLI_ASSET_STEM"\n' in assemble
    assert names == [
        identity.debian_package_name(version, "amd64"),
        identity.debian_package_name(version, "arm64"),
        *(identity.archive_name(version, target) for target in RELEASE_TARGETS),
        f"{identity.ASSET_STEM}-v{version}-sbom.cdx.json",
    ]
    # The installers and the source identity are published, and therefore
    # listed in the checksum inventory, under asset names of their own.
    assert 'install -m 0755 packaging/install.sh "build/release/${stem}-install.sh"' in assemble
    assert 'install -m 0644 packaging/install.ps1 "build/release/${stem}-install.ps1"' in assemble
    assert '> "build/release/${stem}-source-identity.txt"' in assemble
    assert "generate_package_manifests.py" in assemble
    assert 'xargs sha256sum > "${stem}-v${VERSION}-checksums.txt"' in assemble
    assert '--bundle "build/release/${stem}-v${VERSION}-checksums.sigstore.json"' in assemble
    sign = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert '--output "portable/${checksums}.asc"' in sign
    assert sign.count('checksums="${stem}-v${VERSION}-checksums.txt"') == 2
    assert sign.count('--pattern "${stem}-install.*"') == 2
    assert sign.count('--pattern "${stem}-source-identity.txt"') == 2
    assert sign.count(r'grep -E "  ${stem}-(install\.sh|install\.ps1|source-identity\.txt)\$"') == 2
    assert sign.count("build/release/install.") == sign.count("release-build/install.") == 0


def test_public_apt_gate_installs_the_identity_package_from_the_identity_url() -> None:
    gate = _jobs("release.yml")["verify-apt"]["steps"][-1]["run"]

    for variable in ("PACKAGE", "APT_URL", "KEYRING", "SOURCE_LIST"):
        assert f"--env RAVENSTASH_CLI_{variable} \\" in gate
    assert (
        '"deb [arch=${ARCHITECTURE} signed-by=/usr/share/keyrings/${RAVENSTASH_CLI_KEYRING}] '
        '${RAVENSTASH_CLI_APT_URL} ${CHANNEL} main"'
    ) in gate
    assert '> "/etc/apt/sources.list.d/${RAVENSTASH_CLI_SOURCE_LIST}"' in gate
    assert 'install --yes "$RAVENSTASH_CLI_PACKAGE"' in gate
    assert "rvs --version" in gate


def test_test_build_names_its_unsigned_inventory_from_the_identity() -> None:
    assemble = _jobs("test-build.yml")["assemble"]["steps"]
    package = identity.PACKAGE_NAME

    # This job has no checkout, so its names are literals pinned here.
    assert not any("uses" in step and "checkout" in step["uses"] for step in assemble)
    assert assemble[0]["with"]["pattern"] == f"{package}-test-target-*-${{{{ inputs.source_sha }}}}"
    assert f"sudo apt install ./{package}_${{VERSION}}_ARCH.deb" in assemble[1]["run"]
    assert f'> "{identity.ASSET_STEM}-v${{VERSION}}-checksums.txt"' in assemble[1]["run"]
    assert assemble[2]["with"]["name"].startswith(f"{package}-test-build-${{{{")
    assert f'artifact="{package}-test-build-${{VERSION}}-' in assemble[3]["run"]


# ── Documentation ─────────────────────────────────────────────────────────────

LINKED_DOCUMENTS = (
    "README.md",
    "SECURITY.md",
    "SUPPORT.md",
    "CONTRIBUTING.md",
    "RELEASING.md",
    "AGENTS.md",
    "packaging/README.md",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/pull_request_template.md",
)


def _documents() -> dict[str, str]:
    paths = [ROOT / path for path in LINKED_DOCUMENTS]
    paths += [
        path
        for path in sorted((ROOT / "docs").rglob("*.md"))
        if "releases" not in path.relative_to(ROOT / "docs").parts
    ]
    return {path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8") for path in paths}


def test_documentation_links_point_at_the_identity() -> None:
    wrong = []
    for path, text in _documents().items():
        for owner, repository in re.findall(
            r"github\.com[/:]([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)", text
        ):
            if owner == identity.REPOSITORY.split("/")[0] and (
                f"{owner}/{repository}" != identity.REPOSITORY
            ):
                wrong.append(f"{path}: github.com/{owner}/{repository}")
        for flake in re.findall(r"github:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", text):
            if flake != identity.NIX_FLAKE:
                wrong.append(f"{path}: {flake}")
        for url in re.findall(r"https://releases\.ravenstash\.com[^\s`)\"']*", text):
            if not url.startswith((identity.APT_REPOSITORY_URL, identity.CHANNELS_URL)):
                wrong.append(f"{path}: {url}")
        for keyring in re.findall(r"/etc/apt/[a-z.]+/[A-Za-z0-9_.-]+", text):
            if keyring not in {identity.APT_KEYRING_PATH, identity.APT_SOURCE_PATH}:
                wrong.append(f"{path}: {keyring}")

    assert wrong == []


def test_community_documents_link_to_the_repository() -> None:
    documents = _documents()
    repository = identity.REPOSITORY_URL

    assert f"({repository}/security/advisories/new)" in documents["SECURITY.md"]
    assert f"({repository}/discussions)" in documents["SUPPORT.md"]
    assert f"({repository}/issues/new/choose)" in documents["SUPPORT.md"]
    links = yaml.safe_load(documents[".github/ISSUE_TEMPLATE/config.yml"])["contact_links"]
    assert [link["url"] for link in links] == [
        f"{repository}/discussions",
        f"{repository}/security/advisories/new",
    ]


def test_documents_name_the_product_and_keep_the_command() -> None:
    documents = _documents()

    assert documents["README.md"].startswith(f"# {identity.PRODUCT_NAME}\n")
    assert documents["CONTRIBUTING.md"].startswith(
        f"# Contributing to the {identity.PRODUCT_NAME}\n"
    )
    assert documents["RELEASING.md"].startswith(f"# Releasing the {identity.PRODUCT_NAME}\n")
    assert documents["packaging/README.md"].startswith(
        f"# {identity.PRODUCT_NAME} release packaging\n"
    )
    assert f"the {identity.PRODUCT_NAME} repository" in " ".join(documents["AGENTS.md"].split())
    assert (
        "curl -fsSL https://ravenstash.com/install.sh | bash\nrvs --version\n"
        in (documents["README.md"])
    )


def test_release_documents_describe_the_published_names() -> None:
    packaging = _read("packaging/README.md")
    releasing = _read("RELEASING.md")
    package = identity.PACKAGE_NAME

    for name in (
        f"dist/packages/{package}_<version>_<arch>.deb",
        f"dist/release/{identity.ASSET_STEM}-v<version>-linux-<arch>.tar.gz",
        f"dist/release/{identity.ASSET_STEM}-v<version>-windows-<arch>.zip",
        f"dist/release/{identity.ASSET_STEM}-v<version>-checksums.txt",
        f"{identity.ASSET_STEM}-install.sh",
        f"{identity.ASSET_STEM}-install.ps1",
        f"{identity.ASSET_STEM}-source-identity.txt",
        f"{identity.ASSET_STEM}-v<version>-<target>/rvs",
        f"`{identity.APT_REPOSITORY_URL}`",
        f"`{identity.CHANNELS_URL}`",
        f"`{identity.APT_KEYRING_PATH}`",
        f"`{identity.APT_SOURCE_PATH}`",
        f"`pool/main/r/{package}/`",
        f"`dist/apt/{identity.KEYRING_NAME}`",
        f"`{identity.RECEIPT_NAME}`",
        f"`~/.local/share/{package}`",
        f"`/opt/{package}`",
        f"`Ravenstash\\{package}`",
        f"`{identity.homebrew_cask('v0')}`",
        f"`{identity.winget_package('v0')}`",
    ):
        assert name in packaging, name
    for secret in sorted(SIGNING_SECRETS | STORAGE_SECRETS | DELIVERY_SECRETS):
        assert f"| secret | `{secret}` |" in packaging
    for document in (packaging, releasing):
        assert f"`{PUBLISHING_VARIABLE}`" in document
    assert f"./{package}_0.15.1.dev12345+g01234567_amd64.deb" in releasing
    assert f"{identity.ASSET_STEM}-v0.15.1.dev12345+g01234567-checksums.txt" in releasing


# ── Release-inventory naming policy ───────────────────────────────────────────

# ``rvs`` used as a package, release asset, storage prefix, archive root,
# keyring, source list, receipt, or build variable. Each pattern is precise: the
# command name itself is never forbidden.
FORBIDDEN = {
    "Debian package file": rf"\b{CMD}_[^\s\"'`/]*\.deb\b",
    "release asset or archive root": rf"\b{CMD}-v(?:[0-9]|[${{<#*])",
    "release storage namespace": (
        rf"/{CMD}/(?:apt|channels\.json)\b|releases\.ravenstash\.com/{CMD}\b"
        rf"|\bs3://[^\s\"']*/{CMD}(?:/|\b)"
    ),
    "keyring, source list, or key file": rf"\bravenstash-{CMD}\b|(?<![\w/.-]){CMD}\.(?:gpg|list|asc)\b",
    "Debian control field": rf"\bPackage: {CMD}\b",
    "APT pool": rf"\bpool/[a-z]+/r/{CMD}\b",
    "APT package argument": (
        rf"\b(?:apt(?:-get)? install|apt-cache policy|dpkg-query)\b[^\n|;&]*[\s\"']{CMD}(?:[\s\"')]|$)"
        rf"|\b{CMD}=(?:[0-9]|[${{])"
    ),
    "installation receipt": rf"\b{CMD}-install\.json\b",
    "Homebrew cask": rf"\b{CMD}@",
    "WinGet package or Windows install root": rf"\bRavenstash[.\\]{CMD}\b",
    "installation or payload directory": rf"(?<![\w.-])/opt/{CMD}\b|\bshare/(?:doc/)?{CMD}\b|\busr/lib/{CMD}\b",
    "build or release variable": (
        rf"(?<![A-Z_]){CMD_ENV}_(?:APT_[A-Z_]*|VERSION|ARCH|BUILDER_IMAGE"
        rf"|CERTIFICATION_BUNDLE|CODESIGN_[A-Z_]*)\b"
    ),
    "artifact, concurrency group, or builder image": (
        rf"\b{CMD}-(?:release|target|test-target|test-build|builder)\b"
    ),
}

# Forms that use the command name by design, with the reason for each one. A
# forbidden match inside one of these is not a finding, and every entry must
# occur in the tree so that the list describes only what exists.
ALLOWED = {
    r"(?<![\w-])rvs (?:--version|update|auth|art)\b": "the `rvs` executable is the command users run",
    r"(?<![\w.-])ravenstash --version\b": "`ravenstash` is the full-name alias of the same entry point",
    r"\bdocker-credential-rvs\b": "Docker resolves its credential helper by this executable name",
    r"\bfrom rvs\b|\brvs/[a-z_]+(?:\.py\b|/)|\brvs\.resources\b": (
        "`rvs` is the importable Python package and its source tree"
    ),
    r"--rvs-[a-z-]+": "`--rvs-*` options separate CLI options from the wrapped tool's own",
    r"~/\.rvs\b|\.rvs\.env\b": "`~/.rvs` and `.rvs.env` hold user state and configuration",
    r"\brvs_(?:slt|ust|uot|oat)": "API token prefixes are issued and parsed by the service",
    r"\bravenstash-rvs-credential-vault-v1\b": (
        "the associated data authenticated with every encrypted credential vault"
    ),
    r'_SERVICE = "rvs"': "the OS keyring service name of the credential store",
    r"\brvs/\{|\"rvs/dev\"": "the User-Agent product token the API identifies clients by",
    r"\bravenstash-cli-installer\b": "the name of the installer Worker",
    r"packaging/pyinstaller/rvs\.spec\b|\bdist/pyinstaller/rvs\b": (
        "PyInstaller names its spec and intermediate bundle after the launcher; neither is shipped"
    ),
    r"\$RUNNER_TEMP/rvs-[a-z-]+|/tmp/rvs-[a-z-]+": (
        "scratch paths inside a runner or build container; never distributed"
    ),
    r"\brvs_(?:version|debian_version|arch)\b": "shell helpers of the packaging scripts",
}

# End-user environment variables carry the command prefix: they are documented
# configuration. Every other variable with that prefix is a finding.
USER_ENVIRONMENT = {
    r"RVS_TOKEN|RVS_ACCOUNT_REF|RVS_SESSION_ID": "credentials and the acting account",
    r"RVS_HOME|RVS_ENV_FILE|RVS_DIR": "locations of user state and configuration",
    r"RVS_PROFILE(?:_[A-Z_]*)?|RVS_API_URL|RVS_REPOSITORY_DOMAIN": "profile and endpoint selection",
    r"RVS_CREDENTIAL_STORE|RVS_GITHUB_TOKEN|RVS_RUNTIME_TARGET": "credential store and downloads",
    r"RVS_INSTALL_[A-Z_]*": "options of the standalone installers",
    r"RVS_NATIVE_TOKEN|RVS_NETRC|RVS_OCI_CONFIG|RVS_OCI_CREDENTIAL_FILE|RVS_EXECUTABLE": (
        "values the CLI hands to the native tools it wraps"
    ),
}

_FORBIDDEN = {name: re.compile(pattern, re.MULTILINE) for name, pattern in FORBIDDEN.items()}
_ALLOWED = [re.compile(pattern) for pattern in ALLOWED]
_USER_ENVIRONMENT = re.compile("|".join(USER_ENVIRONMENT))
_COMMAND_VARIABLE = re.compile(rf"(?<![A-Za-z0-9_]){CMD_ENV}_[A-Z0-9_]+")


def _naming_findings(text: str) -> list[tuple[int, str, str]]:
    """Return the line, kind, and text of each forbidden name in ``text``."""
    findings = []
    for number, line in enumerate(text.splitlines(), start=1):
        allowed = [match.span() for pattern in _ALLOWED for match in pattern.finditer(line)]
        for kind, pattern in _FORBIDDEN.items():
            for match in pattern.finditer(line):
                if not any(start <= match.start() and match.end() <= end for start, end in allowed):
                    findings.append((number, kind, match.group()))
        for match in _COMMAND_VARIABLE.finditer(line):
            if _USER_ENVIRONMENT.fullmatch(match.group()) is None and not any(
                pattern.search(match.group()) for pattern in _FORBIDDEN.values()
            ):
                findings.append((number, "unlisted environment variable", match.group()))
    return findings


@pytest.fixture(scope="module")
def tracked_text() -> dict[str, str]:
    git = shutil.which("git")
    if git is None or not (ROOT / ".git").exists():
        pytest.skip("the naming policy runs in a Git checkout")
    listed = subprocess.run(
        [git, "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout
    texts = {}
    for path in listed.decode("utf-8").split("\0"):
        if not path:
            continue
        try:
            texts[path] = (ROOT / path).read_text(encoding="utf-8")
        except UnicodeDecodeError, FileNotFoundError, IsADirectoryError:
            continue
    return texts


def test_tracked_files_never_use_the_command_name_as_a_distribution_name(
    tracked_text: dict[str, str],
) -> None:
    findings = [
        f"{path}:{number}: {kind}: {found}"
        for path, text in tracked_text.items()
        for number, kind, found in _naming_findings(text)
    ]
    findings += [
        f"{path}: {kind} in the path"
        for path in tracked_text
        for _number, kind, _found in _naming_findings(path)
    ]

    assert findings == []


def test_every_allowed_form_occurs(tracked_text: dict[str, str]) -> None:
    tree = "\n".join(tracked_text.values())

    assert [pattern for pattern in ALLOWED if re.search(pattern, tree) is None] == []
    assert all(reason and not reason.endswith(".") for reason in ALLOWED.values())
    variables = set(_COMMAND_VARIABLE.findall(tree))
    assert [
        pattern
        for pattern in USER_ENVIRONMENT
        if not any(re.fullmatch(pattern, variable) for variable in variables)
    ] == []


def test_naming_policy_reports_each_forbidden_form() -> None:
    planted = {
        "Debian package file": [
            f"{CMD}_0.15.0_amd64.deb",
            f"dist/packages/{CMD}_${{VERSION}}_${{ARCH}}.deb",
            f"/release/{CMD}_*_amd64.deb",
            f"{CMD}_<version>_<arch>.deb",
        ],
        "release asset or archive root": [
            f"{CMD}-v0.15.0-linux-amd64.tar.gz",
            f"{CMD}-v${{VERSION}}-checksums.txt",
            f'f"{CMD}-v{{version}}-{{target}}"',
            f"{CMD}-v<version>-sbom.cdx.json",
            f'binary "{CMD}-v#{{version}}-macos-#{{arch}}/{CMD}"',
            f"-name '{CMD}-v*'",
            f'"{CMD}-v$ReleaseVersion-windows-$architecture.zip"',
        ],
        "release storage namespace": [
            f"https://releases.ravenstash.com/{CMD}/apt",
            f"https://releases.ravenstash.com/{CMD}/channels.json.gpg",
            f"s3://ravenstash-cli-releases/{CMD}",
            f'"s3://${{bucket}}/{CMD}/apt"',
        ],
        "keyring, source list, or key file": [
            f"/etc/apt/keyrings/ravenstash-{CMD}.gpg",
            f"/etc/apt/sources.list.d/ravenstash-{CMD}.list",
            f"keys/ravenstash-{CMD}.asc",
            f'root / "{CMD}.gpg"',
        ],
        "Debian control field": [f"Package: {CMD}"],
        "APT pool": [f"pool/main/r/{CMD}/"],
        "APT package argument": [
            f"apt-get install -y -qq {CMD}",
            f"sudo apt install {CMD}",
            f"dpkg-query -W -f='${{Version}}' {CMD})",
            f'"install", "--yes", f"{CMD}={{target}}"',
            f"apt-get install --allow-downgrades {CMD}=0.15.0",
        ],
        "installation receipt": [f"{CMD}-install.json"],
        "Homebrew cask": [f'cask "{CMD}@0" do', f"brew upgrade {CMD}@0"],
        "WinGet package or Windows install root": [
            f"Ravenstash.{CMD}.v0",
            f'"Ravenstash\\{CMD}"',
        ],
        "installation or payload directory": [
            f"/opt/{CMD}",
            f"~/.local/share/{CMD}",
            f"/usr/share/doc/{CMD}/README.md",
            f"/usr/lib/{CMD}/_internal",
        ],
        "build or release variable": [
            f"{CMD_ENV}_APT_GPG_KEY_ID",
            f"{CMD_ENV}_APT_CHANNEL",
            f"${{{CMD_ENV}_VERSION:-}}",
            f"{CMD_ENV}_ARCH",
            f"{CMD_ENV}_BUILDER_IMAGE",
            f"{CMD_ENV}_CERTIFICATION_BUNDLE",
            f"{CMD_ENV}_CODESIGN_IDENTITY",
        ],
        "artifact, concurrency group, or builder image": [
            f"group: {CMD}-release",
            f"default: {CMD}-target",
            f"{CMD}-test-target-*",
            f"{CMD}-test-build-0.15.0",
            f"{CMD}-builder-ubuntu20",
        ],
    }

    assert planted.keys() == FORBIDDEN.keys()
    for kind, samples in planted.items():
        for sample in samples:
            assert kind in {found for _number, found, _text in _naming_findings(sample)}, sample
    assert _naming_findings(f"export {CMD_ENV}_RELEASE_CHANNEL=v0") == [
        (1, "unlisted environment variable", f"{CMD_ENV}_RELEASE_CHANNEL")
    ]


def test_naming_policy_allows_the_command_contracts() -> None:
    allowed = "\n".join(
        [
            "rvs --version && ravenstash --version",
            "rvs update --to 0.15 --apply",
            "/usr/bin/rvs /usr/bin/ravenstash /usr/bin/docker-credential-rvs",
            "/usr/lib/ravenstash-cli/rvs",
            "ravenstash-cli-v0.15.0-linux-amd64/rvs",
            "from rvs import identity",
            "rvs/resources/ravenstash-cli.asc",
            "packaging/pyinstaller/rvs.spec dist/pyinstaller/rvs/rvs",
            "RVS_TOKEN RVS_HOME RVS_ENV_FILE RVS_GITHUB_TOKEN RVS_CREDENTIAL_STORE",
            "RVS_INSTALL_METHOD=portable RVS_INSTALL_VERSION=0.15.0 RVS_PROFILE_STAGING_API_URL",
            "rvs pip install internal-sdk --rvs-target platform/packages --rvs-yes",
            "~/.rvs/config.toml .rvs.env",
            "rvs_sltAAAA rvs_oatBBBB",
            'b"ravenstash-rvs-credential-vault-v1"',
            '_SERVICE = "rvs"',
            'f"rvs/{version}"',
            'name = "ravenstash-cli-installer"',
            "export TWINE_USERNAME=rvs",
            "rvs_home rvs_path _rvs_user_agent rvs-managed runtimes",
            "/usr/local/lib/rvs/rvs",
            "$RUNNER_TEMP/rvs-apt-signing /tmp/rvs-build-venv",
            "apt-get install --yes ravenstash-cli && rvs --version",
        ]
    )

    assert _naming_findings(allowed) == []
