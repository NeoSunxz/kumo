"""订阅: 按女优 / 系列 / 片商等条件, 定期从资料站获取新作.

目前的资料站是 JavBus: 女优、系列、片商、发行商、导演、类别都有按发行日期倒序的作品列表,
每页 30 部, 「下一页」链接给出是否还有更多. 有码与无码是两套路径 (无码在 ``/uncensored/`` 下).
订阅以页面地址标识, 用户直接粘贴 JavBus 上对应页面的地址即可.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from .magnets import JAVBUS, Fetcher, SiteError, javbus_get

KIND_LABELS = {
    "star": "女优",
    "series": "系列",
    "studio": "片商",
    "label": "发行商",
    "director": "导演",
    "genre": "类别",
}
_PATH = re.compile(r"^/(uncensored/)?(star|series|studio|label|director|genre)/([0-9a-zA-Z]+)(?:/\d+)?/?$")
_TITLE = re.compile(r"<title>\s*([^<]+?)\s+-\s+[^<-]+-\s")
_BOX = re.compile(r'<a class="movie-box" href="([^"]+)">(.*?)</a>', re.S)
_DATES = re.compile(r"<date>([^<]+)</date>")
_IMG = re.compile(r'<img src="([^"]+)" title="([^"]*)"')
_NEXT = re.compile(r'<a id="next" href="[^"]+"')


@dataclass(frozen=True)
class SubscriptionKey:
    kind: str
    ident: str
    uncensored: bool

    @property
    def label(self) -> str:
        return ("无码" if self.uncensored else "") + KIND_LABELS[self.kind]

    def page_url(self, page: int = 1) -> str:
        path = f"{'uncensored/' if self.uncensored else ''}{self.kind}/{self.ident}"
        return f"{JAVBUS}/{path}" + (f"/{page}" if page > 1 else "")


@dataclass
class ListedRelease:
    number: str
    title: str
    date: str | None
    cover: str | None
    url: str


def parse_subscription_url(url: str) -> SubscriptionKey:
    """接受 JavBus 的女优 / 系列 / 片商 / 发行商 / 导演 / 类别页面地址 (含分页与镜像域名)."""
    parts = urlsplit(url.strip())
    m = _PATH.match(parts.path)
    if not parts.scheme.startswith("http") or not m:
        raise ValueError("请粘贴 JavBus 上女优、系列、片商、发行商、导演或类别页面的地址, 如 https://www.javbus.com/star/1248")
    return SubscriptionKey(kind=m.group(2), ident=m.group(3), uncensored=bool(m.group(1)))


def parse_list(text: str, base: str = JAVBUS) -> tuple[str, list[ListedRelease], bool]:
    """返回 (页面上的名称, 作品列表, 是否还有下一页)."""
    title = _TITLE.search(text)
    releases: list[ListedRelease] = []
    for href, box in _BOX.findall(text):
        dates = _DATES.findall(box)
        img = _IMG.search(box)
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
    return (html.unescape(title.group(1)) if title else ""), releases, bool(_NEXT.search(text))


async def fetch_list(fetcher: Fetcher, key: SubscriptionKey, page: int = 1) -> tuple[str, list[ListedRelease], bool]:
    status, text = await javbus_get(fetcher, key.page_url(page))
    if status == 404:
        raise SiteError("JavBus 上找不到这个页面")
    if status != 200:
        raise SiteError(f"javbus 返回 HTTP {status}")
    return parse_list(text)
