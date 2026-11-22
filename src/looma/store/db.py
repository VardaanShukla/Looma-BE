import sqlite3
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path

from looma import paths

# Each entry upgrades the database by one version. Append only; never edit a shipped one.
MIGRATIONS = [
    files("looma.store").joinpath(name).read_text()
    for name in ("schema.sql", "migration_002.sql", "migration_003.sql", "migration_004.sql",
                 "migration_005.sql", "migration_006.sql", "migration_007.sql", "migration_008.sql",
                 "migration_009.sql", "migration_010.sql", "migration_011.sql", "migration_012.sql",
                 "migration_013.sql", "migration_014.sql")
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or paths.db_path(), timeout=5)
    conn.row_factory = sqlite3.Row
    # WAL lets hooks write while the API reads.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= len(MIGRATIONS):
        return
    # Migrations rebuild tables, which foreign keys would block; the setting can't
    # change inside a transaction, so it is turned off around them.
    conn.execute("PRAGMA foreign_keys=OFF")
    for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
        # One transaction per migration, so a failed upgrade leaves the old version intact.
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version={number};\nCOMMIT;")
    conn.execute("PRAGMA foreign_keys=ON")
