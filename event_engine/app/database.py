"""Local SQLite storage for Smart Tech events."""
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          TEXT PRIMARY KEY,
    ts          REAL NOT NULL,
    kind        TEXT NOT NULL,          -- detection | camera | system | test
    level       TEXT NOT NULL,          -- HIGH | MEDIUM | LOW | INFO
    rule        TEXT,
    camera      TEXT,
    camera_name TEXT,
    zone        TEXT,
    zone_name   TEXT,
    label       TEXT,
    person      TEXT,                   -- name, or "Unknown"
    known       INTEGER,
    score       REAL,
    message     TEXT NOT NULL,
    snapshot    TEXT,                   -- /snapshots/<id>.jpg
    frigate_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_person ON events(person);
"""

COLUMNS = ["id", "ts", "kind", "level", "rule", "camera", "camera_name", "zone", "zone_name",
           "label", "person", "known", "score", "message", "snapshot", "frigate_id"]


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def save(self, event: dict) -> None:
        values = [event.get(c) for c in COLUMNS]
        with self._lock, self._conn:
            self._conn.execute(
                f"INSERT OR REPLACE INTO events ({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})",
                values)

    def recent(self, limit: int = 200, level: str | None = None) -> list[dict]:
        query, args = "SELECT * FROM events", []
        if level:
            query += " WHERE level = ?"
            args.append(level)
        query += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [dict(row) for row in self._conn.execute(query, args)]

    def counts_since(self, ts: float) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT level, COUNT(*) FROM events WHERE ts >= ? AND kind != 'test' GROUP BY level", (ts,))
            return {level: n for level, n in rows}

    def _delete(self, where: str, args: tuple) -> list[str]:
        """Deletes matching rows and returns their snapshot paths."""
        with self._lock, self._conn:
            snaps = [r[0] for r in self._conn.execute(f"SELECT snapshot FROM events WHERE {where}", args) if r[0]]
            self._conn.execute(f"DELETE FROM events WHERE {where}", args)
        return snaps

    def delete_older_than(self, ts: float) -> list[str]:
        return self._delete("ts < ?", (ts,))

    def delete_person(self, name: str) -> list[str]:
        return self._delete("person = ? COLLATE NOCASE", (name,))

    def delete_all(self) -> list[str]:
        return self._delete("1 = 1", ())
