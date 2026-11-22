import os
import shutil
import sqlite3
import stat
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from fastapi.testclient import TestClient

import pmsbridge
from pmsbridge.app import create_app
from pmsbridge.core.settings import Settings, override_settings
from pmsbridge.core.storage import (
    BACKUP_NAME,
    MIGRATIONS_DIR,
    Base,
    StorageError,
    alembic_config,
    import_model_modules,
    migrate,
    prune_pre_migration_backups,
    reset_database,
)

BASELINE = "a3f1c9d27b40"


def real_head() -> str:
    head = ScriptDirectory.from_config(alembic_config(Path("unused.db"))).get_current_head()
    assert head is not None
    return head


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_the_real_history_has_exactly_one_head() -> None:
    heads = ScriptDirectory.from_config(alembic_config(Path("unused.db"))).get_heads()
    assert len(heads) == 1, (
        f"migrations/versions has {len(heads)} heads ({', '.join(sorted(heads))}). A new revision "
        "must set down_revision to the current head so the history stays a single line."
    )


def test_migrations_ship_inside_the_package() -> None:
    assert Path(pmsbridge.__file__).parent / "migrations" == MIGRATIONS_DIR
    assert (MIGRATIONS_DIR / "env.py").is_file()
    assert (MIGRATIONS_DIR / "script.py.mako").is_file()
    assert list((MIGRATIONS_DIR / "versions").glob("*_baseline.py"))


# ---- Scripts built for the tests: the baseline plus probe revisions on top of it ----


def revision_file(directory: Path, revision: str, down: str, body: str) -> None:
    (directory / "versions" / f"{revision}_probe.py").write_text(
        "import sqlalchemy as sa\nfrom alembic import op\n"
        f"revision = {revision!r}\ndown_revision = {down!r}\n"
        "branch_labels = None\ndepends_on = None\n\n"
        f"def upgrade() -> None:\n    {body}\n\n"
        "def downgrade() -> None:\n    pass\n"
    )


@pytest.fixture
def scripts(tmp_path: Path) -> Path:
    """Only env.py, the template and the baseline, so these tests ignore later real migrations."""
    target = tmp_path / "scripts"
    (target / "versions").mkdir(parents=True)
    for name in ("env.py", "script.py.mako"):
        shutil.copy(MIGRATIONS_DIR / name, target / name)
    for baseline in (MIGRATIONS_DIR / "versions").glob("*_baseline.py"):
        shutil.copy(baseline, target / "versions" / baseline.name)
    return target


def baseline_db(settings: Settings, scripts: Path, *statements: str) -> None:
    """A database at the baseline revision, plus some tables of its own."""
    migrate(settings, script_location=scripts)
    raw = sqlite3.connect(settings.database_path)
    for statement in statements:
        raw.execute(statement)
    raw.commit()
    raw.close()


def backups(settings: Settings) -> list[Path]:
    return sorted(settings.backup_dir.glob("pre-migration-*.db"))


