"""RSS 输入与输出.

输入: 订阅用户的 RSS/Atom. 条目里有磁力就直接用; 只有 .torrent 链接时下载种子算出 info hash
转成磁力 (115 云下载收到 HTTP 链接只会把 .torrent 文件本身存下来); 都没有就从标题识别番号, 交给磁力刮削.

输出: 已提交 / 已完成的番号, 供 Amane 的远程发现订阅. Amane 用 ``extract_number(title)`` 解析番号,
因此条目标题只写番号本身; ``guid`` 按番号固定, 同一番号不会被 Amane 重复入队.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import formatdate
from urllib.parse import quote
from xml.sax.saxutils import escape

from .models import Resource, Wanted, format_size, info_hash_of
from .numbers import extract_number

_MAGNET = re.compile(r"magnet:\?xt=urn:btih:[0-9A-Za-z]{32,40}[^\s\"'<>]*")
_CST = timezone(timedelta(hours=8))
_ATOM = "{http://www.w3.org/2005/Atom}"


@dataclass
class FeedEntry:
    key: str
    title: str
    magnet: str | None
    torrent_url: str | None
    number: str | None


def parse_feed(text: str) -> list[FeedEntry]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError("不是有效的 RSS/Atom") from exc
    entries: list[FeedEntry] = []
    for item in root.iter("item"):
        enclosure = item.find("enclosure")
        entries.append(
            _entry(
                title=item.findtext("title") or "",
                link=item.findtext("link") or "",
                guid=item.findtext("guid") or "",
                description=item.findtext("description") or "",
                enclosure=enclosure.get("url", "") if enclosure is not None else "",
            )
        )
    for item in root.iter(f"{_ATOM}entry"):
        links = [link.get("href", "") for link in item.findall(f"{_ATOM}link")]
        entries.append(
            _entry(
                title=item.findtext(f"{_ATOM}title") or "",
                link=links[0] if links else "",
                guid=item.findtext(f"{_ATOM}id") or "",
                description=(item.findtext(f"{_ATOM}summary") or "") + (item.findtext(f"{_ATOM}content") or ""),
                enclosure=next((h for h in links if h.endswith(".torrent") or h.startswith("magnet:")), ""),
            )
        )
    return entries


def _entry(*, title: str, link: str, guid: str, description: str, enclosure: str) -> FeedEntry:
    title = title.strip()
    magnet = next((m.group(0) for t in (enclosure, link, description) if (m := _MAGNET.search(t))), None)
    torrent = next((u for u in (enclosure, link) if u.startswith("http") and ".torrent" in u.lower()), None)
    return FeedEntry(
        key=(guid or link or title).strip(),
        title=title,
        magnet=magnet,
        torrent_url=None if magnet else torrent,
        number=extract_number(title) or extract_number(description),
    )


# --- .torrent → 磁力 ---


def _bdecode_info_span(data: bytes) -> tuple[int, int]:
    """返回顶层字典里 ``info`` 值的字节区间; info hash 是这段原始字节的 SHA-1."""

    def skip(i: int) -> int:
        c = data[i : i + 1]
        if c == b"i":
            return data.index(b"e", i) + 1
        if c in (b"l", b"d"):
            i += 1
            while data[i : i + 1] != b"e":
                i = skip(i)
            return i + 1
        colon = data.index(b":", i)
        return colon + 1 + int(data[i:colon])

    if data[:1] != b"d":
        raise ValueError("不是 bencode 字典")
    i = 1
    while data[i : i + 1] != b"e":
        key_end = skip(i)
        key = data[data.index(b":", i) + 1 : key_end]
        value_end = skip(key_end)
        if key == b"info":
            return key_end, value_end
        i = value_end
    raise ValueError("种子里没有 info 字段")


def torrent_to_magnet(data: bytes, name: str = "") -> str:
    start, end = _bdecode_info_span(data)
    info_hash = hashlib.sha1(data[start:end]).hexdigest()
    return f"magnet:?xt=urn:btih:{info_hash}" + (f"&dn={quote(name)}" if name else "")


# --- 输出 ---


def render_rss(items: list[tuple[Wanted, str]], *, link: str) -> str:
    """``items`` 为 (番号记录, 所选磁力的标题)."""
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0"><channel>',
        "<title>kumo</title>",
        f"<link>{escape(link)}</link>",
        "<description>kumo 已提交 115 云下载的番号</description>",
    ]
    for wanted, title in items:
        when = wanted.submitted_at or wanted.updated_at
        parts.append(
            "<item>"
            f"<title>{escape(wanted.number)}</title>"
            f'<guid isPermaLink="false">kumo-{escape(wanted.number)}</guid>'
            f"<pubDate>{formatdate(when, usegmt=True)}</pubDate>"
            f"<description>{escape(title)}</description>"
            "</item>"
        )
    parts.append("</channel></rss>")
    return "\n".join(parts)


def render_resource_rss(items: list[Resource], *, title: str, link: str, thread_url: Callable[[int], str]) -> str:
    """论坛资源的 RSS.

    标题写「番号 原标题」: Amane 从标题解析番号, 原标题留给人看. ``guid`` 用帖子编号, 同一帖子
    不会被重复入队; 磁力放在 enclosure 里, 供需要直接下载的订阅端使用.
    """
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0"><channel>',
        f"<title>{escape(title)}</title>",
        f"<link>{escape(link)}</link>",
        f"<description>{escape(title)}</description>",
    ]
    for r in items:
        heading = f"{r.number} {r.title}" if r.number and not r.title.upper().startswith(r.number) else r.title
        when = _posted_timestamp(r.posted) or r.crawled_at
        desc = " · ".join(x for x in (r.forum_name, r.type_name, format_size(r.size) if r.size else "") if x)
        enclosure = (
            f'<enclosure url="{escape(r.magnet, {chr(34): "&quot;"})}" type="application/x-bittorrent" length="{r.size or 0}"/>'
            if r.magnet
            else ""
        )
        parts.append(
            "<item>"
            f"<title>{escape(heading)}</title>"
            f"<link>{escape(thread_url(r.tid))}</link>"
            f'<guid isPermaLink="false">{escape(r.source)}-{r.tid}</guid>'
            f"<pubDate>{formatdate(when, usegmt=True)}</pubDate>"
            f"<description>{escape(desc)}</description>"
            f"{enclosure}"
            "</item>"
        )
    parts.append("</channel></rss>")
    return "\n".join(parts)


def _posted_timestamp(posted: str | None) -> float | None:
    """论坛时间是北京时间, 只有日期时按当天零点."""
    if not posted:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(posted, fmt).replace(tzinfo=_CST).timestamp()
        except ValueError:
            continue
    return None


def magnet_hash(magnet: str) -> str | None:
    return info_hash_of(magnet)
