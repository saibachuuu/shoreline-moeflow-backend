"""Discovery, locking and execution for versioned MongoDB migrations."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
import importlib
import inspect
import logging
import pkgutil
import socket
import time
from typing import Optional
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

logger = logging.getLogger(__name__)

MIGRATION_RECORDS = "migration_record"
MIGRATION_LOCK = "migration_lock"
LOCK_ID = "migrate"
LOCK_LEASE = timedelta(minutes=30)


class MigrationError(RuntimeError):
    """A migration cannot safely continue."""


class IrreversibleMigration(MigrationError):
    """A migration deliberately has no safe down operation."""


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    description: str
    level: str
    module: object
    checksum: str

    def up(self, db):
        return self.module.up(db)

    def down(self, db):
        return self.module.down(db)

    def verify(self, db) -> bool:
        return bool(self.module.verify(db))


def _checksum(module) -> str:
    """Hash a migration's *semantics*, not its source text.

    A text hash would flip every time ``ruff format`` rewrapped a line or
    someone touched a comment, which would make an already-applied migration
    look tampered with and block the init container.  Hashing the parsed AST
    (without position attributes) ignores formatting and comments while still
    catching a real change to what the migration does.
    """
    tree = ast.parse(inspect.getsource(module))
    return sha256(ast.dump(tree).encode("utf-8")).hexdigest()


def discover() -> list[Migration]:
    """Load migrations from ``app.migrations.versions`` in version order."""
    package = importlib.import_module("app.migrations.versions")
    migrations = []
    for module_info in pkgutil.iter_modules(package.__path__):
        if not module_info.name.startswith("m"):
            continue
        module = importlib.import_module(f"{package.__name__}.{module_info.name}")
        required = ("VERSION", "NAME", "DESCRIPTION", "LEVEL", "up", "down", "verify")
        missing = [
            attribute for attribute in required if not hasattr(module, attribute)
        ]
        if missing:
            raise MigrationError(
                f"Migration {module_info.name} is missing: {', '.join(missing)}"
            )
        migrations.append(
            Migration(
                version=module.VERSION,
                name=module.NAME,
                description=module.DESCRIPTION,
                level=module.LEVEL.upper(),
                module=module,
                checksum=_checksum(module),
            )
        )
    migrations.sort(key=lambda migration: migration.version)
    versions = [migration.version for migration in migrations]
    if len(versions) != len(set(versions)):
        raise MigrationError("Migration versions must be unique")
    return migrations


def _ensure_metadata_indexes(db) -> None:
    db[MIGRATION_RECORDS].create_index(
        [("v", 1)], name="migration_version_unique", unique=True, background=True
    )


def _applied_records(db) -> dict[str, dict]:
    return {record["v"]: record for record in db[MIGRATION_RECORDS].find({})}


def pending(db, level: Optional[str] = None) -> list[Migration]:
    """Return discovered migrations that have not been recorded as applied."""
    applied = _applied_records(db)
    selected_level = level.upper() if level else None
    return [
        migration
        for migration in discover()
        if migration.version not in applied
        and (selected_level in (None, "ALL") or migration.level == selected_level)
    ]


def status(db) -> list[dict]:
    """Return migration status and reject silently edited applied migrations."""
    applied = _applied_records(db)
    result = []
    for migration in discover():
        record = applied.get(migration.version)
        if record and record.get("c") and record["c"] != migration.checksum:
            raise MigrationError(
                f"Applied migration {migration.version} has been modified"
            )
        result.append(
            {
                "version": migration.version,
                "name": migration.name,
                "level": migration.level,
                "applied": record is not None,
                "applied_at": record.get("at") if record else None,
                "duration_ms": record.get("d") if record else None,
            }
        )
    return result


def _acquire_lock(db, holder: str, lease: timedelta) -> bool:
    now = datetime.utcnow()
    try:
        lock = db[MIGRATION_LOCK].find_one_and_update(
            {
                "_id": LOCK_ID,
                "$or": [
                    {"expires_at": {"$lte": now}},
                    {"expires_at": {"$exists": False}},
                    {"holder": holder},
                ],
            },
            {"$set": {"holder": holder, "expires_at": now + lease}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        # A non-expired lease belongs to another process.  ``upsert`` attempts
        # the duplicate _id insert in this case, so treating it as a lock miss
        # is required rather than letting it escape as an infrastructure error.
        return False
    return lock is not None and lock.get("holder") == holder


def _renew_lock(db, holder: str, lease: timedelta) -> None:
    """Extend our own lease so a slow migration cannot lose it mid-run."""
    db[MIGRATION_LOCK].update_one(
        {"_id": LOCK_ID, "holder": holder},
        {"$set": {"expires_at": datetime.utcnow() + lease}},
    )


def _release_lock(db, holder: str) -> None:
    db[MIGRATION_LOCK].update_one(
        {"_id": LOCK_ID, "holder": holder}, {"$set": {"expires_at": datetime.utcnow()}}
    )


def _record_applied(db, migration: Migration, duration_ms: int) -> None:
    try:
        db[MIGRATION_RECORDS].insert_one(
            {
                "v": migration.version,
                "n": migration.name,
                "at": datetime.utcnow(),
                "d": duration_ms,
                "c": migration.checksum,
            }
        )
    except DuplicateKeyError as error:
        record = db[MIGRATION_RECORDS].find_one({"v": migration.version})
        if not record or record.get("c") != migration.checksum:
            raise MigrationError(
                f"Migration {migration.version} was concurrently recorded with different content"
            ) from error


def run_pending(db, level: str = "AUTO", dry_run: bool = False) -> list[Migration]:
    """Run pending migrations while holding a lease; failed migrations stop the run."""
    # Validate checksums even when there is nothing pending.  Otherwise an
    # edited historical migration would remain invisible to deploy --check.
    status(db)
    migrations = pending(db, level)
    if dry_run or not migrations:
        return migrations

    # Applying a later AUTO migration while an earlier non-AUTO one is still
    # pending would record the newer version and hide the gap from every
    # ordering check afterwards.  Refuse instead of reordering history.
    blocking = [
        migration
        for migration in pending(db, "ALL")
        if migration.version < migrations[-1].version and migration not in migrations
    ]
    if blocking:
        raise MigrationError(
            "Cannot apply migrations out of order; these earlier migrations are "
            "still pending and are excluded by --level "
            f"{level}: {', '.join(m.version + ' (' + m.level + ')' for m in blocking)}"
        )

    _ensure_metadata_indexes(db)
    holder = f"{socket.gethostname()}:{uuid4()}"
    if not _acquire_lock(db, holder, LOCK_LEASE):
        # Never return normally here.  ``manage.py migrate`` is the
        # ``service_completed_successfully`` gate for the whole compose stack,
        # so exiting 0 with migrations still pending would boot gunicorn and
        # both celery workers against a half-migrated database.  A stale lease
        # from an OOM-killed or interrupted run reaches this branch too, which
        # is exactly when starting the app is most dangerous.
        raise MigrationError(
            "Another instance holds the migration lock. If no migration is "
            f"running, the previous run left a lease that expires within "
            f"{int(LOCK_LEASE.total_seconds() // 60)} minutes; wait for it or "
            f"clear the '{LOCK_ID}' document in the {MIGRATION_LOCK} collection."
        )
    try:
        # Re-read after getting the lock in case another runner finished first.
        applied_migrations = []
        for migration in pending(db, level):
            started = time.monotonic()
            logger.info("Applying migration %s %s", migration.version, migration.name)
            migration.up(db)
            if not migration.verify(db):
                raise MigrationError(
                    f"Migration {migration.version} verification failed"
                )
            duration_ms = round((time.monotonic() - started) * 1000)
            _record_applied(db, migration, duration_ms)
            applied_migrations.append(migration)
            logger.info("Applied migration %s in %sms", migration.version, duration_ms)
            # Renew the lease after every migration.  A single index build or
            # backfill over the production ``file`` collection can outlive a
            # fixed lease on a small server, and a lapsed lease lets a second
            # runner execute the same ``up()`` bodies concurrently.
            _renew_lock(db, holder, LOCK_LEASE)
    finally:
        _release_lock(db, holder)
    return applied_migrations


def rollback(db, target_version: str) -> list[Migration]:
    """Rollback applied migrations newer than ``target_version`` in reverse order."""
    known_versions = [migration.version for migration in discover()]
    if target_version not in known_versions:
        # Versions are compared as strings below.  An unvalidated value is not
        # merely rejected later: "--to 0" would satisfy "0000" > "0" and roll
        # the baseline back too, while "--to 1" silently matches nothing and
        # exits 0 as if it had succeeded.
        raise MigrationError(
            f"Unknown target version {target_version!r}; "
            f"choose one of: {', '.join(known_versions)}"
        )
    _ensure_metadata_indexes(db)
    holder = f"{socket.gethostname()}:{uuid4()}"
    if not _acquire_lock(db, holder, LOCK_LEASE):
        raise MigrationError("Another instance is executing migrations")
    try:
        applied = _applied_records(db)
        migrations = [
            migration
            for migration in discover()
            if migration.version in applied and migration.version > target_version
        ]
        for migration in reversed(migrations):
            logger.warning(
                "Rolling back migration %s %s", migration.version, migration.name
            )
            migration.down(db)
            db[MIGRATION_RECORDS].delete_one({"v": migration.version})
            _renew_lock(db, holder, LOCK_LEASE)
        return migrations
    finally:
        _release_lock(db, holder)