def tables(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    finally:
        connection.close()


def test_migrate_a_fresh_database_takes_no_backup(dev_settings: Settings) -> None:
    result = migrate(dev_settings)
    assert result.backup_path is None
    assert result.revisions_before == ()
    assert result.revisions_after == (real_head(),)
    assert backups(dev_settings) == []


def test_migrate_an_up_to_date_database_takes_no_backup(dev_settings: Settings) -> None:
    migrate(dev_settings)
    result = migrate(dev_settings)
    assert result.backup_path is None
    assert result.revisions_before == result.revisions_after == (real_head(),)
    assert backups(dev_settings) == []


def test_pending_migration_takes_a_backup_first(dev_settings: Settings, scripts: Path) -> None:
    baseline_db(
        dev_settings,
        scripts,
        "CREATE TABLE keepsake (note TEXT)",
        "INSERT INTO keepsake VALUES ('before')",
    )
    revision_file(
        scripts, "b100", BASELINE, 'op.create_table("probe", sa.Column("id", sa.Integer))'
    )

    result = migrate(dev_settings, script_location=scripts)

    assert result.revisions_after == ("b100",)
    assert result.backup_path is not None
    assert result.backup_path.parent == dev_settings.backup_dir
    assert BACKUP_NAME.match(result.backup_path.name)
    assert mode(result.backup_path) == 0o600
    before = tables(result.backup_path)
    assert "keepsake" in before
    assert "probe" not in before
    assert "probe" in tables(dev_settings.database_path)


def make_old_backups(settings: Settings, *stamps: str, origin: str = "x") -> list[Path]:
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    paths = [settings.backup_dir / f"pre-migration-{stamp}-1-from-{origin}.db" for stamp in stamps]
    for path in paths:
        path.write_bytes(b"old")
    return paths


def test_old_pre_migration_backups_are_pruned(dev_settings: Settings, scripts: Path) -> None:
    settings = dev_settings.model_copy(update={"pre_migration_backups_keep": 3})
    baseline_db(settings, scripts)
    old = make_old_backups(settings, "20200101T000000Z", "20200102T000000Z", "20200103T000000Z")
    unrelated = settings.backup_dir / "manual-backup.db"
    unrelated.write_bytes(b"mine")
    revision_file(scripts, "b100", BASELINE, "pass")

    result = migrate(settings, script_location=scripts)

    assert result.backup_path is not None
    assert sorted(backups(settings)) == sorted([old[1], old[2], result.backup_path])
    assert unrelated.exists()


def test_keep_one_retains_the_backup_just_taken(dev_settings: Settings, scripts: Path) -> None:
    settings = dev_settings.model_copy(update={"pre_migration_backups_keep": 1})
    baseline_db(settings, scripts)
    make_old_backups(settings, "20200101T000000Z", "20200102T000000Z")
    revision_file(scripts, "b100", BASELINE, "pass")

    result = migrate(settings, script_location=scripts)

    assert result.backup_path is not None
    assert backups(settings) == [result.backup_path]


def test_prune_never_deletes_the_protected_file_even_if_it_sorts_oldest(tmp_path: Path) -> None:
    newest_by_name = tmp_path / "pre-migration-20300101T000000Z-1-from-x.db"
    protected = tmp_path / "pre-migration-20200101T000000Z-1-from-x.db"
    for path in (newest_by_name, protected):
        path.write_bytes(b"x")
    removed = prune_pre_migration_backups(tmp_path, keep=1, protect=protected)
    assert removed == [newest_by_name]
    assert protected.exists()


def test_prune_orders_by_stamp_then_counter(tmp_path: Path) -> None:
    names = [
        "pre-migration-20200101T000000Z-2-from-x.db",
        "pre-migration-20200101T000000Z-10-from-x.db",
        "pre-migration-20200101T000000Z-1-from-x.db",
    ]
    for name in names:
        (tmp_path / name).write_bytes(b"x")
    prune_pre_migration_backups(tmp_path, keep=1)
    assert [p.name for p in tmp_path.iterdir()] == [names[1]]


def failing_probe(scripts: Path) -> None:
    revision_file(scripts, "b100", BASELINE, 'op.execute("NOT SQL")')


def test_failed_migration_rolls_back_and_the_message_is_operator_ready(
    dev_settings: Settings, scripts: Path
) -> None:
    baseline_db(dev_settings, scripts, "CREATE TABLE keepsake (note TEXT)")
    revision_file(
        scripts,
        "b100",
        BASELINE,
        'op.create_table("half_done", sa.Column("id", sa.Integer))\n    op.execute("NOT SQL")',
    )

    with pytest.raises(StorageError) as caught:
        migrate(dev_settings, script_location=scripts)

    message = str(caught.value)
    assert "left unchanged" in message
    assert str(backups(dev_settings)[0]) in message
    assert "previous version" in message
    found = tables(dev_settings.database_path)
    assert "half_done" not in found
    assert "keepsake" in found


def test_repeated_failing_restarts_take_one_backup_and_keep_older_versions(
    dev_settings: Settings, scripts: Path
) -> None:
    settings = dev_settings.model_copy(update={"pre_migration_backups_keep": 2})
    baseline_db(settings, scripts, "CREATE TABLE keepsake (note TEXT)")
    older = make_old_backups(settings, "20200101T000000Z", origin="olderrev")[0]
    failing_probe(scripts)
    time.sleep(0.05)

    for _ in range(4):
        with pytest.raises(StorageError, match="left unchanged"):
            migrate(settings, script_location=scripts)

    assert older.exists()
    assert len(backups(settings)) == 2
    newest = backups(settings)[-1]
    assert newest.name.endswith(f"-from-{BASELINE}.db")
    assert tables(newest) >= {"keepsake"}


def test_a_change_to_the_database_takes_a_new_backup(dev_settings: Settings, scripts: Path) -> None:
    baseline_db(dev_settings, scripts, "CREATE TABLE keepsake (note TEXT)")
    failing_probe(scripts)
    with pytest.raises(StorageError):
        migrate(dev_settings, script_location=scripts)
    time.sleep(0.05)
    raw = sqlite3.connect(dev_settings.database_path)
    raw.execute("INSERT INTO keepsake VALUES ('later')")
    raw.commit()
    raw.close()
    os.utime(dev_settings.database_path)
    with pytest.raises(StorageError):
        migrate(dev_settings, script_location=scripts)
    assert len(backups(dev_settings)) == 2


def test_a_database_from_a_newer_version_gets_its_own_message(
    dev_settings: Settings, scripts: Path
) -> None:
    baseline_db(dev_settings, scripts)
    raw = sqlite3.connect(dev_settings.database_path)
    raw.execute("UPDATE alembic_version SET version_num = 'from_the_future'")
    raw.commit()
    raw.close()

    with pytest.raises(StorageError) as caught:
        migrate(dev_settings, script_location=scripts)

    message = str(caught.value)
    assert "newer version" in message
    assert "from_the_future" in message
    assert "not changed" in message
    assert str(dev_settings.backup_dir) in message
    assert backups(dev_settings) == []


def test_an_unreadable_database_file_is_reported_without_changes(
    dev_settings: Settings,
) -> None:
    dev_settings.data_dir.mkdir(parents=True)
    dev_settings.database_path.write_bytes(b"this is not a sqlite database" * 50)
    with pytest.raises(StorageError, match="cannot be opened"):
        migrate(dev_settings)
    assert dev_settings.database_path.read_bytes().startswith(b"this is not")


def test_foreign_key_violations_after_a_migration_roll_it_back(
    dev_settings: Settings, scripts: Path
) -> None:
    baseline_db(
        dev_settings,
        scripts,
        "CREATE TABLE parent (id INTEGER PRIMARY KEY)",
        "CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))",
    )
    revision_file(scripts, "b100", BASELINE, 'op.execute("INSERT INTO child VALUES (42)")')

    with pytest.raises(StorageError, match="upgrade failed"):
        migrate(dev_settings, script_location=scripts)

    live = sqlite3.connect(dev_settings.database_path)
    assert live.execute("SELECT count(*) FROM child").fetchone() == (0,)
    assert live.execute("SELECT version_num FROM alembic_version").fetchall() == [(BASELINE,)]
    live.close()


def test_batch_mode_can_alter_a_table(dev_settings: Settings, scripts: Path) -> None:
    baseline_db(dev_settings, scripts, "CREATE TABLE widget (id INTEGER PRIMARY KEY, name TEXT)")
    revision_file(
        scripts,
        "b100",
        BASELINE,
        'with op.batch_alter_table("widget") as batch:\n        batch.drop_column("name")',
    )
    migrate(dev_settings, script_location=scripts)
    live = sqlite3.connect(dev_settings.database_path)
    columns = [row[1] for row in live.execute("PRAGMA table_info(widget)")]
    live.close()
    assert columns == ["id"]


async def test_migrate_works_inside_a_running_event_loop(dev_settings: Settings) -> None:
    assert migrate(dev_settings).revisions_after == (real_head(),)


def test_model_registry_skips_only_missing_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "fake_models_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "good.py").write_text("LOADED = True\n")
    (package / "broken.py").write_text("import a_dependency_that_is_not_installed\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert import_model_modules(("fake_models_pkg.good", "fake_models_pkg.absent")) == [
        "fake_models_pkg.good"
    ]
    with pytest.raises(ModuleNotFoundError):
        import_model_modules(("fake_models_pkg.broken",))
    assert Base.metadata.naming_convention["pk"] == "pk_%(table_name)s"


# ---- Startup wiring and readiness ----


@pytest.fixture
async def web_app(dev_settings: Settings) -> AsyncIterator[FastAPI]:
    with override_settings(dev_settings):
        app = create_app(("pmsbridge.core.web",))
    yield app
    await reset_database()


async def test_register_migrates_and_the_readiness_check_passes(
    dev_settings: Settings, web_app: FastAPI
) -> None:
    live = sqlite3.connect(dev_settings.database_path)
    assert live.execute("SELECT version_num FROM alembic_version").fetchall() == [(real_head(),)]
    live.close()
    with TestClient(web_app) as client:
        response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": [{"name": "database", "ok": True}]}


async def test_readiness_check_fails_when_the_database_is_unreadable(
    dev_settings: Settings, web_app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    await web_app.state.database.dispose()
    await reset_database()
    for sidecar in dev_settings.database_path.parent.glob("pmsbridge.db-*"):
        sidecar.unlink()
    dev_settings.database_path.write_bytes(b"this is not a sqlite database" * 50)
    with TestClient(web_app) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"] == [{"name": "database", "ok": False}]
    assert "readiness check failed: database" in caplog.text


def test_startup_raises_the_storage_error_and_does_not_log_it_itself(
    dev_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    dev_settings.data_dir.mkdir(parents=True)
    dev_settings.database_path.write_bytes(b"this is not a sqlite database" * 50)
    with override_settings(dev_settings), pytest.raises(StorageError, match="cannot be opened"):
        create_app(("pmsbridge.core.web",))
    assert [r for r in caplog.records if r.levelname == "CRITICAL"] == []
