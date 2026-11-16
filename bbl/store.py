"""SQLite history: sessions, samples and events.

Samples are buffered in memory and written every flush_seconds to avoid frequent
disk writes; events are written immediately (they are rare)."""
import json
import os
import sqlite3

from .paths import DB_FILE

GPU_CODES = {"suspended": 0, "active": 1, "busy": 2}  # -1 = no dGPU / unknown

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sessions(
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, boot_id TEXT,
    start REAL NOT NULL, end REAL, start_pct REAL, end_pct REAL,
    energy_wh REAL DEFAULT 0, dgpu_awake_s REAL DEFAULT 0, sleep_s REAL DEFAULT 0,
    full_wh REAL, design_wh REAL, cycles INTEGER, last_ts REAL);
CREATE TABLE IF NOT EXISTS samples(
    ts REAL NOT NULL, session INTEGER NOT NULL, watts REAL, pct REAL, gpu INTEGER,
    dgpu_w REAL, cpu_w REAL, igpu_w REAL, ram_w REAL);  -- *_w: reserved for RAPL (v2)
CREATE INDEX IF NOT EXISTS samples_session_ts ON samples(session, ts);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, session INTEGER,
    kind TEXT NOT NULL, message TEXT NOT NULL, data TEXT);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
"""


def connect(path=DB_FILE, readonly=False):
    if readonly:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        db = sqlite3.connect(path, timeout=5)
        db.execute("PRAGMA auto_vacuum=INCREMENTAL")  # only takes effect on a new database
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.executescript(SCHEMA)
    db.row_factory = sqlite3.Row
    return db


class Store:
    def __init__(self, path=DB_FILE):
        self.db = connect(path)
        self.pending = []

    # meta
    def get_meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(value)))
        self.db.commit()

    # sessions
    def open_session(self, kind, boot_id, ts, pct, bat):
        cur = self.db.execute(
            "INSERT INTO sessions(kind, boot_id, start, start_pct, full_wh, design_wh, cycles, last_ts)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (kind, boot_id, ts, pct, bat and bat["full_wh"], bat and bat["design_wh"], bat and bat["cycles"], ts))
        self.db.commit()
        return cur.lastrowid

    def current_session(self):
        return self.db.execute("SELECT * FROM sessions WHERE end IS NULL ORDER BY id DESC LIMIT 1").fetchone()

    def update_session(self, sid, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE sessions SET {cols} WHERE id=?", (*fields.values(), sid))

    def close_session(self, sid, ts, pct, **fields):
        self.update_session(sid, end=ts, end_pct=pct, **fields)
        self.db.commit()

    def trim(self, keep_battery_sessions):
        """Keep the last N battery sessions and everything after the oldest kept one."""
        row = self.db.execute(
            "SELECT start FROM sessions WHERE kind='battery' ORDER BY start DESC LIMIT 1 OFFSET ?",
            (keep_battery_sessions - 1,)).fetchone()
        if not row:
            return 0
        old = [r[0] for r in self.db.execute(
            "SELECT id FROM sessions WHERE start < ? AND end IS NOT NULL", (row[0],))]
        if old:
            marks = ",".join("?" * len(old))
            self.db.execute(f"DELETE FROM samples WHERE session IN ({marks})", old)
            self.db.execute(f"DELETE FROM events WHERE session IN ({marks})", old)
            self.db.execute(f"DELETE FROM sessions WHERE id IN ({marks})", old)
            self.db.commit()
            self.db.execute("PRAGMA incremental_vacuum")
        return len(old)

    # samples / events
    def add_sample(self, ts, session, watts, pct, gpu, dgpu_w=None):
        self.pending.append((ts, session, watts, pct, gpu, dgpu_w))

    def flush(self):
        if self.pending:
            self.db.executemany(
                "INSERT INTO samples(ts, session, watts, pct, gpu, dgpu_w) VALUES(?,?,?,?,?,?)", self.pending)
            self.pending.clear()
        self.db.commit()

    def add_event(self, ts, session, kind, message, data=None):
        self.db.execute("INSERT INTO events(ts, session, kind, message, data) VALUES(?,?,?,?,?)",
                        (ts, session, kind, message, json.dumps(data) if data else None))
        self.db.commit()

    def last_event(self):
        return self.db.execute("SELECT * FROM events ORDER BY ts DESC, id DESC LIMIT 1").fetchone()

    def last_sample_ts(self):
        row = self.db.execute("SELECT MAX(ts) FROM samples").fetchone()
        return row[0] if row else None

    def recent_events(self, n):
        return [dict(r) for r in self.db.execute(
            "SELECT ts, kind, message FROM events ORDER BY ts DESC, id DESC LIMIT ?", (n,))]

    def close(self):
        self.flush()
        self.db.close()
