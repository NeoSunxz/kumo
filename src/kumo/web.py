"""网页与 HTTP 接口."""

from __future__ import annotations

import asyncio
import base64
import re
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import segno
from fastapi import FastAPI, Form, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from . import __version__
from .db import Repo
from .amane import AmaneError
from .images import ImageCache, ImageError
from .magnets import SITES, SiteError
from .models import FILTER_TAGS, SOURCE_KINDS, STATUS_LABELS, TAG_LABELS, MagnetSource, SourceFilter, WantedStatus, format_size
from .numbers import parse_input
from .pan115 import DeviceAuth, Pan115Error
from .sehuatang import Sehuatang
from .select import day, ranked, tier
from .subscriptions import KIND_LABELS, SOURCE_LABELS
from .service import Service
from .settings import Env, Settings

JOB_LABELS = {
    "search": "搜索磁力", "submit": "提交云下载", "tasks": "刷新下载进度", "sync": "同步 115 目录",
    "feeds": "读取 RSS", "crawl": "98堂爬取", "subs": "检查订阅", "sukebei": "sukebei 爬取", "amane": "同步 Amane",
}

#: 搜索番号时可选的磁力来源; sehuatang 查的是本地已爬取的资源库.
SEARCH_SOURCES = [*SITES, "sehuatang"]

_TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
_TEMPLATES.env.filters["size"] = format_size
_TEMPLATES.env.filters["day"] = lambda text: day(text) or "-"
_TEMPLATES.env.filters["ts"] = lambda t: datetime.fromtimestamp(t).strftime("%m-%d %H:%M") if t else "-"
# 资料站图片经 kumo 代理 (JavBus 校验 Referer, 直接引用会 403).
_TEMPLATES.env.filters["img"] = lambda url: f"/img?u={quote(url, safe='')}" if url else ""
# Amane 的海报 / 封面 / 演员图, 由 kumo 带 token 去 Amane 取.
_TEMPLATES.env.filters["amane_img"] = lambda url, w=None: (f"/img/amane?u={quote(url, safe='')}" + (f"&w={w}" if w else "")) if url else ""


def _cover(release: Any = None, work: Any = None, magnets: Any = (), *, hires: bool = False) -> str:
    """作品封面 (横图): 资料站封面优先 (800×538, 列表里够用且加载快), 其次 Amane 的封面或海报, 最后是论坛帖子里的第一张图.

    ``hires``: 作品页的大图, Amane 的封面优先 (多为 DMM 原图, 2000 多像素宽).
    """
    if hires and work is not None and work.thumb:
        return f"/img/amane?u={quote(work.thumb, safe='')}"
    if release is not None and release.cover:
        return f"/img?u={quote(release.cover, safe='')}"
    if work is not None and (work.thumb or work.poster):
        return f"/img/amane?u={quote(work.thumb or work.poster, safe='')}"
    for m in magnets:
        if m.images:
            return f"/img/res/{m.id}/0"
    return ""


def _poster(release: Any = None, work: Any = None, magnets: Any = ()) -> tuple[str, bool]:
    """挑选作品的竖版小图: Amane 的海报 (缩成 320 宽) 优先; 没有时用横版封面 (第二项为真, 页面裁出右半边的正面).

    Amane 里还没有的作品会被自动提交刮削, 刮好之后就换成海报.
    """
    if work is not None and work.poster:
        return f"/img/amane?u={quote(work.poster, safe='')}&w=320", False
    url = _cover(release, None, magnets)
    return url, bool(url)


