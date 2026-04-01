"""
Database connection, schema, and migrations.
"""

import logging
import sqlite3
from pathlib import Path

from .config import DB_PATH, ADMIN_SECRET, ADMIN_EMAIL, ADMIN_NAME
from .passwords import hash_password

logger = logging.getLogger("ogai")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ─────────────────────────────────────────────
# Schema & migrations
# ─────────────────────────────────────────────

_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS users (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        email         TEXT    NOT NULL UNIQUE COLLATE NOCASE,
        display_name  TEXT    NOT NULL,
        password_hash TEXT    NOT NULL,
        password_salt TEXT    NOT NULL,
        role          TEXT    NOT NULL DEFAULT 'reviewer',
        created_at    TEXT    DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS sessions (
        token      TEXT    PRIMARY KEY,
        user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at TEXT    DEFAULT (datetime('now')),
        expires_at TEXT    NOT NULL
    );

    CREATE TABLE IF NOT EXISTS projects (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT    NOT NULL,
        user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
        created_at TEXT    DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS papers (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        filename   TEXT    NOT NULL,
        sha256     TEXT    NOT NULL,
        user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
        project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
        created_at TEXT    DEFAULT (datetime('now')),
        UNIQUE(sha256, user_id)
    );

    CREATE TABLE IF NOT EXISTS annotations (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        paper_id    INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
        reviewer_id TEXT    NOT NULL,
        data_json   TEXT    DEFAULT '{}',
        timestamp   TEXT    DEFAULT (datetime('now')),
        correction_notes          TEXT,
        corrections_json          TEXT,
        pipeline_predictions_json TEXT,
        field_annotations_json    TEXT,
        version     INTEGER NOT NULL DEFAULT 1,
        UNIQUE(paper_id, reviewer_id)
    );

    CREATE TABLE IF NOT EXISTS spans (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        paper_id    INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
        reviewer_id TEXT    NOT NULL,
        field_name  TEXT    NOT NULL,
        page        INTEGER,
        text        TEXT,
        x0 REAL, y0 REAL, x1 REAL, y1 REAL
    );

    CREATE TABLE IF NOT EXISTS schema_version (
        id      INTEGER PRIMARY KEY CHECK (id = 1),
        version INTEGER NOT NULL DEFAULT 0
    );
"""

# Each migration is (version_number, description, sql).
# Only migrations with version > current schema_version are applied.
_MIGRATIONS = [
    (1, "add sha256 to papers",            "ALTER TABLE papers ADD COLUMN sha256 TEXT"),
    (2, "add user_id to papers",           "ALTER TABLE papers ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE"),
    (3, "add project_id to papers",        "ALTER TABLE papers ADD COLUMN project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL"),
    (4, "add created_at to papers",        "ALTER TABLE papers ADD COLUMN created_at TEXT DEFAULT (datetime('now'))"),
    (5, "add disk_filename to papers",     "ALTER TABLE papers ADD COLUMN disk_filename TEXT"),
    (6, "add user_id to projects",         "ALTER TABLE projects ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE"),
    (7, "add correction_notes",            "ALTER TABLE annotations ADD COLUMN correction_notes TEXT"),
    (8, "add corrections_json",            "ALTER TABLE annotations ADD COLUMN corrections_json TEXT"),
    (9, "add pipeline_predictions_json",   "ALTER TABLE annotations ADD COLUMN pipeline_predictions_json TEXT"),
    (10, "add field_annotations_json",     "ALTER TABLE annotations ADD COLUMN field_annotations_json TEXT"),
    (11, "add version to annotations",     "ALTER TABLE annotations ADD COLUMN version INTEGER NOT NULL DEFAULT 1"),
]


def _get_schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT version FROM schema_version WHERE id=1").fetchone()
        return row["version"] if row else 0
    except sqlite3.OperationalError:
        return 0


def _set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT INTO schema_version (id, version) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET version=excluded.version",
        (version,),
    )


def init_db() -> None:
    conn = get_db()
    with conn:
        conn.executescript(_SCHEMA_SQL)

        current_version = _get_schema_version(conn)
        for ver, desc, sql in _MIGRATIONS:
            if ver <= current_version:
                continue
            try:
                conn.execute(sql)
                logger.info("migration %d applied: %s", ver, desc)
            except sqlite3.OperationalError as e:
                # Column already exists — that's fine
                logger.debug("migration %d skipped (%s): %s", ver, desc, e)
            _set_schema_version(conn, ver)

        # Backfill NULL ids for legacy TEXT PRIMARY KEY schema
        try:
            conn.execute("UPDATE papers SET id=CAST(rowid AS TEXT) WHERE id IS NULL OR id=''")
        except Exception:
            pass

        conn.commit()
    conn.close()
    _ensure_admin_user()


def _ensure_admin_user() -> None:
    if not ADMIN_SECRET:
        return
    conn = get_db()
    existing = conn.execute("SELECT id FROM users WHERE email=?", (ADMIN_EMAIL,)).fetchone()
    if not existing:
        ph, ps = hash_password(ADMIN_SECRET)
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO users (email, display_name, password_hash, password_salt, role) VALUES (?,?,?,?,?)",
                (ADMIN_EMAIL, ADMIN_NAME, ph, ps, "admin"),
            )
            conn.commit()
    conn.close()
