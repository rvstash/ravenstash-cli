# Ravenstash CLI release packaging

This directory contains the cross-platform packaging path for the Ravenstash CLI. User-facing installs use self-contained native bundles, signed APT packages, or the Nix flake rather than a global Python install.

## Release identity

The CLI is published as the `ravenstash-cli` package and installs the `rvs`, `ravenstash`, and `docker-credential-rvs` commands. Every name under which it is built, published, and updated has one source:

- [`identity.env`](identity.env) for packaging scripts, repository tooling, and workflows, which load it into each job environment; and
- [`rvs/identity.py`](../rvs/identity.py) for the running CLI.

`install.sh` and `install.ps1` run standalone and embed their own copies. The test suite fails when either source, an installer, a workflow literal, a generated manifest, or a documented link disagrees.

## Build tools

Required commands:

```bash
docker
dpkg-deb
apt-ftparchive
gpg
gpgv
sha256sum
```

The canonical build uses `packaging/Dockerfile.ubuntu20`, whose Ubuntu and uv images are selected by immutable manifest digests. It installs the exact PyInstaller version from `uv.lock`.

## Local build

From the repository root:

```bash
packaging/scripts/build-pyinstaller.sh
packaging/scripts/build-deb.sh
packaging/scripts/build-tarball.sh
packaging/scripts/build-release-artifacts.sh
packaging/scripts/build-in-ubuntu20.sh
```

The generated artifacts are written under `dist/`:

```text
dist/pyinstaller/rvs/              # frozen onedir bundle
dist/packages/ravenstash-cli_<version>_<arch>.deb
dist/release/ravenstash-cli-v<version>-linux-<arch>.tar.gz
dist/release/ravenstash-cli-v<version>-linux-musl-<arch>.tar.gz
dist/release/ravenstash-cli-v<version>-macos-<arch>.tar.gz
dist/release/ravenstash-cli-v<version>-windows-<arch>.zip
dist/release/ravenstash-cli-v<version>-sbom.cdx.json
dist/release/ravenstash-cli-v<version>-checksums.txt
dist/release/ravenstash-cli-v<version>-package-manifests.tar.gz
dist/release/ravenstash-cli-install.sh
```

A release also carries `ravenstash-cli-install.ps1`, `ravenstash-cli-source-identity.txt`, and the checksum inventory's `.txt.asc` and `.sigstore.json` signatures. Each portable archive unpacks into one directory named after the archive:

```text
ravenstash-cli-v<version>-<target>/rvs
ravenstash-cli-v<version>-<target>/ravenstash
ravenstash-cli-v<version>-<target>/docker-credential-rvs
```

The frozen bundle and Debian package expose both `rvs` and the long-form `ravenstash` alias. Portable archives ship each command alias as a regular file, because `rvs update` refuses archives that contain links; the Debian package links its aliases instead. During Debian installation, the post-install script reports an informational notice when an AMD ROCm Validation Suite `rvs` executable is present under `/opt/rocm*`.

Native symbols are stripped from Linux and macOS release bundles. Windows bundles omit the encrypted-vault `cryptography` dependency because that vault is unavailable on Windows; Windows credentials use Credential Manager instead.

## APT repository

After building the `.deb`, generate static APT repository metadata. The channel is derived from the package major version (`0.15.x` and `0.16.x` -> `v0`) and an explicit override must match that policy:

```bash
RAVENSTASH_CLI_APT_GPG_KEY_ID=<key-id> \
RAVENSTASH_CLI_APT_GPG_FINGERPRINT=<full-fingerprint> \
RAVENSTASH_CLI_APT_CHANNEL=v0 \
  packaging/scripts/update-apt-repo.sh
```

Unsigned metadata can be generated without `RAVENSTASH_CLI_APT_GPG_KEY_ID` for local inspection, but published repositories must be signed. A passphrase-protected key can be used by setting `RAVENSTASH_CLI_APT_GPG_PASSPHRASE_FILE` to a mode-`0600` file. The generated repository includes the binary public key `dist/apt/ravenstash-cli.gpg`.

