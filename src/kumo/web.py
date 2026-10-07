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

import segno
from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from . import __version__
from .db import Repo
from .feeds import render_rss
from .magnets import SITES
from .models import STATUS_LABELS, TAG_LABELS, WantedStatus, format_size
from .numbers import parse_input
from .pan115 import DeviceAuth, Pan115Error
from .service import Service
from .settings import Env, InputFeed, Settings

_TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
_TEMPLATES.env.filters["size"] = format_size
_TEMPLATES.env.filters["ts"] = lambda t: datetime.fromtimestamp(t).strftime("%m-%d %H:%M") if t else "-"
_TEMPLATES.env.globals.update(STATUS_LABELS=STATUS_LABELS, TAG_LABELS=TAG_LABELS, version=__version__)


def create_app(env: Env | None = None, service: Service | None = None) -> FastAPI:
    env = env or Env.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        svc = service or Service(Repo(env.data_dir / "kumo.db"))
        app.state.service = svc
        app.state.device_auth = None
        if service is None:
            svc.start()
        yield
        await svc.stop()

    app = FastAPI(title="kumo", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def basic_auth(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if not env.password or request.url.path == "/health":
            return await call_next(request)
        if request.url.path == "/rss.xml" and secrets.compare_digest(request.query_params.get("key", ""), env.password):
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
        return page(request, "settings.html", s=svc(request).settings, sites=list(SITES), message="")

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
        try:
            settings = Settings(
                app_id=data.get("app_id", ""),
                download_path=data.get("download_path", ""),
                sync_paths=lines("sync_paths"),
                sync_interval_hours=float(data.get("sync_interval_hours") or 6),
                sites=[name for name in SITES if data.get(f"site_{name}")],
                scrape_proxy=data.get("scrape_proxy", ""),
                flaresolverr_url=data.get("flaresolverr_url", ""),
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
            )
        except (ValidationError, ValueError) as exc:
            return page(request, "settings.html", s=svc(request).settings, sites=list(SITES), message=f"保存失败: {exc}")
        svc(request).repo.save_settings(settings)
        return page(request, "settings.html", s=settings, sites=list(SITES), message="已保存")

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

    # --- 手动触发与日志 ---

    @app.post("/run/{job}")
    async def run_job(request: Request, job: str) -> RedirectResponse:
        s = svc(request)
        jobs = {"search": s.run_search, "submit": s.run_submit, "tasks": s.run_tasks, "sync": s.run_sync, "feeds": s.run_feeds}
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

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
