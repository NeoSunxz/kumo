"""网页与 HTTP 接口."""

from __future__ import annotations

import asyncio
import base64
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import segno
from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from . import __version__
from .db import Repo
from .feeds import render_release_rss, render_resource_rss, render_rss
from .images import ImageCache, ImageError
from .magnets import SITES, SiteError
from .models import STATUS_LABELS, TAG_LABELS, WantedStatus, format_size
from .numbers import parse_input
from .pan115 import DeviceAuth, Pan115Error
from .sehuatang import Sehuatang
from .subscriptions import KIND_LABELS, SOURCE_LABELS
from .service import Service
from .settings import CrawlForum, Env, InputFeed, Settings

#: 搜索番号时可选的磁力来源; sehuatang 查的是本地已爬取的资源库.
SEARCH_SOURCES = [*SITES, "sehuatang"]

_TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
_TEMPLATES.env.filters["size"] = format_size
_TEMPLATES.env.filters["ts"] = lambda t: datetime.fromtimestamp(t).strftime("%m-%d %H:%M") if t else "-"
# 资料站图片经 kumo 代理 (JavBus 校验 Referer, 直接引用会 403).
_TEMPLATES.env.filters["img"] = lambda url: f"/img?u={quote(url, safe='')}" if url else ""
_TEMPLATES.env.globals["today"] = lambda: datetime.now().strftime("%Y-%m-%d")
_TEMPLATES.env.globals.update(
    STATUS_LABELS=STATUS_LABELS, TAG_LABELS=TAG_LABELS, KIND_LABELS=KIND_LABELS, SOURCE_LABELS=SOURCE_LABELS,
    version=__version__,
)


