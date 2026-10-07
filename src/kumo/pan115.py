"""115 开放平台客户端 (仅官方接口, 文档: https://www.yuque.com/115yun/open).

授权: 手机扫码 PKCE 设备码模式, 不需要 AppSecret. ``access_token`` 有效期约 2 小时,
``refresh_token`` 1 年; 快过期时用 refresh_token 续期. 文档要求「请勿频繁刷新」,
因此只在过期前 5 分钟内或接口明确报授权失效时刷新.

限流: 平台频控策略不公开. 所有资源接口串行调用并保持最小间隔; 返回失败时抛 ``Pan115Error``,
由调度层退避, 不在这里重试.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from .db import Repo
from .models import info_hash_of

PASSPORT = "https://passportapi.115.com"
QRCODE = "https://qrcodeapi.115.com"
API = "https://proapi.115.com"
_TOKEN_KEY = "pan115_token"
_MIN_INTERVAL = 1.0
_REFRESH_MARGIN = 300


class Pan115Error(Exception):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class NotAuthorized(Pan115Error):
    pass


@dataclass
class OfflineTask:
    info_hash: str
    name: str
    status: int
    """-1 失败, 0 等待, 1 下载中, 2 成功."""
    percent: float
    file_id: str
    size: int


@dataclass
class RemoteFile:
    fid: str
    name: str
    is_dir: bool
    size: int
    pick_code: str


@dataclass
class AddResult:
    url: str
    ok: bool
    info_hash: str
    message: str


@dataclass
class DeviceAuth:
    uid: str
    time: int
    sign: str
    qrcode: str
    verifier: str


class Pan115(ABC):
    """调度层依赖的最小接口; 真实实现与测试用的内存实现都遵守它."""

    @abstractmethod
    def authorized(self) -> bool: ...

    @abstractmethod
    async def folder_id(self, path: str) -> str: ...

    @abstractmethod
    def walk(self, cid: str) -> AsyncIterator[RemoteFile]: ...

    @abstractmethod
    async def add_urls(self, urls: list[str], folder_id: str) -> list[AddResult]: ...

    @abstractmethod
    async def tasks(self, page: int = 1) -> tuple[list[OfflineTask], int]:
        """返回 (任务, 总页数)."""
        ...

    @abstractmethod
    async def quota(self) -> dict[str, int]: ...


class OpenClient(Pan115):
    def __init__(self, app_id: str, repo: Repo) -> None:
        self._app_id = app_id
        self._repo = repo
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self._token: dict[str, Any] | None = None
        raw = repo.get_kv(_TOKEN_KEY)
        if raw:
            self._token = json.loads(raw)

    async def aclose(self) -> None:
        await self._http.aclose()

    def authorized(self) -> bool:
        return bool(self._token and self._token.get("refresh_token"))

    def logout(self) -> None:
        self._token = None
        self._repo.set_kv(_TOKEN_KEY, None)

    # --- 授权 ---

    async def start_device_auth(self) -> DeviceAuth:
        if not self._app_id:
            raise Pan115Error("未填写 AppID")
        verifier = secrets.token_urlsafe(48)[:64]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        resp = await self._http.post(
            f"{PASSPORT}/open/authDeviceCode",
            data={"client_id": self._app_id, "code_challenge": challenge, "code_challenge_method": "sha256"},
        )
        data = _payload(resp)
        return DeviceAuth(uid=data["uid"], time=int(data["time"]), sign=data["sign"], qrcode=data["qrcode"], verifier=verifier)

    async def poll_device_auth(self, auth: DeviceAuth) -> int:
        """长轮询二维码状态: 返回 0 未扫码, 1 已扫码待确认, 2 已确认; 二维码失效抛 ``Pan115Error``."""
        try:
            resp = await self._http.get(
                f"{QRCODE}/get/status/",
                params={"uid": auth.uid, "time": auth.time, "sign": auth.sign},
                timeout=httpx.Timeout(40.0),
            )
        except httpx.TimeoutException:
            return 0
        body = resp.json()
        if body.get("state") == 0:
            raise Pan115Error(body.get("message") or "二维码已失效")
        return int((body.get("data") or {}).get("status") or 0)

    async def finish_device_auth(self, auth: DeviceAuth) -> None:
        resp = await self._http.post(
            f"{PASSPORT}/open/deviceCodeToToken", data={"uid": auth.uid, "code_verifier": auth.verifier}
        )
        self._store_token(_payload(resp))

    async def _refresh(self) -> None:
        if not self._token:
            raise NotAuthorized("115 未授权")
        resp = await self._http.post(
            f"{PASSPORT}/open/refreshToken", data={"refresh_token": self._token["refresh_token"]}
        )
        try:
            self._store_token(_payload(resp))
        except Pan115Error as exc:
            # refresh_token 失效 (过期或被撤销) 只能重新扫码.
            self.logout()
            raise NotAuthorized(f"授权已失效, 请重新扫码: {exc}") from exc

    def _store_token(self, data: dict[str, Any]) -> None:
        self._token = {
            "access_token": data["access_token"],
            "refresh_token": data["refresh_token"],
            "expires_at": time.time() + int(data.get("expires_in") or 7200),
        }
        self._repo.set_kv(_TOKEN_KEY, json.dumps(self._token))

    # --- 资源接口 ---

    async def _call(self, method: str, path: str, *, params: dict[str, Any] | None = None, data: dict[str, Any] | None = None) -> Any:
        if not self._token:
            raise NotAuthorized("115 未授权")
        async with self._lock:
            if self._token["expires_at"] - time.time() < _REFRESH_MARGIN:
                await self._refresh()
            for attempt in range(2):
                wait = _MIN_INTERVAL - (time.monotonic() - self._last_call)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_call = time.monotonic()
                try:
                    resp = await self._http.request(
                        method,
                        f"{API}{path}",
                        params=params,
                        data=data,
                        headers={"Authorization": f"Bearer {self._token['access_token']}"},
                    )
                except httpx.HTTPError as exc:
                    raise Pan115Error(f"115 接口无法访问: {type(exc).__name__}") from exc
                if attempt == 0 and _looks_unauthorized(resp):
                    await self._refresh()
                    continue
                return _payload(resp)
        raise Pan115Error("115 授权刷新后仍被拒绝")

    async def folder_id(self, path: str) -> str:
        path = "/" + path.strip().strip("/")
        if path == "/":
            return "0"
        data = await self._call("POST", "/open/folder/get_info", data={"path": path})
        if str(data.get("file_category")) != "0":
            raise Pan115Error(f"{path} 不是文件夹")
        return str(data["file_id"])

    async def walk(self, cid: str) -> AsyncIterator[RemoteFile]:
        """深度优先列出目录下全部文件 (不含文件夹本身)."""
        stack = [cid]
        while stack:
            current = stack.pop()
            offset = 0
            while True:
                body = await self._call_raw_list(current, offset)
                items = body.get("data") or []
                for it in items:
                    if str(it.get("fc")) == "0":
                        stack.append(str(it["fid"]))
                    else:
                        yield RemoteFile(
                            fid=str(it["fid"]),
                            name=it.get("fn") or "",
                            is_dir=False,
                            size=int(it.get("fs") or 0),
                            pick_code=it.get("pc") or "",
                        )
                offset += len(items)
                if not items or offset >= int(body.get("count") or 0):
                    break

    async def _call_raw_list(self, cid: str, offset: int) -> dict[str, Any]:
        # 文件列表的分页信息 (count) 在顶层而不在 data 里, 因此取整个响应体.
        params = {"cid": cid, "limit": 1150, "offset": offset, "show_dir": 1, "cur": 1}
        return await self._call("GET", "/open/ufile/files", params=params)

    async def add_urls(self, urls: list[str], folder_id: str) -> list[AddResult]:
        data = await self._call("POST", "/open/offline/add_task_urls", data={"urls": "\n".join(urls), "wp_path_id": folder_id})
        results = data if isinstance(data, list) else []
        return [
            AddResult(
                url=r.get("url") or "",
                ok=bool(r.get("state")),
                info_hash=(r.get("info_hash") or "").lower(),
                message=r.get("message") or "",
            )
            for r in results
        ]

    async def tasks(self, page: int = 1) -> tuple[list[OfflineTask], int]:
        data = await self._call("GET", "/open/offline/get_task_list", params={"page": page})
        tasks = [
            OfflineTask(
                info_hash=(t.get("info_hash") or "").lower(),
                name=t.get("name") or "",
                status=int(t.get("status") or 0),
                percent=float(t.get("percentDone") or 0),
                file_id=str(t.get("file_id") or ""),
                size=int(t.get("size") or 0),
            )
            for t in (data.get("tasks") or [])
        ]
        return tasks, int(data.get("page_count") or 1)

    async def quota(self) -> dict[str, int]:
        data = await self._call("GET", "/open/offline/get_quota_info")
        return {k: int(data.get(k) or 0) for k in ("count", "used", "surplus")}


def _looks_unauthorized(resp: httpx.Response) -> bool:
    if resp.status_code == 401:
        return True
    try:
        body = resp.json()
    except ValueError:
        return False
    if body.get("state") in (True, 1):
        return False
    message = f"{body.get('message', '')}{body.get('error', '')}".lower()
    return "token" in message or "授权" in message or "过期" in message


def _payload(resp: httpx.Response) -> Any:
    """取出 ``data``; 失败时抛出带平台消息的 ``Pan115Error``.

    文件列表接口把分页字段放在顶层, 因此当 ``data`` 是列表且顶层带 ``count`` 时返回整个响应体.
    """
    try:
        body = resp.json()
    except ValueError as exc:
        raise Pan115Error(f"115 返回了非 JSON 内容 (HTTP {resp.status_code})") from exc
    if body.get("state") in (False, 0) or (body.get("code") not in (None, 0) and body.get("state") is not True):
        message = body.get("message") or body.get("error") or f"HTTP {resp.status_code}"
        raise Pan115Error(f"115: {message}", code=body.get("code") or body.get("errno"))
    if isinstance(body.get("data"), list) and "count" in body:
        return body
    return body.get("data")


class FakePan115(Pan115):
    """内存实现: 拿到 AppID 之前用于本地联调与测试."""

    def __init__(self) -> None:
        self.files: dict[str, list[RemoteFile]] = {"0": []}
        self.folders: dict[str, str] = {"/": "0"}
        self.task_list: dict[str, OfflineTask] = {}
        self.added: list[tuple[str, str]] = []

    def authorized(self) -> bool:
        return True

    def mkdir(self, path: str, files: list[str] | None = None) -> str:
        cid = str(1000 + len(self.folders))
        self.folders[path] = cid
        self.files[cid] = [RemoteFile(fid=f"{cid}-{i}", name=n, is_dir=False, size=2 * 1024**3, pick_code=f"pc{cid}{i}") for i, n in enumerate(files or [])]
        return cid

    async def folder_id(self, path: str) -> str:
        path = "/" + path.strip().strip("/") if path.strip("/") else "/"
        if path not in self.folders:
            raise Pan115Error(f"{path} 不存在")
        return self.folders[path]

    async def walk(self, cid: str) -> AsyncIterator[RemoteFile]:
        for f in self.files.get(cid, []):
            yield f

    async def add_urls(self, urls: list[str], folder_id: str) -> list[AddResult]:
        out = []
        for url in urls:
            h = info_hash_of(url) or ""
            self.added.append((url, folder_id))
            if h in self.task_list:
                out.append(AddResult(url=url, ok=False, info_hash="", message="任务已存在"))
                continue
            self.task_list[h] = OfflineTask(info_hash=h, name=url[:40], status=0, percent=0, file_id="", size=0)
            out.append(AddResult(url=url, ok=True, info_hash=h, message=""))
        return out

    async def tasks(self, page: int = 1) -> tuple[list[OfflineTask], int]:
        return list(self.task_list.values()), 1

    async def quota(self) -> dict[str, int]:
        return {"count": 1500, "used": len(self.added), "surplus": 1500 - len(self.added)}
