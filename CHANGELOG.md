# Changelog

All notable user-facing changes to the Ravenstash CLI are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The CLI and the Ravenstash API are pre-1.0: commands, options, and output can change between 0.x releases, always with a note here. Stay on the latest release; every release from 0.15.0 on can update itself to the latest one.

## [Unreleased]

## [0.15.1] - 2026-10-12

### Changed

- Updated the bundled libraries: Click 8.5.0, Typer 0.27.3, httpx2 2.13.1, cryptography 50.0.2, and idna 3.20. No command, option, or output changes.

Detailed release notes: [Ravenstash CLI 0.15.1](docs/releases/0.15.1.md).

## [0.15.0] - 2026-10-11

### Added

- First public release of the Ravenstash CLI. `rvs`, also installed under its full name `ravenstash`, signs in to Ravenstash, selects a personal or organization account, and works with private repositories for PyPI, npm, Maven, and OCI content (container images and Helm charts).
- Sign-in with the browser device flow, named profiles, and tokens for automation through `RVS_TOKEN`. Secrets stay in the operating system keyring or an encrypted vault; `~/.rvs/config.toml` holds only non-secret profile data.
- Repository and private-mirror management: list, create, select, inspect package sources, and print native addresses and setup snippets for each package tool.
- Package management: list packages, versions, and files; yank and deprecate versions; manage package tags and npm dist-tags; delete versions and OCI content into the repository trash; manage OCI paths, manifests, and tags.
- Package-tool commands that run pip, uv, Twine, npm, Maven, Docker, Helm, and ORAS with short-lived Ravenstash access, so no credential is written to a project file. Publishing commands show the account, repository, and files before asking for confirmation.
- Short-lived access tokens for native tools with `rvs art token mint`, and a Docker credential helper, `docker-credential-rvs`.
- Package security evidence: upload SBOMs and dependency lock evidence for a package version, and download analysis reports.
- Local Python, Node.js, and Java runtimes with `rvs runtime`.
- Signed releases for Linux amd64 and arm64 (Debian packages, glibc and musl portable archives), macOS Intel and Apple Silicon, Windows x64 and ARM64, and Nix. `rvs update` verifies the signed release channel and installs the latest release in the rolling `v0` channel; `rvs update --to 0.MINOR` installs the latest patch of one minor line without pinning it.
- A configuration written by a pre-release version of `rvs` is not migrated: `rvs` leaves it untouched and asks you to run `rvs profile delete --all` and sign in again.

Detailed release notes: [Ravenstash CLI 0.15.0](docs/releases/0.15.0.md).

[Unreleased]: https://github.com/rvstash/ravenstash-cli/compare/v0.15.1...HEAD
[0.15.1]: https://github.com/rvstash/ravenstash-cli/releases/tag/v0.15.1
[0.15.0]: https://github.com/rvstash/ravenstash-cli/releases/tag/v0.15.0
