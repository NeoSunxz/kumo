"""SQLite 存储.

单进程、低并发: 一个连接加一把锁, 所有方法都是短事务, 在事件循环里直接调用.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .models import Candidate, LibraryFile, Resource, Wanted, WantedStatus
from .settings import Settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wanted (
    id            INTEGER PRIMARY KEY,
    number        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    status        TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT 'manual',
    note          TEXT NOT NULL DEFAULT '',
    chosen_id     INTEGER,
    info_hash     TEXT,
    percent       REAL NOT NULL DEFAULT 0,
    searched_at   REAL,
    submitted_at  REAL,
    done_at       REAL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_wanted_status ON wanted(status);
CREATE TABLE IF NOT EXISTS candidate (
    id         INTEGER PRIMARY KEY,
    wanted_id  INTEGER NOT NULL REFERENCES wanted(id) ON DELETE CASCADE,
    site       TEXT NOT NULL,
    title      TEXT NOT NULL,
    magnet     TEXT NOT NULL,
    info_hash  TEXT NOT NULL,
    size       INTEGER,
    seeders    INTEGER,
    published  TEXT,
    tags       TEXT NOT NULL DEFAULT '[]',
    score      REAL NOT NULL DEFAULT 0,
    rejected   TEXT NOT NULL DEFAULT '',
    UNIQUE (wanted_id, info_hash)
);
CREATE TABLE IF NOT EXISTS library (
    fid        TEXT PRIMARY KEY,
    root       TEXT NOT NULL,
    name       TEXT NOT NULL,
    size       INTEGER NOT NULL DEFAULT 0,
    pick_code  TEXT NOT NULL DEFAULT '',
    number     TEXT COLLATE NOCASE,
    subtitle   INTEGER NOT NULL DEFAULT 0,
    seen_run   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_library_number ON library(number);
CREATE TABLE IF NOT EXISTS feed_seen (
    feed_url  TEXT NOT NULL,
    item_key  TEXT NOT NULL,
    seen_at   REAL NOT NULL,
    PRIMARY KEY (feed_url, item_key)
);
CREATE TABLE IF NOT EXISTS resource (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    tid         INTEGER NOT NULL,
    forum_id    INTEGER NOT NULL,
    forum_name  TEXT NOT NULL DEFAULT '',
    type_name   TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL,
    number      TEXT COLLATE NOCASE,
    magnet      TEXT,
    info_hash   TEXT,
    size        INTEGER,
    posted      TEXT,
    subtitle    INTEGER NOT NULL DEFAULT 0,
    wanted_id   INTEGER REFERENCES wanted(id) ON DELETE SET NULL,
    crawled_at  REAL NOT NULL,
    UNIQUE (source, tid)
);
CREATE INDEX IF NOT EXISTS ix_resource_number ON resource(number);
CREATE TABLE IF NOT EXISTS event (
    id       INTEGER PRIMARY KEY,
    at       REAL NOT NULL,
    level    TEXT NOT NULL,
    message  TEXT NOT NULL
);
"""

#: 仍可能需要动作的状态: 搜索、选择或提交.
ACTIVE_STATUSES = (WantedStatus.PENDING, WantedStatus.NO_MAGNET, WantedStatus.NEED_CHOICE, WantedStatus.QUEUED)


