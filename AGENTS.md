# AGENTS.md

This file is the complete operating guide for automated contributors to the Ravenstash CLI repository; it relies on nothing outside this repository.

## Repository scope

`rvs` is the Ravenstash command-line client. Application code lives in `rvs/`, tests in `tests/`, user documentation in `docs/`, and release/install tooling in `packaging/` and `.github/workflows/`.

Keep user identity, the local profile, the acting account, and the selected artifact target distinct. Keep secrets in the established credential stores; non-secret profile metadata belongs in `~/.rvs/config.toml`. Native install and publish flows should continue to delegate to their native tools. Keep the control-plane URL separate from package download and upload URLs.

## Contribution workflow

- Use a short-lived branch and pull request for every change. After approval and successful required gates, land it in one of two linear ways: locally run `git merge --ff-only` and push `main` (preferred; it keeps the reviewed, signed commit objects), or create one verified squash commit when the intermediate history should become one commit.
- Do not use GitHub's **Rebase and merge** or create merge commits. Never force-push a protected branch.
- Target `main` for all development and releases.
- Keep commits reviewable and self-contained. If a branch cannot fast-forward, either use the allowed squash mode or have the contributor update and re-sign the branch; GitHub's rebase operation is not an allowed landing mode.
- Preserve unrelated changes in a dirty worktree.
- Do not commit, push, open or merge a pull request, tag, publish, release, deploy, or otherwise mutate an external environment unless the user explicitly requests that action.

## Implementation rules

- Keep command modules thin and route shared behavior through common helpers.
- Keep registry-specific protocol behavior in the applicable `rvs/artifacts/` or `rvs/native/` module; do not mix it into unrelated command groups.
- Do not hardcode development or staging endpoints. Support alternate environments through user configuration or process environment variables.
- Take every published name (repository, release storage, package, asset, keyring, receipt, package-manager identifier) from `rvs/identity.py` in the CLI and from `packaging/identity.env` in packaging and workflows. Only the standalone installers embed copies, and `tests/test_identity.py` pins them.
- Never add destructive APT reset behavior. The bytes and metadata of a retained APT suite are immutable; corrections receive a new patch version. This repository's tooling never removes a distribution namespace.
- Release candidates use PEP 440 `X.Y.ZrcN` versions and GitHub prereleases. They must not be added to stable APT suites.
- Test builds use `X.Y.Z.dev<RUN_ID>+g<SHA8>`, remain unsigned seven-day GitHub Actions artifacts, and must never create tags, releases, or APT state.
- A release bump is a dedicated final commit containing the version, matching lockfile and installer updates, and release notes.

## Verification

Run from the repository root:

```bash
.venv/bin/coverage run -m pytest
.venv/bin/coverage report
.venv/bin/ruff format --check rvs tests packaging/repository packaging/scripts
.venv/bin/ruff check rvs tests packaging/repository packaging/scripts
.venv/bin/pyright
```

For workflow or packaging changes, also run actionlint, shellcheck, the `packaging/repository` unit tests, and the installer Worker tests when those tools are available. Report files changed, checks run and their results, and any relevant check that could not be run.
