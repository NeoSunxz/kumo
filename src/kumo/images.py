"""封面与头像的代理与磁盘缓存.

JavBus 的图片校验 Referer, 浏览器从 kumo 的页面直接引用会得到 403; 因此图片一律由 kumo 带上
对应站点的 Referer 取回, 缓存到数据目录, 之后直接从磁盘返回. 只代理资料站的图片域名,
不是通用代理.

Amane 的图片 (``get_amane``) 由 Amane 自己取回或从本地返回, kumo 带上 API token 去 Amane 取, 同样缓存.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

from .magnets import UA

if TYPE_CHECKING:
    from .amane import Amane

# 允许代理的图片域名 (含子域) 与取图时使用的 Referer.
_HOSTS = {
    "javbus.com": "https://www.javbus.com/",
    "jdbstatic.com": "https://javdb.com/",
    "javdb.com": "https://javdb.com/",
}
_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
_MAX_BYTES = 5 * 1024 * 1024


class ImageError(Exception):
    pass


def referer_for(url: str) -> str | None:
    """允许代理时返回该图片应带的 Referer; 不在白名单内返回 ``None``."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https"):
        return None
    for suffix, referer in _HOSTS.items():
        if host == suffix or host.endswith("." + suffix):
            return referer
    return None


class ImageCache:
    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)
        # 一页封面墙会同时请求几十张图, 限制对源站的并发.
        self._gate = asyncio.Semaphore(6)

    def _cached(self, key: str) -> Path | None:
        for ext in _TYPES.values():
            path = self._dir / f"{key}{ext}"
            if path.exists():
                return path
        return None

    async def get(self, url: str, *, proxy: str = "") -> Path:
        referer = referer_for(url)
        if referer is None:
            raise ImageError("不代理这个地址")

        async def fetch() -> httpx.Response:
            async with httpx.AsyncClient(proxy=proxy or None, timeout=20.0, follow_redirects=True) as client:
                return await client.get(url, headers={"User-Agent": UA, "Referer": referer})

        return await self._load(url, fetch)

    async def get_with_referer(self, url: str, referer: str, *, proxy: str = "") -> Path:
        """论坛帖子里的图片: 图床校验 Referer. 地址来自已入库的帖子, 不接受任意地址 (见 web 的 ``/img/res``)."""

        async def fetch() -> httpx.Response:
            async with httpx.AsyncClient(proxy=proxy or None, timeout=20.0, follow_redirects=True) as client:
                return await client.get(url, headers={"User-Agent": UA, "Referer": referer})

        return await self._load(url, fetch)

    async def get_amane(self, url: str, amane: Amane, *, width: int | None = None) -> Path:
        """Amane 作品 / 演员的图片; 缓存键带上 Amane 地址, 换了 Amane 不会串图.

        ``width``: 缩成这个宽度 (列表里的小图). Amane 的海报原图约 1000×1500、上百 KB 到 1MB, 一页上百张会很慢.
        """
        from .amane import AmaneError

        async def fetch() -> httpx.Response:
            try:
                return await amane.fetch_image(url)
            except AmaneError as exc:
                raise ImageError(str(exc)) from exc

        original = await self._load(f"amane:{amane.base_url}:{url}", fetch)
        return await self.resized(original, width) if width else original

    async def resized(self, original: Path, width: int) -> Path:
        """缩略图 (同目录, 文件名带宽度); 没装 Pillow 时 (镜像还没重新构建) 返回原图."""
        target = original.with_name(f"{original.stem}_w{width}.jpg")
        if target.exists():
            return target
        try:
            from PIL import Image
        except ImportError:
            return original

        def work() -> Path:
            with Image.open(original) as im:
                if im.width <= width:
                    return original
                im = im.convert("RGB")
                im.thumbnail((width, width * 4))
                tmp = target.with_suffix(".part")
                im.save(tmp, "JPEG", quality=82, optimize=True)
                tmp.replace(target)
            return target

        try:
            return await asyncio.to_thread(work)
        except OSError:
            return original

    async def _load(self, cache_key: str, fetch: Callable[[], Awaitable[httpx.Response]]) -> Path:
        key = hashlib.sha1(cache_key.encode()).hexdigest()
        cached = self._cached(key)
        if cached:
            return cached
        async with self._gate:
            cached = self._cached(key)  # 等待期间可能已被别的请求取回
            if cached:
                return cached
            try:
                resp = await fetch()
            except httpx.HTTPError as exc:
                raise ImageError(f"取图失败: {type(exc).__name__}") from exc
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if resp.status_code != 200 or ctype not in _TYPES or len(resp.content) > _MAX_BYTES:
            raise ImageError(f"源站返回 HTTP {resp.status_code} {ctype}")
        path = self._dir / f"{key}{_TYPES[ctype]}"
        tmp = path.with_suffix(".part")
        tmp.write_bytes(resp.content)
        tmp.replace(path)
        return path
