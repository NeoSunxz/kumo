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
    """有码作品的无码破解 / 流出 / 去马赛克."""
    UNCENSORED = "uncensored"
    """无码厂牌原版 (HEYZO、一本道、加勒比、H4610 等)."""
    WESTERN = "western"
    FC2 = "fc2"


TAG_LABELS: dict[str, str] = {
    Tag.SUBTITLE: "中字",
    Tag.HD: "高清",
    Tag.UHD: "4K",
    Tag.H265: "H265",
    Tag.LEAK: "破解",
    Tag.UNCENSORED: "无码",
    Tag.WESTERN: "欧美",
    Tag.FC2: "FC2",
}

#: 用户可选的标签 (筛选、磁力订阅的入池条件), 按网页上的显示顺序.
FILTER_TAGS = [Tag.SUBTITLE, Tag.LEAK, Tag.UNCENSORED, Tag.WESTERN, Tag.FC2, Tag.UHD, Tag.HD, Tag.H265]

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
    rank: int = 999
    """来源的优先顺序: 磁力订阅按「磁力订阅」页的排序 (0 最优先), 按番号搜索各站的结果排在所有订阅之后."""
    origin: str = ""
    """来自哪条磁力订阅 (显示用)."""
    id: int | None = None
    wanted_id: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Candidate:
        data = {k: row[k] for k in row.keys() if k in cls.__dataclass_fields__}
        data["tags"] = set(json.loads(data["tags"]))
        data["origin"] = data.get("origin") or ""
        data["rank"] = 999 if data.get("rank") is None else data["rank"]
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


@dataclass
class Resource:
    """磁力池的一条: 论坛帖子或 sukebei 种子.

    sukebei 的 ``tid`` 是种子编号, ``forum_id`` 固定为 0, ``type_name`` 是命中的规则名.
    """

    source: str
    tid: int
    forum_id: int
    forum_name: str
    type_name: str
    title: str
    type_id: int | None = None
    number: str | None = None
    magnet: str | None = None
    info_hash: str | None = None
    size: int | None = None
    posted: str | None = None
    subtitle: bool = False
    seeders: int | None = None
    wanted_id: int | None = None
    tags: set[str] = field(default_factory=set)
    """从标题识别的标签 (字幕、破解、无码、欧美…)."""
    brackets: list[str] = field(default_factory=list)
    """标题里方括号中的标记 (片头 ``[新模型无码破解]`` 等)."""
    source_id: int | None = None
    """入池的磁力订阅."""
    images: list[str] = field(default_factory=list)
    """帖子里的图片 (论坛才有); 经 ``/img/res/<id>/<序号>`` 带 Referer 取."""
    crawled_at: float = 0.0
    id: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Resource:
        data = {k: row[k] for k in row.keys() if k in cls.__dataclass_fields__}
        data["subtitle"] = bool(data["subtitle"])
        data["tags"] = set(json.loads(data.get("tags") or "[]"))
        data["brackets"] = json.loads(data.get("brackets") or "[]")
        data["images"] = json.loads(data.get("images") or "[]")
        return cls(**data)


SOURCE_KINDS = {"sehuatang": "98堂", "sukebei": "sukebei", "rss": "RSS"}


@dataclass
class SourceFilter:
    """磁力订阅的入池条件; 都为空表示全部入池. 只看标题, 论坛可以在打开帖子前就判断."""

    tags_all: list[str] = field(default_factory=list)
    """这些标签都要有 (如「中字 + 破解」)."""
    tags_any: list[str] = field(default_factory=list)
    """至少有其中一个标签."""
    tags_none: list[str] = field(default_factory=list)
    """不能有这些标签 (如不要「欧美」)."""
    brackets: list[str] = field(default_factory=list)
    """片头方括号里含其中之一 (如「破解」匹配 ``[新模型无码破解]``)."""
    types: list[str] = field(default_factory=list)
    """论坛分类名."""
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    numbered: bool = False
    """只要识别出番号的."""

    def accepts(self, title: str, tags: set[str], brackets: list[str], type_name: str = "", number: str | None = None) -> bool:
        return not self.reject_reason(title, tags, brackets, type_name, number)

    def reject_reason(self, title: str, tags: set[str], brackets: list[str], type_name: str = "", number: str | None = None) -> str:
        """不符合入池条件的原因; 符合时返回空串. 预览页据此逐条说明."""
        if missing := [t for t in self.tags_all if t not in tags]:
            return "没有" + "、".join(TAG_LABELS.get(t, t) for t in missing)
        if self.tags_any and not tags & set(self.tags_any):
            return "没有" + "/".join(TAG_LABELS.get(t, t) for t in self.tags_any)
        if hit := tags & set(self.tags_none):
            return "有" + "/".join(TAG_LABELS.get(t, t) for t in sorted(hit))
        if self.brackets and not any(b in br for b in self.brackets for br in brackets):
            return "片头不含 " + "/".join(f"[{b}]" for b in self.brackets)
        if self.types and type_name not in self.types:
            return f"分类「{type_name or '无'}」不在范围内"
        lowered = title.lower()
        if self.include and not any(k.lower() in lowered for k in self.include):
            return "标题不含 " + "/".join(self.include)
        if hit_word := next((k for k in self.exclude if k.lower() in lowered), None):
            return f"标题含排除词「{hit_word}」"
        if self.numbered and not number:
            return "识别不出番号"
        return ""

    def summary(self) -> list[str]:
        out = ["要" + "+".join(TAG_LABELS.get(t, t) for t in self.tags_all)] if self.tags_all else []
        if self.tags_any:
            out.append("要" + "或".join(TAG_LABELS.get(t, t) for t in self.tags_any))
        out += [f"不要{TAG_LABELS.get(t, t)}" for t in self.tags_none]
        out += [f"片头[{b}]" for b in self.brackets] + [f"分类:{t}" for t in self.types]
        out += [f"+{k}" for k in self.include] + [f"-{k}" for k in self.exclude]
        return out + (["只要有番号"] if self.numbered else [])