def create_app(env: Env | None = None, service: Service | None = None) -> FastAPI:
    env = env or Env.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        svc = service or Service(Repo(env.data_dir / "kumo.db"))
        app.state.service = svc
        app.state.device_auth = None
        app.state.images = ImageCache(env.data_dir / "img")
        if service is None:
            svc.start()
        yield
        await svc.stop()

    app = FastAPI(title="kumo", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def basic_auth(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if not env.password or request.url.path == "/health":
            return await call_next(request)
        if request.url.path.startswith("/rss") and secrets.compare_digest(request.query_params.get("key", ""), env.password):
            return await call_next(request)
        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
            try:
                _, _, given = base64.b64decode(header[6:]).decode().partition(":")
            except ValueError:
                given = ""
            if secrets.compare_digest(given, env.password):
                return await call_next(request)
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="kumo"'})

    def svc(request: Request) -> Service:
        return request.app.state.service

    def page(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        s: Service = svc(request)
        client = s.client()
        ctx.setdefault("authorized", bool(client and client.authorized()))
        ctx.setdefault("errors", s.last_error)
        return _TEMPLATES.TemplateResponse(request, name, ctx)

    def back(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    # --- 番号列表 ---

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, status: str = "", q: str = "") -> HTMLResponse:
        repo = svc(request).repo
        statuses = [WantedStatus(status)] if status in WantedStatus._value2member_map_ else None
        return page(
            request,
            "index.html",
            items=repo.list_wanted(statuses, search=q.strip()),
            counts=repo.status_counts(),
            status=status,
            q=q,
        )

    @app.post("/wanted")
    async def add_wanted(request: Request, numbers: Annotated[str, Form()]) -> RedirectResponse:
        parsed = [n for n in (parse_input(t) for t in re.split(r"[\n,，;；]+", numbers)) if n]
        svc(request).add_numbers(parsed)
        asyncio.create_task(svc(request).run_guarded("search", svc(request).run_search))
        return back("/")

    @app.get("/wanted/{wanted_id}", response_class=HTMLResponse)
    async def wanted_detail(request: Request, wanted_id: int) -> HTMLResponse:
        repo = svc(request).repo
        wanted = repo.wanted(wanted_id)
        if wanted is None:
            raise HTTPException(404)
        return page(request, "wanted.html", w=wanted, candidates=repo.candidates(wanted_id), owned=repo.owned(wanted.number))

    @app.post("/wanted/{wanted_id}/choose/{candidate_id}")
    async def choose(request: Request, wanted_id: int, candidate_id: int) -> RedirectResponse:
        try:
            svc(request).choose_candidate(wanted_id, candidate_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        asyncio.create_task(svc(request).run_guarded("submit", svc(request).run_submit))
        return back(f"/wanted/{wanted_id}")

    @app.post("/wanted/{wanted_id}/magnet")
    async def manual_magnet(request: Request, wanted_id: int, magnet: Annotated[str, Form()]) -> RedirectResponse:
        try:
            svc(request).add_magnet(wanted_id, magnet.strip())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        asyncio.create_task(svc(request).run_guarded("submit", svc(request).run_submit))
        return back(f"/wanted/{wanted_id}")

    @app.post("/wanted/{wanted_id}/{action}")
    async def wanted_action(request: Request, wanted_id: int, action: str) -> RedirectResponse:
        s = svc(request)
        w = s.repo.wanted(wanted_id)
        if w is None:
            raise HTTPException(404)
        if action == "retry":
            s.repo.update_wanted(wanted_id, status=WantedStatus.PENDING, searched_at=None, note="")
            asyncio.create_task(s.run_guarded("search", s.run_search))
        elif action == "resubmit" and w.chosen_id:
            s.repo.update_wanted(wanted_id, status=WantedStatus.QUEUED, note="")
            asyncio.create_task(s.run_guarded("submit", s.run_submit))
        elif action == "ignore":
            s.repo.update_wanted(wanted_id, status=WantedStatus.IGNORED)
        elif action == "delete":
            s.repo.delete_wanted(wanted_id)
            return back("/")
        else:
            raise HTTPException(400, "未知操作")
        return back(f"/wanted/{wanted_id}")

    # --- 设置 ---

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request) -> HTMLResponse:
        return page(request, "settings.html", s=svc(request).settings, sites=SEARCH_SOURCES, message="")

    @app.post("/settings", response_class=HTMLResponse)
    async def save_settings(request: Request) -> Response:
        form = await request.form()
        data = {k: str(v) for k, v in form.items()}

        def lines(key: str) -> list[str]:
            return [x.strip() for x in data.get(key, "").splitlines() if x.strip()]

        feeds = []
        for line in lines("input_feeds"):
            url, _, rest = line.partition("|")
            include, _, exclude = rest.partition("|")
            feeds.append(
                InputFeed(
                    url=url.strip(),
                    include_keywords=[k.strip() for k in include.split(",") if k.strip()],
                    exclude_keywords=[k.strip() for k in exclude.split(",") if k.strip()],
                )
            )
        def keywords(text: str) -> list[str]:
            return [k.strip() for k in text.split(",") if k.strip()]

        try:
            forums = []
            for line in lines("crawl_forums"):
                parts = [p.strip() for p in line.split("|")] + [""] * 5
                fid, sep, typeid = parts[0].partition(":")
                if not fid.strip().isdigit() or (sep and not typeid.strip().isdigit()):
                    raise ValueError(f"板块写成「编号」或「编号:分类编号」: {line}")
                forums.append(
                    CrawlForum(
                        fid=int(fid),
                        typeid=int(typeid) if typeid else None,
                        auto_download=parts[1] in ("自动", "是", "auto", "yes", "1"),
                        types=keywords(parts[2]),
                        include_keywords=keywords(parts[3]),
                        exclude_keywords=keywords(parts[4]),
                    )
                )
            settings = Settings(
                app_id=data.get("app_id", ""),
                download_path=data.get("download_path", ""),
                sync_paths=lines("sync_paths"),
                sync_interval_hours=float(data.get("sync_interval_hours") or 6),
                sites=[name for name in SEARCH_SOURCES if data.get(f"site_{name}")],
                scrape_proxy=data.get("scrape_proxy", ""),
                flaresolverr_url=data.get("flaresolverr_url", ""),
                # 不改动时表单提交空串: 留空表示保持原值, 「清除」勾选框才删除.
                javdb_cookie=""
                if "clear_javdb_cookie" in data
                else (data.get("javdb_cookie") or svc(request).settings.javdb_cookie),
                no_magnet_retry_hours=float(data.get("no_magnet_retry_hours") or 24),
                size_min_gb=float(data.get("size_min_gb") or 0),
                size_max_gb=float(data.get("size_max_gb") or 0),
                prefer_subtitle="prefer_subtitle" in data,
                require_subtitle="require_subtitle" in data,
                allow_4k="allow_4k" in data,
                avoid_h265="avoid_h265" in data,
                exclude_keywords=[k.strip() for k in data.get("exclude_keywords", "").split(",") if k.strip()],
                submit_batch=int(data.get("submit_batch") or 5),
                task_poll_minutes=float(data.get("task_poll_minutes") or 5),
                input_feeds=feeds,
                feed_interval_minutes=float(data.get("feed_interval_minutes") or 30),
                crawl_enabled="crawl_enabled" in data,
                crawl_base_url=data.get("crawl_base_url") or "https://www.sehuatang.net",
                crawl_forums=forums,
                crawl_pages=int(data.get("crawl_pages") or 2),
                crawl_interval_minutes=float(data.get("crawl_interval_minutes") or 60),
                crawl_request_delay=float(data.get("crawl_request_delay") or 1.5),
                subs_interval_hours=float(data.get("subs_interval_hours") or 12),
                subs_pages=int(data.get("subs_pages") or 1),
                subs_initial_pages=int(data.get("subs_initial_pages") or 3),
            )
        except (ValidationError, ValueError) as exc:
            return page(request, "settings.html", s=svc(request).settings, sites=SEARCH_SOURCES, message=f"保存失败: {exc}")
        svc(request).repo.save_settings(settings)
        return page(request, "settings.html", s=settings, sites=SEARCH_SOURCES, message="已保存")

    # --- 115 授权 ---

    @app.get("/auth", response_class=HTMLResponse)
    async def auth_page(request: Request) -> HTMLResponse:
        s = svc(request)
        quota: dict[str, int] | None = None
        client = s.client()
        if client and client.authorized():
            try:
                quota = await client.quota()
            except Pan115Error as exc:
                s.last_error["auth"] = str(exc)
        auth: DeviceAuth | None = request.app.state.device_auth
        qr = segno.make(auth.qrcode, error="m").svg_inline(scale=6, border=2) if auth else None
        return page(request, "auth.html", app_id=s.settings.app_id, qr=qr, quota=quota)

    @app.post("/auth/start")
    async def auth_start(request: Request) -> RedirectResponse:
        client = svc(request).open_client()
        if client is None:
            raise HTTPException(400, "请先在设置里填写 AppID")
        try:
            request.app.state.device_auth = await client.start_device_auth()
        except Pan115Error as exc:
            raise HTTPException(502, str(exc)) from exc
        return back("/auth")

    @app.get("/auth/poll")
    async def auth_poll(request: Request) -> JSONResponse:
        client = svc(request).open_client()
        auth: DeviceAuth | None = request.app.state.device_auth
        if client is None or auth is None:
            return JSONResponse({"status": -1, "message": "没有进行中的授权"})
        try:
            status = await client.poll_device_auth(auth)
            if status == 2:
                await client.finish_device_auth(auth)
                request.app.state.device_auth = None
                svc(request).repo.log("info", "115 授权成功")
        except Pan115Error as exc:
            request.app.state.device_auth = None
            return JSONResponse({"status": -1, "message": str(exc)})
        return JSONResponse({"status": status})

    @app.post("/auth/logout")
    async def auth_logout(request: Request) -> RedirectResponse:
        client = svc(request).open_client()
        if client is not None:
            client.logout()
        return back("/auth")

    # --- 115 目录 ---

    @app.get("/library", response_class=HTMLResponse)
    async def library(request: Request, q: str = "") -> HTMLResponse:
        repo = svc(request).repo
        return page(request, "library.html", stats=repo.library_stats(), files=repo.search_library(q.strip()) if q.strip() else [], q=q)

    # --- 论坛爬取的资源 ---

    @app.get("/resources", response_class=HTMLResponse)
    async def resources(
        request: Request, q: str = "", forum: int | None = None, typeid: int | None = None, subtitle: str = ""
    ) -> HTMLResponse:
        repo = svc(request).repo
        params = {k: v for k, v in (("fid", forum), ("typeid", typeid), ("subtitle", subtitle), ("q", q.strip())) if v}
        rss_url = str(request.base_url) + "rss/sehuatang.xml" + ("?" + urlencode(params) if params else "")
        if env.password:
            rss_url += ("&" if params else "?") + "key=<密码>"
        return page(
            request,
            "resources.html",
            items=repo.resources(
                search=q.strip(),
                forum_ids=[forum] if forum else None,
                type_ids=[typeid] if typeid else None,
                subtitle=True if subtitle == "1" else None,
            ),
            forums=repo.resource_forums(),
            types=repo.resource_types(forum) if forum else [],
            q=q,
            forum=forum,
            typeid=typeid,
            subtitle=subtitle,
            rss_url=rss_url,
            crawl_enabled=svc(request).settings.crawl_enabled,
        )

    @app.post("/resources/{resource_id}/download")
    async def download_resource(request: Request, resource_id: int) -> RedirectResponse:
        try:
            wanted_id = svc(request).download_resource(resource_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        asyncio.create_task(svc(request).run_guarded("submit", svc(request).run_submit))
        return back(f"/wanted/{wanted_id}")

    # --- 订阅与新作 ---

    @app.get("/subscriptions", response_class=HTMLResponse)
    async def subscriptions(request: Request, message: str = "") -> HTMLResponse:
        s = svc(request)
        base = str(request.base_url)
        return page(request, "subscriptions.html", subs=s.repo.subscriptions(), base=base, message=message)

    @app.post("/subscriptions")
    async def add_subscription(
        request: Request,
        url: Annotated[str, Form()],
        auto_search: Annotated[str, Form()] = "",
        name: Annotated[str, Form()] = "",
    ) -> Response:
        s = svc(request)
        try:
            sub = await s.add_subscription(url, auto_search=bool(auto_search), name=name)
        except (ValueError, SiteError) as exc:
            return page(request, "subscriptions.html", subs=s.repo.subscriptions(), base=str(request.base_url), message=f"添加失败: {exc}")
        asyncio.create_task(s.run_guarded("subs", lambda: s.check_subscription(sub)))
        return back("/subscriptions")

    @app.post("/subscriptions/{sub_id}/rename")
    async def rename_subscription(request: Request, sub_id: int, name: Annotated[str, Form()]) -> RedirectResponse:
        s = svc(request)
        if s.repo.subscription(sub_id) is None:
            raise HTTPException(404)
        if name.strip():
            s.repo.update_subscription(sub_id, name=name.strip())
        return back("/subscriptions")

    @app.post("/subscriptions/{sub_id}/{action}")
    async def subscription_action(request: Request, sub_id: int, action: str) -> RedirectResponse:
        s = svc(request)
        sub = s.repo.subscription(sub_id)
        if sub is None:
            raise HTTPException(404)
        if action == "check":
            asyncio.create_task(s.run_guarded("subs", lambda: s.check_subscription(sub)))
        elif action == "toggle":
            s.repo.update_subscription(sub_id, enabled=not sub.enabled)
        elif action == "auto":
            s.repo.update_subscription(sub_id, auto_search=not sub.auto_search)
        elif action == "delete":
            s.repo.delete_subscription(sub_id)
        else:
            raise HTTPException(400, "未知操作")
        return back("/subscriptions")

    def _release_filters(sub: str, kind: str) -> tuple[list[int], list[str]]:
        try:
            sub_ids = _int_list(sub)
        except ValueError as exc:
            raise HTTPException(400, "sub 应为逗号分隔的订阅编号") from exc
        kinds = [k for k in kind.split(",") if k in KIND_LABELS]
        return sub_ids, kinds

    @app.get("/releases", response_class=HTMLResponse)
    async def releases(
        request: Request, sub: str = "", kind: str = "", q: str = "", days: int | None = None, todo: str = "", message: str = ""
    ) -> HTMLResponse:
        s = svc(request)
        sub_ids, kinds = _release_filters(sub, kind)
        params = {k: v for k, v in (("sub", sub), ("kind", kind), ("q", q.strip()), ("days", days)) if v}
        rss_url = str(request.base_url) + "rss/releases.xml" + ("?" + urlencode(params) if params else "")
        if env.password:
            rss_url += ("&" if params else "?") + "key=<密码>"
        return page(
            request,
            "releases.html",
            items=s.repo.releases(sub_ids=sub_ids, kinds=kinds, search=q.strip(), days=days, not_queued=todo == "1", limit=300),
            subs=s.repo.subscriptions(),
            sub=sub,
            kind=kind,
            q=q,
            days=days,
            todo=todo,
            rss_url=rss_url,
            message=message,
        )

    @app.post("/releases/queue")
    async def queue_releases(request: Request) -> RedirectResponse:
        form = await request.form()
        ids = [int(v) for v in form.getlist("ids") if str(v).isdigit()]
        s = svc(request)
        added = s.queue_releases(ids)
        if added:
            asyncio.create_task(s.run_guarded("search", s.run_search))
        # 回到提交时所在的页面 (新作列表或女优详情), 保留筛选条件, 只替换提示.
        referer = urlsplit(request.headers.get("referer") or "/releases")
        path = referer.path if referer.path.startswith(("/releases", "/actresses")) else "/releases"
        query = [(k, v) for k, v in parse_qsl(referer.query) if k != "message"]
        query.append(("message", f"已加入下载队列 {added} 部 (已在队列中的跳过)"))
        return back(f"{path}?{urlencode(query)}")

    @app.get("/actresses", response_class=HTMLResponse)
    async def actresses(request: Request, q: str = "") -> HTMLResponse:
        groups = svc(request).actress_groups()
        if q.strip():
            groups = [g for g in groups if q.strip().casefold() in g.name.casefold()]
        return page(request, "actresses.html", groups=groups, q=q)

    @app.get("/actresses/{sub_id}", response_class=HTMLResponse)
    async def actress(request: Request, sub_id: int, todo: str = "", message: str = "") -> HTMLResponse:
        s = svc(request)
        group = s.actress_group(sub_id)
        if group is None:
            raise HTTPException(404)
        items = s.repo.releases(sub_ids=group.sub_ids, not_queued=todo == "1", limit=2000)
        rss_url = f"{request.base_url}rss/releases.xml?sub={','.join(map(str, group.sub_ids))}"
        return page(request, "actress.html", g=group, items=items, todo=todo, message=message, rss_url=rss_url)

    @app.get("/img")
    async def image(request: Request, u: str) -> Response:
        try:
            path = await request.app.state.images.get(u, proxy=svc(request).settings.scrape_proxy)
        except ImageError as exc:
            raise HTTPException(404, str(exc)) from exc
        return FileResponse(path, headers={"Cache-Control": "public, max-age=2592000"})

    @app.get("/rss/releases.xml")
    async def rss_releases(
        request: Request, sub: str = "", kind: str = "", q: str = "", days: int | None = None, limit: int = 100
    ) -> Response:
        """订阅新作 RSS, 供 Amane 刮削元数据; 参数与新作页面的筛选一致."""
        s = svc(request)
        sub_ids, kinds = _release_filters(sub, kind)
        rows = s.repo.releases(sub_ids=sub_ids, kinds=kinds, search=q.strip(), days=days, limit=max(1, min(limit, 500)))
        label = " ".join(x for x in ("kumo 新作", sub and f"sub={sub}", kind, q) if x)
        body = render_release_rss([(r, subs) for r, subs, _, _ in rows], title=label, link=str(request.url))
        return Response(body, media_type="application/rss+xml; charset=utf-8")

    # --- 手动触发与日志 ---

    @app.post("/run/{job}")
    async def run_job(request: Request, job: str) -> RedirectResponse:
        s = svc(request)
        jobs = {
            "search": s.run_search, "submit": s.run_submit, "tasks": s.run_tasks, "sync": s.run_sync,
            "feeds": s.run_feeds, "crawl": s.run_crawl, "subs": lambda: s.run_subscriptions(force=True),
        }
        if job not in jobs:
            raise HTTPException(404)
        asyncio.create_task(s.run_guarded(job, jobs[job]))
        return back(request.headers.get("referer") or "/")

    @app.get("/events", response_class=HTMLResponse)
    async def events(request: Request) -> HTMLResponse:
        return page(request, "events.html", events=svc(request).repo.events(300))

    # --- 输出 RSS 与健康检查 ---

    @app.get("/rss.xml")
    async def rss(request: Request) -> Response:
        s = svc(request)
        statuses = [WantedStatus(x) for x in s.settings.rss_statuses if x in WantedStatus._value2member_map_]
        items = []
        for w in s.repo.list_wanted(statuses, limit=300):
            cand = s.repo.candidate(w.chosen_id) if w.chosen_id else None
            items.append((w, cand.title if cand else ""))
        items.sort(key=lambda it: it[0].submitted_at or it[0].updated_at, reverse=True)
        return Response(render_rss(items, link=str(request.base_url)), media_type="application/rss+xml; charset=utf-8")

    @app.get("/rss/sehuatang.xml")
    async def rss_sehuatang(
        request: Request,
        fid: str = "",
        typeid: str = "",
        subtitle: str = "",
        q: str = "",
        numbered: str = "1",
        limit: int = 100,
    ) -> Response:
        """按参数筛选的论坛资源 RSS; 每组参数就是一个可单独订阅的源."""
        try:
            forum_ids = _int_list(fid)
            type_ids = _int_list(typeid)
        except ValueError as exc:
            raise HTTPException(400, "fid / typeid 应为逗号分隔的数字") from exc
        s = svc(request)
        items = s.repo.resources(
            search=q.strip(),
            forum_ids=forum_ids,
            type_ids=type_ids,
            subtitle=True if subtitle == "1" else None,
            numbered=numbered != "0",
            with_magnet=True,
            limit=max(1, min(limit, 500)),
        )
        site = Sehuatang(s.settings.crawl_base_url)
        label = " ".join(x for x in ("sehuatang", fid and f"fid={fid}", typeid and f"typeid={typeid}", q) if x)
        body = render_resource_rss(items, title=label, link=str(request.url), thread_url=site.thread_url)
        return Response(body, media_type="application/rss+xml; charset=utf-8")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def _int_list(text: str) -> list[int]:
    return [int(x) for x in text.replace("，", ",").split(",") if x.strip()]
