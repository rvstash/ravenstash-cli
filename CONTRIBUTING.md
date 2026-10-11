# Contributing to the Ravenstash CLI

Thank you for helping improve the Ravenstash CLI.

Unless explicitly stated otherwise, contributions intentionally submitted for inclusion in this repository are licensed under the Apache License 2.0, the same license as the project, without additional terms or conditions.

## Branches and pull requests

All development targets `main`.

1. Update the target branch and create a short-lived branch such as `feat/<topic>`, `fix/<topic>`, `ci/<topic>`, or `docs/<topic>`.
2. Make focused commits and open a pull request against `main`.
3. Mark the pull request ready when it should enter CI. Draft pull requests do not run the expensive suites.
4. Resolve review conversations and wait for the aggregate `CI gate`.
5. After approval and successful required gates, a maintainer lands the pull request locally: `git merge --ff-only` the reviewed branch into `main` and push `main`. GitHub then marks the pull request as merged. When a pull request's intermediate history should intentionally become one commit, the maintainer instead lands one verified squash commit.
6. The maintainer deletes the short-lived remote branch after it lands.

Both landing modes keep history linear. A fast-forward preserves the reviewed commit objects and signatures; a squash replaces them with one new commit. GitHub's **Rebase and merge** is not used, and merge commits are disabled. When `main` advances, update and re-sign the topic branch so it can fast-forward, and do not force-push a protected branch as part of normal development.

Push to `main` only to land a reviewed pull request that has passed its gates, never to bypass review, even when an administrator bypass technically permits it. That bypass is reserved for an explicitly authorized emergency recovery operation. The same required checks must pass for the exact resulting protected-branch commit.

## When CI runs

- A push to a feature branch with no pull request runs no repository CI.
- Opening, reopening, updating, or marking ready a non-draft pull request to `main` starts one CI run. The test and lint job runs for every change, because it includes repository-wide policy checks. A selector adds only the other checks affected by the changed paths; independent jobs run in parallel and converge on the stable `CI gate`. A new commit cancels the superseded run.
- A fast-forward or squash landing on `main` reruns that path-aware CI for the exact protected-branch commit. It does not build every release target and never publishes anything. Release automation accepts only an exact SHA that passed the aggregate gate.
- A manual CI dispatch deliberately runs every check. Use it when a path-limited run is insufficient for investigation; it is not a release operation.
- Platform certification is manual and scheduled. Release candidates and stable releases require a successful certification run for their exact SHA.

This means ordinary branch experimentation is quiet, while every commit that could merge or release is tested.

## Local checks

```bash
.venv/bin/coverage run -m pytest
.venv/bin/coverage report
.venv/bin/ruff format --check rvs tests packaging/repository packaging/scripts
.venv/bin/ruff check rvs tests packaging/repository packaging/scripts
.venv/bin/pyright
```

`coverage report` enforces the branch-coverage floor set in `pyproject.toml`; new code should come with tests that keep it there or raise it.

Workflow or packaging changes must also pass actionlint, shellcheck, repository policy tests, and installer Worker tests.

## Releases

Prepare every version on `main`. The final version commit contains only the project version, lockfile, installer constants, and release notes. See [RELEASING.md](RELEASING.md) for stable and candidate procedures.

## Community

Please follow the [Code of Conduct](CODE_OF_CONDUCT.md). Report vulnerabilities through the private process in [SECURITY.md](SECURITY.md), not a public issue. Use [SUPPORT.md](SUPPORT.md) to choose between support and an issue.
