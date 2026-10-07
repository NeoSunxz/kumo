"""订阅: 按女优 / 系列 / 片商等条件, 定期从资料站获取新作.

资料站:

- JavBus: 女优、系列、片商、发行商、导演、类别的作品列表, 每页 30 部, 按发行日期倒序;
  有码与无码是两套路径 (无码在 ``/uncensored/`` 下).
- JavDB: 演员、系列、片商、发行商、导演、番号前缀的作品列表, 每页 40 部, 按发行日期倒序,
  含已公布未发行的作品 (日期在未来), 因此新作出现得比 JavBus 早.

订阅以页面地址标识, 用户直接粘贴资料站上对应页面的地址即可.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from .magnets import JAVBUS, JAVDB, Fetcher, SiteError, javbus_get

KIND_LABELS = {
    "star": "女优",
    "series": "系列",
    "studio": "片商",
    "label": "发行商",
    "director": "导演",
    "genre": "类别",
    "code": "番号前缀",
}
SOURCE_LABELS = {"javbus": "JavBus", "javdb": "JavDB"}

_JAVBUS_PATH = re.compile(r"^/(uncensored/)?(star|series|studio|label|director|genre)/([0-9a-zA-Z]+)(?:/\d+)?/?$")
# JavDB 的路径名与 kumo 的类型名对应关系
_JAVDB_KINDS = {"actors": "star", "series": "series", "makers": "studio", "publishers": "label", "directors": "director", "video_codes": "code"}
_JAVDB_PATHS = {v: k for k, v in _JAVDB_KINDS.items()}
_JAVDB_PATH = re.compile(rf"^/({'|'.join(_JAVDB_KINDS)})/([0-9a-zA-Z_-]+)/?$")
# JavDB 上这些「编号」其实是分类入口而不是具体的演员 / 片商.
_JAVDB_CATEGORY_IDS = frozenset({"censored", "uncensored", "western"})


@dataclass(frozen=True)
class SubscriptionKey:
    kind: str
    ident: str
    uncensored: bool = False
    source: str = "javbus"

    @property
    def label(self) -> str:
        return ("无码" if self.uncensored else "") + KIND_LABELS[self.kind]

    def page_url(self, page: int = 1) -> str:
        if self.source == "javdb":
            return f"{JAVDB}/{_JAVDB_PATHS[self.kind]}/{self.ident}" + (f"?page={page}" if page > 1 else "")
        path = f"{'uncensored/' if self.uncensored else ''}{self.kind}/{self.ident}"
        return f"{JAVBUS}/{path}" + (f"/{page}" if page > 1 else "")


@dataclass
class ListedRelease:
    number: str
    title: str
    date: str | None
    cover: str | None
    url: str


_HINT = (
    "请粘贴 JavBus 或 JavDB 上演员、系列、片商、发行商、导演 (JavBus 还有类别, JavDB 还有番号前缀) 页面的地址, "
    "如 https://www.javbus.com/star/1248 或 https://javdb.com/actors/B85e9"
)


def parse_subscription_url(url: str) -> SubscriptionKey:
    """识别站点与页面类型; 接受分页地址与镜像域名 (域名含 javdb 的按 JavDB 处理)."""
    parts = urlsplit(url.strip())
    if not parts.scheme.startswith("http"):
        raise ValueError(_HINT)
    if "javdb" in (parts.hostname or ""):
        m = _JAVDB_PATH.match(parts.path)
        if not m or m.group(2) in _JAVDB_CATEGORY_IDS:
            raise ValueError(_HINT)
        return SubscriptionKey(kind=_JAVDB_KINDS[m.group(1)], ident=m.group(2), source="javdb")
    m = _JAVBUS_PATH.match(parts.path)
    if not m:
        raise ValueError(_HINT)
    return SubscriptionKey(kind=m.group(2), ident=m.group(3), uncensored=bool(m.group(1)))


# --- JavBus 列表 ---

_JB_TITLE = re.compile(r"<title>\s*([^<]+?)\s+-\s+[^<-]+-\s")
_JB_BOX = re.compile(r'<a class="movie-box" href="([^"]+)">(.*?)</a>', re.S)
_JB_DATES = re.compile(r"<date>([^<]+)</date>")
_JB_IMG = re.compile(r'<img src="([^"]+)" title="([^"]*)"')
_JB_NEXT = re.compile(r'<a id="next" href="[^"]+"')


def parse_list(text: str, base: str = JAVBUS) -> tuple[str, list[ListedRelease], bool]:
    """JavBus 列表页: 返回 (页面上的名称, 作品列表, 是否还有下一页)."""
    title = _JB_TITLE.search(text)
    releases: list[ListedRelease] = []
    for href, box in _JB_BOX.findall(text):
        dates = _JB_DATES.findall(box)
        img = _JB_IMG.search(box)
        if not dates:
            continue
        releases.append(
            ListedRelease(
                number=dates[0].strip().upper(),
                title=html.unescape(img.group(2)).strip() if img else "",
                date=dates[1].strip() if len(dates) > 1 else None,
                cover=urljoin(base, img.group(1)) if img else None,
                url=href,
            )
        )
    return (html.unescape(title.group(1)) if title else ""), releases, bool(_JB_NEXT.search(text))


# --- JavDB 列表 ---

_JD_TITLE = re.compile(r"<title>\s*([^<|]+?)\s*\|")
_JD_BOX = re.compile(r'<a href="(/v/[0-9a-zA-Z]+)" class="box" title="([^"]*)">(.*?)</a>', re.S)
_JD_NUMBER = re.compile(r"<strong>([^<]+)</strong>")
_JD_DATE = re.compile(r'<div class="meta">\s*([0-9]{4}-[0-9]{2}-[0-9]{2})\s*</div>')
_JD_IMG = re.compile(r'<img[^>]+src="([^"]+)"')
_JD_NEXT = re.compile(r'<a rel="next" class="pagination-next"')


def parse_javdb_list(text: str, base: str = JAVDB) -> tuple[str, list[ListedRelease], bool]:
    """JavDB 列表页 (演员 / 片商 / 番号前缀 ... 以及搜索结果): 返回 (名称, 作品列表, 是否还有下一页)."""
    title = _JD_TITLE.search(text)
    releases: list[ListedRelease] = []
    for href, box_title, box in _JD_BOX.findall(text):
        number = _JD_NUMBER.search(box)
        if not number:
            continue
        date = _JD_DATE.search(box)
        img = _JD_IMG.search(box)
        releases.append(
            ListedRelease(
                number=html.unescape(number.group(1)).strip().upper(),
                title=html.unescape(box_title).strip(),
                date=date.group(1) if date else None,
                cover=urljoin(base, img.group(1)) if img else None,
                url=urljoin(base, href),
            )
        )
    return (html.unescape(title.group(1)).strip() if title else ""), releases, bool(_JD_NEXT.search(text))


async def fetch_list(fetcher: Fetcher, key: SubscriptionKey, page: int = 1) -> tuple[str, list[ListedRelease], bool]:
    if key.source == "javdb":
        status, text = await fetcher.get_text(key.page_url(page))
        parse = parse_javdb_list
    else:
        status, text = await javbus_get(fetcher, key.page_url(page))
        parse = parse_list
    if status == 404:
        raise SiteError(f"{SOURCE_LABELS[key.source]} 上找不到这个页面")
    if status == 429:
        raise SiteError(f"{SOURCE_LABELS[key.source]} 请求过于频繁, 请调大请求间隔")
    if status != 200:
        raise SiteError(f"{key.source} 返回 HTTP {status}")
    return parse(text)