The public repository is served at `https://releases.ravenstash.com/ravenstash/apt`. A client trusts it through `/etc/apt/keyrings/ravenstash-cli.gpg` and `/etc/apt/sources.list.d/ravenstash-cli.list`, and installs the `ravenstash-cli` package from `pool/main/r/ravenstash-cli/`.

Production publishing is owned by this repository. One release workflow builds eight isolated native targets in parallel, rejects filename collisions during assembly, attests one immutable inventory, and publishes those exact bytes without rebuilding them. It authenticates the prior `InRelease`, every metadata digest, every listed package digest, and the absence of unlisted pool objects before a separate signing job sees the tree. Both Debian architectures are appended before one index-generation and signing pass. A different job batches immutable pool/by-hash objects first, then mutable indexes and releases, with `InRelease` and channel discovery last. If an interrupted publish omitted an architecture's indexes, the storage-side restore may reconstruct only bytes that exactly match the already-authenticated `InRelease` SHA-256 inventory; any mismatch remains a hard failure. A daily split-credential workflow refreshes the seven-day `Valid-Until`, so several consecutive missed or failed runs are needed before public metadata expires. Its schedule is idle until the repository variable `RAVENSTASH_CLI_PUBLISHING` is `enabled`, which a maintainer sets once the first stable release has published the repository. The refresh restores the read-only public tree under the signing environment, passes only bounded signed metadata between jobs, and restores the canonical tree again under the storage environment. It does not consume GitHub Actions artifact storage.

APT suites are rolling major channels. All pre-1 stable packages share the `v0` suite, and its signed channel manifest maps each `0.MINOR` selector to the latest published stable patch in that line. Plain APT upgrades follow the newest `v0` release; an exact `rvs update --to 0.MINOR` installs the mapped package version without changing the configured suite.

Published suites contain separate `amd64` and `arm64` indexes. Immutable pool filenames carry a digest suffix, so index generation filters the package's declared architecture from its metadata instead of relying on Debian filename conventions. A newly added architecture may have an empty index in an older major suite, while every suite must retain at least one valid package.

The POSIX user-facing installer source is `packaging/install.sh`; the Windows source is `packaging/install.ps1`. On a Debian-family system the shell installer verifies the expected signing-key fingerprint, configures this APT repository, and installs `ravenstash-cli`. On Linux glibc, Linux musl, and macOS for amd64 or arm64 it verifies an OpenPGP-signed release inventory plus the archive's exact SHA-256 digest and performs a user-local install by default. The PowerShell installer selects Windows x64 or ARM64, verifies the release digest, installs per-user by default, and updates user `PATH`. Portable installations live under `~/.local/share/ravenstash-cli` (or `/opt/ravenstash-cli` system-wide) and under `Ravenstash\ravenstash-cli` on Windows. Both portable installers write the non-secret `ravenstash-cli-install.json` receipt used to preserve scope and target during `rvs update`. The updater bundles the pinned release public key and verifies the OpenPGP-signed channel and checksum inventories on every portable platform.

Ephemeral CI can force the authenticated portable path on Debian-family runners without configuring APT:

```bash
curl -fsSL https://ravenstash.com/install.sh \
  | RVS_INSTALL_METHOD=portable RVS_INSTALL_NO_BANNER=1 \
      RVS_INSTALL_NO_HINTS=1 bash
```

