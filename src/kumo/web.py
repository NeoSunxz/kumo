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
from .settings import CrawlForum, Env, InputFeed, Settings, SukebeiRule

JOB_LABELS = {
    "search": "搜索磁力", "submit": "提交云下载", "tasks": "刷新下载进度", "sync": "同步 115 目录",
    "feeds": "读取 RSS", "crawl": "论坛爬取", "subs": "检查订阅", "sukebei": "sukebei 爬取", "amane": "同步 Amane",
}

#: 搜索番号时可选的磁力来源; sehuatang 查的是本地已爬取的资源库.
SEARCH_SOURCES = [*SITES, "sehuatang"]

_TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
_TEMPLATES.env.filters["size"] = format_size
_TEMPLATES.env.filters["ts"] = lambda t: datetime.fromtimestamp(t).strftime("%m-%d %H:%M") if t else "-"
# 资料站图片经 kumo 代理 (JavBus 校验 Referer, 直接引用会 403).
_TEMPLATES.env.filters["img"] = lambda url: f"/img?u={quote(url, safe='')}" if url else ""
# Amane 的海报 / 封面 / 演员图, 由 kumo 带 token 去 Amane 取.
_TEMPLATES.env.filters["amane_img"] = lambda url: f"/img/amane?u={quote(url, safe='')}" if url else ""


def _cover(release: Any = None, work: Any = None) -> str:
    """作品封面 (横图): 资料站封面优先, 没有时用 Amane 的封面或海报."""
    if release is not None and release.cover:
        return f"/img?u={quote(release.cover, safe='')}"
    if work is not None and (work.thumb or work.poster):
        return f"/img/amane?u={quote(work.thumb or work.poster, safe='')}"
    return ""


_TEMPLATES.env.globals["cover"] = _cover
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
        ctx.setdefault("errors", {JOB_LABELS.get(k, k): v for k, v in s.last_error.items()})
        ctx.setdefault("running", [JOB_LABELS.get(k, k) for k in s.running])
        ctx.setdefault("flash", request.query_params.get("flash", ""))
        ctx.setdefault("pool_pending", s.repo.pool_pending_count())
        ctx.setdefault("amane_url", s.settings.amane_url.rstrip("/"))
        return _TEMPLATES.TemplateResponse(request, name, ctx)

    def back(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    # --- 番号列表 ---

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, status: str = "", q: str = "") -> HTMLResponse:
        repo = svc(request).repo
        statuses = [WantedStatus(status)] if status in WantedStatus._value2member_map_ else None
        items = repo.list_wanted(statuses, search=q.strip())
        return page(
            request,
            "index.html",
            items=items,
            works=repo.works(w.number for w in items),
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
        key = wanted.number.upper()
        return page(
            request, "wanted.html", w=wanted, candidates=repo.candidates(wanted_id), owned=repo.owned(wanted.number),
            work=repo.works([key]).get(key), release=repo.releases_by_numbers([key]).get(key),
        )

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
            return [k.strip() for k in re.split(r"[,，]", text) if k.strip()]

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
            rules = [_parse_sukebei_rule(line) for line in lines("sukebei_rules")]
            current = svc(request).settings
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
                sukebei_rules=rules,
                sukebei_interval_minutes=float(data.get("sukebei_interval_minutes") or 60),
                amane_url=data.get("amane_url", "").rstrip("/"),
                amane_token="" if "clear_amane_token" in data else (data.get("amane_token") or current.amane_token),
                amane_interval_minutes=float(data.get("amane_interval_minutes") or 30),
                amane_refresh_days=float(data.get("amane_refresh_days") or 7),
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
        request: Request, q: str = "", forum: int | None = None, typeid: int | None = None, subtitle: str = "", source: str = ""
    ) -> HTMLResponse:
        repo = svc(request).repo
        sources = [source] if source in ("sehuatang", "sukebei") else None
        if forum or typeid:
            sources = ["sehuatang"]
        params = {k: v for k, v in (("fid", forum), ("typeid", typeid), ("subtitle", subtitle), ("q", q.strip())) if v}
        rss_url = str(request.base_url) + "rss/sehuatang.xml" + ("?" + urlencode(params) if params else "")
        if env.password:
            rss_url += ("&" if params else "?") + "key=<密码>"
        return page(
            request,
            "resources.html",
            items=repo.resources(
                search=q.strip(),
                sources=sources,
                forum_ids=[forum] if forum else None,
                type_ids=[typeid] if typeid else None,
                subtitle=True if subtitle == "1" else None,
            ),
            forums=repo.resource_forums(),
            source_counts=repo.resource_sources(),
            source=source if sources and not forum else "",
            types=repo.resource_types(forum) if forum else [],
            q=q,
            forum=forum,
            typeid=typeid,
            subtitle=subtitle,
            rss_url=rss_url,
            crawl_enabled=svc(request).settings.crawl_enabled,
        )

    @app.get("/pool", response_class=HTMLResponse)
    async def pool(request: Request, q: str = "", source: str = "") -> HTMLResponse:
        s = svc(request)
        sources = [source] if source in ("sehuatang", "sukebei") else None
        return page(
            request, "pool.html", groups=s.pool_groups(sources=sources, search=q.strip()), q=q, source=source if sources else "",
            source_counts=s.repo.resource_sources(), settings=s.settings,
        )

    @app.post("/pool/{action}")
    async def pool_action(request: Request, action: str) -> RedirectResponse:
        """确认或忽略: ``only`` 指定单个番号 (卡片上的按钮), 否则处理勾选的番号."""
        form = await request.form()
        only = str(form.get("only") or "")
        numbers = [only] if only else [str(v) for v in form.getlist("ids")]
        s = svc(request)
        if action == "confirm":
            picks = [int(v) for n in numbers if str(v := form.get(f"pick_{n}") or "").isdigit()]
            done = s.confirm_pool(picks)
            if done:
                asyncio.create_task(s.run_guarded("submit", s.run_submit))
            flash = f"已加入下载队列 {done} 部"
        elif action == "dismiss":
            s.repo.dismiss_numbers(numbers)
            flash = f"已忽略 {len(numbers)} 部"
        else:
            raise HTTPException(400, "未知操作")
        referer = urlsplit(request.headers.get("referer") or "/pool")
        query = [(k, v) for k, v in parse_qsl(referer.query) if k != "flash"] + [("flash", flash)]
        return back(f"/pool?{urlencode(query)}")

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
        items = s.repo.releases(sub_ids=sub_ids, kinds=kinds, search=q.strip(), days=days, not_queued=todo == "1", limit=300)
        return page(
            request,
            "releases.html",
            items=items,
            **_shared(s, [r.number for r, *_ in items]),
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
        return page(
            request, "actress.html", g=group, items=items, todo=todo, message=message, rss_url=rss_url,
            **_shared(s, [r.number for r, *_ in items]),
        )

    @app.get("/img")
    async def image(request: Request, u: str) -> Response:
        try:
            path = await request.app.state.images.get(u, proxy=svc(request).settings.scrape_proxy)
        except ImageError as exc:
            raise HTTPException(404, str(exc)) from exc
        return FileResponse(path, headers={"Cache-Control": "public, max-age=2592000"})

    @app.get("/img/amane")
    async def amane_image(request: Request, u: str) -> Response:
        amane = svc(request).amane()
        if amane is None:
            raise HTTPException(404, "未接入 Amane")
        try:
            path = await request.app.state.images.get_amane(u, amane)
        except ImageError as exc:
            raise HTTPException(404, str(exc)) from exc
        finally:
            await amane.aclose()
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
            "feeds": s.run_feeds, "crawl": lambda: s.run_crawl(force=True), "subs": lambda: s.run_subscriptions(force=True),
            "sukebei": lambda: s.run_sukebei(force=True), "amane": s.run_amane,
        }
        if job not in jobs:
            raise HTTPException(404)
        if job in s.running:
            flash = f"{JOB_LABELS[job]}已经在运行"
        else:
            asyncio.create_task(s.run_guarded(job, jobs[job]))
            flash = f"已开始{JOB_LABELS[job]}, 在后台运行, 稍后刷新页面查看结果"
        referer = urlsplit(request.headers.get("referer") or "/")
        path = referer.path if referer.path.startswith("/") else "/"
        query = [(k, v) for k, v in parse_qsl(referer.query) if k != "flash"] + [("flash", flash)]
        return back(f"{path}?{urlencode(query)}")

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
            sources=["sehuatang"],
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


