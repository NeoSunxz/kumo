"""SQLite 存储.

单进程、低并发: 一个连接加一把锁, 所有方法都是短事务, 在事件循环里直接调用.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .models import (
    AmaneActor, Candidate, LibraryFile, MagnetSource, Release, Resource, Subscription, Wanted, WantedStatus, Work,
)
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
    type_id     INTEGER,
    type_name   TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL,
    number      TEXT COLLATE NOCASE,
    magnet      TEXT,
    info_hash   TEXT,
    size        INTEGER,
    posted      TEXT,
    subtitle    INTEGER NOT NULL DEFAULT 0,
    seeders     INTEGER,
    wanted_id   INTEGER REFERENCES wanted(id) ON DELETE SET NULL,
    dismissed   INTEGER NOT NULL DEFAULT 0,
    tags        TEXT,
    brackets    TEXT,
    source_id   INTEGER,
    images      TEXT,
    crawled_at  REAL NOT NULL,
    UNIQUE (source, tid)
);
CREATE INDEX IF NOT EXISTS ix_resource_number ON resource(number);
CREATE INDEX IF NOT EXISTS ix_resource_forum ON resource(forum_id, type_id);
CREATE TABLE IF NOT EXISTS subscription (
    id             INTEGER PRIMARY KEY,
    source         TEXT NOT NULL,
    kind           TEXT NOT NULL,
    ident          TEXT NOT NULL,
    uncensored     INTEGER NOT NULL DEFAULT 0,
    name           TEXT NOT NULL DEFAULT '',
    enabled        INTEGER NOT NULL DEFAULT 1,
    auto_search    INTEGER NOT NULL DEFAULT 0,
    checked_at     REAL,
    last_error     TEXT NOT NULL DEFAULT '',
    release_count  INTEGER NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL,
    avatar         TEXT,
    profile        TEXT NOT NULL DEFAULT '{}',
    UNIQUE (source, kind, ident, uncensored)
);
CREATE TABLE IF NOT EXISTS release (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    number      TEXT NOT NULL COLLATE NOCASE,
    title       TEXT NOT NULL DEFAULT '',
    date        TEXT,
    cover       TEXT,
    url         TEXT NOT NULL DEFAULT '',
    first_seen  REAL NOT NULL,
    UNIQUE (source, number)
);
CREATE INDEX IF NOT EXISTS ix_release_date ON release(date);
CREATE TABLE IF NOT EXISTS release_sub (
    release_id       INTEGER NOT NULL REFERENCES release(id) ON DELETE CASCADE,
    subscription_id  INTEGER NOT NULL REFERENCES subscription(id) ON DELETE CASCADE,
    PRIMARY KEY (release_id, subscription_id)
);
CREATE TABLE IF NOT EXISTS magnet_source (
    id             INTEGER PRIMARY KEY,
    kind           TEXT NOT NULL,
    name           TEXT NOT NULL,
    params         TEXT NOT NULL DEFAULT '{}',
    filter         TEXT NOT NULL DEFAULT '{}',
    enabled        INTEGER NOT NULL DEFAULT 1,
    auto_download  INTEGER NOT NULL DEFAULT 0,
    last_run       REAL,
    last_error     TEXT NOT NULL DEFAULT '',
    created_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS source_seen (
    source_id  INTEGER NOT NULL,
    key        TEXT NOT NULL,
    PRIMARY KEY (source_id, key)
);
CREATE TABLE IF NOT EXISTS dismissed (
    number  TEXT PRIMARY KEY COLLATE NOCASE,
    at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS amane_work (
    number      TEXT PRIMARY KEY COLLATE NOCASE,
    found       INTEGER NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    date        TEXT,
    actors      TEXT NOT NULL DEFAULT '[]',
    studio      TEXT NOT NULL DEFAULT '',
    poster      TEXT,
    thumb       TEXT,
    amane_id    INTEGER,
    file_count  INTEGER NOT NULL DEFAULT 0,
    checked_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS amane_actor (
    name        TEXT PRIMARY KEY COLLATE NOCASE,
    found       INTEGER NOT NULL,
    image       TEXT,
    profile     TEXT NOT NULL DEFAULT '{}',
    amane_id    INTEGER,
    checked_at  REAL NOT NULL
);
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
        self._migrate()
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()
        self._backfill()

    def _backfill(self) -> None:
        """旧数据补齐: 资源的标签与片头标记 (按标题重新识别), 旧的「忽略」并入忽略列表."""
        from .select import brackets, title_tags

        rows = self._conn.execute("SELECT id, title, number FROM resource WHERE tags IS NULL").fetchall()
        for r in rows:
            self._conn.execute(
                "UPDATE resource SET tags = ?, brackets = ? WHERE id = ?",
                (json.dumps(sorted(title_tags(r["title"], r["number"]))), json.dumps(brackets(r["title"]), ensure_ascii=False), r["id"]),
            )
        self._conn.execute(
            "INSERT OR IGNORE INTO dismissed(number, at) SELECT number, crawled_at FROM resource"
            " WHERE dismissed = 1 AND number IS NOT NULL"
        )
        self._conn.execute("UPDATE resource SET dismissed = 0 WHERE dismissed = 1")

    def _migrate(self) -> None:
        """给旧库补列; 新库由建表语句直接建好."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(resource)")}
        if cols and "type_id" not in cols:
            self._conn.execute("ALTER TABLE resource ADD COLUMN type_id INTEGER")
        if cols and "seeders" not in cols:
            self._conn.execute("ALTER TABLE resource ADD COLUMN seeders INTEGER")
            self._conn.execute("ALTER TABLE resource ADD COLUMN dismissed INTEGER NOT NULL DEFAULT 0")
        if cols and "tags" not in cols:
            self._conn.execute("ALTER TABLE resource ADD COLUMN tags TEXT")
            self._conn.execute("ALTER TABLE resource ADD COLUMN brackets TEXT")
            self._conn.execute("ALTER TABLE resource ADD COLUMN source_id INTEGER")
        if cols and "images" not in cols:
            self._conn.execute("ALTER TABLE resource ADD COLUMN images TEXT")
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(subscription)")}
        if cols and "avatar" not in cols:
            self._conn.execute("ALTER TABLE subscription ADD COLUMN avatar TEXT")
            self._conn.execute("ALTER TABLE subscription ADD COLUMN profile TEXT NOT NULL DEFAULT '{}'")

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
        if not r.tags and not r.brackets:
            # 调用方没识别时按标题识别, 入池的磁力都有标签可筛.
            from .select import brackets, title_tags

            r.tags, r.brackets = title_tags(r.title, r.number), brackets(r.title)
        with self._tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO resource(source, tid, forum_id, forum_name, type_id, type_name, title, number,"
                " magnet, info_hash, size, posted, subtitle, seeders, tags, brackets, source_id, images, crawled_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    r.source, r.tid, r.forum_id, r.forum_name, r.type_id, r.type_name, r.title, r.number,
                    r.magnet, r.info_hash, r.size, r.posted, int(r.subtitle), r.seeders,
                    json.dumps(sorted(r.tags)), json.dumps(r.brackets, ensure_ascii=False), r.source_id,
                    json.dumps(r.images), time.time(),
                ),
            )
            row = c.execute("SELECT * FROM resource WHERE source = ? AND tid = ?", (r.source, r.tid)).fetchone()
        return Resource.from_row(row)

    def resource(self, resource_id: int) -> Resource | None:
        row = self._one("SELECT * FROM resource WHERE id = ?", (resource_id,))
        return Resource.from_row(row) if row else None

    def resources(
        self,
        *,
        search: str = "",
        sources: list[str] | None = None,
        source_ids: list[int] | None = None,
        tags: list[str] | None = None,
        forum_ids: list[int] | None = None,
        type_ids: list[int] | None = None,
        subtitle: bool | None = None,
        numbered: bool = False,
        with_magnet: bool = False,
        limit: int = 200,
    ) -> list[Resource]:
        sql, params = "SELECT * FROM resource WHERE 1=1", []
        if search:
            sql += " AND (title LIKE ? OR number LIKE ?)"
            params += [f"%{search}%", f"%{search}%"]
        if sources:
            sql += f" AND source IN ({','.join('?' * len(sources))})"
            params += sources
        if source_ids:
            sql += f" AND source_id IN ({','.join('?' * len(source_ids))})"
            params += source_ids
        for tag in tags or []:
            sql += " AND tags LIKE ?"
            params.append(f'%"{tag}"%')
        if forum_ids:
            sql += f" AND forum_id IN ({','.join('?' * len(forum_ids))})"
            params += forum_ids
        if type_ids:
            sql += f" AND type_id IN ({','.join('?' * len(type_ids))})"
            params += type_ids
        if subtitle is not None:
            sql += " AND subtitle = ?"
            params.append(int(subtitle))
        if numbered:
            sql += " AND number IS NOT NULL"
        if with_magnet:
            sql += " AND magnet IS NOT NULL"
        sql += " ORDER BY crawled_at DESC, tid DESC LIMIT ?"
        params.append(limit)
        return [Resource.from_row(r) for r in self._all(sql, params)]

    def resources_by_number(self, number: str) -> list[Resource]:
        rows = self._all("SELECT * FROM resource WHERE number = ? AND magnet IS NOT NULL", (number,))
        return [Resource.from_row(r) for r in rows]

    def resource_forums(self) -> list[sqlite3.Row]:
        return self._all(
            "SELECT forum_id, MAX(forum_name) AS forum_name, COUNT(*) AS n FROM resource WHERE source = 'sehuatang'"
            " GROUP BY forum_id ORDER BY forum_id"
        )

    def resource_sources(self) -> dict[str, int]:
        return {r["source"]: r["n"] for r in self._all("SELECT source, COUNT(*) AS n FROM resource GROUP BY source")}

    # --- 挑选作品: 两条路径共用的「待挑选」= 没进下载队列、115 里没有、没被忽略 ---

    _UNHANDLED = (
        "number NOT IN (SELECT number FROM wanted)"
        " AND number NOT IN (SELECT number FROM library WHERE number IS NOT NULL)"
        " AND number NOT IN (SELECT number FROM dismissed)"
    )
    _PENDING = f"number IS NOT NULL AND wanted_id IS NULL AND {_UNHANDLED}"

    def pool_resources(
        self, *, source_ids: list[int] | None = None, tags: list[str] | None = None, search: str = "", days: int | None = None,
        limit: int = 3000,
    ) -> list[Resource]:
        """磁力池里识别出番号的资源 (挑选作品页按番号聚合)."""
        sql, params = "SELECT * FROM resource WHERE number IS NOT NULL", []
        if source_ids:
            sql += f" AND source_id IN ({','.join('?' * len(source_ids))})"
            params += source_ids
        for tag in tags or []:
            sql += " AND tags LIKE ?"
            params.append(f'%"{tag}"%')
        if search:
            sql += " AND (title LIKE ? OR number LIKE ?)"
            params += [f"%{search}%", f"%{search}%"]
        if days:
            sql += " AND crawled_at >= ?"
            params.append(time.time() - days * 86400)
        sql += " ORDER BY crawled_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return [Resource.from_row(r) for r in self._all(sql, params)]

    def new_unhandled_count(self, since: float) -> int:
        """最近新出现、还待挑选的作品数 (导航角标)."""
        row = self._one(
            "SELECT COUNT(DISTINCT number) AS n FROM ("
            f" SELECT number FROM resource WHERE {self._PENDING} AND crawled_at >= ?"
            f" UNION SELECT number FROM release WHERE {self._UNHANDLED} AND first_seen >= ?)",
            (since, since),
        )
        return row["n"] if row else 0

    def dismiss_numbers(self, numbers: list[str]) -> int:
        with self._tx() as c:
            return sum(c.execute("INSERT OR IGNORE INTO dismissed(number, at) VALUES(?, ?)", (n, time.time())).rowcount for n in numbers)

    def undismiss_numbers(self, numbers: list[str]) -> None:
        with self._tx() as c:
            c.executemany("DELETE FROM dismissed WHERE number = ?", [(n,) for n in numbers])

    def dismissed_numbers(self, numbers: Iterable[str]) -> set[str]:
        return {r["number"].upper() for r in self._in_chunks("SELECT number FROM dismissed WHERE number IN ({})", numbers)}

    def wanted_by_numbers(self, numbers: Iterable[str]) -> dict[str, Wanted]:
        return {r["number"].upper(): Wanted.from_row(r) for r in self._in_chunks("SELECT * FROM wanted WHERE number IN ({})", numbers)}

    # --- 磁力订阅 ---

    def add_source(self, src: MagnetSource) -> MagnetSource:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO magnet_source(kind, name, params, filter, enabled, auto_download, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    src.kind, src.name, json.dumps(src.params, ensure_ascii=False), json.dumps(asdict(src.filter), ensure_ascii=False),
                    int(src.enabled), int(src.auto_download), time.time(),
                ),
            )
            row = c.execute("SELECT * FROM magnet_source WHERE id = ?", (cur.lastrowid,)).fetchone()
        return MagnetSource.from_row(row)

    def source(self, source_id: int) -> MagnetSource | None:
        row = self._one("SELECT * FROM magnet_source WHERE id = ?", (source_id,))
        return MagnetSource.from_row(row) if row else None

    def sources(self, *, kind: str | None = None, enabled_only: bool = False) -> list[MagnetSource]:
        sql, params = (
            "SELECT s.*, (SELECT COUNT(*) FROM resource r WHERE r.source_id = s.id) AS found FROM magnet_source s WHERE 1=1",
            [],
        )
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if enabled_only:
            sql += " AND enabled = 1"
        return [MagnetSource.from_row(r) for r in self._all(sql + " ORDER BY kind, id", params)]

    def update_source(self, source_id: int, **fields: Any) -> None:
        if not fields:
            return
        values = []
        for k, v in fields.items():
            if k == "filter":
                v = json.dumps(asdict(v), ensure_ascii=False)
            elif k == "params":
                v = json.dumps(v, ensure_ascii=False)
            elif isinstance(v, bool):
                v = int(v)
            values.append(v)
        with self._tx() as c:
            c.execute(f"UPDATE magnet_source SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*values, source_id))

    def delete_source(self, source_id: int) -> None:
        """删除订阅; 已入池的磁力保留."""
        with self._tx() as c:
            c.execute("DELETE FROM magnet_source WHERE id = ?", (source_id,))
            c.execute("DELETE FROM source_seen WHERE source_id = ?", (source_id,))
            c.execute("UPDATE resource SET source_id = NULL WHERE source_id = ?", (source_id,))

    def mark_seen(self, source_id: int, key: str) -> None:
        """记下被入池条件过滤掉的条目, 之后不再打开, 也作为「翻到已见过的就停」的依据."""
        with self._tx() as c:
            c.execute("INSERT OR IGNORE INTO source_seen(source_id, key) VALUES(?, ?)", (source_id, key))

    def is_seen(self, source_id: int, key: str) -> bool:
        return self._one("SELECT 1 FROM source_seen WHERE source_id = ? AND key = ?", (source_id, key)) is not None

    def pool_counts(self, numbers: Iterable[str]) -> dict[str, int]:
        """番号 (大写) → 磁力池里带磁力的资源数."""
        return {
            r["number"].upper(): r["n"]
            for r in self._in_chunks(
                "SELECT number, COUNT(*) AS n FROM resource WHERE magnet IS NOT NULL AND number IN ({}) GROUP BY number",
                numbers,
            )
        }

    def owned_numbers(self, numbers: Iterable[str]) -> set[str]:
        return {r["number"].upper() for r in self._in_chunks("SELECT DISTINCT number FROM library WHERE number IN ({})", numbers)}

    def _in_chunks(self, sql: str, values: Iterable[str]) -> list[sqlite3.Row]:
        """``IN (...)`` 查询按 500 个一组执行, 避开 SQLite 的参数个数上限."""
        values = list(dict.fromkeys(v for v in values if v))
        out: list[sqlite3.Row] = []
        for i in range(0, len(values), 500):
            chunk = values[i : i + 500]
            out += self._all(sql.format(",".join("?" * len(chunk))), chunk)
        return out

    # --- Amane 缓存 ---

    def works(self, numbers: Iterable[str]) -> dict[str, Work]:
        """番号 (大写) → Amane 里找到的作品; 没查过或没找到的不在结果里."""
        rows = self._in_chunks("SELECT * FROM amane_work WHERE found = 1 AND number IN ({})", numbers)
        return {r["number"].upper(): Work.from_row(r) for r in rows}

    def save_work(self, w: Work) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO amane_work(number, found, title, date, actors, studio, poster, thumb, amane_id, file_count,"
                " checked_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(number) DO UPDATE SET found=excluded.found,"
                " title=excluded.title, date=excluded.date, actors=excluded.actors, studio=excluded.studio,"
                " poster=excluded.poster, thumb=excluded.thumb, amane_id=excluded.amane_id,"
                " file_count=excluded.file_count, checked_at=excluded.checked_at",
                (
                    w.number, int(w.found), w.title, w.date, json.dumps(w.actors, ensure_ascii=False), w.studio,
                    w.poster, w.thumb, w.amane_id, w.file_count, time.time(),
                ),
            )

    def numbers_for_amane(self, refresh_before: float, limit: int) -> list[str]:
        """需要去 Amane 查的番号: 从没查过或上次查询早于 ``refresh_before`` 的.

        先查下载队列, 再查待确认的磁力池, 最后是作品库 (按发行日期从新到旧).
        """
        rows = self._all(
            "SELECT number, MIN(prio) AS prio, MAX(ord) AS ord FROM ("
            " SELECT number, 0 AS prio, updated_at AS ord FROM wanted"
            f" UNION ALL SELECT number, 1, crawled_at FROM resource WHERE {self._PENDING}"
            " UNION ALL SELECT number, 2, COALESCE(date, '') FROM release"
            ") AS n WHERE number NOT IN (SELECT number FROM amane_work WHERE checked_at >= ?)"
            " GROUP BY number COLLATE NOCASE ORDER BY prio, ord DESC LIMIT ?",
            (refresh_before, limit),
        )
        return [r["number"] for r in rows]

    def amane_actor(self, name: str) -> AmaneActor | None:
        row = self._one("SELECT * FROM amane_actor WHERE name = ?", (name,))
        return AmaneActor.from_row(row) if row else None

    def save_amane_actor(self, a: AmaneActor) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO amane_actor(name, found, image, profile, amane_id, checked_at) VALUES(?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET found=excluded.found, image=excluded.image, profile=excluded.profile,"
                " amane_id=excluded.amane_id, checked_at=excluded.checked_at",
                (a.name, int(a.found), a.image, json.dumps(a.profile, ensure_ascii=False), a.amane_id, time.time()),
            )

    def resource_types(self, forum_id: int) -> list[sqlite3.Row]:
        return self._all(
            "SELECT type_id, MAX(type_name) AS type_name, COUNT(*) AS n FROM resource"
            " WHERE forum_id = ? AND type_id IS NOT NULL GROUP BY type_id ORDER BY n DESC",
            (forum_id,),
        )

    def link_resource(self, resource_id: int, wanted_id: int) -> None:
        with self._tx() as c:
            c.execute("UPDATE resource SET wanted_id = ? WHERE id = ?", (wanted_id, resource_id))

    # --- 订阅与新作 ---

    def add_subscription(self, sub: Subscription) -> Subscription:
        """新增订阅; 同一页面已订阅时返回已有的那条."""
        with self._tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO subscription(source, kind, ident, uncensored, name, auto_search, created_at,"
                " avatar, profile) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sub.source, sub.kind, sub.ident, int(sub.uncensored), sub.name, int(sub.auto_search), time.time(),
                    sub.avatar, json.dumps(sub.profile, ensure_ascii=False),
                ),
            )
            row = c.execute(
                "SELECT * FROM subscription WHERE source = ? AND kind = ? AND ident = ? AND uncensored = ?",
                (sub.source, sub.kind, sub.ident, int(sub.uncensored)),
            ).fetchone()
        return Subscription.from_row(row)

    def subscription(self, sub_id: int) -> Subscription | None:
        row = self._one("SELECT * FROM subscription WHERE id = ?", (sub_id,))
        return Subscription.from_row(row) if row else None

    def subscriptions(self, *, enabled_only: bool = False, kind: str | None = None) -> list[Subscription]:
        sql, params = "SELECT * FROM subscription WHERE 1=1", []
        if enabled_only:
            sql += " AND enabled = 1"
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        return [Subscription.from_row(r) for r in self._all(sql + " ORDER BY kind, name", params)]

    def update_subscription(self, sub_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = [
            int(v) if isinstance(v, bool) else json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else v
            for v in fields.values()
        ]
        with self._tx() as c:
            c.execute(f"UPDATE subscription SET {cols} WHERE id = ?", (*values, sub_id))

    def delete_subscription(self, sub_id: int) -> None:
        """删除订阅; 只属于它的新作一并删除, 同时属于其它订阅的保留."""
        with self._tx() as c:
            c.execute("DELETE FROM subscription WHERE id = ?", (sub_id,))
            c.execute("DELETE FROM release WHERE id NOT IN (SELECT release_id FROM release_sub)")

    def known_release_numbers(self, sub_id: int) -> set[str]:
        rows = self._all(
            "SELECT r.number FROM release r JOIN release_sub rs ON rs.release_id = r.id WHERE rs.subscription_id = ?",
            (sub_id,),
        )
        return {r["number"].upper() for r in rows}

    def add_release(self, rel: Release, sub_id: int) -> tuple[Release, bool]:
        """写入作品并关联到订阅; 返回 (作品, 是否是这个订阅新见到的).

        同一番号只存一行, 不论来自哪个资料站: 同一女优在 JavBus 与 JavDB 上都订阅时, 新作库与 RSS
        里每部作品只出现一次. 先见到的站点作为来源与链接, 后来的只补全缺失的日期与封面.
        """
        with self._tx() as c:
            row = c.execute("SELECT * FROM release WHERE number = ?", (rel.number,)).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO release(source, number, title, date, cover, url, first_seen) VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (rel.source, rel.number, rel.title, rel.date, rel.cover, rel.url, time.time()),
                )
            else:
                c.execute(
                    "UPDATE release SET title = CASE WHEN source = ? THEN ? ELSE title END,"
                    " date = COALESCE(date, ?), cover = COALESCE(cover, ?) WHERE id = ?",
                    (rel.source, rel.title, rel.date, rel.cover, row["id"]),
                )
            row = c.execute("SELECT * FROM release WHERE number = ?", (rel.number,)).fetchone()
            cur = c.execute(
                "INSERT OR IGNORE INTO release_sub(release_id, subscription_id) VALUES(?, ?)", (row["id"], sub_id)
            )
            c.execute(
                "UPDATE subscription SET release_count = (SELECT COUNT(*) FROM release_sub WHERE subscription_id = ?)"
                " WHERE id = ?",
                (sub_id, sub_id),
            )
        return Release.from_row(row), cur.rowcount == 1

    def releases(
        self,
        *,
        sub_ids: list[int] | None = None,
        kinds: list[str] | None = None,
        search: str = "",
        days: int | None = None,
        not_queued: bool = False,
        limit: int = 200,
    ) -> list[tuple[Release, str, int | None, str | None]]:
        """返回 (作品, 所属订阅名, 下载队列里的番号记录 id, 其状态)."""
        sql = (
            "SELECT r.*, GROUP_CONCAT(DISTINCT s.name) AS subs, w.id AS wanted_id, w.status AS wanted_status"
            " FROM release r JOIN release_sub rs ON rs.release_id = r.id JOIN subscription s ON s.id = rs.subscription_id"
            " LEFT JOIN wanted w ON w.number = r.number WHERE 1=1"
        )
        params: list[Any] = []
        if sub_ids:
            sql += f" AND s.id IN ({','.join('?' * len(sub_ids))})"
            params += sub_ids
        if kinds:
            sql += f" AND s.kind IN ({','.join('?' * len(kinds))})"
            params += kinds
        if search:
            sql += " AND (r.number LIKE ? OR r.title LIKE ?)"
            params += [f"%{search}%", f"%{search}%"]
        if days:
            sql += " AND r.date >= date('now', ?)"
            params.append(f"-{int(days)} day")
        if not_queued:
            sql += " AND w.id IS NULL"
        sql += " GROUP BY r.id ORDER BY r.date DESC, r.id DESC LIMIT ?"
        params.append(limit)
        return [(Release.from_row(row), row["subs"] or "", row["wanted_id"], row["wanted_status"]) for row in self._all(sql, params)]

    def releases_by_numbers(self, numbers: Iterable[str]) -> dict[str, Release]:
        """番号 (大写) → 作品库里的作品."""
        return {r["number"].upper(): Release.from_row(r) for r in self._in_chunks("SELECT * FROM release WHERE number IN ({})", numbers)}

    def release(self, release_id: int) -> Release | None:
        row = self._one("SELECT * FROM release WHERE id = ?", (release_id,))
        return Release.from_row(row) if row else None