`RVS_INSTALL_VERSION=X.Y.Z` selects an exact stable release, or `X.Y.ZrcN` selects an exact candidate, and is accepted only with `RVS_INSTALL_METHOD=portable`. The archive still passes the same signed inventory and checksum verification. `RVS_INSTALL_ROOT` and `RVS_INSTALL_BIN_DIR` may point at a runner-local tool directory; they must never contain credentials. The installer publishes `portable_installer_api=1` as the compatibility contract for CI setup tooling. The channel policy is published at the distribution-neutral `https://releases.ravenstash.com/ravenstash/channels.json` path. The signed manifest carries `generated_at` and `expires` (seven days later, like the APT `Valid-Until`). The daily metadata refresh re-signs it for APT and, when only those two fields changed, for the portable path as well. `rvs update` rejects a manifest without these fields, an expired manifest, and one older than the newest it already accepted. After the corresponding GitHub release is public, stable release automation publishes the neutral update policy alongside validation and signing for the protected installer-promotion jobs. Installer deployment waits for both, deploys the exact release installers, and updates the recommended series only after verifying the live bytes. These jobs are part of the stable release workflow so their credential boundaries and dependencies remain visible in one run. Homebrew and WinGet update their versioned channel package (`ravenstash-cli@0` and `Ravenstash.RavenstashCLI.v0`) in place, or replace it with rollback for an explicit series migration. Nix updates replace the tag-pinned `ravenstash-cli` profile element with the selected immutable release tag and restore the prior tag when replacement fails. Homebrew and WinGet manifests are generated in the release bundle but are not generally installable until separately accepted into their external catalogs; the signed portable installers remain the supported macOS and Windows path. Apple notarization and Windows Authenticode are deferred while these platforms use command-line distribution. The protected APT signer authenticates the APT repository and portable inventories. The build includes the installer in the checksummed and attested release artifacts. After the APT repository and GitHub release pass their publication gates, the protected promotion stage embeds the exact attested bytes in a dedicated Cloudflare Worker at `https://ravenstash.com/install.sh` and `https://ravenstash.com/install.ps1`. The Worker does not fetch executable shell code from R2. The promotion jobs of the stable release workflow independently check out the immutable release source, refuses installer rollback, deploys only bytes that match the release's signed inventory, verifies the live routes, and publishes the newly signed recommended-channel manifest last.

Path-aware CI runs source tests on native Linux ARM64, macOS Intel/Apple Silicon, and Windows x64/ARM64 in parallel, with quality checks and an Ubuntu 20.04 package smoke test on Linux amd64. It evaluates the Nix flake and builds the x86_64 Linux package when Nix or dependency inputs change. The manual/scheduled platform certification builds the frozen bundle on Linux, macOS, and Windows for both architectures and on Alpine musl, exercises real managed-runtime downloads there, runs the native credential store on macOS and Windows, and runs a disposable Secret Service round trip on Linux. Every release and test build smoke-tests its bundle where it is built: startup, help, the `ravenstash` alias, the unauthenticated status exit code, and unpacking the built archive with the updater's own extraction. Each glibc build also installs the Debian package on a clean Ubuntu 20.04 image. Desktop keyring behavior is covered separately by provider tests and that disposable round trip because containers do not supply a real graphical D-Bus session.

Only protected GitHub environments in this repository define these Actions values:

| Kind | Name | Purpose |
| --- | --- | --- |
| secret | `RAVENSTASH_CLI_APT_GPG_PRIVATE_KEY` | ASCII-armored private signing key |
| secret | `RAVENSTASH_CLI_APT_GPG_PASSPHRASE` | Signing-key passphrase; may be empty |
| secret | `R2_APT_ACCESS_KEY_ID` | Bucket-scoped R2 write credential |
| secret | `R2_APT_SECRET_ACCESS_KEY` | Bucket-scoped R2 write credential |
| secret | `R2_APT_ACCOUNT_ID` | Cloudflare account ID, kept out of workflow logs |
| secret | `CLOUDFLARE_API_TOKEN` | Worker-only deployment token |
| secret | `CLOUDFLARE_ACCOUNT_ID` | Cloudflare account ID, kept out of workflow logs |

Secret values are configured only in the protected GitHub environments; do not commit them or create independent unmanaged copies. The one repository-level value is the non-secret variable `RAVENSTASH_CLI_PUBLISHING`, which enables the scheduled metadata refresh when set to `enabled`. The R2 bucket must be publicly readable through `releases.ravenstash.com` while its S3 write API remains private. Connecting that custom domain and provisioning the bucket-scoped token are infrastructure prerequisites, not responsibilities of this source repository.

The `apt-signing`, `apt-storage`, and `installer-delivery` environments keep their credentials separated. Releases are manual exact-SHA dispatches represented by one end-to-end workflow run. The built-in token creates a draft against that source commit, populates it once, and publishes it.

The committed public key is an identity pin, not a secret. The private key, passphrase, R2 credentials, Worker token, and revocation certificate are never committed. The revocation certificate is deliberately not copied into GitHub.

See [`RELEASING.md`](../RELEASING.md) for the release procedure.
