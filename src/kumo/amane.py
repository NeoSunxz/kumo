"""读取 Amane 的作品与演员信息, 用作 kumo 页面的海报、封面、演员图等内容.

只读 Amane 的 HTTP 接口 (``/api/metadata``、``/api/actors``), 不写入 Amane. Amane 设了 API token 时
用 ``Authorization: Bearer`` 访问. 图片地址可能是外链, 也可能是 Amane 本地的 ``/api/resources/{hash}``;
都由 kumo 带 token 经 Amane 取回 (外链走 Amane 的 ``/api/resources/proxy``), 见 ``fetch_image``.
"""

from __future__ import annotations

from typing import Any

import httpx

from .models import AmaneActor, Work

#: Amane 演员资料里展示到女优页的字段.
_PROFILE_FIELDS = (
    ("birthday", "生日"), ("height", "身高"), ("cup", "罩杯"), ("bust", "胸围"), ("waist", "腰围"), ("hip", "臀围"),
    ("birthplace", "出生地"),
)


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
        data = await self._get("/api/metadata", {"search": number, "limit": 20})
        for item in data.get("items") or []:
            if str(item.get("number", "")).casefold() != number.casefold():
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
