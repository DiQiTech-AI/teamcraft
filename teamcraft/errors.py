"""Shared exception hierarchy for TeamCraft core boundaries."""


class TeamCraftError(Exception):
    """Base class for errors callers may handle without parsing messages."""


class ConfigurationError(TeamCraftError):
    """Configuration is missing or invalid."""


class ValidationError(TeamCraftError):
    """A value violates a core boundary contract."""


class StorageError(TeamCraftError):
    """Base class for SQLite storage boundary failures."""


class UnsupportedLocationError(StorageError):
    """The requested database path is outside the supported local-disk boundary.

    The checks are best-effort capability guards (absolute path, regular file,
    writable parent, WAL capability); they deliberately do NOT claim to detect
    every synced or network filesystem reliably.
    """


class SchemaTooNewError(StorageError):
    """The database schema is newer than this build supports.

    Raised before any write statement so the database is never downgraded,
    migrated, or otherwise modified by an older binary.
    """


class MigrationError(StorageError):
    """Migration cannot proceed safely (partial junk, version mismatch, ...)."""


class BackupError(StorageError):
    """Backup or restore cannot be performed safely."""


class CorruptDatabaseError(StorageError):
    """The database failed an integrity gate and must not be written to."""


class WriteOutsideTransactionError(StorageError):
    """A write was attempted through a public entry point outside the gate.

    Writes against a :class:`teamcraft.storage.TeamCraftConnection` must run
    inside ``teamcraft.transaction()``: only there is the schema version
    re-checked after the write lock is held.  This is raised instead of
    silently writing through an ungated ``execute``/``executemany``/``cursor``
    or an implicit-committing ``executescript``/``commit``.
    """
