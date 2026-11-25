"""Schema version and forward migrations for ``~/.rvs/config.toml``.

The schema version numbers the stored shape of the config file. It is
independent of the rvs release version and changes only when that shape does.

To change the stored shape, raise :data:`CURRENT_SCHEMA_VERSION` by one and
register the transform from the previous version in :data:`MIGRATIONS`.
:mod:`rvs.config` applies the chain when it loads a file written with an older
schema; it owns the backup and the atomic replacement of the file.
"""

import copy
from collections.abc import Callable


SCHEMA_VERSION_KEY = "schema_version"
CURRENT_SCHEMA_VERSION = 2

Migration = Callable[[dict], dict]

_FORMATS = ("pypi", "npm", "maven", "oci")


def _per_format_selections(raw: dict) -> dict:
    """Schema 1 -> 2: one selected target per account becomes one per format.

    A selection that names its format keeps exactly that format. A repository
    selection without a format (a repository with several formats) applied to
    every wrapper in schema 1, whatever format the wrapper used, so it is kept
    for all four formats; selecting the repository again records only the
    formats it actually has. A selection that cannot be placed is dropped.
    """
    profiles = raw.get("profiles")
    if not isinstance(profiles, dict):
        return raw
    for profile in profiles.values():
        accounts = profile.get("accounts") if isinstance(profile, dict) else None
        if not isinstance(accounts, dict):
            continue
        for account in accounts.values():
            if not isinstance(account, dict) or "selected_target" not in account:
                continue
            selected = account.pop("selected_target")
            if not isinstance(selected, dict):
                continue
            stored_format = selected.get("format")
            if stored_format in _FORMATS:
                kinds: tuple[str, ...] = (stored_format,)
            elif stored_format is None and selected.get("target_type") == "repository":
                kinds = _FORMATS
            else:
                continue
            account["selected_targets"] = {kind: selected | {"format": kind} for kind in kinds}
    return raw


# Maps a schema version to the pure transform that upgrades a parsed config of
# that version to the next one. Schema 1 is the first, so nothing migrates to it.
MIGRATIONS: dict[int, Migration] = {1: _per_format_selections}


def migrate(raw: dict, *, from_version: int, to_version: int) -> dict:
    """Return *raw* upgraded one schema version at a time; *raw* is not modified."""
    migrated = copy.deepcopy(raw)
    for version in range(from_version, to_version):
        step = MIGRATIONS.get(version)
        if step is None:
            raise ValueError(f"no migration is registered from schema {version}")
        migrated = step(migrated)
        if not isinstance(migrated, dict):
            raise ValueError(f"the migration from schema {version} did not return a table")
        migrated[SCHEMA_VERSION_KEY] = version + 1
    return migrated