def _shared(s: Service, numbers: list[str]) -> dict[str, Any]:
    """片源页面用到的、按番号与磁力那边共享的数据: Amane 作品、磁力池数量、115 是否已有."""
    return {
        "works": s.repo.works(numbers),
        "pool": s.repo.pool_counts(numbers),
        "owned_set": s.repo.owned_numbers(numbers),
    }


_RULE_SEEDERS = re.compile(r"(?:做种|seed(?:ers)?)\s*(?:>=|≥|:|：)?\s*(\d+)", re.I)


def _parse_sukebei_rule(line: str) -> SukebeiRule:
    """``名称 | 搜索词 | 选项``; 选项用空格分隔: 自动、可信、做种>=N、上传者:名字、分类:2_2、+包含词、-排除词."""
    parts = [p.strip() for p in line.split("|")]
    if not parts[0]:
        raise ValueError(f"sukebei 规则要有名称: {line}")
    rule: dict[str, Any] = {"name": parts[0], "query": parts[1] if len(parts) > 1 else "", "include_keywords": [], "exclude_keywords": []}
    for opt in (parts[2] if len(parts) > 2 else "").split():
        key, sep, value = opt.replace("：", ":").partition(":")
        if opt in ("自动", "auto"):
            rule["auto_download"] = True
        elif opt in ("可信", "trusted"):
            rule["trusted_only"] = True
        elif m := _RULE_SEEDERS.fullmatch(opt):
            rule["min_seeders"] = int(m.group(1))
        elif sep and key in ("上传者", "user", "u"):
            rule["user"] = value
        elif sep and key in ("分类", "category", "c"):
            rule["category"] = value
        elif opt.startswith("+") and len(opt) > 1:
            rule["include_keywords"].append(opt[1:])
        elif opt.startswith("-") and len(opt) > 1:
            rule["exclude_keywords"].append(opt[1:])
        else:
            raise ValueError(f"sukebei 规则「{parts[0]}」有无法识别的选项: {opt}")
    return SukebeiRule(**rule)


def format_sukebei_rule(rule: SukebeiRule) -> str:
    opts = []
    if rule.auto_download:
        opts.append("自动")
    if rule.trusted_only:
        opts.append("可信")
    if rule.min_seeders:
        opts.append(f"做种>={rule.min_seeders}")
    if rule.user:
        opts.append(f"上传者:{rule.user}")
    if rule.category != "2_2":
        opts.append(f"分类:{rule.category}")
    opts += [f"+{k}" for k in rule.include_keywords] + [f"-{k}" for k in rule.exclude_keywords]
    return " | ".join(x for x in (rule.name, rule.query, " ".join(opts)) if x is not None).rstrip(" |")


_TEMPLATES.env.filters["sukebei_rule"] = format_sukebei_rule


def _int_list(text: str) -> list[int]:
    return [int(x) for x in text.replace("，", ",").split(",") if x.strip()]
