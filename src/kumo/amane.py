"""读取 Amane 的作品与演员信息, 用作 kumo 页面的海报、封面、演员图等内容; 按番号让 Amane 刮削.

读 Amane 的 HTTP 接口 (``/api/metadata``、``/api/actors``); 唯一的写入是提交刮削任务 (``POST /api/tasks``).
Amane 不对同一番号去重, 由 kumo 记下提交过的番号. Amane 设了 API token 时
用 ``Authorization: Bearer`` 访问. 图片地址可能是外链, 也可能是 Amane 本地的 ``/api/resources/{hash}``;
都由 kumo 带 token 经 Amane 取回 (外链走 Amane 的 ``/api/resources/proxy``), 见 ``fetch_image``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from .models import AmaneActor, Work

#: Amane 演员资料里展示到女优页的字段.
_PROFILE_FIELDS = (
    ("birthday", "生日"), ("height", "身高"), ("cup", "罩杯"), ("bust", "胸围"), ("waist", "腰围"), ("hip", "臀围"),
    ("birthplace", "出生地"),
)


@dataclass
class AmaneDetail:
    id: int
    title: str
    plot: str
    series: str
    studio: str
    publisher: str
    directors: list[str]
    tags: list[str]
    actors: list[str]
    release: str | None
    runtime: int | None
    score: float | None
    poster: str | None
    thumb: str | None
    fanart: list[str]
    trailer: str | None
    actor_ids: dict[str, int] = field(default_factory=dict)
    files: int = 0
    """Amane 媒体库里关联的文件数."""


def amane_number(number: str) -> str:
    """kumo 的番号写法转成 Amane 的: FC2 在 Amane 里是 ``FC2-1234567`` (没有 PPV), 其余相同."""
    return "FC2-" + number.removeprefix("FC2-PPV-") if number.upper().startswith("FC2-PPV-") else number


class AmaneError(Exception):
    pass


class Amane:
    def __init__(self, base_url: str, token: str = "", *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=20.0, follow_redirects=True, transport=transport
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def scrape(self, number: str) -> int:
        """让 Amane 按番号刮削 (内容类型由 Amane 按番号推断); 返回任务编号."""
        try:
            resp = await self._client.post("/api/tasks", json={"type": "scrape", "number": amane_number(number)})
        except httpx.HTTPError as exc:
            raise AmaneError(f"无法访问 Amane: {type(exc).__name__}") from exc
        if resp.status_code == 401:
            raise AmaneError("Amane 拒绝访问, 请检查 API token")
        if resp.status_code not in (200, 201, 202):
            raise AmaneError(f"Amane 提交刮削失败: HTTP {resp.status_code} {resp.text[:120]}")
        return int(resp.json().get("id") or 0)

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        try:
            resp = await self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise AmaneError(f"无法访问 Amane: {type(exc).__name__}") from exc
        if resp.status_code == 401:
            raise AmaneError("Amane 拒绝访问, 请检查 API token")
        if resp.status_code != 200:
            raise AmaneError(f"Amane 返回 HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise AmaneError("Amane 返回的不是 JSON, 请检查地址") from exc

    async def check(self) -> int:
        """测试连接与 token; 返回 Amane 里的作品总数. ``/api/health`` 不校验 token, 所以查一页作品."""
        data = await self._get("/api/metadata", {"limit": 1})
        return int(data.get("total") or 0)

    async def work(self, number: str) -> Work:
        """按番号查作品. ``/api/metadata?search=`` 是番号与标题的模糊匹配, 这里只取番号完全一致的."""
        query = amane_number(number)
        data = await self._get("/api/metadata", {"search": query, "limit": 20})
        for item in data.get("items") or []:
            if str(item.get("number", "")).casefold() != query.casefold():
                continue
            posters = item.get("poster_urls") or []
            thumbs = item.get("thumb_urls") or []
            return Work(
                number=number,
                found=True,
                title=item.get("title") or "",
                date=item.get("release"),
                actors=list(item.get("actors") or []),
                studio=item.get("studio") or "",
                poster=posters[0] if posters else None,
                thumb=thumbs[0] if thumbs else None,
                amane_id=item.get("id"),
                file_count=int(item.get("file_count") or 0),
            )
        return Work(number=number, found=False)

    async def detail(self, amane_id: int) -> AmaneDetail:
        """作品详情 (简介、剧照、预告片、标签等), 作品页用; 不入库, 由调用方短时缓存."""
        data = await self._get(f"/api/metadata/{amane_id}", {})
        m = data.get("metadata") or {}
        fanart: list[str] = []
        for urls in (m.get("extrafanart_urls") or {}).values():
            fanart += [u for u in urls if u not in fanart]
        return AmaneDetail(
            id=amane_id, title=m.get("title") or "", plot=m.get("plot") or "", series=m.get("series") or "",
            studio=m.get("studio") or "", publisher=m.get("publisher") or "", directors=list(m.get("directors") or []),
            tags=list(m.get("tags") or []), actors=list(m.get("actors") or []), release=m.get("release"),
            runtime=m.get("runtime"), score=m.get("score"),
            poster=(m.get("poster_urls") or [None])[0], thumb=(m.get("thumb_urls") or [None])[0],
            fanart=fanart[:24], trailer=(m.get("trailer_urls") or [None])[0],
            actor_ids=dict(data.get("actor_ids") or {}), files=len(data.get("files") or []),
        )

    async def actor(self, name: str) -> AmaneActor:
        # 资料站的名字可能带空格 (「春陽 モカ」), Amane 的搜索是字面匹配, 去掉空格再搜.
        key = "".join(name.split())
        data = await self._get("/api/actors", {"search": key, "limit": 20})
        key = key.casefold()
        for item in data.get("items") or []:
            if "".join(str(item.get("name", "")).split()).casefold() != key:
                continue
            images = item.get("image_urls") or []
            profile = {}
            for field, label in _PROFILE_FIELDS:
                value = item.get(field)
                if value:
                    profile[label] = f"{value}cm" if field in ("height", "bust", "waist", "hip") else str(value)
            return AmaneActor(name=name, found=True, image=images[0] if images else None, profile=profile, amane_id=item.get("id"))
        return AmaneActor(name=name, found=False)

    def image_request(self, url: str) -> tuple[str, dict[str, str]]:
        """图片的取回地址与参数: Amane 本地资源直接取, 外链交给 Amane 的图片代理."""
        if url.startswith("/"):
            return url, {}
        return "/api/resources/proxy", {"url": url}

    async def fetch_image(self, url: str) -> httpx.Response:
        path, params = self.image_request(url)
        try:
            return await self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise AmaneError(f"无法访问 Amane: {type(exc).__name__}") from exc
