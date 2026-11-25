# Ravenstash CLI quickstart

This guide takes you from sign-in to installing a private package.

## 1. Install and sign in

```bash
curl -fsSL https://ravenstash.com/install.sh | bash
rvs auth login
```

The login command opens Ravenstash in your browser. Return to the terminal when the browser confirms that sign-in is complete.

Your personal account is ready for public packages at once: with nothing selected, `rvs pip`, `rvs uv`, `rvs npm`, `rvs pnpm`, and `rvs mvn` install from your account's default Ravenstash mirror of PyPI, npm, or Maven Central:

```bash
rvs pip install requests
```

`rvs` names the mirror it uses on standard error. See [Use a private mirror](#use-a-private-mirror) for the details.

## 2. Choose a personal account or organization

List the accounts you can use:

```bash
rvs account list
```

Choose one interactively:

```bash
rvs account switch
```

Use up/down and Enter to select a personal account or organization. You can also switch directly with `rvs account switch user:USERNAME` or `rvs account switch org:HANDLE`. The CLI always displays account handles with one of these prefixes, while namespace names remain unprefixed. A bare account handle is also accepted.

## 3. Choose a repository

```bash
rvs art repo list
rvs art select platform/packages
```

The first name is the namespace and the second is the repository. Your choice is remembered for the selected account and profile, separately for each format: this selects the repository for every format it has. Add `--format pypi` (or `-f pypi,npm`) to select it only for those formats, and use `rvs art clear --format FORMAT` to forget one format's selection.

Check it at any time:

```bash
rvs status
```

## 4. Install a package

Use the command for your package format:

```bash
rvs pip install internal-sdk
rvs npm install @acme/design-system
rvs mvn verify
```

`rvs` gives the package tool temporary access for that command. It does not add a Ravenstash token to the project's package settings.

## Use a private mirror

A personal account has a private mirror of each Ravenstash-provided source. When nothing is selected for pypi, npm, or maven, installs and other reads through `rvs pip`, `rvs uv`, `rvs npm`, `rvs pnpm`, and `rvs mvn` use the default mirror of that format automatically. This applies only to a personal account signed in with `rvs auth login`; organization accounts and `RVS_TOKEN` automation always need a selection or `--rvs-target`, and publishing never uses a mirror.

To make the choice explicit, or to use another Ravenstash-provided source:

```bash
rvs art mirror select pypiorg
rvs pip install requests
```

Use `npmjs` for npm or `maven-central` for Maven. A mirror is read-only and covers one format, so selecting it leaves your other formats unchanged. If you deleted a mirror, add it again with `rvs art mirror create --format pypi --select`.

Container images and Helm charts have no mirror: `rvs docker`, `rvs helm`, and `rvs oras` always need a private repository with the oci format, selected with `rvs art select NAMESPACE/REPOSITORY`.

## Publish a package

Select the destination first:

```bash
rvs art select platform/packages
rvs python -m build
rvs twine upload dist/*
```

With uv, use `rvs uv build` and `rvs uv publish` instead.

You can also publish with `rvs npm publish` or `rvs mvn deploy`. Ravenstash shows the destination and asks for confirmation before uploading.

Check what the selected repository now holds:

```bash
rvs art package list
rvs art package show PACKAGE
```

Add `--format pypi`, `npm`, or `maven` when the repository has more than one of these formats.

In a protected automation job, add `--rvs-yes` before the wrapped tool's normal arguments:

```bash
rvs npm --rvs-yes publish
```

## Use another profile

Profiles are useful when one computer connects to more than one Ravenstash environment:

```bash
rvs auth login --profile work
rvs profile use work
rvs profile current
```

## Troubleshooting

Show the current choices:

```bash
rvs status
rvs art status
```

`rvs art status` shows one line per format: the selected repository or mirror, the default mirror `rvs` uses automatically, or `not selected`. If nothing is selected for a format you need, choose a repository with `rvs art select NAMESPACE/REPOSITORY`. If a name is unclear, use `rvs art repo list` and copy the namespace and repository names shown there.

For more help, see the [public CLI documentation](https://docs.ravenstash.com/cli/overview/).
