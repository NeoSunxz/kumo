"""RSS 输入 (磁力订阅的 RSS 来源).

条目里有磁力就直接用; 只有 .torrent 链接时下载种子算出 info hash 转成磁力
(115 云下载收到 HTTP 链接只会把 .torrent 文件本身存下来); 都没有就从标题识别番号.
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import quote

from .models import info_hash_of
from .numbers import extract_number

_MAGNET = re.compile(r"magnet:\?xt=urn:btih:[0-9A-Za-z]{32,40}[^\s\"'<>]*")
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


def magnet_hash(magnet: str) -> str | None:
    return info_hash_of(magnet)
