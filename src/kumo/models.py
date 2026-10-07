from __future__ import annotations

import base64
import json
import re
import sqlite3
from dataclasses import dataclass, field
from enum import StrEnum


class WantedStatus(StrEnum):
    PENDING = "pending"
    """待搜索磁力."""
    NO_MAGNET = "no_magnet"
    """没搜到, 过一段时间重试."""
    NEED_CHOICE = "need_choice"
    """有候选但没有达标的, 等人工选择."""
    QUEUED = "queued"
    """已选定磁力, 等待提交 (115 未授权或未配置下载目录时停在这里)."""
    SUBMITTED = "submitted"
    """已提交云下载, 等待 115 开始."""
    DOWNLOADING = "downloading"
    DONE = "done"
    FAILED = "failed"
    OWNED = "owned"
    """115 目录里已经有了, 跳过."""
    IGNORED = "ignored"


STATUS_LABELS: dict[WantedStatus, str] = {
    WantedStatus.PENDING: "待搜索",
    WantedStatus.NO_MAGNET: "无磁力",
    WantedStatus.NEED_CHOICE: "待选择",
    WantedStatus.QUEUED: "待提交",
    WantedStatus.SUBMITTED: "已提交",
    WantedStatus.DOWNLOADING: "下载中",
    WantedStatus.DONE: "已完成",
    WantedStatus.FAILED: "失败",
    WantedStatus.OWNED: "已拥有",
    WantedStatus.IGNORED: "已忽略",
}


@dataclass
class Wanted:
    id: int
    number: str
    status: WantedStatus
    source: str
    note: str
    chosen_id: int | None
    info_hash: str | None
    percent: float
    searched_at: float | None
    submitted_at: float | None
    done_at: float | None
    created_at: float
    updated_at: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Wanted:
        data = dict(row)
        data["status"] = WantedStatus(data["status"])
        return cls(**data)


class Tag(StrEnum):
    SUBTITLE = "subtitle"
    HD = "hd"
    UHD = "4k"
    H265 = "h265"
    LEAK = "leak"
    """无码破解 / 去马赛克."""


TAG_LABELS: dict[str, str] = {
    Tag.SUBTITLE: "字幕",
    Tag.HD: "高清",
    Tag.UHD: "4K",
    Tag.H265: "H265",
    Tag.LEAK: "破解",
}

_MAGNET_HASH = re.compile(r"urn:btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})")


def info_hash_of(magnet: str) -> str | None:
    m = _MAGNET_HASH.search(magnet)
    if not m:
        return None
    value = m.group(1)
    if len(value) == 32:  # base32 形式
        return base64.b32decode(value.upper()).hex()
    return value.lower()


@dataclass
class Candidate:
    site: str
    title: str
    magnet: str
    info_hash: str
    size: int | None = None
    seeders: int | None = None
    published: str | None = None
    tags: set[str] = field(default_factory=set)
    score: float = 0.0
    rejected: str = ""
    """不满足硬性条件的原因; 空串表示可自动选择."""
    id: int | None = None
    wanted_id: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Candidate:
        data = dict(row)
        data["tags"] = set(json.loads(data["tags"]))
        return cls(**data)


@dataclass
class LibraryFile:
    fid: str
    name: str
    size: int = 0
    pick_code: str = ""
    number: str | None = None
    subtitle: bool = False
    root: str = ""
    seen_run: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> LibraryFile:
        data = dict(row)
        data["subtitle"] = bool(data["subtitle"])
        return cls(**data)


def format_size(size: int | None) -> str:
    if not size:
        return "-"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}TB"


_SIZE = re.compile(r"([\d.]+)\s*([KMGT]i?B)", re.I)
_UNITS = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def parse_size(text: str) -> int | None:
    m = _SIZE.search(text)
    if not m:
        return None
    return int(float(m.group(1)) * _UNITS[m.group(2)[0].upper()])
