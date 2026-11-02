"""Stable, side-effect-free boundaries for the TeamCraft core.

The existing project-local ``teamctl.py`` remains the operational CLI during
M1. This package contains no scheduler or service startup code; ``storage``
provides only the M1-03 SQLite repository boundary (isolated databases, no
production cutover).
"""

from .clock import Clock, SystemClock, require_utc
from .config import TeamCraftConfig, load_config
from .errors import (
    BackupError,
    ConfigurationError,
    CorruptDatabaseError,
    MigrationError,
    SchemaTooNewError,
    StorageError,
    TeamCraftError,
    UnsupportedLocationError,
    ValidationError,
    WriteOutsideTransactionError,
)
from .fresh_start import (
    FreshStartError,
    FreshStartIncompleteError,
    FreshStartReport,
    LegacyStatePresentError,
    UnsupportedDatabaseFileError,
    initialize_project_root,
)
from .ids import EntityId, IdFactory, require_entity_id
from .storage import (
    APP_VERSION,
    DEFAULT_DATABASE_FILENAME,
    OCCUPYING_ATTEMPT_STATUSES,
    SCHEMA_VERSION,
    TASK_KINDS,
    TeamCraftConnection,
    backup_database,
    connect,
    describe_path_support,
    migrate_to_head,
    plan_retention,
    restore_backup,
    transaction,
    verify_database,
)

__all__ = [
    "APP_VERSION",
    "DEFAULT_DATABASE_FILENAME",
    "OCCUPYING_ATTEMPT_STATUSES",
    "SCHEMA_VERSION",
    "TASK_KINDS",
    "BackupError",
    "Clock",
    "ConfigurationError",
    "CorruptDatabaseError",
    "EntityId",
    "FreshStartError",
    "FreshStartIncompleteError",
    "FreshStartReport",
    "IdFactory",
    "LegacyStatePresentError",
    "MigrationError",
    "SchemaTooNewError",
    "StorageError",
    "SystemClock",
    "TeamCraftConfig",
    "TeamCraftConnection",
    "TeamCraftError",
    "UnsupportedDatabaseFileError",
    "UnsupportedLocationError",
    "ValidationError",
    "WriteOutsideTransactionError",
    "backup_database",
    "connect",
    "describe_path_support",
    "initialize_project_root",
    "load_config",
    "migrate_to_head",
    "plan_retention",
    "require_entity_id",
    "require_utc",
    "restore_backup",
    "transaction",
    "verify_database",
]
