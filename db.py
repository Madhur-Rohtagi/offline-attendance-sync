"""SQLite helpers shared by the server and both clients.

All three databases (client_A.db, client_B.db, server.db) use the SAME schema.
On a client, a row with synced = 0 is a pending (not yet synchronized) update.
On the server, the synced column is simply always 1.
"""
import sqlite3
from datetime import datetime, timezone

# The same small class list is seeded into every database.
STUDENTS = [
    (101, "Aarav Sharma"),
    (102, "Diya Patel"),
    (103, "Kabir Singh"),
    (104, "Meera Iyer"),
    (105, "Rohan Gupta"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    student_id INTEGER PRIMARY KEY,
    name       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attendance (
    student_id  INTEGER NOT NULL REFERENCES students(student_id),
    att_date    TEXT    NOT NULL,                       -- 'YYYY-MM-DD'
    status      TEXT    NOT NULL CHECK (status IN ('Present', 'Absent')),
    client_id   TEXT    NOT NULL,                       -- who made this version
    update_id   TEXT    NOT NULL UNIQUE,                -- uuid of this version
    modified_at TEXT    NOT NULL,                       -- UTC, see now_utc()
    synced      INTEGER NOT NULL DEFAULT 0,             -- 0 = pending, 1 = synced
    -- one logical record per student per day: duplicates are impossible
    PRIMARY KEY (student_id, att_date)
);
"""

# Fixed-width UTC format, e.g. 2026-11-20T10:05:00.000000Z.
# Because every timestamp has exactly the same width, comparing the strings
# gives the same answer as comparing the times.
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

ATTENDANCE_COLUMNS = ["student_id", "att_date", "status", "client_id",
                      "update_id", "modified_at", "synced"]


def now_utc():
    """Current UTC time as a fixed-width string."""
    return datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row          # rows behave like dicts
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path):
    """Create the tables (if missing) and seed the student list."""
    conn = connect(db_path)
    with conn:  # 'with conn' = one transaction: commit on success, rollback on error
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT OR IGNORE INTO students (student_id, name) VALUES (?, ?)",
            STUDENTS,
        )
    conn.close()


def get_students(conn):
    return [dict(r) for r in conn.execute(
        "SELECT student_id, name FROM students ORDER BY student_id")]


def get_all_attendance(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM attendance ORDER BY att_date, student_id")]


def get_record(conn, student_id, att_date):
    row = conn.execute(
        "SELECT * FROM attendance WHERE student_id = ? AND att_date = ?",
        (student_id, att_date),
    ).fetchone()
    return dict(row) if row else None


def save_record(conn, record, synced):
    """Insert or replace the row for (student_id, att_date)."""
    conn.execute(
        """INSERT OR REPLACE INTO attendance
           (student_id, att_date, status, client_id, update_id, modified_at, synced)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (record["student_id"], record["att_date"], record["status"],
         record["client_id"], record["update_id"], record["modified_at"], synced),
    )
