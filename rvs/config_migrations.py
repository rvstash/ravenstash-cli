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
CURRENT_SCHEMA_VERSION = 1

Migration = Callable[[dict], dict]

# Maps a schema version to the pure transform that upgrades a parsed config of
# that version to the next one. Schema 1 is the first, so nothing migrates to it.
MIGRATIONS: dict[int, Migration] = {}


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