class Repo:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def _one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    # --- kv ---

    def get_kv(self, key: str) -> str | None:
        row = self._one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_kv(self, key: str, value: str | None) -> None:
        with self._tx() as c:
            if value is None:
                c.execute("DELETE FROM kv WHERE key = ?", (key,))
            else:
                c.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def settings(self) -> Settings:
        raw = self.get_kv("settings")
        return Settings.model_validate_json(raw) if raw else Settings()

    def save_settings(self, settings: Settings) -> None:
        self.set_kv("settings", settings.model_dump_json())

    # --- events ---

    def log(self, level: str, message: str) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO event(at, level, message) VALUES(?, ?, ?)", (time.time(), level, message))
            c.execute("DELETE FROM event WHERE id <= (SELECT MAX(id) - 1000 FROM event)")

    def events(self, limit: int = 100) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM event ORDER BY id DESC LIMIT ?", (limit,))

    # --- wanted ---

    def add_wanted(self, number: str, source: str = "manual", status: WantedStatus = WantedStatus.PENDING) -> Wanted | None:
        """新增想要的番号; 已存在时返回 ``None``."""
        now = time.time()
        with self._tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO wanted(number, status, source, created_at, updated_at) VALUES(?, ?, ?, ?, ?)",
                (number, status.value, source, now, now),
            )
            if cur.rowcount == 0:
                return None
            row = c.execute("SELECT * FROM wanted WHERE id = ?", (cur.lastrowid,)).fetchone()
        return Wanted.from_row(row)

    def wanted(self, wanted_id: int) -> Wanted | None:
        row = self._one("SELECT * FROM wanted WHERE id = ?", (wanted_id,))
        return Wanted.from_row(row) if row else None

    def wanted_by_number(self, number: str) -> Wanted | None:
        row = self._one("SELECT * FROM wanted WHERE number = ?", (number,))
        return Wanted.from_row(row) if row else None

    def list_wanted(
        self, statuses: Iterable[WantedStatus] | None = None, *, search: str = "", limit: int = 500
    ) -> list[Wanted]:
        sql, params = "SELECT * FROM wanted WHERE 1=1", []
        if statuses is not None:
            values = [s.value for s in statuses]
            sql += f" AND status IN ({','.join('?' * len(values))})"
            params += values
        if search:
            sql += " AND number LIKE ?"
            params.append(f"%{search}%")
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        return [Wanted.from_row(r) for r in self._all(sql, params)]

    def status_counts(self) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self._all("SELECT status, COUNT(*) AS n FROM wanted GROUP BY status")}

    def update_wanted(self, wanted_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = [v.value if isinstance(v, WantedStatus) else v for v in fields.values()]
        with self._tx() as c:
            c.execute(f"UPDATE wanted SET {cols} WHERE id = ?", (*values, wanted_id))

    def delete_wanted(self, wanted_id: int) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM wanted WHERE id = ?", (wanted_id,))

    def due_for_search(self, retry_after: float, limit: int) -> list[Wanted]:
        """待搜索: 新加入的, 以及上次没搜到且已过重试间隔的."""
        rows = self._all(
            "SELECT * FROM wanted WHERE status = ? OR (status = ? AND (searched_at IS NULL OR searched_at < ?))"
            " ORDER BY created_at LIMIT ?",
            (WantedStatus.PENDING.value, WantedStatus.NO_MAGNET.value, time.time() - retry_after, limit),
        )
        return [Wanted.from_row(r) for r in rows]

    # --- candidates ---

    def replace_candidates(self, wanted_id: int, candidates: list[Candidate]) -> list[Candidate]:
        with self._tx() as c:
            c.execute("DELETE FROM candidate WHERE wanted_id = ?", (wanted_id,))
            for cand in candidates:
                cur = c.execute(
                    "INSERT OR IGNORE INTO candidate(wanted_id, site, title, magnet, info_hash, size, seeders, published,"
                    " tags, score, rejected) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        wanted_id, cand.site, cand.title, cand.magnet, cand.info_hash, cand.size, cand.seeders,
                        cand.published, json.dumps(sorted(cand.tags)), cand.score, cand.rejected,
                    ),
                )
                cand.id = cur.lastrowid
        return candidates

    def candidates(self, wanted_id: int) -> list[Candidate]:
        rows = self._all("SELECT * FROM candidate WHERE wanted_id = ? ORDER BY rejected = '' DESC, score DESC", (wanted_id,))
        return [Candidate.from_row(r) for r in rows]

    def candidate(self, candidate_id: int) -> Candidate | None:
        row = self._one("SELECT * FROM candidate WHERE id = ?", (candidate_id,))
        return Candidate.from_row(row) if row else None

    # --- library (115 目录同步结果) ---

    def replace_library_root(self, root: str, files: list[LibraryFile]) -> None:
        """整体替换某个同步根目录下的文件快照."""
        run = int(time.time() * 1000)
        with self._tx() as c:
            for f in files:
                c.execute(
                    "INSERT INTO library(fid, root, name, size, pick_code, number, subtitle, seen_run)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(fid) DO UPDATE SET root=excluded.root, name=excluded.name,"
                    " size=excluded.size, pick_code=excluded.pick_code, number=excluded.number,"
                    " subtitle=excluded.subtitle, seen_run=excluded.seen_run",
                    (f.fid, root, f.name, f.size, f.pick_code, f.number, int(f.subtitle), run),
                )
            c.execute("DELETE FROM library WHERE root = ? AND seen_run <> ?", (root, run))

    def drop_library_roots_except(self, roots: list[str]) -> None:
        with self._tx() as c:
            if roots:
                c.execute(f"DELETE FROM library WHERE root NOT IN ({','.join('?' * len(roots))})", roots)
            else:
                c.execute("DELETE FROM library")

    def owned(self, number: str) -> list[LibraryFile]:
        rows = self._all("SELECT * FROM library WHERE number = ?", (number,))
        return [LibraryFile.from_row(r) for r in rows]

    def library_stats(self) -> list[sqlite3.Row]:
        return self._all(
            "SELECT root, COUNT(*) AS files, COUNT(number) AS numbered, SUM(size) AS bytes FROM library GROUP BY root"
        )

    def search_library(self, text: str, limit: int = 200) -> list[LibraryFile]:
        rows = self._all(
            "SELECT * FROM library WHERE name LIKE ? OR number LIKE ? ORDER BY name LIMIT ?",
            (f"%{text}%", f"%{text}%", limit),
        )
        return [LibraryFile.from_row(r) for r in rows]

    # --- 输入 RSS 去重 ---

    def mark_feed_seen(self, feed_url: str, item_key: str) -> bool:
        """返回是否为首次见到."""
        with self._tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO feed_seen(feed_url, item_key, seen_at) VALUES(?, ?, ?)",
                (feed_url, item_key, time.time()),
            )
            return cur.rowcount == 1

    # --- 论坛爬取的资源 ---

    def resource_exists(self, source: str, tid: int) -> bool:
        return self._one("SELECT 1 FROM resource WHERE source = ? AND tid = ?", (source, tid)) is not None

    def add_resource(self, r: Resource) -> Resource:
        with self._tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO resource(source, tid, forum_id, forum_name, type_name, title, number, magnet,"
                " info_hash, size, posted, subtitle, crawled_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    r.source, r.tid, r.forum_id, r.forum_name, r.type_name, r.title, r.number, r.magnet,
                    r.info_hash, r.size, r.posted, int(r.subtitle), time.time(),
                ),
            )
            row = c.execute("SELECT * FROM resource WHERE source = ? AND tid = ?", (r.source, r.tid)).fetchone()
        return Resource.from_row(row)

    def resource(self, resource_id: int) -> Resource | None:
        row = self._one("SELECT * FROM resource WHERE id = ?", (resource_id,))
        return Resource.from_row(row) if row else None

    def resources(self, *, search: str = "", forum_id: int | None = None, limit: int = 200) -> list[Resource]:
        sql, params = "SELECT * FROM resource WHERE 1=1", []
        if search:
            sql += " AND (title LIKE ? OR number LIKE ?)"
            params += [f"%{search}%", f"%{search}%"]
        if forum_id is not None:
            sql += " AND forum_id = ?"
            params.append(forum_id)
        sql += " ORDER BY tid DESC LIMIT ?"
        params.append(limit)
        return [Resource.from_row(r) for r in self._all(sql, params)]

    def resources_by_number(self, number: str) -> list[Resource]:
        rows = self._all("SELECT * FROM resource WHERE number = ? AND magnet IS NOT NULL", (number,))
        return [Resource.from_row(r) for r in rows]

    def resource_forums(self) -> list[sqlite3.Row]:
        return self._all(
            "SELECT forum_id, forum_name, COUNT(*) AS n FROM resource GROUP BY forum_id, forum_name ORDER BY forum_id"
        )

    def link_resource(self, resource_id: int, wanted_id: int) -> None:
        with self._tx() as c:
            c.execute("UPDATE resource SET wanted_id = ? WHERE id = ?", (wanted_id, resource_id))