@dataclass
class MagnetSource:
    """一条磁力订阅: 论坛板块 / sukebei 搜索 / RSS, 新磁力按入池条件进入磁力池."""

    kind: str
    name: str
    params: dict[str, object] = field(default_factory=dict)
    """sehuatang: fid、typeid; sukebei: query、user、category、trusted_only、min_seeders; rss: url."""
    filter: SourceFilter = field(default_factory=SourceFilter)
    enabled: bool = True
    auto_download: bool = False
    """入池后直接加入下载队列 (跳过挑选作品), 磁力按选择规则自动挑."""
    last_run: float | None = None
    last_error: str = ""
    created_at: float = 0.0
    priority: int = 0
    """磁力选择时的优先顺序, 小的优先 (「磁力订阅」页上排序)."""
    id: int | None = None
    found: int = 0
    """入池的磁力数 (查询时统计)."""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> MagnetSource:
        data = {k: row[k] for k in row.keys() if k in cls.__dataclass_fields__}
        data["params"] = json.loads(data.get("params") or "{}")
        data["filter"] = SourceFilter(**json.loads(row["filter"] or "{}"))
        data["enabled"] = bool(data["enabled"])
        data["auto_download"] = bool(data["auto_download"])
        return cls(**data)

    @property
    def label(self) -> str:
        return SOURCE_KINDS.get(self.kind, self.kind)

    def describe(self) -> str:
        p = self.params
        if self.kind == "sehuatang":
            return f"板块 {p.get('fid')}" + (f" · 分类 {p.get('typeid')}" if p.get("typeid") else "")
        if self.kind == "sukebei":
            parts = [f"搜「{p.get('query')}」" if p.get("query") else "全部", f"分类 {p.get('category') or '2_2'}"]
            parts += [f"上传者 {p.get('user')}"] if p.get("user") else []
            parts += ["只要可信"] if p.get("trusted_only") else []
            parts += [f"做种≥{p.get('min_seeders')}"] if p.get("min_seeders") else []
            return " · ".join(parts)
        return str(p.get("url", ""))


@dataclass
class Work:
    """按番号缓存的作品信息, 取自 Amane (海报、封面、演员); 片源与磁力两边共用."""

    number: str
    found: bool
    """Amane 里有没有这个番号; 没有时其余字段为空, 过一段时间再查."""
    title: str = ""
    date: str | None = None
    actors: list[str] = field(default_factory=list)
    studio: str = ""
    poster: str | None = None
    thumb: str | None = None
    amane_id: int | None = None
    file_count: int = 0
    """Amane 媒体库里关联的文件数; 大于 0 表示已入库."""
    checked_at: float = 0.0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Work:
        data = dict(row)
        data["found"] = bool(data["found"])
        data["actors"] = json.loads(data["actors"] or "[]")
        return cls(**data)


@dataclass
class AmaneActor:
    """按名称缓存的 Amane 演员: 头像与资料, 用来填充女优页."""

    name: str
    found: bool
    image: str | None = None
    profile: dict[str, str] = field(default_factory=dict)
    amane_id: int | None = None
    checked_at: float = 0.0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> AmaneActor:
        data = dict(row)
        data["found"] = bool(data["found"])
        data["profile"] = json.loads(data["profile"] or "{}")
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


@dataclass
class Subscription:
    """订阅的一个资料站页面 (女优 / 系列 / 片商 ...)."""

    source: str
    kind: str
    ident: str
    uncensored: bool
    name: str
    enabled: bool = True
    auto_search: bool = False
    """新作自动加入下载队列去搜磁力; 默认只入新作库, 由用户手动挑选."""
    checked_at: float | None = None
    last_error: str = ""
    release_count: int = 0
    created_at: float = 0.0
    avatar: str | None = None
    profile: dict[str, str] = field(default_factory=dict)
    """资料站页面上的资料 (生日、身高…); 只有女优页有."""
    id: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Subscription:
        data = dict(row)
        for key in ("uncensored", "enabled", "auto_search"):
            data[key] = bool(data[key])
        data["profile"] = json.loads(data.get("profile") or "{}")
        return cls(**data)


@dataclass
class Release:
    """资料站上的一部作品 (新作库的一行); 同一作品可属于多个订阅."""

    source: str
    number: str
    title: str
    date: str | None
    cover: str | None
    url: str
    first_seen: float = 0.0
    id: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Release:
        return cls(**{k: row[k] for k in row.keys() if k in cls.__dataclass_fields__})
