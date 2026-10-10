"""磁力站点刮削.

每个站点把搜索结果解析成 ``Candidate`` (标题、磁力、info hash、大小、做种数、站点标签);
是否与番号匹配、打分与取舍由 ``select`` 负责. 站点失败只影响自己, 由调用方记录后继续其它站点.
"""

from __future__ import annotations

import asyncio
import html
import re
import secrets
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import dataclass
from email.utils import formatdate
from urllib.parse import quote, urlencode, urlsplit

import httpx

from .models import Candidate, Tag, info_hash_of, parse_size
from .numbers import extract_number

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


class SiteError(Exception):
    """站点不可用或返回了无法解析的内容; 消息直接展示给用户."""


class Fetcher:
    """站点 HTTP 访问: 直连 (可走代理), 遇到 Cloudflare 验证且配置了 FlareSolverr 时改由它取页面."""

    def __init__(self, *, proxy: str = "", flaresolverr_url: str = "", javdb_cookie: str = "") -> None:
        self._client = httpx.AsyncClient(
            proxy=proxy or None,
            headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8"},
            follow_redirects=True,
            timeout=httpx.Timeout(30.0),
        )
        # JavBus 默认隐藏一部分条目; existmag=all 让详情页与磁力列表完整显示.
        self._client.cookies.set("existmag", "all", domain=".javbus.com")
        for name, value in parse_cookie_header(javdb_cookie):
            self._client.cookies.set(name, value, domain=".javdb.com")
        self._solver = flaresolverr_url.rstrip("/")

    async def aclose(self) -> None:
        await self._client.aclose()

    def set_cookie(self, name: str, value: str, domain: str) -> None:
        self._client.cookies.set(name, value, domain=domain)

    async def get_text(self, url: str, *, headers: dict[str, str] | None = None) -> tuple[int, str]:
        resp = await self._send("GET", url, headers=headers)
        if _is_cloudflare_block(resp) and self._solver:
            return await self._via_solver(url)
        if _is_cloudflare_block(resp):
            raise SiteError(f"{httpx.URL(url).host} 被 Cloudflare 拦截 (未配置 FlareSolverr)")
        return resp.status_code, resp.text

    async def post_form(self, url: str, data: dict[str, str], *, headers: dict[str, str] | None = None) -> int:
        return (await self._send("POST", url, data=data, headers=headers)).status_code

    async def _send(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        """连接层错误 (握手中断、超时) 重试两次; 站点在国内网络下经常偶发断连."""
        for attempt in range(3):
            try:
                return await self._client.request(method, url, **kwargs)  # type: ignore[arg-type]
            except httpx.TransportError as exc:
                if attempt == 2:
                    raise SiteError(f"无法访问 {httpx.URL(url).host}: {type(exc).__name__}") from exc
                await asyncio.sleep(2 * (attempt + 1))
        raise AssertionError("unreachable")

    async def _via_solver(self, url: str) -> tuple[int, str]:
        try:
            resp = await self._client.post(
                f"{self._solver}/v1",
                json={"cmd": "request.get", "url": url, "maxTimeout": 120000},
                timeout=150.0,
            )
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SiteError(f"FlareSolverr 不可用: {type(exc).__name__}") from exc
        if data.get("status") != "ok":
            raise SiteError(f"FlareSolverr 失败: {str(data.get('message', ''))[:120]}")
        solution = data.get("solution") or {}
        return int(solution.get("status") or 0), solution.get("response") or ""


def parse_cookie_header(text: str) -> list[tuple[str, str]]:
    """浏览器里复制的 ``a=1; b=2`` 形式; 也接受只粘贴了值 (视为 ``_jdb_session``)."""
    text = text.strip().removeprefix("Cookie:").strip()
    if not text:
        return []
    if "=" not in text:
        return [("_jdb_session", text)]
    pairs = []
    for part in text.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name.strip():
            pairs.append((name.strip(), value.strip()))
    return pairs


def _is_cloudflare_block(resp: httpx.Response) -> bool:
    if resp.status_code not in (403, 429, 503):
        return False
    if "cloudflare" not in resp.headers.get("server", "").lower():
        return False
    return True


class MagnetSite(ABC):
    name: str

    @abstractmethod
    async def search(self, fetcher: Fetcher, number: str) -> list[Candidate]: ...


# --- sukebei.nyaa.si: RSS 搜索, 自带 info hash / 大小 / 做种数 ---

_NYAA_NS = {"nyaa": "https://sukebei.nyaa.si/xmlns/nyaa"}


class Sukebei(MagnetSite):
    name = "sukebei"
    base = "https://sukebei.nyaa.si"

    @classmethod
    def rss_url(cls, *, query: str = "", user: str = "", category: str = "0_0", trusted_only: bool = False) -> str:
        params = {"page": "rss", "q": query, "c": category or "0_0", "f": "2" if trusted_only else "0"}
        if user:
            params["u"] = user
        return f"{cls.base}/?{urlencode(params)}"

    @classmethod
    def list_url(cls, *, query: str = "", user: str = "", category: str = "0_0", trusted_only: bool = False, page: int = 1) -> str:
        """网页版列表: RSS 不能翻页 (只给最新 75 条), 翻页与总页数要看网页版."""
        params = {"q": query, "c": category or "0_0", "f": "2" if trusted_only else "0"}
        if user:
            params["u"] = user
        if page > 1:
            params["p"] = str(page)
        return f"{cls.base}/?{urlencode(params)}"

    async def search(self, fetcher: Fetcher, number: str) -> list[Candidate]:
        status, text = await fetcher.get_text(self.rss_url(query=number))
        if status != 200:
            raise SiteError(f"sukebei 返回 HTTP {status}")
        return parse_sukebei_rss(text)


@dataclass
class SukebeiItem:
    id: int
    """种子编号 (``/view/<id>``)."""
    candidate: Candidate
    category: str
    trusted: bool


_NYAA_VIEW = re.compile(r"/view/(\d+)")


def parse_sukebei_items(text: str) -> list[SukebeiItem]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise SiteError("sukebei 返回的不是 RSS") from exc
    out: list[SukebeiItem] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        info_hash = (item.findtext("nyaa:infoHash", namespaces=_NYAA_NS) or "").strip().lower()
        view = _NYAA_VIEW.search(item.findtext("guid") or "")
        if not title or not re.fullmatch(r"[0-9a-f]{40}", info_hash) or not view:
            continue
        seeders = item.findtext("nyaa:seeders", namespaces=_NYAA_NS)
        out.append(
            SukebeiItem(
                id=int(view.group(1)),
                candidate=Candidate(
                    site="sukebei",
                    title=title,
                    magnet=f"magnet:?xt=urn:btih:{info_hash}&dn={quote(title)}",
                    info_hash=info_hash,
                    size=parse_size(item.findtext("nyaa:size", namespaces=_NYAA_NS) or ""),
                    seeders=int(seeders) if seeders and seeders.isdigit() else None,
                    published=(item.findtext("pubDate") or "").strip() or None,
                ),
                category=(item.findtext("nyaa:category", namespaces=_NYAA_NS) or "").strip(),
                trusted=(item.findtext("nyaa:trusted", namespaces=_NYAA_NS) or "").strip() == "Yes",
            )
        )
    return out


_NYAA_ROW = re.compile(r'<tr class="(default|success|danger)">(.*?)</tr>', re.S)
_NYAA_ROW_VIEW = re.compile(r'<a href="/view/(\d+)" title="([^"]+)"')
_NYAA_ROW_MAGNET = re.compile(r'href="(magnet:\?xt=urn:btih:[^"]+)"')
_NYAA_ROW_CELLS = re.compile(r'<td class="text-center"(?: data-timestamp="(\d+)")?>([^<]*)</td>')
_NYAA_PAGES = re.compile(r'[?&]p=(\d+)"')


def parse_sukebei_html(text: str) -> tuple[list[SukebeiItem], int]:
    """网页版列表页: (条目, 总页数). 行的 class: success 是可信上传者, danger 是 remake."""
    items: list[SukebeiItem] = []
    for kind, row in _NYAA_ROW.findall(text):
        view, magnet = _NYAA_ROW_VIEW.search(row), _NYAA_ROW_MAGNET.search(row)
        if not view or not magnet:
            continue
        title, link = html.unescape(view.group(2)).strip(), html.unescape(magnet.group(1))
        info_hash = info_hash_of(link)
        if not info_hash:
            continue
        cells = _NYAA_ROW_CELLS.findall(row)
        size = parse_size(cells[0][1]) if cells else None
        stamp = next((int(ts) for ts, _ in cells if ts), None)
        numbers = [v for ts, v in cells[2:] if not ts and v.strip().isdigit()]
        items.append(SukebeiItem(
            id=int(view.group(1)),
            candidate=Candidate(
                site="sukebei", title=title, magnet=f"magnet:?xt=urn:btih:{info_hash}&dn={quote(title)}", info_hash=info_hash,
                size=size, seeders=int(numbers[0]) if numbers else None,
                published=formatdate(stamp, usegmt=True) if stamp else None,
            ),
            category="", trusted=kind == "success",
        ))
    pages = [int(p) for p in _NYAA_PAGES.findall(text)]
    current = re.search(r'<li class="active"><a>(\d+)</a>', text)
    return items, max(pages + ([int(current.group(1))] if current else []) + [1])


def parse_sukebei_rss(text: str) -> list[Candidate]:
    return [item.candidate for item in parse_sukebei_items(text)]


# --- JavBus: 详情页取 gid / uc / img, 再请求磁力列表 ---

_JB_GID = re.compile(r"var gid = (\d+)")
_JB_UC = re.compile(r"var uc = (\d+)")
_JB_IMG = re.compile(r"var img = '([^']*)'")
_JB_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_JB_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_JB_MAGNET = re.compile(r'href="(magnet:\?xt=urn:btih:[^"]+)"')
_JB_TAG = re.compile(r'class="btn btn-mini-new btn-(\w+)')
_TAG_CLASS = {"primary": Tag.HD, "warning": Tag.SUBTITLE}


JAVBUS = "https://www.javbus.com"
JAVDB = "https://javdb.com"


def is_javbus_verify(text: str) -> bool:
    # 验证页独有的表单; 详情页、列表页、磁力列表都没有.
    return 'id="form1"' in text and "你是否已經成年" in text


async def javbus_get(fetcher: Fetcher, url: str) -> tuple[int, str]:
    """GET JavBus 页面; 落到年龄验证页时提交「我已經成年」表单后重试.

    验证是间歇性的 (按 IP / 时段触发): 多数时候直接给出页面, 有时 302 到 ``/doc/driver-verify``.
    验证页的表单只有一个提交按钮, 通过后状态记在会话 (PHPSESSID) 里. 重试后仍是验证页就报错,
    不能当成「没有结果」, 否则搜索会静默地什么都找不到.
    """
    status, text = await fetcher.get_text(url)
    if not is_javbus_verify(text):
        return status, text
    host = urlsplit(url)
    verify = f"{host.scheme}://{host.netloc}/doc/driver-verify?referer={quote(url, safe='')}"
    await fetcher.post_form(
        verify, {"Submit": "確認"}, headers={"Referer": verify, "Origin": f"{host.scheme}://{host.netloc}"}
    )
    status, text = await fetcher.get_text(url)
    if is_javbus_verify(text):
        raise SiteError("javbus 年龄验证未通过")
    return status, text


class JavBus(MagnetSite):
    name = "javbus"
    base = JAVBUS

    async def search(self, fetcher: Fetcher, number: str) -> list[Candidate]:
        status, page = await javbus_get(fetcher, f"{self.base}/{number}")
        if status == 404:
            return []
        if status != 200:
            raise SiteError(f"javbus 返回 HTTP {status}")
        gid, uc, img = _JB_GID.search(page), _JB_UC.search(page), _JB_IMG.search(page)
        if not gid:
            return []  # 没有该番号的详情页 (搜索结果页或未收录)
        query = urlencode(
            {
                "gid": gid.group(1),
                "lang": "zh",
                "img": img.group(1) if img else "",
                "uc": uc.group(1) if uc else "0",
                "floor": secrets.randbelow(1000) + 1,
            }
        )
        status, text = await fetcher.get_text(
            f"{self.base}/ajax/uncledatoolsbyajax.php?{query}",
            headers={"Referer": f"{self.base}/{number}", "X-Requested-With": "XMLHttpRequest"},
        )
        if status != 200:
            raise SiteError(f"javbus 磁力列表返回 HTTP {status}")
        return parse_javbus_magnets(text)


def parse_javbus_magnets(text: str) -> list[Candidate]:
    out: list[Candidate] = []
    for row in _JB_ROW.findall(text):
        m = _JB_MAGNET.search(row)
        if not m:
            continue
        magnet = html.unescape(m.group(1))
        info_hash = info_hash_of(magnet)
        if not info_hash:
            continue
        cells = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip() for c in _JB_CELL.findall(row)]
        tags = {_TAG_CLASS[t] for t in _JB_TAG.findall(row) if t in _TAG_CLASS}
        title = cells[0] if cells else ""
        # 标题格里混着标签按钮的文字 (高清 / 字幕), 去掉以免干扰番号与关键词匹配.
        title = re.sub(r"\s*(高清|字幕)\s*", " ", title).strip()
        out.append(
            Candidate(
                site="javbus",
                title=title,
                magnet=magnet,
                info_hash=info_hash,
                size=parse_size(cells[1]) if len(cells) > 1 else None,
                published=cells[2] if len(cells) > 2 else None,
                tags=tags,
            )
        )
    return out


