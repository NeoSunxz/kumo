"""调度与业务流程.

kumo 有两种订阅, 在「挑选作品」汇合, 之后走同一条路:

- 片源订阅: 资料站的女优 / 系列 / 片商 / 标签 → 作品
- 磁力订阅: 98堂 (sehuatang) 板块 / sukebei / RSS 按入池条件爬取磁力 → 识别番号与标签 → 作品
- 挑选作品 (两边按番号合并) → 加入下载队列 → 磁力选择 (磁力池 + 按番号搜索, 按规则打分) → 115 云下载

磁力订阅开了「自动」的跳过挑选, 直接进下载队列. 作品信息 (海报、封面、演员) 从 Amane 按番号缓存,
各页面都用它填充.

一个番号的生命周期: pending → (搜索) → queued / need_choice / no_magnet → (提交) → submitted
→ downloading → done / failed. 任何阶段只要 115 目录同步发现已有该番号, 未提交的条目转为 owned.

所有任务都串行跑在同一个循环里, 每类任务一把锁, 网页上的「立即执行」与定时调度不会重叠.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections.abc import Callable
from typing import Any
from dataclasses import dataclass, field
from datetime import date, timedelta

import httpx

from .amane import Amane, AmaneError
from .db import Repo
from .feeds import parse_feed, torrent_to_magnet
from .magnets import SITES, Fetcher, SiteError, Sukebei, parse_sukebei_items
from .models import (
    AmaneActor, Candidate, LibraryFile, MagnetSource, Release, Resource, SourceFilter, Subscription, Tag, Wanted,
    WantedStatus, Work, info_hash_of,
)
from .numbers import extract_number, has_subtitle_suffix
from .pan115 import FakePan115, NotAuthorized, OpenClient, Pan115, Pan115Error
from .sehuatang import Sehuatang
from .subscriptions import SubscriptionKey, fetch_list, parse_subscription_url
from .select import brackets, choose, day, evaluate, merge, title_tags
from .settings import Settings

log = logging.getLogger("kumo")

VIDEO_EXTS = frozenset({".mp4", ".mkv", ".avi", ".wmv", ".mov", ".ts", ".m2ts", ".iso", ".rmvb", ".flv", ".webm", ".m4v", ".mpg", ".mpeg"})
_TICK = 30.0
_SEARCH_PER_TICK = 5
_AMANE_PER_RUN = 200


class Service:
    def __init__(self, repo: Repo, *, client_factory: Callable[[Settings, Repo], Pan115 | None] | None = None) -> None:
        self.repo = repo
        self._client_factory = client_factory or _default_client
        self._client: Pan115 | None = None
        self._client_app_id: str | None = None
        self._folder_ids: dict[str, str] = {}
        self._locks = {name: asyncio.Lock() for name in ("search", "submit", "tasks", "sync", "feeds", "crawl", "subs", "sukebei", "amane")}
        self._last_run: dict[str, float] = {}
        self._sync_requested = False
        self._task: asyncio.Task[None] | None = None
        self.last_error: dict[str, str] = {}
        self.running: dict[str, float] = {}
        """正在运行的任务与开始时间, 页面据此显示进度."""
        self._migrate_sources()

    def _migrate_sources(self) -> None:
        """磁力订阅原先写在设置里 (论坛板块、sukebei 规则、输入 RSS), 第一次启动时搬到磁力订阅表."""
        if self.repo.get_kv("sources_migrated"):
            return
        s = self.settings
        # 板块列表有默认值, 只有当时开了定时爬取才算用户真的在用.
        for f in s.crawl_forums if s.crawl_enabled else []:
            self.repo.add_source(
                MagnetSource(
                    kind="sehuatang", name=f"板块 {f.fid}" + (f":{f.typeid}" if f.typeid else ""),
                    params={"fid": f.fid, "typeid": f.typeid}, auto_download=f.auto_download,
                    # 原来的分类 / 关键词只管自动下载; 现在是入池条件, 不开自动的不带过来, 以免少爬.
                    filter=SourceFilter(types=f.types, include=f.include_keywords, exclude=f.exclude_keywords)
                    if f.auto_download else SourceFilter(),
                )
            )
        for r in s.sukebei_rules:
            self.repo.add_source(
                MagnetSource(
                    kind="sukebei", name=r.name, auto_download=r.auto_download,
                    params={"query": r.query, "user": r.user, "category": r.category, "trusted_only": r.trusted_only,
                            "min_seeders": r.min_seeders},
                    filter=SourceFilter(include=r.include_keywords, exclude=r.exclude_keywords),
                )
            )
        for feed in s.input_feeds:
            self.repo.add_source(
                MagnetSource(
                    kind="rss", name=feed.url.split("//")[-1][:40], params={"url": feed.url}, auto_download=True,
                    filter=SourceFilter(include=feed.include_keywords, exclude=feed.exclude_keywords),
                )
            )
        self.repo.set_kv("sources_migrated", "1")

    # --- 115 客户端 ---

    @property
    def settings(self) -> Settings:
        return self.repo.settings()

    def client(self) -> Pan115 | None:
        s = self.settings
        if self._client is None or self._client_app_id != s.app_id:
            self._client = self._client_factory(s, self.repo)
            self._client_app_id = s.app_id
            self._folder_ids.clear()
        return self._client

    def open_client(self) -> OpenClient | None:
        c = self.client()
        return c if isinstance(c, OpenClient) else None

    async def _folder_id(self, client: Pan115, path: str) -> str:
        if path not in self._folder_ids:
            self._folder_ids[path] = await client.folder_id(path)
        return self._folder_ids[path]

    def amane(self) -> Amane | None:
        s = self.settings
        return Amane(s.amane_url, s.amane_token) if s.amane_url else None

    # --- 番号 ---

    def add_numbers(self, numbers: list[str], source: str = "manual") -> tuple[int, int]:
        added = skipped = 0
        for number in numbers:
            if self.repo.add_wanted(number, source) is None:
                skipped += 1
                continue
            added += 1
            if self.repo.owned(number):
                w = self.repo.wanted_by_number(number)
                assert w is not None
                self.repo.update_wanted(w.id, status=WantedStatus.OWNED, note="115 目录中已有")
        return added, skipped

    async def search(self, wanted: Wanted) -> WantedStatus:
        s = self.settings
        if self.repo.owned(wanted.number):
            self.repo.update_wanted(wanted.id, status=WantedStatus.OWNED, note="115 目录中已有", searched_at=time.time())
            return WantedStatus.OWNED
        fetcher = self._fetcher()
        found: list[Candidate] = []
        errors: list[str] = []
        try:
            for site in s.sites:
                if site not in SITES:
                    continue
                try:
                    found += await SITES[site].search(fetcher, wanted.number)
                except SiteError as exc:
                    errors.append(f"{site}: {exc}")
        finally:
            await fetcher.aclose()
        if "sehuatang" in s.sites:
            # 磁力池里的磁力带上订阅的优先顺序与名称; 不属于任何订阅的排在订阅之后、搜索结果之前.
            sources = self.repo.sources()
            ranks = {src.id: i for i, src in enumerate(sources)}
            names = {src.id: src.name for src in sources}
            for r in self.repo.resources_by_number(wanted.number):
                cand = _resource_candidate(r)
                cand.rank = ranks.get(r.source_id, len(sources))
                cand.origin = names.get(r.source_id, "")
                found.append(cand)
        candidates = merge(found)
        chosen = choose(candidates, wanted.number, s)
        self.repo.replace_candidates(wanted.id, candidates)
        now = time.time()
        if chosen is not None:
            status, note = WantedStatus.QUEUED, ""
        elif any(not c.rejected or "番号" not in c.rejected for c in candidates):
            status, note = WantedStatus.NEED_CHOICE, "没有符合条件的磁力, 请手动选择"
        else:
            status, note = WantedStatus.NO_MAGNET, "；".join(errors) or "未搜到磁力"
        self.repo.update_wanted(
            wanted.id, status=status, note=note, chosen_id=chosen.id if chosen else None, searched_at=now
        )
        return status

    def choose_candidate(self, wanted_id: int, candidate_id: int) -> None:
        cand = self.repo.candidate(candidate_id)
        if cand is None or cand.wanted_id != wanted_id:
            raise ValueError("候选不存在")
        self.repo.update_wanted(wanted_id, status=WantedStatus.QUEUED, chosen_id=candidate_id, note="手动选择")

    def add_magnet(self, wanted_id: int, magnet: str, title: str = "") -> None:
        """手动指定磁力 (网页粘贴或输入 RSS 自带)."""
        wanted = self.repo.wanted(wanted_id)
        info_hash = info_hash_of(magnet)
        if wanted is None or info_hash is None:
            raise ValueError("磁力链接无效")
        cand = Candidate(site="manual", title=title or wanted.number, magnet=magnet, info_hash=info_hash)
        evaluate(cand, wanted.number, self.settings)
        existing = [c for c in self.repo.candidates(wanted_id) if c.info_hash != info_hash]
        stored = self.repo.replace_candidates(wanted_id, [cand, *existing])
        self.repo.update_wanted(wanted_id, status=WantedStatus.QUEUED, chosen_id=stored[0].id, note="手动指定磁力")

    def download_resource(self, resource_id: int) -> int:
        """把爬到的资源转成待下载的番号; 返回番号记录的 id.

        番号已在列表里时: 还没选定磁力的直接用这条资源, 已在下载流程中的只做关联, 不改动.
        """
        r = self.repo.resource(resource_id)
        if r is None:
            raise ValueError("资源不存在")
        if not r.magnet or not r.number:
            raise ValueError("该帖子没有磁力链接或识别不出番号")
        self.add_numbers([r.number], source=f"{r.source}:{r.forum_id}")
        w = self.repo.wanted_by_number(r.number)
        assert w is not None
        if w.status in (WantedStatus.PENDING, WantedStatus.NO_MAGNET, WantedStatus.NEED_CHOICE):
            self.add_magnet(w.id, r.magnet, r.title)
        self.repo.link_resource(resource_id, w.id)
        return w.id

    # --- 订阅 ---

    def _fetcher(self) -> Fetcher:
        s = self.settings
        return Fetcher(proxy=s.scrape_proxy, flaresolverr_url=s.flaresolverr_url, javdb_cookie=s.javdb_cookie)

    async def add_subscription(self, url: str, *, auto_search: bool = False, name: str = "") -> Subscription:
        """按页面地址新增订阅: 先取第一页拿到名称 (同时验证地址可用), 再回填作品.

        ``name`` 为空时用页面上的名称; 标签页没有可用的名称, 用户不填时用标签参数代替.
        """
        key = parse_subscription_url(url)
        fetcher = self._fetcher()
        try:
            first = await fetch_list(fetcher, key)
        finally:
            await fetcher.aclose()
        sub = self.repo.add_subscription(
            Subscription(
                source=key.source, kind=key.kind, ident=key.ident, uncensored=key.uncensored,
                name=name.strip() or (first.name if key.kind != "tag" else "") or key.ident,
                auto_search=auto_search, avatar=first.avatar, profile=first.profile,
            )
        )
        self.repo.log("info", f"新增订阅: {key.label} {sub.name}")
        return sub

    async def check_subscription(self, sub: Subscription) -> int:
        """拉取订阅的作品列表, 返回新见到的作品数."""
        assert sub.id is not None
        s = self.settings
        key = SubscriptionKey(kind=sub.kind, ident=sub.ident, uncensored=sub.uncensored, source=sub.source)
        known = self.repo.known_release_numbers(sub.id)
        pages = s.subs_pages if known else s.subs_initial_pages
        fresh: list[Release] = []
        fetcher = self._fetcher()
        try:
            for page in range(1, pages + 1):
                if page > 1:
                    await asyncio.sleep(s.crawl_request_delay)
                listing = await fetch_list(fetcher, key, page)
                if page == 1 and (listing.avatar or listing.profile):
                    # 头像与资料会更新 (换头像、补资料), 每次检查顺带刷新.
                    self.repo.update_subscription(sub.id, avatar=listing.avatar or sub.avatar, profile=listing.profile or sub.profile)
                seen_any = False
                for item in listing.items:
                    rel, new = self.repo.add_release(
                        Release(source=sub.source, number=item.number, title=item.title, date=item.date,
                                cover=item.cover, url=item.url),
                        sub.id,
                    )
                    seen_any |= item.number in known
                    if new and item.number not in known:
                        fresh.append(rel)
                if seen_any or not listing.more:
                    break
        except SiteError as exc:
            self.repo.update_subscription(sub.id, last_error=str(exc), checked_at=time.time())
            raise
        finally:
            await fetcher.aclose()
        self.repo.update_subscription(sub.id, last_error="", checked_at=time.time())
        if sub.auto_search and fresh:
            self.add_numbers([r.number for r in fresh], source=f"订阅:{sub.name}")
        if fresh:
            self.repo.log("info", f"订阅 {sub.name}: 新作 {len(fresh)} 部")
        return len(fresh)

    async def run_subscriptions(self, *, force: bool = False) -> None:
        async with self._locks["subs"]:
            s = self.settings
            due_before = time.time() - s.subs_interval_hours * 3600
            for sub in self.repo.subscriptions(enabled_only=True):
                if not force and sub.checked_at and sub.checked_at > due_before:
                    continue
                try:
                    await self.check_subscription(sub)
                except SiteError as exc:
                    self.repo.log("warning", f"订阅 {sub.name} 检查失败: {exc}")
                await asyncio.sleep(s.crawl_request_delay)

    def actress_groups(self) -> list[ActressGroup]:
        """订阅的女优, 同名的 (同一人在 JavBus 与 JavDB 上的订阅) 合成一组; 有新作的排在前面."""
        groups: dict[str, list[Subscription]] = {}
        for sub in self.repo.subscriptions(kind="star"):
            groups.setdefault(_name_key(sub.name), []).append(sub)
        out = [self._actress_group(subs) for subs in groups.values()]
        out.sort(key=lambda g: (g.latest.date or "") if g.latest else "", reverse=True)
        return out

    def actress_group(self, sub_id: int) -> ActressGroup | None:
        sub = self.repo.subscription(sub_id)
        if sub is None or sub.kind != "star":
            return None
        key = _name_key(sub.name)
        return self._actress_group([s for s in self.repo.subscriptions(kind="star") if _name_key(s.name) == key])

    def _actress_group(self, subs: list[Subscription]) -> ActressGroup:
        # 名称、头像、资料都以最早添加的订阅为准, 后加的只补缺.
        subs = sorted(subs, key=lambda s: s.id or 0)
        rows = self.repo.releases(sub_ids=[s.id for s in subs if s.id], limit=100_000)
        since = (date.today() - timedelta(days=30)).isoformat()
        profile: dict[str, str] = {}
        for s in subs:
            profile.update({k: v for k, v in s.profile.items() if k not in profile})
        return ActressGroup(
            name=subs[0].name,
            subs=subs,
            avatar=next((s.avatar for s in subs if s.avatar), None),
            amane=self.repo.amane_actor(subs[0].name),
            profile=profile,
            total=len(rows),
            latest=rows[0][0] if rows else None,
            recent=sum(1 for r, *_ in rows if r.date and since <= r.date <= date.today().isoformat()),
            upcoming=sum(1 for r, *_ in rows if r.date and r.date > date.today().isoformat()),
            queued=sum(1 for _, _, wanted_id, _ in rows if wanted_id),
        )

    def queue_releases(self, release_ids: list[int]) -> int:
        """把选中的新作加入下载队列 (去搜磁力); 返回实际新加入的数量."""
        numbers = [r.number for r in (self.repo.release(i) for i in release_ids) if r is not None]
        added, _ = self.add_numbers(numbers, source="新作")
        return added

    # --- 挑选作品: 片源与磁力两边按番号合并 ---

    def works(
        self, *, origin: str = "", sub_ids: list[int] | None = None, source_ids: list[int] | None = None,
        tags: list[str] | None = None, search: str = "", days: int | None = None, todo: bool = True, limit: int = 300,
    ) -> list[WorkItem]:
        """``origin``: ``subs`` 只看片源订阅, ``magnet`` 只看磁力订阅, 空为两边都看. ``todo`` 只看待挑选的."""
        items: dict[str, WorkItem] = {}
        if origin in ("", "subs") and not source_ids:
            for rel, subs_name, _, _ in self.repo.releases(sub_ids=sub_ids, search=search, days=days, limit=3000):
                key = rel.number.upper()
                items[key] = WorkItem(number=rel.number, release=rel, subs_name=subs_name)
        if origin in ("", "magnet") and not sub_ids:
            names = {src.id: src.name for src in self.repo.sources()}
            only_pool = origin == "magnet" or bool(tags)
            for r in self.repo.pool_resources(source_ids=source_ids, search=search, days=None if not only_pool else days):
                assert r.number
                key = r.number.upper()
                item = items.get(key)
                if item is None:
                    if not only_pool and days and not _recent(r, days):
                        continue
                    item = items[key] = WorkItem(number=r.number)
                item.magnets.append(r)
                if r.source_id in names and names[r.source_id] not in item.sources:
                    item.sources.append(names[r.source_id])
        if tags:
            items = {k: v for k, v in items.items() if set(tags) <= v.tags}
        wanted = self.repo.wanted_by_numbers(items)
        owned = self.repo.owned_numbers(items)
        dismissed = self.repo.dismissed_numbers(items)
        for key, item in items.items():
            item.wanted = wanted.get(key)
            item.owned = key in owned
            item.dismissed = key in dismissed
        out = [i for i in items.values() if not todo or not (i.wanted or i.owned or i.dismissed)]
        out.sort(key=lambda i: i.date or "", reverse=True)
        out = out[:limit]
        works = self.repo.works(i.number for i in out)
        for item in out:
            item.work = works.get(item.number.upper())
        return out

    def queue_numbers(self, numbers: list[str]) -> int:
        """挑选的作品加入下载队列, 之后自动磁力选择; 返回新加入的数量."""
        added, _ = self.add_numbers(numbers, source="挑选")
        return added

    # --- 定时任务 ---
    # --- 定时任务 ---

    async def run_search(self) -> None:
        async with self._locks["search"]:
            s = self.settings
            for wanted in self.repo.due_for_search(s.no_magnet_retry_hours * 3600, _SEARCH_PER_TICK):
                status = await self.search(wanted)
                self.repo.log("info", f"{wanted.number} 搜索完成: {status.value}")

    async def run_submit(self) -> None:
        async with self._locks["submit"]:
            s = self.settings
            queued = list(reversed(self.repo.list_wanted([WantedStatus.QUEUED])))[: s.submit_batch]
            if not queued:
                return
            client = self.client()
            if client is None or not client.authorized():
                self.last_error["submit"] = "115 未授权, 待提交的任务会在授权后自动提交"
                return
            if not s.download_path:
                self.last_error["submit"] = "未设置云下载保存目录"
                return
            folder = await self._folder_id(client, s.download_path)
            pairs = [(w, self.repo.candidate(w.chosen_id)) for w in queued if w.chosen_id]
            pairs = [(w, c) for w, c in pairs if c is not None]
            results = await client.add_urls([c.magnet for _, c in pairs], folder)
            by_hash = {r.info_hash: r for r in results if r.info_hash}
            now = time.time()
            for i, (w, cand) in enumerate(pairs):
                r = by_hash.get(cand.info_hash) or (results[i] if len(results) == len(pairs) else None)
                if r is not None and (r.ok or _already_exists(r.message)):
                    self.repo.update_wanted(
                        w.id, status=WantedStatus.SUBMITTED, info_hash=cand.info_hash, submitted_at=now,
                        note="" if r.ok else "115 中已有同一任务",
                    )
                    self.repo.log("info", f"{w.number} 已提交云下载")
                else:
                    message = r.message if r else "115 没有返回该链接的结果"
                    self.repo.update_wanted(w.id, status=WantedStatus.FAILED, note=f"提交失败: {message}")
                    self.repo.log("warning", f"{w.number} 提交失败: {message}")
            self.last_error.pop("submit", None)

    async def run_tasks(self) -> None:
        async with self._locks["tasks"]:
            active = {w.info_hash: w for w in self.repo.list_wanted([WantedStatus.SUBMITTED, WantedStatus.DOWNLOADING]) if w.info_hash}
            client = self.client()
            if not active or client is None or not client.authorized():
                return
            page, pages = 1, 1
            while active and page <= min(pages, 20):
                tasks, pages = await client.tasks(page)
                for t in tasks:
                    w = active.pop(t.info_hash, None)
                    if w is None:
                        continue
                    if t.status == 2:
                        self.repo.update_wanted(w.id, status=WantedStatus.DONE, percent=100, done_at=time.time(), note="")
                        self.repo.log("info", f"{w.number} 云下载完成")
                        self._sync_requested = True
                    elif t.status == -1:
                        self.repo.update_wanted(w.id, status=WantedStatus.FAILED, note="115 云下载失败")
                        self.repo.log("warning", f"{w.number} 云下载失败")
                    elif t.status == 1:
                        self.repo.update_wanted(w.id, status=WantedStatus.DOWNLOADING, percent=t.percent)
                page += 1

    async def run_sync(self) -> None:
        async with self._locks["sync"]:
            s = self.settings
            client = self.client()
            if client is None or not client.authorized():
                return
            for path in s.sync_paths:
                cid = await self._folder_id(client, path)
                files: list[LibraryFile] = []
                async for f in client.walk(cid):
                    ext = f.name[f.name.rfind(".") :].lower() if "." in f.name else ""
                    number = extract_number(f.name) if ext in VIDEO_EXTS else None
                    files.append(
                        LibraryFile(
                            fid=f.fid, name=f.name, size=f.size, pick_code=f.pick_code, number=number,
                            subtitle=bool(number and has_subtitle_suffix(f.name, number)),
                        )
                    )
                self.repo.replace_library_root(path, files)
                self.repo.log("info", f"同步 {path}: {len(files)} 个文件")
            self.repo.drop_library_roots_except(s.sync_paths)
            for w in self.repo.list_wanted([WantedStatus.PENDING, WantedStatus.NO_MAGNET, WantedStatus.NEED_CHOICE, WantedStatus.QUEUED]):
                if self.repo.owned(w.number):
                    self.repo.update_wanted(w.id, status=WantedStatus.OWNED, note="115 目录中已有")
            self._sync_requested = False

    def _pool(self, src: MagnetSource, res: Resource) -> None:
        """新磁力入池; 订阅开了自动的, 有番号且符合磁力选择规则就直接进下载队列."""
        stored = self.repo.add_resource(res)
        if not (src.auto_download and stored.id and stored.number):
            return
        if not stored.magnet:
            self.add_numbers([stored.number], source=f"自动:{src.name}")
            return
        cand = _resource_candidate(stored)
        evaluate(cand, stored.number, self.settings)
        if not cand.rejected:
            self.download_resource(stored.id)

    def _sources(self, kind: str, source_id: int | None) -> list[MagnetSource]:
        """要爬的订阅: 指定了编号就只爬那一个 (停用的也爬), 否则爬全部启用的."""
        if source_id is not None:
            src = self.repo.source(source_id)
            return [src] if src and src.kind == kind else []
        return self.repo.sources(kind=kind, enabled_only=True)

    async def _run_sources(self, kind: str, source_id: int | None, crawl: Callable[[MagnetSource, Fetcher], Any]) -> None:
        """逐个订阅爬取; 一个失败不影响其它, 错误记在订阅上, 最后一个错误也报给任务."""
        s = self.settings
        fetcher = Fetcher(proxy=s.scrape_proxy, flaresolverr_url=s.flaresolverr_url)
        error: SiteError | None = None
        try:
            for i, src in enumerate(self._sources(kind, source_id)):
                assert src.id is not None
                if i:
                    await asyncio.sleep(s.crawl_request_delay)
                try:
                    added = await crawl(src, fetcher)
                except SiteError as exc:
                    error = exc
                    self.repo.update_source(src.id, last_error=str(exc), last_run=time.time())
                    self.repo.log("warning", f"磁力订阅 {src.name}: {exc}")
                    continue
                self.repo.update_source(src.id, last_error="", last_run=time.time())
                if added:
                    self.repo.log("info", f"磁力订阅 {src.name}: 新入池 {added} 个")
        finally:
            await fetcher.aclose()
        if error is not None:
            raise error

    async def run_crawl(self, *, source_id: int | None = None) -> None:
        async with self._locks["crawl"]:
            site = Sehuatang(self.settings.crawl_base_url)
            await self._run_sources("sehuatang", source_id, lambda src, f: self._crawl_forum(site, src, f))

    async def _crawl_forum(self, site: Sehuatang, src: MagnetSource, fetcher: Fetcher) -> int:
        """按发帖时间翻页; 列表标题不符合入池条件的不打开帖子, 遇到已见过的帖子即停止翻页."""
        assert src.id is not None
        s = self.settings
        fid, typeid = int(src.params["fid"]), src.params.get("typeid")  # type: ignore[arg-type]
        added = 0
        for page in range(1, s.crawl_pages + 1):
            if page > 1:
                await asyncio.sleep(s.crawl_request_delay)
            forum_name, refs = await site.forum_page(fetcher, fid, page, int(typeid) if typeid else None)  # type: ignore[arg-type]
            known = 0
            for ref in refs:
                key = str(ref.tid)
                if self.repo.resource_exists("sehuatang", ref.tid) or self.repo.is_seen(src.id, key):
                    known += 1
                    continue
                number = extract_number(ref.title)
                if not src.filter.accepts(ref.title, _forum_tags(forum_name, ref.title, number), brackets(ref.title), ref.type_name, number):
                    self.repo.mark_seen(src.id, key)
                    continue
                await asyncio.sleep(s.crawl_request_delay)
                try:
                    detail = await site.thread(fetcher, ref.tid)
                except SiteError as exc:
                    # 不入库, 下一轮再试.
                    self.repo.log("warning", f"sehuatang 帖子 {ref.tid} 读取失败: {exc}")
                    continue
                title = detail.title or ref.title
                number = extract_number(title)
                tags = _forum_tags(forum_name, title, number)
                self._pool(src, Resource(
                    source="sehuatang", tid=ref.tid, forum_id=fid, forum_name=forum_name, type_id=ref.type_id,
                    type_name=ref.type_name, title=title, number=number, magnet=detail.magnet,
                    info_hash=info_hash_of(detail.magnet) if detail.magnet else None, size=detail.size,
                    posted=detail.posted or ref.posted, subtitle=Tag.SUBTITLE in tags, tags=tags, brackets=brackets(title),
                    source_id=src.id, images=detail.images,
                ))
                added += 1
            if known or not refs:
                break
        return added

    async def run_sukebei(self, *, source_id: int | None = None) -> None:
        async with self._locks["sukebei"]:
            await self._run_sources("sukebei", source_id, self._crawl_sukebei)

    async def _crawl_sukebei(self, src: MagnetSource, fetcher: Fetcher) -> int:
        assert src.id is not None
        p = src.params
        url = Sukebei.rss_url(
            query=str(p.get("query") or ""), user=str(p.get("user") or ""), category=str(p.get("category") or "2_2"),
            trusted_only=bool(p.get("trusted_only")),
        )
        status, text = await fetcher.get_text(url)
        if status != 200:
            raise SiteError(f"sukebei 返回 HTTP {status}")
        added = 0
        for item in parse_sukebei_items(text):
            cand = item.candidate
            if self.repo.resource_exists("sukebei", item.id) or self.repo.is_seen(src.id, str(item.id)):
                continue
            number = extract_number(cand.title)
            tags = title_tags(cand.title, number)
            min_seeders = int(p.get("min_seeders") or 0)  # type: ignore[call-overload]
            if (
                (p.get("trusted_only") and not item.trusted)
                or (cand.seeders or 0) < min_seeders
                or not src.filter.accepts(cand.title, tags, brackets(cand.title), number=number)
            ):
                self.repo.mark_seen(src.id, str(item.id))
                continue
            self._pool(src, Resource(
                source="sukebei", tid=item.id, forum_id=0, forum_name="sukebei", type_name=src.name, title=cand.title,
                number=number, magnet=cand.magnet, info_hash=cand.info_hash, size=cand.size, posted=cand.published,
                seeders=cand.seeders, subtitle=Tag.SUBTITLE in tags, tags=tags, brackets=brackets(cand.title), source_id=src.id,
            ))
            added += 1
        return added

    async def run_feeds(self, *, source_id: int | None = None) -> None:
        async with self._locks["feeds"]:
            await self._run_sources("rss", source_id, self._crawl_feed)

    async def _crawl_feed(self, src: MagnetSource, fetcher: Fetcher) -> int:
        """RSS 条目: 带磁力 (或种子) 的入池; 只有番号的作为待挑选的作品入池 (没有磁力, 加入队列后再搜)."""
        assert src.id is not None
        url = str(src.params.get("url") or "")
        added = 0
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http:
            try:
                resp = await http.get(url)
                entries = parse_feed(resp.text)
            except (httpx.HTTPError, ValueError) as exc:
                raise SiteError(f"读取 RSS 失败: {exc}") from exc
            for entry in entries:
                if self.repo.is_seen(src.id, entry.key):
                    continue
                self.repo.mark_seen(src.id, entry.key)
                tags = title_tags(entry.title, entry.number)
                if not src.filter.accepts(entry.title, tags, brackets(entry.title), number=entry.number):
                    continue
                if entry.number is None:
                    self.repo.log("warning", f"RSS 条目无法识别番号, 已跳过: {entry.title[:80]}")
                    continue
                magnet = entry.magnet
                if magnet is None and entry.torrent_url:
                    try:
                        torrent = await http.get(entry.torrent_url)
                        magnet = torrent_to_magnet(torrent.content, entry.title)
                    except (httpx.HTTPError, ValueError) as exc:
                        self.repo.log("warning", f"种子下载或解析失败 {entry.title[:60]}: {exc}")
                tid = int(hashlib.sha1(f"{url}\n{entry.key}".encode()).hexdigest()[:15], 16)
                self._pool(src, Resource(
                    source="rss", tid=tid, forum_id=0, forum_name="rss", type_name=src.name, title=entry.title,
                    number=entry.number, magnet=magnet, info_hash=info_hash_of(magnet) if magnet else None,
                    subtitle=Tag.SUBTITLE in tags, tags=tags, brackets=brackets(entry.title), source_id=src.id,
                ))
                added += 1
        return added

    async def preview_source(self, src: MagnetSource) -> list[PreviewRow]:
        """试爬第一页: 列出每条识别出的番号、标签、片头, 以及是否入池与原因; 不入库, 也不打开论坛帖子."""
        s = self.settings
        rows: list[PreviewRow] = []
        fetcher = Fetcher(proxy=s.scrape_proxy, flaresolverr_url=s.flaresolverr_url)
        try:
            if src.kind == "sehuatang":
                typeid = src.params.get("typeid")
                forum_name, refs = await Sehuatang(s.crawl_base_url).forum_page(
                    fetcher, int(src.params["fid"]), 1, int(typeid) if typeid else None  # type: ignore[arg-type]
                )
                for ref in refs:
                    rows.append(self._preview_row(
                        src, ref.title, ref.type_name, known=self.repo.resource_exists("sehuatang", ref.tid), forum_name=forum_name,
                    ))
            elif src.kind == "sukebei":
                p = src.params
                url = Sukebei.rss_url(
                    query=str(p.get("query") or ""), user=str(p.get("user") or ""),
                    category=str(p.get("category") or "2_2"), trusted_only=bool(p.get("trusted_only")),
                )
                status, text = await fetcher.get_text(url)
                if status != 200:
                    raise SiteError(f"sukebei 返回 HTTP {status}")
                for item in parse_sukebei_items(text):
                    cand = item.candidate
                    row = self._preview_row(src, cand.title, known=self.repo.resource_exists("sukebei", item.id))
                    row.size, row.seeders = cand.size, cand.seeders
                    if not row.reason and p.get("trusted_only") and not item.trusted:
                        row.reason = "上传者不可信"
                    if not row.reason and (cand.seeders or 0) < int(p.get("min_seeders") or 0):  # type: ignore[call-overload]
                        row.reason = f"做种不足 {p.get('min_seeders')}"
                    rows.append(row)
            else:
                async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http:
                    try:
                        entries = parse_feed((await http.get(str(src.params.get("url") or ""))).text)
                    except (httpx.HTTPError, ValueError) as exc:
                        raise SiteError(f"读取 RSS 失败: {exc}") from exc
                for entry in entries:
                    row = self._preview_row(src, entry.title, known=src.id is not None and self.repo.is_seen(src.id, entry.key))
                    if not row.reason and entry.number is None:
                        row.reason = "识别不出番号"
                    rows.append(row)
        finally:
            await fetcher.aclose()
        return rows

    def _preview_row(
        self, src: MagnetSource, title: str, type_name: str = "", *, known: bool = False, forum_name: str = ""
    ) -> PreviewRow:
        number = extract_number(title)
        tags, marks = _forum_tags(forum_name, title, number), brackets(title)
        reason = src.filter.reject_reason(title, tags, marks, type_name, number)
        return PreviewRow(title=title, number=number, tags=tags, brackets=marks, type_name=type_name, reason=reason, known=known)

    async def run_amane(self) -> None:
        """按番号把 Amane 里的作品信息 (海报、封面、演员) 缓存下来, 订阅的女优按名称缓存头像与资料."""
        async with self._locks["amane"]:
            s = self.settings
            amane = self.amane()
            if amane is None:
                return
            refresh_before = time.time() - s.amane_refresh_days * 86400
            found = 0
            try:
                for number in self.repo.numbers_for_amane(refresh_before, _AMANE_PER_RUN):
                    work = await amane.work(number)
                    self.repo.save_work(work)
                    found += work.found
                names = {sub.name for sub in self.repo.subscriptions(kind="star")}
                for name in sorted(names):
                    cached = self.repo.amane_actor(name)
                    if cached and cached.checked_at >= refresh_before:
                        continue
                    self.repo.save_amane_actor(await amane.actor(name))
            except AmaneError as exc:
                raise SiteError(f"Amane: {exc}") from exc
            finally:
                await amane.aclose()
            if found:
                self.repo.log("info", f"Amane: 取到 {found} 部作品的信息")

    # --- 调度循环 ---

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="kumo-scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def _due(self, name: str, interval: float) -> bool:
        return time.time() - self._last_run.get(name, 0) >= interval

    async def tick(self) -> None:
        s = self.settings
        jobs: list[tuple[str, float, Callable[[], object]]] = [
            ("feeds", s.feed_interval_minutes * 60, self.run_feeds),
            ("crawl", s.crawl_interval_minutes * 60, self.run_crawl),
            # 订阅各自记录检查时间, 这里每 10 分钟看一次是否有到期的.
            ("subs", 600, self.run_subscriptions),
            ("sukebei", s.sukebei_interval_minutes * 60, self.run_sukebei),
            ("amane", s.amane_interval_minutes * 60, self.run_amane),
            ("search", 0, self.run_search),
            ("submit", 0, self.run_submit),
            ("tasks", s.task_poll_minutes * 60, self.run_tasks),
            ("sync", 0 if self._sync_requested else s.sync_interval_hours * 3600, self.run_sync),
        ]
        for name, interval, job in jobs:
            if not self._due(name, interval):
                continue
            self._last_run[name] = time.time()
            await self.run_guarded(name, job)

    async def run_guarded(self, name: str, job: Callable[[], object]) -> None:
        self.running[name] = time.time()
        try:
            await job()  # type: ignore[misc]
            if name != "submit":
                self.last_error.pop(name, None)
        except NotAuthorized as exc:
            self.last_error[name] = str(exc)
        except (Pan115Error, SiteError, httpx.HTTPError) as exc:
            self.last_error[name] = str(exc)
            self.repo.log("error", f"{name}: {exc}")
        except Exception as exc:  # 调度循环不能因单个任务异常而停止
            log.exception("job %s failed", name)
            self.last_error[name] = f"{type(exc).__name__}: {exc}"
            self.repo.log("error", f"{name}: {type(exc).__name__}: {exc}")
        finally:
            self.running.pop(name, None)

    async def _loop(self) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(_TICK)


@dataclass
class ActressGroup:
    """「女优」页面的一张卡片: 同名女优在各站的订阅合成一组."""

    name: str
    subs: list[Subscription]
    avatar: str | None
    profile: dict[str, str] = field(default_factory=dict)
    total: int = 0
    latest: Release | None = None
    recent: int = 0
    """最近 30 天发行的作品数."""
    upcoming: int = 0
    """已公布未发行的作品数 (JavDB 会列出)."""
    queued: int = 0
    """已加入下载队列的作品数."""
    amane: AmaneActor | None = None
    """Amane 里的同名演员; 资料站没有头像或资料时用它补."""

    @property
    def amane_avatar(self) -> str | None:
        """资料站没有头像时用 Amane 的."""
        if self.avatar or self.amane is None or not self.amane.found:
            return None
        return self.amane.image

    @property
    def full_profile(self) -> dict[str, str]:
        out = dict(self.profile)
        if self.amane and self.amane.found:
            out.update({k: v for k, v in self.amane.profile.items() if k not in out})
        return out

    @property
    def sub_ids(self) -> list[int]:
        return [s.id for s in self.subs if s.id]


@dataclass
class PreviewRow:
    """磁力订阅预览的一条."""

    title: str
    number: str | None
    tags: set[str]
    brackets: list[str]
    type_name: str = ""
    reason: str = ""
    """不入池的原因; 空串表示会入池."""
    known: bool = False
    """已经在磁力池里."""
    size: int | None = None
    seeders: int | None = None


@dataclass
class WorkItem:
    """「挑选作品」的一行: 一个番号, 合并了片源订阅的作品与磁力池里它的磁力."""

    number: str
    release: Release | None = None
    """片源订阅里的作品."""
    subs_name: str = ""
    magnets: list[Resource] = field(default_factory=list)
    """磁力池里这个番号的磁力."""
    sources: list[str] = field(default_factory=list)
    """入池的磁力订阅名."""
    work: Work | None = None
    """Amane 里的作品 (海报、封面、演员)."""
    wanted: Wanted | None = None
    owned: bool = False
    dismissed: bool = False

    @property
    def title(self) -> str:
        if self.work and self.work.title:
            return self.work.title
        if self.release and self.release.title:
            return self.release.title
        return self.magnets[0].title if self.magnets else ""

    @property
    def date(self) -> str | None:
        if self.release and self.release.date:
            return self.release.date
        if self.work and self.work.date:
            return self.work.date
        posted = next((m.posted for m in self.magnets if m.posted), None)
        return day(posted)

    @property
    def tags(self) -> set[str]:
        return set().union(*(m.tags for m in self.magnets)) if self.magnets else set()


def _forum_tags(forum_name: str, title: str, number: str | None) -> set[str]:
    """标题识别的标签; 字幕板块 (高清中文字幕) 的帖子都算中字, 标题里不一定写."""
    tags = title_tags(title, number)
    if "字幕" in forum_name:
        tags.add(Tag.SUBTITLE)
    return tags


def _recent(r: Resource, days: int) -> bool:
    return r.crawled_at >= time.time() - days * 86400


def _name_key(name: str) -> str:
    return re.sub(r"\s+", "", name).casefold()


def _resource_candidate(r: Resource) -> Candidate:
    assert r.magnet and r.info_hash
    return Candidate(
        site=r.source, title=r.title, magnet=r.magnet, info_hash=r.info_hash, size=r.size, published=r.posted,
        seeders=r.seeders, tags={Tag.SUBTITLE} if r.subtitle else set(),
    )


def _already_exists(message: str) -> bool:
    return any(k in message for k in ("已存在", "重复", "exist"))


def _default_client(s: Settings, repo: Repo) -> Pan115 | None:
    if s.app_id == "fake":
        # 联调用: 不连 115, 在内存里模拟.
        return FakePan115()
    return OpenClient(s.app_id, repo) if s.app_id else None
