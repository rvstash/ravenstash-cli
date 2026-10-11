# Platform compatibility policy

Ravenstash treats platform support as an installation, runtime, credential-storage, update, and native-client contract. Binary support is gated on native artifacts for the following targets:

| Operating system | Architectures | Artifact and install path |
| --- | --- | --- |
| Linux with glibc 2.28+ | amd64, arm64 | signed APT package and portable archive |
| Linux with musl, including Alpine | amd64, arm64 | signed portable archive |
| macOS 14+ on Apple Silicon; macOS 15+ on Intel | Intel, Apple Silicon | checksummed portable archive; `install.sh` |
| Windows 11 | x64, ARM64 | checksummed ZIP; `install.ps1` |
| NixOS and Nix on Linux/macOS | x86_64, aarch64 | flake package |
| WSL2 | x86_64, aarch64 | matching Linux path |

Path-aware CI tests source on native Linux, macOS, and Windows runners and builds one Ubuntu 20.04 baseline package when runtime or packaging inputs change. Nix checks run only when Nix or dependency inputs change. The scheduled or manual platform-certification workflow builds the frozen bundle on native Linux, macOS, and Windows runners for both architectures and on Alpine musl, and exercises real managed-runtime downloads there. One release workflow builds isolated native artifacts from one exact source commit, assembles one collision-free checksum inventory, creates keyless Sigstore provenance for that complete inventory, and publishes those same bytes without rebuilding them. Maintainers can also build selected targets from an exact commit as an unsigned, seven-day Actions artifact without publishing.

Passing compatibility CI establishes that the source and release bundles run on the target hosts. Public support begins only when the exact release artifacts also pass integrity, installation, and clean-system certification. This prevents an evaluated Nix output, a smoke-test bundle, or inherited Linux coverage from being presented as a certified release for Nix, macOS, Windows, or WSL.

The glibc artifacts are built on Ubuntu 20.04. Each glibc build installs the Debian package on a clean Ubuntu 20.04 image and runs it, and smoke-tests the portable archive in the build environment, including unpacking it the way `rvs update` does. A stable release also installs the published package from the public APT repository on Ubuntu 20.04 for amd64 and arm64. The repository's workflows exercise no other glibc distribution. Debian packages are published for both amd64 and arm64 in the same signed channel.

`RVS_GITHUB_TOKEN` is optional and can raise GitHub API limits for installer downloads. It is passed in an HTTP authorization header and is never placed in a child process argument.

## Credential storage

Interactive login selects an explicit store, a profile preference, the global preference, or automatic discovery in that order. Automatic discovery tests a disposable write/read/delete operation before browser authorization.

- macOS uses Keychain and Windows uses Credential Manager through Python Keyring.
- Linux desktops use Secret Service providers such as GNOME Keyring or KWallet.
- POSIX environments may use an initialized `pass` store or the encrypted Ravenstash vault. The vault's in-memory session agent uses a protected Unix socket and is not offered on Windows.
- `RVS_TOKEN` works without a credential store for CI and other headless use.
- Plaintext storage remains an explicitly acknowledged fallback and is never selected automatically.

Platform certification runs `rvs auth storage doctor` against the real credential store on macOS and Windows runners and against an unlocked GNOME Keyring Secret Service session on Linux. It does not exercise a locked or absent desktop keyring on a real host. Unit tests own provider selection, credential-pair rollback, refresh locking, vault behavior, and plaintext acknowledgement, but do not replace that host-level certification.

## Managed runtimes and native clients

Python runtime installation selects python-build-standalone assets for Linux glibc, Linux musl, macOS, and Windows on both architectures. Java selects the corresponding Temurin build. Node.js supports the official Linux glibc, macOS, and Windows archives. On every musl system, including Alpine on amd64 and arm64, install Node.js through the system package manager or another version manager; `rvs` resolves it from `PATH`.

Native pip, uv, twine, npm, Maven, Docker, Helm, and ORAS wrappers inherit terminal I/O and use temporary credentials. Windows uses native executable suffixes and command shims. If Windows policy prevents directory symlinks, the Docker context and plugin metadata are copied into the temporary overlay instead.

The wrappers favor broad compatibility and do not reject a native client because it is old. `rvs` warns below these advisory versions, then continues the command: pip 23.0, uv 0.4.30, Twine 4.0.2, npm 10.0, Maven 3.9.0, Docker 25.0.2, Helm 3.8.0, and ORAS 1.1.0. Version detection is best-effort; an unavailable, failed, timed-out, or unparseable probe does not block execution. The advisory levels identify clients that predate relevant protocol controls, maintained upstream lines, or security fixes.

## Update boundaries

Pre-1 releases share one rolling `v0` channel across APT, portable, Homebrew, WinGet, and Nix installations. Plain `rvs update` advances to the newest stable `v0` release. `rvs update --to 0.MINOR` is a one-shot selector for the latest stable patch in that minor line; it does not create a persistent pin. Portable Linux, Alpine/musl, macOS, and Windows installations authenticate the signed channel manifest and release checksum inventory before staging the native bundle. POSIX activation switches one version pointer atomically. Windows uses a detached helper after the running executable exits. Nix, Homebrew, and WinGet remain package-manager-owned, so `rvs` delegates replacement instead of writing into their stores. Homebrew and WinGet use one major-channel package and reject exact-minor selection without mutating package state; the portable path supports it. Their manifests are generated release artifacts; these package-manager paths are not generally available until the manifests are published in the external catalogs. The supported macOS and Windows installers therefore remain portable. For tag-pinned Nix installs, `rvs` replaces only the `ravenstash-cli` profile element with the selected immutable release tag and attempts to restore the prior tag if installation fails.

## Additional platforms

FreeBSD, OpenBSD, NetBSD, Linux armv7, and Linux RISC-V remain source-compatible evaluation targets rather than supported binary promises. Promoting one requires a native, repeatable builder, secure credential-store behavior, signed installation and updates, and the same packaged native-client certification. PyInstaller does not test those BSD targets upstream, so a successful one-off build is insufficient.

Review this matrix at least quarterly and before changing the Python, PyInstaller, cryptography, or keyring baselines. A target becomes supported only after its release artifact and clean-system certification pass. Apple notarization and Windows Authenticode are deferred; their absence must remain explicit in installation and compatibility documentation.
