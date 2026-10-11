"""Schema versioning of ``~/.rvs/config.toml``: refusal, migration, and reset."""

import hashlib
import os
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import tomli_w
from rvs import cli, config_migrations, entrypoint
from rvs import config as cfg_mod
from rvs.auth import commands as auth_cmd
from typer.testing import CliRunner


runner = CliRunner()

FIXTURES = Path(__file__).parent / "fixtures" / "config"
# The file the first public schema writes. Every later migration test starts
# from it, so it is never edited; a new schema adds a new fixture instead.
SCHEMA_1 = FIXTURES / "schema-1.toml"
SCHEMA_1_SHA256 = "905d00f11ce2ce17830f54b3aabc07acfcabfb2cbcf4bdbb3d700b3f65ac2fc7"
PRE_RELEASE_6 = FIXTURES / "pre-release.toml"

CURRENT = config_migrations.CURRENT_SCHEMA_VERSION
NEWER = CURRENT + 1
BACKUP_1 = "config.toml.schema-1.bak"
# Long before any test runs, so a rewrite can never reproduce it.
OLD_MTIME_NS = 1_600_000_000 * 10**9


def _point_config(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    config_dir = tmp_path / ".rvs"
    config_file = config_dir / "config.toml"
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_file)
    monkeypatch.setattr(cfg_mod, "PROFILE_ENV_FILE", config_dir / "profiles.env")
    monkeypatch.chdir(tmp_path)
    for key in (
        "RVS_ACCOUNT_REF",
        "RVS_API_URL",
        "RVS_ENV_FILE",
        "RVS_PROFILE",
        "RVS_REPOSITORY_DOMAIN",
        "RVS_SESSION_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    config_dir.mkdir(mode=0o700)
    return config_dir, config_file


def _install(config_file: Path, content: Path | bytes) -> bytes:
    data = content.read_bytes() if isinstance(content, Path) else content
    config_file.write_bytes(data)
    config_file.chmod(0o600)
    os.utime(config_file, ns=(OLD_MTIME_NS, OLD_MTIME_NS))
    return data


def _snapshot(config_dir: Path) -> dict[str, tuple[bytes, int, int, int]]:
    """Capture each file's bytes, modification time, inode, and mode."""
    return {
        path.name: (
            path.read_bytes(),
            path.stat().st_mtime_ns,
            path.stat().st_ino,
            stat.S_IMODE(path.stat().st_mode),
        )
        for path in config_dir.iterdir()
    }


def _stored(config_file: Path) -> dict:
    return tomllib.loads(config_file.read_text(encoding="utf-8"))


def _rename_profile(raw: dict, old: str, new: str) -> dict:
    raw["profiles"][new] = raw["profiles"].pop(old)
    if raw["default_profile"] == old:
        raw["default_profile"] = new
    return raw


@pytest.fixture
def synthetic_chain(monkeypatch) -> list[tuple[int, int]]:
    """Simulate schemas 2 and 3; the shipped registry holds no migration yet.

    Each step records its registry key and the schema version it was handed.
    The second step only succeeds on the result of the first.
    """
    applied: list[tuple[int, int]] = []

    def work_to_office(raw: dict) -> dict:
        applied.append((1, raw["schema_version"]))
        return _rename_profile(raw, "work", "office")

    def office_to_hq(raw: dict) -> dict:
        applied.append((2, raw["schema_version"]))
        return _rename_profile(raw, "office", "hq")

    monkeypatch.setattr(config_migrations, "CURRENT_SCHEMA_VERSION", 3)
    monkeypatch.setattr(config_migrations, "MIGRATIONS", {1: work_to_office, 2: office_to_hq})
    return applied


# ── The first public schema ───────────────────────────────────────────────────


def test_schema_1_fixture_is_never_edited() -> None:
    content = SCHEMA_1.read_bytes().replace(b"\r\n", b"\n")

    assert hashlib.sha256(content).hexdigest() == SCHEMA_1_SHA256


def test_registry_holds_one_migration_for_every_earlier_schema() -> None:
    assert config_migrations.CURRENT_SCHEMA_VERSION >= 1
    assert set(config_migrations.MIGRATIONS) == set(
        range(1, config_migrations.CURRENT_SCHEMA_VERSION)
    )


def test_reader_accepts_the_schema_1_fixture_without_touching_it(
    monkeypatch, tmp_path: Path
) -> None:
    config_dir, config_file = _point_config(monkeypatch, tmp_path)
    _install(config_file, SCHEMA_1)
    before = _snapshot(config_dir)

    cfg = cfg_mod.load()

    assert cfg.default_profile == "work"
    assert cfg.credential_store == "vault"
    assert list(cfg.profiles) == ["default", "work"]
    assert cfg.profiles["default"].api_url == "https://api.example.test"
    assert cfg.profiles["default"].account_ref is None
    work = cfg.profiles["work"]
    assert work.api_url == "https://api.work.example.test"
    assert work.credential_api_url == "https://api.work.example.test"
    assert work.repository_domain == "work.example.test"
    assert work.native_registries.npm.push_base_url == "https://push.npm.work.example.test"
    assert work.native_registries.oci_registry_base_url == "https://oci.work.example.test"
    assert work.account_ref == "ac_fake2222"
    assert work.active_account_ref == "ac_fake3333"
    assert work.credential_store == "vault"
    assert work.credential_type == "expiring"
    assert work.expires_at == "2099-01-01T00:00:00+00:00"
    assert work.refresh_expires_at == "2099-01-02T00:00:00+00:00"
    personal = work.accounts["ac_fake2222"]
    assert personal.account_type == "personal"
    assert personal.account_handle == "example-user"
    assert personal.selected_target is not None
    assert personal.selected_target.target_type == "official_cache"
    assert personal.selected_target.stable_selector == "mirror:rc_fake6666"
    assert personal.selected_target.format == "pypi"
    organization = work.accounts["ac_fake3333"]
    assert organization.organization_role == "admin"
    assert organization.authority_revision == 7
    assert organization.selected_target is not None
    assert organization.selected_target.display_selector == "repo:example-space/example-repo"
    assert organization.selected_target.namespace_ref == "in_fake4444"
    assert organization.selected_target.repository_ref == "ar_fake5555"
    assert _snapshot(config_dir) == before


def test_writer_reproduces_the_schema_1_fixture(monkeypatch, tmp_path: Path) -> None:
    _config_dir, config_file = _point_config(monkeypatch, tmp_path)
    _install(config_file, SCHEMA_1)

    cfg_mod.save(cfg_mod.load())

    written = config_file.read_text(encoding="utf-8")
    assert written.startswith("schema_version = 1\n")
    assert tomllib.loads(written) == tomllib.loads(SCHEMA_1.read_text(encoding="utf-8"))


def test_first_save_writes_the_schema_marker(monkeypatch, tmp_path: Path) -> None:
    _config_dir, config_file = _point_config(monkeypatch, tmp_path)

    cfg_mod.set_credential_store("vault")

    assert _stored(config_file) == {
        "schema_version": CURRENT,
        "default_profile": "default",
        "credential_store": "vault",
    }


@pytest.mark.parametrize("content", [b"", b"\n", b"# no settings yet\n"])
def test_empty_file_is_a_fresh_installation(monkeypatch, tmp_path: Path, content: bytes) -> None:
    config_dir, config_file = _point_config(monkeypatch, tmp_path)
    _install(config_file, content)
    before = _snapshot(config_dir)

    assert cfg_mod.load() == cfg_mod.RvsConfig()
    assert _snapshot(config_dir) == before


# ── Refused configurations ────────────────────────────────────────────────────

_PRE_RELEASE_CONFIGS: dict[str, Path | bytes] = {
    "config_version 6": PRE_RELEASE_6,
    "config_version without profiles": b'config_version = 6\ndefault_profile = "default"\n',
    "config_version of another type": b'config_version = "6"\n',
    "config_version beside schema_version": b"config_version = 6\nschema_version = 1\n",
    "no version with profiles": b'[profiles.default]\napi_url = "https://api.example.test"\n',
    "no version without profiles": b'default_profile = "default"\n',
}
_NEWER_SCHEMA = (
    f'schema_version = {NEWER}\n\n[profiles.default]\napi_url = "https://api.example.test"\n'
).encode()

_WRITE_PATHS = {
    "set_profile_metadata": lambda: cfg_mod.set_profile_metadata(
        "default", account_ref="ac_fake2222"
    ),
    "set_profile_value": lambda: cfg_mod.set_profile_value(
        "default", api_url="https://api.other.example.test"
    ),
    "clear_profile_credential_metadata": lambda: cfg_mod.clear_profile_credential_metadata(
        "default"
    ),
    "set_credential_store": lambda: cfg_mod.set_credential_store("vault"),
    "set_default_profile": lambda: cfg_mod.set_default_profile("default"),
    "cache_account": lambda: cfg_mod.cache_account(
        profile="default", account={"ref": "ac_fake2222", "type": "personal"}
    ),
    "set_selected_artifact_target": lambda: cfg_mod.set_selected_artifact_target(
        None, profile="default", account_ref="ac_fake2222"
    ),
    "delete_profile": lambda: cfg_mod.delete_profile("default"),
}


@pytest.mark.parametrize("content", _PRE_RELEASE_CONFIGS.values(), ids=_PRE_RELEASE_CONFIGS.keys())
def test_pre_release_config_is_refused_untouched(
    monkeypatch, tmp_path: Path, content: Path | bytes
) -> None:
    config_dir, config_file = _point_config(monkeypatch, tmp_path)
    _install(config_file, content)
    before = _snapshot(config_dir)

    with pytest.raises(cfg_mod.PreReleaseConfigError) as exc_info:
        cfg_mod.load()

    assert str(exc_info.value) == (
        f"{config_file} was written by a pre-release version of rvs and cannot be "
        "used by this release; it was left unchanged"
    )
    assert exc_info.value.hint == (
        "Run `rvs profile delete --all` to reset your local profiles, "
        "then sign in again with `rvs auth login`."
    )
    assert _snapshot(config_dir) == before


@pytest.mark.parametrize("version", [NEWER, 999])
def test_newer_schema_is_refused_untouched(monkeypatch, tmp_path: Path, version: int) -> None:
    config_dir, config_file = _point_config(monkeypatch, tmp_path)
    _install(config_file, _NEWER_SCHEMA.replace(f"= {NEWER}".encode(), f"= {version}".encode()))
    before = _snapshot(config_dir)

    with pytest.raises(cfg_mod.NewerConfigSchemaError) as exc_info:
        cfg_mod.load()

    assert str(exc_info.value) == (
        f"{config_file} uses configuration schema {version}, which is newer than "
        f"this rvs release supports (schema {CURRENT}); it was left unchanged"
    )
    assert exc_info.value.hint == (
        "Run `rvs update` or reinstall rvs to use a release that supports it."
    )
    assert "profile delete" not in exc_info.value.hint
    assert _snapshot(config_dir) == before


@pytest.mark.parametrize("content", [PRE_RELEASE_6, _NEWER_SCHEMA], ids=["pre-release", "newer"])
@pytest.mark.parametrize("write", _WRITE_PATHS.values(), ids=_WRITE_PATHS.keys())
def test_refused_config_blocks_every_write_path(
    monkeypatch, tmp_path: Path, content: Path | bytes, write
) -> None:
    config_dir, config_file = _point_config(monkeypatch, tmp_path)
    _install(config_file, content)
    before = _snapshot(config_dir)

    with pytest.raises(cfg_mod.ConfigError):
        write()

    assert _snapshot(config_dir) == before


@pytest.mark.parametrize("value", ['"1"', "true", "0", "-1", "1.0"])
def test_malformed_schema_version_is_refused_untouched(
    monkeypatch, tmp_path: Path, value: str
) -> None:
    config_dir, config_file = _point_config(monkeypatch, tmp_path)
    _install(config_file, f"schema_version = {value}\n".encode())
    before = _snapshot(config_dir)

    with pytest.raises(cfg_mod.ConfigError, match="schema_version must be a positive integer"):
        cfg_mod.load()

    assert _snapshot(config_dir) == before


def _entry_point_error(monkeypatch, capsys) -> str:
    monkeypatch.setattr(cli, "app", cfg_mod.load)
    monkeypatch.setattr(sys, "argv", ["rvs", "profile", "current"])

    with pytest.raises(SystemExit) as exc_info:
        entrypoint.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    return " ".join(captured.err.split())


def test_entry_point_tells_a_pre_release_user_to_reset_and_sign_in(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    _config_dir, config_file = _point_config(monkeypatch, tmp_path)
    _install(config_file, PRE_RELEASE_6)

    assert _entry_point_error(monkeypatch, capsys) == (
        f"Error: {config_file} was written by a pre-release version of rvs and cannot be "
        "used by this release; it was left unchanged. "
        "Run `rvs profile delete --all` to reset your local profiles, "
        "then sign in again with `rvs auth login`."
    )


def test_entry_point_tells_a_newer_schema_user_to_update(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    _config_dir, config_file = _point_config(monkeypatch, tmp_path)
    _install(config_file, _NEWER_SCHEMA)

    assert _entry_point_error(monkeypatch, capsys) == (
        f"Error: {config_file} uses configuration schema {NEWER}, which is newer than "
        f"this rvs release supports (schema {CURRENT}); it was left unchanged. "
        "Run `rvs update` or reinstall rvs to use a release that supports it."
    )


@pytest.mark.parametrize(
    ("content", "expected", "unexpected"),
    [
        (PRE_RELEASE_6, ("pre-release version of rvs", "rvs profile delete --all"), "rvs update"),
        (_NEWER_SCHEMA, (f"configuration schema {NEWER}", "rvs update"), "rvs profile delete"),
    ],
    ids=["config_version 6", "newer schema"],
)
def test_cli_refuses_an_unusable_config_without_touching_it(
    tmp_path: Path, content: Path | bytes, expected: tuple[str, ...], unexpected: str
) -> None:
    _install(tmp_path / "config.toml", content)
    before = _snapshot(tmp_path)

    result = subprocess.run(
        [sys.executable, "-m", "rvs.entrypoint", "profile", "current"],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ | {"RVS_HOME": str(tmp_path)},
    )

    stderr = " ".join(result.stderr.split())
    assert result.returncode == 1
    assert result.stdout == ""
    for text in expected:
        assert text in stderr
    assert unexpected not in stderr
    assert "Traceback" not in stderr
    assert _snapshot(tmp_path) == before


def test_update_never_depends_on_the_config_file() -> None:
    # `rvs update` is the way out of a newer schema, so that file must not block it.
    code = "import sys, rvs.update; assert 'rvs.config' not in sys.modules"

    result = subprocess.run(
        [sys.executable, "-c", code], check=False, capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr


# ── Reset ─────────────────────────────────────────────────────────────────────


def _unexpected_revocation(profile: str) -> None:
    raise AssertionError(f"a refused config must not reach the server for {profile}")


@pytest.mark.parametrize(
    "content",
    [PRE_RELEASE_6, _PRE_RELEASE_CONFIGS["no version with profiles"]],
    ids=["config_version 6", "no version"],
)
def test_reset_command_recovers_from_a_pre_release_config(
    monkeypatch, tmp_path: Path, content: Path | bytes
) -> None:
    config_dir, config_file = _point_config(monkeypatch, tmp_path)
    _install(config_file, content)
    profiles = list(_stored(config_file)["profiles"])
    deleted: list[str] = []
    monkeypatch.setattr(auth_cmd.auth_mod, "delete_token_from_all_stores", deleted.append)
    monkeypatch.setattr(auth_cmd, "_revoke_server_session", _unexpected_revocation)

    result = runner.invoke(auth_cmd.profile_app, ["delete", "--all"])

    assert result.exit_code == 0, result.output
    assert "All profiles deleted." in result.output
    output = " ".join(result.output.split())
    assert (
        "The local configuration was written by a pre-release version of rvs, so device "
        "sessions are not revoked on the server; their refresh tokens stay valid until they expire."
    ) in output
    assert "cannot be read" not in output
    assert deleted == profiles
    assert _stored(config_file) == {
        "schema_version": CURRENT,
        "default_profile": "default",
        "credential_store": "auto",
    }
    assert cfg_mod.load() == cfg_mod.RvsConfig()
    # The pre-release file is replaced, never copied aside.
    assert [path.name for path in config_dir.iterdir()] == ["config.toml"]


def test_reset_command_recovers_from_an_unreadable_config(monkeypatch, tmp_path: Path) -> None:
    _config_dir, config_file = _point_config(monkeypatch, tmp_path)
    _install(
        config_file,
        b'schema_version = "1"\n\n[profiles.default]\napi_url = "https://api.example.test"\n',
    )
    deleted: list[str] = []
    monkeypatch.setattr(auth_cmd.auth_mod, "delete_token_from_all_stores", deleted.append)
    monkeypatch.setattr(auth_cmd, "_revoke_server_session", _unexpected_revocation)

    result = runner.invoke(auth_cmd.profile_app, ["delete", "--all"])

    assert result.exit_code == 0, result.output
    assert "All profiles deleted." in result.output
    output = " ".join(result.output.split())
    assert (
        "The local configuration cannot be read, so device sessions are not revoked on the "
        "server; their refresh tokens stay valid until they expire."
    ) in output
    assert "pre-release" not in output
    assert deleted == ["default"]
    assert cfg_mod.load() == cfg_mod.RvsConfig()


# ── Migration ─────────────────────────────────────────────────────────────────


def test_migrate_applies_steps_without_modifying_its_input(synthetic_chain) -> None:
    raw = tomllib.loads(SCHEMA_1.read_text(encoding="utf-8"))
    original = tomllib.loads(SCHEMA_1.read_text(encoding="utf-8"))

    migrated = config_migrations.migrate(raw, from_version=1, to_version=3)

    assert raw == original
    assert synthetic_chain == [(1, 1), (2, 2)]
    assert migrated["schema_version"] == 3
    assert list(migrated["profiles"]) == ["default", "hq"]
    assert config_migrations.migrate(migrated, from_version=3, to_version=3) == migrated


def test_migration_runs_each_step_in_order_and_keeps_the_original(
    monkeypatch, tmp_path: Path, synthetic_chain
) -> None:
    config_dir, config_file = _point_config(monkeypatch, tmp_path)
    original = _install(config_file, SCHEMA_1)

    cfg = cfg_mod.load()

    assert synthetic_chain == [(1, 1), (2, 2)]
    assert cfg.default_profile == "hq"
    assert list(cfg.profiles) == ["default", "hq"]
    assert cfg.profiles["hq"].account_ref == "ac_fake2222"
    expected = _rename_profile(tomllib.loads(original.decode("utf-8")), "work", "hq")
    assert _stored(config_file) == expected | {"schema_version": 3}
    assert (config_dir / BACKUP_1).read_bytes() == original
    assert sorted(path.name for path in config_dir.iterdir()) == ["config.toml", BACKUP_1]


def test_migration_starts_from_the_stored_schema(
    monkeypatch, tmp_path: Path, synthetic_chain
) -> None:
    config_dir, config_file = _point_config(monkeypatch, tmp_path)
    schema_2 = config_migrations.migrate(
        tomllib.loads(SCHEMA_1.read_text(encoding="utf-8")), from_version=1, to_version=2
    )
    original = _install(config_file, tomli_w.dumps(schema_2).encode("utf-8"))
    synthetic_chain.clear()

    cfg = cfg_mod.load()

    assert synthetic_chain == [(2, 2)]
    assert cfg.default_profile == "hq"
    assert (config_dir / "config.toml.schema-2.bak").read_bytes() == original
    assert not (config_dir / BACKUP_1).exists()


def test_migration_is_idempotent(monkeypatch, tmp_path: Path, synthetic_chain) -> None:
    config_dir, config_file = _point_config(monkeypatch, tmp_path)
    _install(config_file, SCHEMA_1)
    first = cfg_mod.load()
    for path in config_dir.iterdir():
        os.utime(path, ns=(OLD_MTIME_NS, OLD_MTIME_NS))
    migrated = _snapshot(config_dir)
    synthetic_chain.clear()

    second = cfg_mod.load()

    assert second == first
    assert synthetic_chain == []
    assert _snapshot(config_dir) == migrated


def test_migration_never_overwrites_an_existing_backup(
    monkeypatch, tmp_path: Path, synthetic_chain
) -> None:
    config_dir, config_file = _point_config(monkeypatch, tmp_path)
    _install(config_file, SCHEMA_1)
    backup = config_dir / BACKUP_1
    _install(backup, b"# an earlier backup of schema 1\n")
    before = _snapshot(config_dir)[BACKUP_1]

    cfg_mod.load()

    assert _stored(config_file)["schema_version"] == 3
    assert _snapshot(config_dir)[BACKUP_1] == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
@pytest.mark.parametrize("mode", [0o600, 0o400, 0o644])
def test_migration_keeps_the_config_and_its_backup_private(
    monkeypatch, tmp_path: Path, synthetic_chain, mode: int
) -> None:
    config_dir, config_file = _point_config(monkeypatch, tmp_path)
    _install(config_file, SCHEMA_1)
    config_file.chmod(mode)

    cfg_mod.load()

    assert stat.S_IMODE(config_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(config_file.stat().st_mode) == 0o600
    assert stat.S_IMODE((config_dir / BACKUP_1).stat().st_mode) == 0o600


def test_migration_backup_holds_only_the_config_file(
    monkeypatch, tmp_path: Path, synthetic_chain
) -> None:
    config_dir, config_file = _point_config(monkeypatch, tmp_path)
    original = _install(config_file, SCHEMA_1)
    # The credential stores live beside the config file.
    stores = {
        "credentials.vault": b"synthetic-vault-credential",
        "credentials.plaintext.json": b'{"work": "synthetic-plaintext-credential"}',
    }
    for name, content in stores.items():
        _install(config_dir / name, content)
    before = _snapshot(config_dir)

    cfg_mod.load()

    after = _snapshot(config_dir)
    assert set(after) - set(before) == {BACKUP_1}
    assert after[BACKUP_1][0] == original
    assert b"synthetic-" not in after[BACKUP_1][0]
    for name in stores:
        assert after[name] == before[name]


def test_stored_config_names_no_credential_field() -> None:
    def keys(table: dict) -> set[str]:
        return set(table) | {
            key for value in table.values() if isinstance(value, dict) for key in keys(value)
        }

    names = keys(tomllib.loads(SCHEMA_1.read_text(encoding="utf-8")))

    assert not {
        name for name in names if any(word in name for word in ("token", "secret", "password"))
    }


class _Crash(BaseException):
    """Stands in for the process dying; nothing in rvs catches it."""


def test_crash_between_backup_and_replacement_leaves_a_config_that_migrates_again(
    monkeypatch, tmp_path: Path, synthetic_chain
) -> None:
    config_dir, config_file = _point_config(monkeypatch, tmp_path)
    original = _install(config_file, SCHEMA_1)
    before = _snapshot(config_dir)["config.toml"]
    replace = os.replace

    def crash_before_replacing_the_config(source, destination) -> None:
        if Path(destination) == config_file:
            raise _Crash
        replace(source, destination)

    with monkeypatch.context() as crashing:
        crashing.setattr(cfg_mod.os, "replace", crash_before_replacing_the_config)
        with pytest.raises(_Crash):
            cfg_mod.load()

    interrupted = _snapshot(config_dir)
    assert interrupted["config.toml"] == before
    assert interrupted[BACKUP_1][0] == original
    # A killed process also leaves its temporary file behind.
    (config_dir / ".config.interrupted.tmp").write_bytes(b"schema_version = ")
    synthetic_chain.clear()

    cfg = cfg_mod.load()

    assert synthetic_chain == [(1, 1), (2, 2)]
    assert cfg.default_profile == "hq"
    assert _stored(config_file)["schema_version"] == 3
    assert _snapshot(config_dir)[BACKUP_1] == interrupted[BACKUP_1]


def _fails(raw: dict) -> dict:
    raise RuntimeError("synthetic failure")


def _returns_nothing(raw: dict) -> None:
    raw["default_profile"] = "changed in place"


def _breaks_profiles(raw: dict) -> dict:
    return raw | {"profiles": "not a table"}


@pytest.mark.parametrize(
    ("second_step", "reason"),
    [
        (_fails, "synthetic failure"),
        (_returns_nothing, "the migration from schema 2 did not return a table"),
        (_breaks_profiles, "profiles must be a table"),
        (None, "no migration is registered from schema 2"),
    ],
    ids=["step raises", "step returns nothing", "invalid result", "missing step"],
)
def test_failed_migration_leaves_the_original_intact(
    monkeypatch, tmp_path: Path, synthetic_chain, second_step, reason: str
) -> None:
    config_dir, config_file = _point_config(monkeypatch, tmp_path)
    original = _install(config_file, SCHEMA_1)
    before = _snapshot(config_dir)["config.toml"]
    working = dict(config_migrations.MIGRATIONS)
    broken = {1: working[1]} | ({2: second_step} if second_step is not None else {})
    monkeypatch.setattr(config_migrations, "MIGRATIONS", broken)

    with pytest.raises(cfg_mod.ConfigMigrationError) as exc_info:
        cfg_mod.load()

    assert str(exc_info.value) == (
        f"could not migrate {config_file} from configuration schema 1 to schema 3: "
        f"{reason}; it was left unchanged"
    )
    assert exc_info.value.hint == (
        "Run `rvs update` and try again, or run `rvs profile delete --all` "
        "to reset your local profiles."
    )
    failed = _snapshot(config_dir)
    assert failed["config.toml"] == before
    assert failed[BACKUP_1][0] == original
    assert set(failed) == {"config.toml", BACKUP_1}

    monkeypatch.setattr(config_migrations, "MIGRATIONS", working)
    assert cfg_mod.load().default_profile == "hq"
    assert _snapshot(config_dir)[BACKUP_1] == failed[BACKUP_1]


def test_migration_that_cannot_write_leaves_the_original_intact(
    monkeypatch, tmp_path: Path, synthetic_chain
) -> None:
    config_dir, config_file = _point_config(monkeypatch, tmp_path)
    _install(config_file, SCHEMA_1)
    before = _snapshot(config_dir)

    def full_disk(source, destination) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cfg_mod.os, "replace", full_disk)

    with pytest.raises(cfg_mod.ConfigMigrationError, match="No space left on device"):
        cfg_mod.load()

    assert synthetic_chain == []
    assert _snapshot(config_dir) == before
