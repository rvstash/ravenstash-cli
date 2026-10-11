# Ravenstash CLI

`rvs` lets you sign in to Ravenstash, choose a personal account or organization, and use private package repositories from the command line.

It works with repositories for PyPI, npm, Maven, and OCI content, including container images and Helm charts. It can also run pip, uv, Twine, npm, Maven, Docker, Helm, and ORAS with temporary Ravenstash access, so credentials do not need to be saved in project files.

## Install

On Linux and macOS:

```bash
curl -fsSL https://ravenstash.com/install.sh | bash
rvs --version
```

On Windows, in PowerShell:

```powershell
irm https://ravenstash.com/install.ps1 | iex
rvs --version
```

Every installation also provides `ravenstash`, a full-name alias that accepts the same commands as `rvs`.

Release artifacts are built for Linux amd64/arm64 (glibc and musl), macOS Intel and Apple Silicon, Windows x64/ARM64, and Nix. The [platform compatibility policy](docs/linux-compatibility.md) gives the support status of each installation path, and the [CLI overview](https://docs.ravenstash.com/cli/overview/) covers supported systems and upgrades.

## Get started

Sign in, choose an account, and choose a repository:

```bash
rvs auth login
rvs account list
rvs account switch
rvs art select platform/packages
```

Use the interactive picker, or pass a typed Ravenstash account handle directly: `rvs account switch user:USERNAME` or `rvs account switch org:HANDLE`. Account handles are always displayed with these prefixes so they cannot be confused with namespace names. A bare handle is also accepted and is matched without regard to letter case.

Then run the package tool you already use:

```bash
rvs pip install internal-sdk
rvs uv sync
rvs npm ci
rvs mvn verify
```

`platform/packages` means the `packages` repository in the `platform` namespace. Run `rvs art repo list` to see the repositories available to the selected account, and `rvs status` or `rvs art status` to see what `rvs` acts on.

## Private mirrors

A private mirror gives your account a read-only view of a public or custom package source:

```bash
rvs art mirror create pypiorg --select
rvs pip install requests
```

You can switch back to a private repository at any time:

```bash
rvs art select platform/packages
```

## Publish safely

Publishing commands show the account, repository, and files before asking for confirmation:

```bash
rvs twine upload dist/*
rvs npm publish
rvs mvn deploy
```

In a protected automation job, use `--rvs-yes` with these wrapped tools. Deletion commands under `rvs art`, such as `rvs art package delete-version`, take `--yes` instead.

## Use in CI

Store a personal access token or an organization automation token as a CI secret and expose it as `RVS_TOKEN`. Commands then act for the account that owns the token, so the job needs no sign-in or account selection:

```bash
rvs pip install internal-sdk --rvs-target platform/packages
rvs twine upload dist/* --rvs-target platform/packages --rvs-yes
```

A saved or shell account selection does not apply while `RVS_TOKEN` is set, and `RVS_ACCOUNT_REF`, if set, must name the same account.

## Inspect packages

Package commands use the repository chosen with `rvs art select` unless you pass `--target NAMESPACE/REPOSITORY`:

```bash
rvs art package list
rvs art package show internal-sdk
rvs art package show internal-sdk --version 1.2.0
rvs art package tag list @acme/widgets
rvs art package tag set @acme/widgets beta 2.0.0-beta.1
```

Add `--format pypi`, `npm`, or `maven` only when the repository has more than one of these formats. `package show` lists the newest 50 versions; use `--limit N` or `--all-versions` for more. `package tag` manages npm distribution tags; unlike OCI tags, a package tag moves when you set it to another version.

Renaming or deleting a repository, changing its package sources, changing a private mirror's package-age policy, deleting a private mirror, and deleting a whole package are done in the Ravenstash web app.

## Repositories and formats

Options that take several formats accept comma-separated values, repeated flags, or both:

```bash
rvs art repo create packages --format pypi,npm,maven,oci
rvs art token mint --target platform/packages -f pypi -f oci
```

Whitespace is trimmed and duplicates are removed. Empty or unknown formats fail before any change is made. Commands that need one format still accept only one.

## OCI content

OCI is one repository format that holds container images and Helm charts. Use `rvs docker`, `rvs helm`, or `rvs oras` to transfer content; ORAS needs no Ravenstash format flag. Wrapper credentials and logouts stay in temporary configuration, including separate ORAS copy source and destination configs.

Inspect and tag content without launching a native tool:

```bash
rvs art oci list --content-type helm_chart --target platform/packages
rvs art oci manifest list charts/api
rvs art oci manifest show charts/api@sha256:YOUR_DIGEST
rvs art oci tag list charts/api
rvs art oci tag list charts/api --digest sha256:YOUR_DIGEST
rvs art oci tag create charts/api@sha256:YOUR_DIGEST stable
```

These OCI lists return one page: they accept `--limit` and `--cursor`, and root `--json` returns the page with its `next_cursor`. Other listings, such as `rvs art repo list` and `rvs art package list`, read every page. Manifest listings show each manifest's newest tags and its tag count; `tag list --digest` lists every tag of one manifest. `tag create` points a new tag at a manifest and never moves a tag that already points at another manifest.

## Package evidence

Stage or attach package evidence without changing native publishing:

```bash
rvs art evidence stage dist/demo-1.0.0-py3-none-any.whl \
  --target platform/packages --format pypi \
  --package demo --version 1.0.0 --scope artifact \
  --evidence cyclonedx=bom.cdx.json --analysis-context observed

rvs art evidence status pe_EXAMPLE --wait
rvs art evidence sbom af_EXAMPLE --analysis-context observed -o demo.cdx.json
```

Staging does not publish the package; use the normal native client afterward. Release scope freezes every explicitly supplied digest and never includes future files. `--profile`/`-p` selects the profile, so evidence uses the separate `--analysis-context` option for its analysis context.

## Native setup and manual tokens

Print instructions without changing files or creating credentials:

```bash
rvs art native config pip
rvs art native config oras
# Prints both read and publish endpoints when the target is a repository.
rvs art endpoint --format npm
rvs art reference backend:latest --format oci
```

For tools that `rvs` does not wrap, `rvs art token mint --format oci --access publish` creates one temporary credential that can push both container images and Helm charts to the selected repository. The printed templates keep that credential in a separate, temporary OCI configuration. If you log in with a native tool instead, remember that its credential store may be shared, so a later logout can affect other tools.

## Updates

`rvs update` previews the latest update in the rolling `v0` channel, and `--apply` installs it through APT, the signed portable updater, or the package manager that installed `rvs`. `rvs update --to 0.MINOR` selects the latest stable patch in that minor line for one update, without pinning future updates to it. To test a signed prerelease on an APT or portable installation, run `rvs update --candidate X.Y.ZrcN`, then add `--apply` after reviewing it.

## Verify a release

Every release is built and published by this repository's [release workflow](.github/workflows/release.yml). Its GitHub release carries a checksum inventory, `ravenstash-cli-v<version>-checksums.txt`, that lists the SHA-256 digest of every archive, package, and installer, together with two signatures of that inventory:

- `…-checksums.txt.asc`, an OpenPGP signature by the Ravenstash CLI release key ([public key](packaging/repository/keys/ravenstash-cli.asc), fingerprint `3B7C 20FC 370D 1A7C 813D F3A2 E967 9F95 1AD8 BAA0`); and
- `…-checksums.sigstore.json`, a Sigstore bundle issued to the release workflow.

What each installation path checks before it installs anything:

| Path | Verification |
| --- | --- |
| `install.sh` on Debian and Ubuntu | Pins the release key by fingerprint and installs from the signed APT repository. |
| `install.sh` elsewhere on Linux, and on macOS | Verifies the OpenPGP signature of the checksum inventory, then the archive's SHA-256 digest. |
| `install.ps1` on Windows | Verifies the archive's SHA-256 digest against the release's checksum inventory, downloaded over HTTPS. It does not verify a signature. |
| `rvs update` | Verifies the signed release-channel manifest. A portable update also verifies the OpenPGP signature of the checksum inventory and the archive's digest; an APT update installs from the signed APT repository. |

To verify a download yourself:

```bash
gpg --import packaging/repository/keys/ravenstash-cli.asc
gpg --verify ravenstash-cli-v<version>-checksums.txt.asc ravenstash-cli-v<version>-checksums.txt
sha256sum --check --ignore-missing ravenstash-cli-v<version>-checksums.txt

cosign verify-blob \
  --bundle ravenstash-cli-v<version>-checksums.sigstore.json \
  --certificate-identity https://github.com/rvstash/ravenstash-cli/.github/workflows/release.yml@refs/heads/main \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ravenstash-cli-v<version>-checksums.txt
```

## Profiles and runtimes

Profiles let one computer remember separate Ravenstash sign-ins or settings:

```bash
rvs auth login --profile work
rvs profile use work
rvs status
```

The CLI can also install and select Python, Node.js, and Java versions:

```bash
rvs runtime install python 3.14
rvs runtime use python 3.14
rvs runtime list
```

## Documentation

- [Quickstart](docs/quickstart.md)
- [Command reference](docs/command-reference.md)
- [Platform compatibility policy](docs/linux-compatibility.md)
- [Changelog](CHANGELOG.md)
- [Security policy](SECURITY.md)
- [Support](SUPPORT.md)
- [Public Ravenstash documentation](https://docs.ravenstash.com/cli/overview/)

## Contributing

Changes go through a short-lived branch and a pull request to `main`. See [CONTRIBUTING.md](CONTRIBUTING.md) for the branch and landing rules, the checks, and the release lifecycle, and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community expectations.

Run the repository checks from this directory:

```bash
.venv/bin/coverage run -m pytest
.venv/bin/coverage report
.venv/bin/ruff format --check rvs tests packaging/repository packaging/scripts
.venv/bin/ruff check rvs tests packaging/repository packaging/scripts
.venv/bin/pyright
```

## License

The Ravenstash CLI is licensed under the [Apache License 2.0](LICENSE).
