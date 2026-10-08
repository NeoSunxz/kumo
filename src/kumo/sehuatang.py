"""sehuatang (98堂) 论坛爬取.

Discuz 论坛, 无 Cloudflare 验证, 但有年龄确认页: 页面里给出 ``safeid``, 点击进入时由脚本写入
``_safe`` Cookie. 因此请求返回确认页时把 safeid 写进 Cookie 再请求一次.

板块列表只给标题与分类, 磁力与大小在帖子正文里 (「磁力链接：」下的代码块, 【影片容量】字段).
列表默认按最后回复时间排序, 旧帖被回复会顶上来; 爬取一律按发帖时间 (``orderby=dateline``),
「遇到已爬过的帖子即停止翻页」才成立. 站点经常更换域名, 地址由配置给出.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .magnets import Fetcher, SiteError
from .models import parse_size

_SAFEID = re.compile(r"var safeid='([^']+)'")
_ROW = re.compile(r'<tbody id="normalthread_(\d+)">(.*?)</tbody>', re.S)
_ROW_TITLE = re.compile(r'class="s xst"[^>]*>(.*?)</a>', re.S)
_ROW_TYPE = re.compile(r"<em>\[<a [^>]*typeid=(\d+)[^>]*>([^<]+)</a>\]</em>")
_ROW_DATE = re.compile(r'<td class="by">\s*<cite>.*?</cite>\s*<em>.*?(?:title="([0-9-]+)"|>([0-9]{4}-[0-9-]+)<)', re.S)
_FORUM_NAME = re.compile(r"<title>\s*([^<-]+?)\s+-")
_SUBJECT = re.compile(r'<span id="thread_subject">([^<]+)</span>')
_FIRST_POST = re.compile(r'<td class="t_f" id="postmessage_\d+">(.*?)</td>', re.S)
_MAGNET = re.compile(r"magnet:\?xt=urn:btih:[0-9A-Za-z]{32,40}")
_SIZE = re.compile(r"【影片容量】\s*[:：]\s*([\d.]+\s*[KMGT]i?B?)", re.I)
_CENSORED = re.compile(r"【是否有码】\s*[:：]\s*([^\s<【]+)")
_IMG = re.compile(r"<img\b[^>]*>", re.I)
_IMG_ATTR = re.compile(r'\b(?:zoomfile|file|src)="(https?://[^"]+)"', re.I)
_POSTED = re.compile(r'发表于 <span title="([^"]+)"|发表于 ([0-9]{4}-[0-9-]+ [0-9:]+)')


@dataclass
class ThreadRef:
    tid: int
    title: str
    type_name: str
    posted: str | None
    type_id: int | None = None


@dataclass
class ThreadDetail:
    tid: int
    title: str
    magnet: str | None
    size: int | None
    censored: str | None
    posted: str | None
    images: list[str] = field(default_factory=list)
    """正文里的图片 (通常第一张是封面, 后面是截图); 图床要带论坛的 Referer 才能取."""


def is_age_gate(text: str) -> bool:
    return _SAFEID.search(text) is not None


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse_forum(text: str) -> tuple[str, list[ThreadRef]]:
    """返回 (板块名, 普通帖子列表); 置顶帖不在 ``normalthread_`` 里, 不会被列出."""
    name = _FORUM_NAME.search(text)
    threads: list[ThreadRef] = []
    for tid, row in _ROW.findall(text):
        title = _ROW_TITLE.search(row)
        if not title:
            continue
        typ = _ROW_TYPE.search(row)
        date = _ROW_DATE.search(row)
        threads.append(
            ThreadRef(
                tid=int(tid),
                title=_text(title.group(1)),
                type_name=typ.group(2).strip() if typ else "",
                type_id=int(typ.group(1)) if typ else None,
                posted=(date.group(1) or date.group(2)) if date else None,
            )
        )
    return (name.group(1).strip() if name else ""), threads


def parse_thread(tid: int, text: str) -> ThreadDetail:
    subject = _SUBJECT.search(text)
    body_match = _FIRST_POST.search(text)
    body = body_match.group(1) if body_match else text
    magnet = _MAGNET.search(body)
    plain = html.unescape(re.sub(r"<[^>]+>", "\n", body))
    size = _SIZE.search(plain)
    size_text = size.group(1).strip() if size else ""
    if size_text and not size_text.upper().endswith("B"):
        size_text += "B"  # 帖子里常写成 5.29G
    censored = _CENSORED.search(plain)
    posted = _POSTED.search(text)
    images: list[str] = []
    for tag in _IMG.findall(body):
        m = _IMG_ATTR.search(tag)
        if m and "static/image" not in m.group(1) and m.group(1) not in images:
            images.append(html.unescape(m.group(1)))
    return ThreadDetail(
        tid=tid,
        title=html.unescape(subject.group(1)).strip() if subject else "",
        magnet=magnet.group(0) if magnet else None,
        size=parse_size(size_text) if size_text else None,
        censored=censored.group(1).strip() if censored else None,
        posted=(posted.group(1) or posted.group(2)) if posted else None,
        images=images[:8],
    )


def forum_path(fid: int, page: int, typeid: int | None = None) -> str:
    """按发帖时间排序的板块列表; 给出 ``typeid`` 时只列该分类."""
    query = f"mod=forumdisplay&fid={fid}&orderby=dateline&page={page}"
    query += f"&filter=typeid&typeid={typeid}" if typeid else "&filter=author"
    return f"forum.php?{query}"


class Sehuatang:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")
        host = urlsplit(self.base).hostname or ""
        # Cookie 写到主域上, www. 与裸域都能用.
        self._cookie_domain = "." + host.removeprefix("www.")

    async def _get(self, fetcher: Fetcher, path: str) -> str:
        url = f"{self.base}/{path}"
        status, text = await fetcher.get_text(url)
        m = _SAFEID.search(text)
        if m:
            fetcher.set_cookie("_safe", m.group(1), self._cookie_domain)
            status, text = await fetcher.get_text(url)
            if is_age_gate(text):
                raise SiteError("sehuatang 年龄确认未通过 (站点可能改了验证方式)")
        if status != 200:
            raise SiteError(f"sehuatang 返回 HTTP {status}")
        return text

    async def forum_page(
        self, fetcher: Fetcher, fid: int, page: int, typeid: int | None = None
    ) -> tuple[str, list[ThreadRef]]:
        return parse_forum(await self._get(fetcher, forum_path(fid, page, typeid)))

    def thread_url(self, tid: int) -> str:
        return f"{self.base}/thread-{tid}-1-1.html"

    async def thread(self, fetcher: Fetcher, tid: int) -> ThreadDetail:
        return parse_thread(tid, await self._get(fetcher, f"thread-{tid}-1-1.html"))
