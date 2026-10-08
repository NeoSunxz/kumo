"""调度与业务流程.

kumo 有两条找片的路径, 汇合到同一个下载队列:

- 片源: 订阅资料站 (女优 / 系列 / 片商 / 标签) 得到作品 → 加入队列后按番号搜磁力 → 选择磁力 → 115.
- 磁力: 论坛与 sukebei 按规则爬取磁力进入磁力池 → 识别番号 → 确认影片 (或按来源自动) → 115.

两边按番号共享数据: 磁力池同时是搜磁力的来源之一, 作品信息 (海报、封面、演员) 从 Amane 按番号缓存,
两边页面都用它填充.

一个番号的生命周期: pending → (搜索) → queued / need_choice / no_magnet → (提交) → submitted
→ downloading → done / failed. 任何阶段只要 115 目录同步发现已有该番号, 未提交的条目转为 owned.

所有任务都串行跑在同一个循环里, 每类任务一把锁, 网页上的「立即执行」与定时调度不会重叠.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

import httpx

from .amane import Amane, AmaneError
from .db import Repo
from .feeds import parse_feed, torrent_to_magnet
from .magnets import SITES, Fetcher, SiteError, Sukebei, parse_sukebei_items
from .models import (
    AmaneActor, Candidate, LibraryFile, Release, Resource, Subscription, Tag, Wanted, WantedStatus, Work, info_hash_of,
)
from .numbers import extract_number, has_subtitle_suffix
from .pan115 import FakePan115, NotAuthorized, OpenClient, Pan115, Pan115Error
from .sehuatang import Sehuatang
from .subscriptions import SubscriptionKey, fetch_list, parse_subscription_url
from .select import choose, detect_tags, evaluate, merge
from .settings import CrawlForum, Settings, SukebeiRule

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
            found += [_resource_candidate(r) for r in self.repo.resources_by_number(wanted.number)]
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

    # --- 磁力池: 确认影片 ---

    def pool_groups(self, *, sources: list[str] | None = None, search: str = "") -> list[PoolGroup]:
        """待确认的磁力按番号分组; 每组的磁力按选择规则打分, 默认选最高分的."""
        s = self.settings
        groups: dict[str, list[Resource]] = {}
        for r in self.repo.pool_pending(sources=sources, search=search):
            assert r.number
            groups.setdefault(r.number.upper(), []).append(r)
        works = self.repo.works(groups)
        releases = self.repo.releases_by_numbers(groups)
        out = []
        for key, resources in groups.items():
            scored = []
            for r in resources:
                cand = _resource_candidate(r)
                evaluate(cand, key, s)
                scored.append(PooledMagnet(resource=r, tags=cand.tags, score=cand.score, rejected=cand.rejected))
            scored.sort(key=lambda m: (not m.rejected, m.score, m.resource.seeders or 0), reverse=True)
            out.append(PoolGroup(number=resources[0].number or key, magnets=scored, work=works.get(key), release=releases.get(key)))
        return out

    def confirm_pool(self, picks: list[int]) -> int:
        """确认影片: 用选中的磁力 (资源编号) 加入下载队列; 返回处理的数量."""
        done = 0
        for resource_id in picks:
            try:
                self.download_resource(resource_id)
                done += 1
            except ValueError as exc:
                self.repo.log("warning", f"确认影片失败 (资源 {resource_id}): {exc}")
        return done

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

    async def run_feeds(self) -> None:
        async with self._locks["feeds"]:
            s = self.settings
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http:
                for feed in s.input_feeds:
                    try:
                        resp = await http.get(feed.url)
                        entries = parse_feed(resp.text)
                    except (httpx.HTTPError, ValueError) as exc:
                        self.repo.log("warning", f"读取 RSS 失败 {feed.url}: {exc}")
                        continue
                    for entry in entries:
                        lowered = entry.title.lower()
                        if feed.include_keywords and not any(k.lower() in lowered for k in feed.include_keywords):
                            continue
                        if any(k.lower() in lowered for k in feed.exclude_keywords):
                            continue
                        if not self.repo.mark_feed_seen(feed.url, entry.key):
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
                        added, _ = self.add_numbers([entry.number], source="rss")
                        w = self.repo.wanted_by_number(entry.number)
                        if added and magnet and w is not None and w.status is WantedStatus.PENDING:
                            self.add_magnet(w.id, magnet, entry.title)

    async def run_crawl(self, *, force: bool = False) -> None:
        """``force``: 网页上手动点「立即爬取」, 不受「启用定时爬取」开关限制 (开关只管定时任务)."""
        async with self._locks["crawl"]:
            s = self.settings
            if not s.crawl_enabled and not force:
                return
            if not s.crawl_forums:
                raise SiteError("没有配置爬取目标, 请在设置 → 论坛爬取里填写板块")
            site = Sehuatang(s.crawl_base_url)
            fetcher = Fetcher(proxy=s.scrape_proxy, flaresolverr_url=s.flaresolverr_url)
            new_total = queued = 0
            try:
                for forum in s.crawl_forums:
                    for page in range(1, s.crawl_pages + 1):
                        forum_name, refs = await site.forum_page(fetcher, forum.fid, page, forum.typeid)
                        fresh = [r for r in refs if not self.repo.resource_exists("sehuatang", r.tid)]
                        for ref in fresh:
                            await asyncio.sleep(s.crawl_request_delay)
                            try:
                                detail = await site.thread(fetcher, ref.tid)
                            except SiteError as exc:
                                # 不入库, 下一轮再试.
                                self.repo.log("warning", f"sehuatang 帖子 {ref.tid} 读取失败: {exc}")
                                continue
                            title = detail.title or ref.title
                            number = extract_number(title)
                            res = self.repo.add_resource(
                                Resource(
                                    source="sehuatang", tid=ref.tid, forum_id=forum.fid, forum_name=forum_name,
                                    type_id=ref.type_id, type_name=ref.type_name, title=title, number=number,
                                    magnet=detail.magnet,
                                    info_hash=info_hash_of(detail.magnet) if detail.magnet else None,
                                    size=detail.size, posted=detail.posted or ref.posted,
                                    subtitle="字幕" in forum_name or "中文字幕" in title
                                    or bool(number and has_subtitle_suffix(title, number)),
                                )
                            )
                            new_total += 1
                            if forum.auto_download and _accepts(forum, res) and res.magnet and res.number and res.id:
                                self.download_resource(res.id)
                                queued += 1
                        # 这一页里出现了已爬过的帖子, 更早的页不用再翻.
                        if len(fresh) < len(refs) or not refs:
                            break
                        await asyncio.sleep(s.crawl_request_delay)
            finally:
                await fetcher.aclose()
                if new_total:
                    self.repo.log("info", f"sehuatang 爬取: 新帖 {new_total} 个, 自动下载 {queued} 个")

    async def run_sukebei(self, *, force: bool = False) -> None:
        """按规则读取 sukebei 的 RSS, 新磁力进入磁力池; 规则开了自动下载的, 符合选择规则的直接提交."""
        async with self._locks["sukebei"]:
            s = self.settings
            if not s.sukebei_rules:
                if force:
                    raise SiteError("没有配置 sukebei 爬取规则, 请在设置 → 磁力 → sukebei 里填写")
                return
            fetcher = Fetcher(proxy=s.scrape_proxy, flaresolverr_url=s.flaresolverr_url)
            new_total = queued = 0
            try:
                for i, rule in enumerate(s.sukebei_rules):
                    if i:
                        await asyncio.sleep(s.crawl_request_delay)
                    url = Sukebei.rss_url(query=rule.query, user=rule.user, category=rule.category, trusted_only=rule.trusted_only)
                    try:
                        status, text = await fetcher.get_text(url)
                        if status != 200:
                            raise SiteError(f"HTTP {status}")
                        items = parse_sukebei_items(text)
                    except SiteError as exc:
                        self.repo.log("warning", f"sukebei 规则 {rule.name} 读取失败: {exc}")
                        continue
                    for item in items:
                        cand = item.candidate
                        if not _rule_accepts(rule, item.trusted, cand) or self.repo.resource_exists("sukebei", item.id):
                            continue
                        number = extract_number(cand.title)
                        res = self.repo.add_resource(
                            Resource(
                                source="sukebei", tid=item.id, forum_id=0, forum_name="sukebei", type_name=rule.name,
                                title=cand.title, number=number, magnet=cand.magnet, info_hash=cand.info_hash,
                                size=cand.size, posted=cand.published, seeders=cand.seeders,
                                subtitle=bool(number and (Tag.SUBTITLE in detect_tags(cand, number))),
                            )
                        )
                        new_total += 1
                        if rule.auto_download and number and res.id:
                            evaluate(cand, number, s)
                            if not cand.rejected:
                                self.download_resource(res.id)
                                queued += 1
            finally:
                await fetcher.aclose()
                if new_total:
                    self.repo.log("info", f"sukebei 爬取: 新磁力 {new_total} 个, 自动下载 {queued} 个")

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
class PooledMagnet:
    resource: Resource
    tags: set[str]
    score: float
    rejected: str


@dataclass
class PoolGroup:
    """「确认影片」的一张卡片: 一个番号, 磁力池里它的磁力, 以及两边共享的作品信息."""

    number: str
    magnets: list[PooledMagnet]
    work: Work | None = None
    """Amane 里的作品 (海报、封面、演员)."""
    release: Release | None = None
    """订阅里见过的同番号作品."""

    @property
    def title(self) -> str:
        return (self.work.title if self.work else "") or (self.release.title if self.release else "")

    @property
    def best(self) -> PooledMagnet:
        return self.magnets[0]


def _name_key(name: str) -> str:
    return re.sub(r"\s+", "", name).casefold()


def _resource_candidate(r: Resource) -> Candidate:
    assert r.magnet and r.info_hash
    return Candidate(
        site=r.source, title=r.title, magnet=r.magnet, info_hash=r.info_hash, size=r.size, published=r.posted,
        seeders=r.seeders, tags={Tag.SUBTITLE} if r.subtitle else set(),
    )


def _rule_accepts(rule: SukebeiRule, trusted: bool, cand: Candidate) -> bool:
    """sukebei 规则的入池条件: 只要可信、最少做种、包含词、排除词."""
    if rule.trusted_only and not trusted:
        return False
    if rule.min_seeders and (cand.seeders or 0) < rule.min_seeders:
        return False
    lowered = cand.title.lower()
    if rule.include_keywords and not any(k.lower() in lowered for k in rule.include_keywords):
        return False
    return not any(k.lower() in lowered for k in rule.exclude_keywords)


def _accepts(forum: CrawlForum, r: Resource) -> bool:
    """板块的自动下载过滤: 分类、包含词、排除词."""
    if forum.types and r.type_name not in forum.types:
        return False
    lowered = r.title.lower()
    if forum.include_keywords and not any(k.lower() in lowered for k in forum.include_keywords):
        return False
    return not any(k.lower() in lowered for k in forum.exclude_keywords)


def _already_exists(message: str) -> bool:
    return any(k in message for k in ("已存在", "重复", "exist"))


def _default_client(s: Settings, repo: Repo) -> Pan115 | None:
    if s.app_id == "fake":
        # 联调用: 不连 115, 在内存里模拟.
        return FakePan115()
    return OpenClient(s.app_id, repo) if s.app_id else None