# --- JavDB: 搜索番号 → 详情页的磁力列表 ---

_JD_BOX = re.compile(r'<a href="(/v/[0-9a-zA-Z]+)" class="box"[^>]*>(.*?)</a>', re.S)
_JD_STRONG = re.compile(r"<strong>([^<]+)</strong>")
_JD_MAGNET = re.compile(
    r'<a href="(magnet:\?xt=urn:btih:[^"]+)"[^>]*>\s*<span class="name">([^<]*)</span>(.*?)</a>\s*</div>'
    r'\s*<div class="date"><span class="time">([^<]*)</span>',
    re.S,
)
_JD_TAG = re.compile(r'<span class="tag[^"]*">([^<]+)</span>')
_JD_META = re.compile(r'<span class="meta">\s*([^<]+?)\s*</span>')
_JD_TAGS = {"字幕": Tag.SUBTITLE, "高清": Tag.HD}


def javdb_detail_path(search_page: str, number: str) -> str | None:
    """搜索结果里番号完全一致的那部的详情页路径; 只有相近番号时返回 ``None``."""
    for href, box in _JD_BOX.findall(search_page):
        strong = _JD_STRONG.search(box)
        if strong and extract_number(html.unescape(strong.group(1))) == number:
            return href
    return None


def parse_javdb_magnets(text: str) -> list[Candidate]:
    out: list[Candidate] = []
    for magnet, name, body, date in _JD_MAGNET.findall(text):
        magnet = html.unescape(magnet)
        info_hash = info_hash_of(magnet)
        if not info_hash:
            continue
        meta = _JD_META.search(body)
        out.append(
            Candidate(
                site="javdb",
                title=html.unescape(name).strip(),
                magnet=magnet,
                info_hash=info_hash,
                size=parse_size(meta.group(1)) if meta else None,
                published=date.strip() or None,
                tags={_JD_TAGS[t.strip()] for t in _JD_TAG.findall(body) if t.strip() in _JD_TAGS},
            )
        )
    return out


class JavDB(MagnetSite):
    name = "javdb"
    base = JAVDB

    async def search(self, fetcher: Fetcher, number: str) -> list[Candidate]:
        status, page = await fetcher.get_text(f"{self.base}/search?{urlencode({'q': number, 'f': 'all'})}")
        if status == 429:
            raise SiteError("javdb 请求过于频繁")
        if status != 200:
            raise SiteError(f"javdb 返回 HTTP {status}")
        path = javdb_detail_path(page, number)
        if path is None:
            return []
        status, detail = await fetcher.get_text(f"{self.base}{path}")
        if status != 200:
            raise SiteError(f"javdb 详情页返回 HTTP {status}")
        return parse_javdb_magnets(detail)


SITES: dict[str, MagnetSite] = {s.name: s for s in (Sukebei(), JavBus(), JavDB())}