_TEMPLATES.env.globals["cover"] = _cover
_TEMPLATES.env.globals["poster"] = _poster
_TEMPLATES.env.globals["today"] = lambda: datetime.now().strftime("%Y-%m-%d")
_TEMPLATES.env.globals.update(
    STATUS_LABELS=STATUS_LABELS, TAG_LABELS=TAG_LABELS, KIND_LABELS=KIND_LABELS, SOURCE_LABELS=SOURCE_LABELS,
    FILTER_TAGS=FILTER_TAGS, SOURCE_KINDS=SOURCE_KINDS,
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
        svc.images = app.state.images
        if service is None:
            svc.start()
        yield
        await svc.stop()

    app = FastAPI(title="kumo", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def basic_auth(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if not env.password or request.url.path == "/health":
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
        ctx.setdefault("todo_new", s.repo.new_unhandled_count(time.time() - 7 * 86400))
        ctx.setdefault("amane_url", s.settings.amane_url.rstrip("/"))
        return _TEMPLATES.TemplateResponse(request, name, ctx)

    def back(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    # --- 番号列表 ---

    @app.get("/queue", response_class=HTMLResponse)
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
        return back("/queue")

    @app.get("/wanted/{wanted_id}", response_class=HTMLResponse)
    async def wanted_detail(request: Request, wanted_id: int) -> HTMLResponse:
        repo = svc(request).repo
        wanted = repo.wanted(wanted_id)
        if wanted is None:
            raise HTTPException(404)
        key = wanted.number.upper()
        return page(
            request, "wanted.html", w=wanted, candidates=ranked(repo.candidates(wanted_id), svc(request).settings),
            tier=lambda c: tier(c, svc(request).settings), owned=repo.owned(wanted.number),
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
            return back("/queue")
        else:
            raise HTTPException(400, "未知操作")
        return back(f"/wanted/{wanted_id}")

    # --- 设置 ---

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request) -> HTMLResponse:
        return page(request, "settings.html", s=svc(request).settings, sites=SEARCH_SOURCES, message="")

    @app.post("/settings", response_class=HTMLResponse)
    async def save_settings(request: Request) -> Response:
        """保存设置; 只改表单里出现的字段, 所以设置也可以分散在各页面的小表单里 (带 ``_back`` 时回到原页面)."""
        form = await request.form()
        current = svc(request).settings
        back_to = str(form.get("_back") or "")
        try:
            settings = _merge_settings(current, form)
        except (ValidationError, ValueError) as exc:
            if back_to:
                return back(_with_flash(back_to, f"保存失败: {exc}"))
            return page(request, "settings.html", s=current, sites=SEARCH_SOURCES, message=f"保存失败: {exc}")
        svc(request).repo.save_settings(settings)
        if back_to:
            return back(_with_flash(back_to, "已保存"))
        return page(request, "settings.html", s=settings, sites=SEARCH_SOURCES, message="已保存")

    @app.post("/settings/amane-clear-scrapes")
    async def amane_clear_scrapes(request: Request) -> RedirectResponse:
        n = svc(request).repo.clear_scrape_requests()
        svc(request).repo.log("info", f"清除让 Amane 刮削的记录 {n} 条")
        return back(_with_flash("/settings#amane", f"已清除 {n} 条提交记录, 这些作品可以重新提交刮削"))

    @app.post("/settings/amane-test")
    async def amane_test(request: Request) -> RedirectResponse:
        """用已保存的地址与 token 测试 Amane; 结果显示在设置页顶部."""
        amane = svc(request).amane()
        if amane is None:
            flash = "还没有填写 Amane 地址"
        else:
            try:
                total = await amane.check()
                flash = f"Amane 连接正常, 媒体库里有 {total} 部作品"
            except AmaneError as exc:
                flash = f"Amane 连接失败: {exc}"
            finally:
                await amane.aclose()
        return back(f"/settings?{urlencode({'flash': flash})}#amane")

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
        request: Request, q: str = "", forum_: str = Query("", alias="forum"), typeid_: str = Query("", alias="typeid"),
        subtitle: str = "", source: str = "", src_: str = Query("", alias="src"),
    ) -> HTMLResponse:
        forum, typeid, src = _opt_int(forum_), _opt_int(typeid_), _opt_int(src_)
        repo = svc(request).repo
        sources = [source] if source in ("sehuatang", "sukebei", "rss") else None
        tags = [t for t in request.query_params.getlist("tag") if t in TAG_LABELS]
        if forum or typeid:
            sources = ["sehuatang"]
        return page(
            request,
            "resources.html",
            items=repo.resources(
                search=q.strip(),
                sources=sources,
                source_ids=[src] if src else None,
                tags=tags,
                forum_ids=[forum] if forum else None,
                type_ids=[typeid] if typeid else None,
                subtitle=True if subtitle == "1" else None,
            ),
            magnet_sources=repo.sources(),
            src=src,
            tags=tags,
            forums=repo.resource_forums(),
            source_counts=repo.resource_sources(),
            source=source if sources and not forum else "",
            types=repo.resource_types(forum) if forum else [],
            q=q,
            forum=forum,
            typeid=typeid,
            subtitle=subtitle,
            has_sources=bool(repo.sources(enabled_only=True)),
        )

    @app.get("/pool")
    async def pool_redirect() -> RedirectResponse:
        return back("/works?origin=magnet")

    # --- 磁力订阅 ---

    @app.get("/sources", response_class=HTMLResponse)
    async def sources(request: Request) -> HTMLResponse:
        s = svc(request)
        return page(request, "sources.html", sources=s.repo.sources(), settings=s.settings, base=str(request.base_url))

    @app.post("/sources")
    async def add_source(request: Request) -> RedirectResponse:
        form = await request.form()
        try:
            src = svc(request).repo.add_source(_source_from_form(form, MagnetSource(kind=str(form.get("kind") or ""), name="")))
        except ValueError as exc:
            return back(_with_flash("/sources", f"添加失败: {exc}"))
        return back(_with_flash(f"/sources#src-{src.id}", f"已添加「{src.name}」，点「立即爬取」试一下"))

    @app.post("/sources/preview", response_class=HTMLResponse)
    async def preview_new_source(request: Request) -> Response:
        """添加表单上的「预览」: 用表单里的条件试爬, 不保存."""
        form = await request.form()
        try:
            src = _source_from_form(form, MagnetSource(kind=str(form.get("kind") or ""), name=""))
        except ValueError as exc:
            return back(_with_flash("/sources", f"预览失败: {exc}"))
        return await _preview(request, src)

    @app.post("/sources/{source_id}/preview", response_class=HTMLResponse)
    async def preview_source(request: Request, source_id: int) -> Response:
        """「预览」: 编辑表单上的按改过 (还没保存) 的条件, 列表上的按已保存的条件."""
        src = svc(request).repo.source(source_id)
        if src is None:
            raise HTTPException(404)
        form = await request.form()
        if "kind" not in form:
            return await _preview(request, src)  # 列表上的「预览」: 按已保存的条件
        try:
            new = _source_from_form(form, src)
        except ValueError as exc:
            return back(_with_flash(f"/sources#src-{source_id}", f"预览失败: {exc}"))
        new.id = source_id
        return await _preview(request, new)

    async def _preview(request: Request, src: MagnetSource) -> HTMLResponse:
        try:
            rows, error = await svc(request).preview_source(src), ""
        except SiteError as exc:
            rows, error = [], str(exc)
        return page(request, "source_preview.html", src=src, rows=rows, error=error)

    @app.post("/sources/{source_id}")
    async def edit_source(request: Request, source_id: int) -> RedirectResponse:
        repo = svc(request).repo
        src = repo.source(source_id)
        if src is None:
            raise HTTPException(404)
        try:
            new = _source_from_form(await request.form(), src)
        except ValueError as exc:
            return back(_with_flash(f"/sources#src-{source_id}", f"保存失败: {exc}"))
        repo.update_source(source_id, name=new.name, params=new.params, filter=new.filter, auto_download=new.auto_download)
        if new.params != src.params or new.filter != src.filter:
            repo.clear_seen(source_id)  # 条件变了, 之前挡掉的重新判断
        return back(_with_flash(f"/sources#src-{source_id}", f"已保存「{new.name}」"))

    @app.post("/sources/{source_id}/{action}")
    async def source_action(request: Request, source_id: int, action: str) -> RedirectResponse:
        s = svc(request)
        src = s.repo.source(source_id)
        if src is None:
            raise HTTPException(404)
        if action == "toggle":
            s.repo.update_source(source_id, enabled=not src.enabled)
        elif action == "auto":
            s.repo.update_source(source_id, auto_download=not src.auto_download)
        elif action in ("up", "down"):
            s.repo.move_source(source_id, -1 if action == "up" else 1)
        elif action == "run":
            job = {"sehuatang": "crawl", "sukebei": "sukebei", "rss": "feeds"}[src.kind]
            run = {"crawl": s.run_crawl, "sukebei": s.run_sukebei, "feeds": s.run_feeds}[job]
            if job in s.running:
                return back(_with_flash(f"/sources#src-{source_id}", f"{JOB_LABELS[job]}正在运行, 稍后再试"))
            asyncio.create_task(s.run_guarded(job, lambda: run(source_id=source_id)))
            return back(_with_flash(f"/sources#src-{source_id}", f"已开始爬取「{src.name}」, 稍后刷新查看"))
        elif action == "delete":
            s.repo.delete_source(source_id)
        else:
            raise HTTPException(400, "未知操作")
        return back(f"/sources#src-{source_id}")

    @app.post("/resources/delete")
    async def delete_resources(request: Request) -> RedirectResponse:
        """删除某来源 (``source``: sehuatang / sukebei / rss) 或某条订阅 (``src``) 爬到的全部磁力."""
        form = await request.form()
        source, src = str(form.get("source") or ""), _opt_int(str(form.get("src") or ""))
        repo = svc(request).repo
        if src is not None:
            n, label = repo.delete_resources(source_id=src), (s.name if (s := repo.source(src)) else f"订阅 {src}")
        elif source in SOURCE_KINDS:
            n, label = repo.delete_resources(source=source), SOURCE_KINDS[source]
        else:
            raise HTTPException(400, "要指定来源或订阅")
        repo.log("info", f"删除磁力: {label} {n} 条")
        return back(_with_flash("/resources", f"已删除「{label}」的磁力 {n} 条; 下次爬取会重新抓取"))

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

    @app.get("/releases")
    async def releases_redirect(request: Request) -> RedirectResponse:
        query = [(k, v) for k, v in request.query_params.multi_items()]
        return back("/works?" + urlencode([("origin", "subs"), *query]))

    @app.get("/", response_class=HTMLResponse)
    @app.get("/works", response_class=HTMLResponse)
    async def works(
        request: Request, origin: str = "", sub: str = "", src: str = "", q: str = "", days_: str = Query("", alias="days"),
        todo: str = "1", message: str = "", shown_: str = Query("", alias="n"),
    ) -> HTMLResponse:
        days = _opt_int(days_)
        shown = max(_opt_int(shown_) or _PAGE, _PAGE)
        s = svc(request)
        try:
            sub_ids, source_ids = _int_list(sub), _int_list(src)
        except ValueError as exc:
            raise HTTPException(400, "sub / src 应为逗号分隔的编号") from exc
        tags = [t for t in request.query_params.getlist("tag") if t in TAG_LABELS]
        items = s.works(
            origin=origin if origin in ("subs", "magnet") else "", sub_ids=sub_ids, source_ids=source_ids, tags=tags,
            search=q.strip(), days=days, todo=todo == "1", limit=shown + 1,
        )
        more = len(items) > shown
        items = items[:shown]
        query = [(k, v) for k, v in request.query_params.multi_items() if k not in ("n", "message")]
        more_url = f"{request.url.path}?{urlencode([*query, ('n', shown + _PAGE)])}"
        return page(
            request, "works.html", items=items, subs=s.repo.subscriptions(), sources=s.repo.sources(), origin=origin, sub=sub,
            src=src, tags=tags, q=q, days=days, todo=todo, message=message, more=more, more_url=more_url,
        )

    @app.get("/works/{number}", response_class=HTMLResponse)
    async def work_page(request: Request, number: str) -> HTMLResponse:
        s = svc(request)
        d = await s.work_detail(number)
        if not (d.release or d.magnets or d.others or d.work or d.wanted):
            resp = page(request, "work_missing.html", number=number, amane_error=d.amane_error, has_amane=bool(s.settings.amane_url))
            resp.status_code = 404
            return resp
        sukebei = {r.tid for _, r in d.magnets if r and r.source == "sukebei"} | {r.tid for r in d.others if r.source == "sukebei"}
        return page(
            request, "work.html", d=d, tier=lambda c: tier(c, s.settings), forum=Sehuatang(s.settings.crawl_base_url),
            sukebei=sukebei,
        )

    @app.post("/works/{number}/scrape")
    async def scrape_work(request: Request, number: str) -> RedirectResponse:
        """作品页: 让 Amane 刮削这个番号."""
        message = await svc(request).request_scrape(number)
        return back(_with_flash(f"/works/{quote(number, safe='')}", message))

    @app.post("/works/{action}")
    async def works_action(request: Request, action: str) -> RedirectResponse:
        """挑选: ``queue`` 加入下载队列 (之后磁力选择), ``dismiss`` 忽略, ``undismiss`` 取消忽略."""
        form = await request.form()
        only = str(form.get("only") or "")
        numbers = [only] if only else [str(v) for v in form.getlist("ids")]
        s = svc(request)
        if action == "queue":
            added = s.queue_numbers(numbers)
            if added:
                asyncio.create_task(s.run_guarded("search", s.run_search))
                if "amane" not in s.running:
                    # 队列里的作品优先: 马上查 Amane, 没有的就让 Amane 刮削, 下载完成前元数据就绪.
                    asyncio.create_task(s.run_guarded("amane", s.run_amane))
            message = f"已加入下载队列 {added} 部, 接下来自动磁力选择 (已在队列中的跳过)"
        elif action == "dismiss":
            s.repo.dismiss_numbers(numbers)
            message = f"已忽略 {len(numbers)} 部"
        elif action == "undismiss":
            s.repo.undismiss_numbers(numbers)
            message = f"已取消忽略 {len(numbers)} 部"
        else:
            raise HTTPException(400, "未知操作")
        referer = urlsplit(request.headers.get("referer") or "/works")
        path = referer.path if referer.path == "/" or referer.path.startswith(("/works", "/actresses")) else "/works"
        query = [(k, v) for k, v in parse_qsl(referer.query) if k != "message"] + [("message", message)]
        return back(f"{path}?{urlencode(query)}")

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
        path = referer.path if referer.path.startswith(("/works", "/actresses")) else "/works"
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
        return page(
            request, "actress.html", g=group, items=items, todo=todo, message=message,
            **_shared(s, [r.number for r, *_ in items]),
        )

    @app.get("/img")
    async def image(request: Request, u: str) -> Response:
        try:
            path = await request.app.state.images.get(u, proxy=svc(request).settings.scrape_proxy)
        except ImageError as exc:
            raise HTTPException(404, str(exc)) from exc
        return FileResponse(path, headers={"Cache-Control": "public, max-age=2592000"})

    @app.get("/img/res/{resource_id}/{index}")
    async def resource_image(request: Request, resource_id: int, index: int) -> Response:
        """磁力池里论坛帖子的图片; 只取入库时记下的地址, 带论坛的 Referer (图床不带会 403)."""
        s = svc(request)
        res = s.repo.resource(resource_id)
        if res is None or not 0 <= index < len(res.images):
            raise HTTPException(404)
        try:
            path = await request.app.state.images.get_with_referer(
                res.images[index], s.settings.crawl_base_url.rstrip("/") + "/", proxy=s.settings.scrape_proxy
            )
        except ImageError as exc:
            raise HTTPException(404, str(exc)) from exc
        return FileResponse(path, headers={"Cache-Control": "public, max-age=2592000"})

    @app.get("/play")
    async def play(request: Request, p: str) -> Response:
        """转发 Amane 的播放流 (带 Range, 可拖动进度); 浏览器不用登录 Amane. 只接受作品页列出过的地址."""
        s = svc(request)
        if not s.stream_allowed(p):
            raise HTTPException(404, "先打开作品页")
        amane = s.amane()
        if amane is None:
            raise HTTPException(404, "未接入 Amane")
        headers = {k: v for k, v in request.headers.items() if k.lower() in ("range", "if-range")}
        try:
            upstream = await amane.open_stream(p, headers)
        except AmaneError as exc:
            await amane.aclose()
            raise HTTPException(502, str(exc)) from exc

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_bytes(256 * 1024):
                    yield chunk
            finally:
                await upstream.aclose()
                await amane.aclose()

        keep = ("content-type", "content-length", "content-range", "accept-ranges", "last-modified", "etag")
        return StreamingResponse(
            body(), status_code=upstream.status_code, headers={k: v for k, v in upstream.headers.items() if k.lower() in keep}
        )

    @app.get("/img/amane")
    async def amane_image(request: Request, u: str, w: int | None = None) -> Response:
        amane = svc(request).amane()
        if amane is None:
            raise HTTPException(404, "未接入 Amane")
        width = w if w in _THUMB_WIDTHS else None
        try:
            path = await request.app.state.images.get_amane(u, amane, width=width)
        except ImageError as exc:
            raise HTTPException(404, str(exc)) from exc
        finally:
            await amane.aclose()
        return FileResponse(path, headers={"Cache-Control": "public, max-age=2592000"})

    # --- 手动触发与日志 ---

    @app.post("/run/{job}")
    async def run_job(request: Request, job: str) -> RedirectResponse:
        s = svc(request)
        jobs = {
            "search": s.run_search, "submit": s.run_submit, "tasks": s.run_tasks, "sync": s.run_sync,
            "feeds": s.run_feeds, "crawl": s.run_crawl, "subs": lambda: s.run_subscriptions(force=True),
            "sukebei": s.run_sukebei, "amane": s.run_amane,
        }
        if job not in jobs:
            raise HTTPException(404)
        kind = {"crawl": "sehuatang", "sukebei": "sukebei", "feeds": "rss"}.get(job)
        if kind and not s.repo.sources(kind=kind, enabled_only=True):
            flash = f"没有启用的{SOURCE_KINDS[kind]}磁力订阅, 到「磁力订阅」添加或启用"
        elif job in s.running:
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

    # --- 健康检查 ---

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


def _with_flash(url: str, flash: str) -> str:
    path, _, fragment = url.partition("#")
    parts = urlsplit(path)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in ("flash", "message")] + [("flash", flash)]
    return f"{parts.path}?{urlencode(query)}" + (f"#{fragment}" if fragment else "")


def _words(text: str) -> list[str]:
    return [k.strip() for k in re.split(r"[,，\n]", text) if k.strip()]


#: 只有表单里出现才算改动的秘密字段: 留空表示保持原值, 「清除」勾选框才删除.
_SECRETS = ("javdb_cookie", "amane_token")
#: 按行分隔的列表字段, 其余列表字段按逗号分隔.
_LINE_LISTS = ("sync_paths",)


def _merge_settings(current: Settings, form: Any) -> Settings:
    """表单里出现的字段覆盖当前设置. 勾选框没勾时不提交, 所以表单用 ``_checkboxes`` 列出它包含的勾选框;
    磁力来源的勾选框是 ``site_<名称>``, 表单带 ``_sites`` 时整体替换."""
    data = {k: str(v) for k, v in form.items()}
    updates: dict[str, Any] = {}
    for name, field in Settings.model_fields.items():
        if name in _SECRETS:
            if f"clear_{name}" in data:
                updates[name] = ""
            elif data.get(name):
                updates[name] = data[name].strip()
            continue
        if name not in data:
            continue
        value, annotation = data[name], str(field.annotation)
        if "list[str]" in annotation:
            updates[name] = [x.strip() for x in value.splitlines() if x.strip()] if name in _LINE_LISTS else _words(value)
        elif annotation in ("<class 'float'>", "float"):
            if value.strip():
                updates[name] = float(value)
        elif annotation in ("<class 'int'>", "int"):
            if value.strip():
                updates[name] = int(value)
        elif annotation in ("<class 'bool'>", "bool"):
            updates[name] = True
        elif annotation in ("<class 'str'>", "str"):
            updates[name] = value.strip().rstrip("/") if name.endswith("_url") else value.strip()
    for name in _words(data.get("_checkboxes", "")):
        if name in Settings.model_fields:
            updates[name] = name in data
    if "_sites" in data:
        updates["sites"] = [n for n in SEARCH_SOURCES if f"site_{n}" in data]
    return Settings.model_validate({**current.model_dump(), **updates})


def _source_from_form(form: Any, src: MagnetSource) -> MagnetSource:
    """磁力订阅的新增 / 编辑表单."""
    data = {k: str(v) for k, v in form.items()}
    kind = src.kind
    if kind not in SOURCE_KINDS:
        raise ValueError("未知的订阅类型")
    if kind == "sehuatang":
        fid, typeid = data.get("fid", "").strip(), data.get("typeid", "").strip()
        if not fid.isdigit() or (typeid and not typeid.isdigit()):
            raise ValueError("板块编号与分类编号应为数字")
        params: dict[str, object] = {"fid": int(fid), "typeid": int(typeid) if typeid else None}
        default_name = f"板块 {fid}" + (f":{typeid}" if typeid else "")
    elif kind == "sukebei":
        seeders = data.get("min_seeders", "").strip() or "0"
        if not seeders.isdigit():
            raise ValueError("最少做种应为数字")
        params = {
            "query": data.get("query", "").strip(), "user": data.get("user", "").strip(),
            "category": data.get("category", "").strip() or "2_2", "trusted_only": "trusted_only" in data,
            "min_seeders": int(seeders),
        }
        default_name = params["query"] or params["user"] or "sukebei"
    else:
        url = data.get("url", "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("RSS 地址应以 http:// 或 https:// 开头")
        params = {"url": url}
        default_name = urlsplit(url).hostname or url
    multi = form.getlist if hasattr(form, "getlist") else (lambda k: [])
    extra = [t for t in multi("extra_tags") if t in TAG_LABELS]
    if extra:
        params["extra_tags"] = extra
    flt = SourceFilter(
        tags_all=[t for t in multi("tags_all") if t in TAG_LABELS],
        tags_any=[t for t in multi("tags_any") if t in TAG_LABELS],
        tags_none=[t for t in multi("tags_none") if t in TAG_LABELS],
        brackets=_words(data.get("brackets", "")),
        types=_words(data.get("types", "")),
        include=_words(data.get("include", "")),
        exclude=_words(data.get("exclude", "")),
        numbered="numbered" in data,
    )
    return MagnetSource(
        kind=kind, name=data.get("name", "").strip() or str(default_name), params=params, filter=flt,
        enabled=src.enabled, auto_download="auto_download" in data,
    )


#: 允许的缩略图宽度 (不接受任意值, 免得缓存里堆满各种尺寸).
_THUMB_WIDTHS = (240, 320, 480)

#: 挑选作品每次多显示的数量.
_PAGE = 120


def _opt_int(text: str) -> int | None:
    """可选的数字参数: 表单里「全部」提交的是空串, 当作没给."""
    text = text.strip()
    return int(text) if text.isdigit() else None


def _int_list(text: str) -> list[int]:
    return [int(x) for x in text.replace("，", ",").split(",") if x.strip()]
