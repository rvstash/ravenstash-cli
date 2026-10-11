# Releasing the Ravenstash CLI

This repository builds every platform artifact in one manually dispatched release workflow. Stable releases and release candidates share validation, parallel builds, assembly, attestations, and signing. Only stable releases publish to APT. A separate manual test-build workflow produces expiring, unsigned artifacts without creating a public release.

## Release source

`main` is the only release source. Rolling major channels remain package and update boundaries, but they do not have maintenance branches. All pre-1 stable releases publish into `v0`; patch releases are immutable tags created by the release workflow.

## Trust boundaries

Publishing credentials exist only in protected GitHub environments:

| Environment | Credential scope |
| --- | --- |
| `apt-signing` | APT/release inventory signing key |
| `apt-storage` | APT object-storage writer |
| `installer-delivery` | Installer deployment only |

The committed public key and fingerprint are public trust anchors. Private keys, passphrases, deployment tokens, and revocation material must never be committed.

## Prepare an exact source commit

1. Update `main` through a reviewed pull request.
2. On a short-lived branch, update `pyproject.toml`, `uv.lock`, both installer version/channel constants, and release notes. Keep this as a dedicated final version commit.
3. Normally open a pull request and wait for all required gates. Land it as one verified squash commit or by fast-forwarding the local owning branch with `git merge --ff-only`, then push that branch.
4. Run Platform certification manually on the exact protected-branch SHA if it has not already passed there.

Do not create a tag manually. The workflow refuses to overwrite an existing tag, draft, prerelease, release, or APT version.

## Expiring test build

Use a test build when maintainers need an installable binary from an exact commit without publishing a public release candidate. Dispatch `test-build.yml` from `main` with:

- the exact 40-character source commit, which may be on a feature branch;
- the intended next stable `X.Y.Z` version; and
- one target for a quick test, or `all` for the eight release targets.

The workflow definition always comes from trusted `main`, checks out the chosen commit as data, and derives a unique version such as `0.15.1.dev12345+g01234567`. Its Debian equivalent is `0.15.1~dev12345+g01234567`, which sorts before both `0.15.1~rc1` and `0.15.1`. It builds selected targets in parallel and creates one GitHub Actions artifact retained for seven days. The run summary contains the exact `gh run download` command.

For example, after downloading a Linux glibc artifact:

```bash
sha256sum --check ravenstash-cli-v0.15.1.dev12345+g01234567-checksums.txt
sudo apt install ./ravenstash-cli_0.15.1.dev12345+g01234567_amd64.deb
rvs --version
```

GitHub requires repository access to download the artifact. Test builds are deliberately unsigned and are not tags, GitHub releases, release candidates, or APT publications. `rvs update` cannot discover them; install the downloaded package or portable bundle directly and replace it with a signed candidate or stable release after testing.

## Signed release candidate

Use a PEP 440 version such as `0.15.1rc1`. Dispatch `release.yml` from `main` with `kind=candidate`, the exact source branch, SHA, version, and matching `vMAJOR` channel. The workflow:

- validates all gates for that exact SHA;
- builds the eight release targets in parallel;
- assembles and attests one collision-free inventory;
- signs the checksum inventory with the release OpenPGP key; and
- publishes an immutable GitHub prerelease.

It does not restore, modify, or publish APT repository state. On a supported APT or portable installation, preview and install it with:

```bash
rvs update --candidate 0.15.1rc1
rvs update --candidate 0.15.1rc1 --apply
```

The CLI selects the architecture-specific Debian package or portable archive, verifies the signed inventory, checksum, version, and target, then asks before installing. Debian uses version `0.15.1~rc1`, ensuring the later stable `0.15.1` sorts as an upgrade. Portable POSIX installations switch an atomic version pointer; Windows stages a verified bundle and activates it after the running process exits.

For stable releases, APT publication and verification finish before the GitHub release is made public. The distribution-neutral signed update manifest is published only after that GitHub release succeeds, so portable clients never discover a stable version before its native archives are available.

## Stable release

Use an exact `X.Y.Z` version. Dispatch `release.yml` from `main` with `kind=stable`, the owning source branch and SHA, and its rolling `vMAJOR` channel. In the same visible workflow run, APT restore runs alongside the platform builds. After assembly, the workflow signs once, stages the immutable GitHub draft while APT publishes and verifies amd64 and arm64 in parallel, and then makes the GitHub release public. Portable update-policy publication runs alongside installer-promotion validation and signing. The workflow joins those results, deploys and verifies the exact signed installer bytes, and publishes the signed recommended-channel manifest last. That manifest also maps every retained `MAJOR.MINOR` line to its latest stable patch for one-shot `--to` selection.

Installer validation, deployment, and channel promotion are jobs in `release.yml`; normal stable publication has one workflow run and no second dispatch.

When the canonical APT store is genuinely empty, the release workflow creates a bounded `BOOTSTRAP` marker and signs a new repository from the release's amd64 and arm64 packages. This is how the first stable release creates the `ravenstash/apt` namespace; nothing is restored or copied from anywhere else. A non-empty store that is unauthenticated, mixed, or uses an unsupported suite name fails closed; the workflow never deletes published objects.

The scheduled `refresh-apt-metadata` workflow renews expiring APT metadata without changing packages, tags, channels, or installer recommendations. Its daily schedule runs only while the repository variable `RAVENSTASH_CLI_PUBLISHING` is set to `enabled`. Set that variable after the first stable release has published the APT repository: before then there is no signed state to refresh, and the scheduled run stays idle instead of failing. A manual dispatch runs whatever the variable says.

## Failure handling

Failed workflows retain short-lived handoff artifacts for diagnosis. Successful runs remove them. Never repair a partial release by overwriting published bytes; delete only an unpublished draft after inspection, or issue a new patch/RC version as appropriate.
