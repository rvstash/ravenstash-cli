"""Generate Homebrew and WinGet manifests from an assembled release."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import tarfile
from pathlib import Path

from release_identity import ASSET_STEM, HOMEBREW_CASK, WINGET_PACKAGE


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("release", type=Path)
    parser.add_argument("version")
    parser.add_argument("repository")
    args = parser.parse_args()
    release: Path = args.release
    version: str = args.version
    repository: str = args.repository
    major, _minor, _patch = version.split(".", 2)
    channel = f"v{major}"
    package_suffix = major
    base = f"https://github.com/{repository}/releases/download/v{version}"
    manifests = release / "package-manifests"
    homebrew = manifests / "homebrew"
    winget = manifests / "winget"
    homebrew.mkdir(parents=True, exist_ok=True)
    winget.mkdir(parents=True, exist_ok=True)

    mac_amd = release / f"{ASSET_STEM}-v{version}-macos-amd64.tar.gz"
    mac_arm = release / f"{ASSET_STEM}-v{version}-macos-arm64.tar.gz"
    win_amd = release / f"{ASSET_STEM}-v{version}-windows-amd64.zip"
    win_arm = release / f"{ASSET_STEM}-v{version}-windows-arm64.zip"
    for artifact in (mac_amd, mac_arm, win_amd, win_arm):
        if not artifact.is_file():
            raise SystemExit(f"missing package-manager artifact: {artifact.name}")

    cask = f"{HOMEBREW_CASK}@{package_suffix}"
    (homebrew / f"{cask}.rb").write_text(
        f'''cask "{cask}" do
  version "{version}"
  arch arm: "arm64", intel: "amd64"

  on_arm do
    sha256 "{digest(mac_arm)}"
    url "{base}/{mac_arm.name}"
  end
  on_intel do
    sha256 "{digest(mac_amd)}"
    url "{base}/{mac_amd.name}"
  end

  name "Ravenstash CLI"
  desc "Ravenstash developer CLI"
  homepage "https://ravenstash.com"
  binary "{ASSET_STEM}-v#{{version}}-macos-#{{arch}}/rvs"
  binary "{ASSET_STEM}-v#{{version}}-macos-#{{arch}}/ravenstash"
  binary "{ASSET_STEM}-v#{{version}}-macos-#{{arch}}/docker-credential-rvs"
end
''',
        encoding="utf-8",
        newline="\n",
    )

    identifier = f"{WINGET_PACKAGE}.{channel}"
    (winget / f"{identifier}.yaml").write_text(
        f"""PackageIdentifier: {identifier}
PackageVersion: {version}
DefaultLocale: en-US
ManifestType: version
ManifestVersion: 1.10.0
""",
        encoding="utf-8",
        newline="\n",
    )
    (winget / f"{identifier}.locale.en-US.yaml").write_text(
        f"""PackageIdentifier: {identifier}
PackageVersion: {version}
PackageLocale: en-US
Publisher: Ravenstash
PackageName: Ravenstash CLI
License: Apache-2.0
ShortDescription: Ravenstash developer CLI
PackageUrl: https://ravenstash.com
ManifestType: defaultLocale
ManifestVersion: 1.10.0
""",
        encoding="utf-8",
        newline="\n",
    )
    installers = []
    for architecture, artifact in (("x64", win_amd), ("arm64", win_arm)):
        root = artifact.name.removesuffix(".zip")
        nested = "\n".join(
            f"      - RelativeFilePath: {root}\\{name}.exe\n        PortableCommandAlias: {name}"
            for name in ("rvs", "ravenstash", "docker-credential-rvs")
        )
        installers.append(
            f"""  - Architecture: {architecture}
    InstallerUrl: {base}/{artifact.name}
    InstallerSha256: {digest(artifact).upper()}
    InstallerType: zip
    NestedInstallerType: portable
    NestedInstallerFiles:
{nested}"""
        )
    (winget / f"{identifier}.installer.yaml").write_text(
        f"""PackageIdentifier: {identifier}
PackageVersion: {version}
Installers:
{chr(10).join(installers)}
ManifestType: installer
ManifestVersion: 1.10.0
""",
        encoding="utf-8",
        newline="\n",
    )

    archive = release / f"{ASSET_STEM}-v{version}-package-manifests.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        output.add(manifests, arcname="package-manifests")
    shutil.rmtree(manifests)


if __name__ == "__main__":
    main()
