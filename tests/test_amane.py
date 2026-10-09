"""Amane 接入: 按番号 / 名字读取作品与演员, 缓存后填充页面; 只读."""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from kumo.amane import Amane, AmaneError
from kumo.images import ImageCache
from kumo.models import AmaneActor, MagnetSource, Release, Resource, Subscription, Work
from kumo.service import Service
from kumo.settings import Env, Settings
from kumo.web import create_app

METADATA = {
    "items": [
        # 模糊搜索会带出别的番号, 只取完全一致的
        {"id": 7, "number": "ABC-0011", "title": "别的", "poster_urls": [], "thumb_urls": []},
        {
            "id": 8, "number": "abc-001", "title": "标题", "release": "2026-09-01", "actors": ["演员甲", "演员乙"],
            "studio": "片商", "poster_urls": ["/api/resources/p1"], "thumb_urls": ["https://img.example/t.jpg"], "file_count": 2,
        },
    ],
    "total": 2,
}
ACTORS = {"items": [{"id": 3, "name": "演员 甲", "image_urls": ["/api/resources/a1"], "height": 158, "cup": "E", "birthday": "2000-01-02"}]}


class FakeAmane:
    """模拟 Amane 的 HTTP 接口, 记录请求."""

    def __init__(self, token: str = "tk") -> None:
        self.token = token
        self.calls: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.token and request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"detail": "缺少或无效的 API token."})
        path = request.url.path
        if path == "/api/metadata":
            q = request.url.params.get("search", "").casefold()
            items = [i for i in METADATA["items"] if q in i["number"].casefold()]
            return httpx.Response(200, json={"items": items, "total": len(items)})
        if path == "/api/metadata/8":
            return httpx.Response(200, json={
                "metadata": {**METADATA["items"][1], "plot": "简介文字", "series": "某系列", "directors": ["某导演"],
                             "tags": ["单体作品", "4K"], "runtime": 97, "score": 4.46,
                             "extrafanart_urls": {"dmm": ["https://img.example/f1.jpg", "https://img.example/f2.jpg"]},
                             "trailer_urls": ["https://img.example/t.mp4"]},
                "files": [{"id": 1}], "actor_ids": {"演员甲": 3, "演员乙": 4},
            })
        if path == "/api/actors":
            return httpx.Response(200, json=ACTORS)
        if path.startswith("/api/resources"):
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"\xff\xd8" + path.encode())
        return httpx.Response(404)

    def client(self, token: str = "tk") -> Amane:
        return Amane("http://amane:8000/", token, transport=httpx.MockTransport(self.handler))


@pytest.fixture
def amane(service: Service, monkeypatch: pytest.MonkeyPatch) -> FakeAmane:
    fake = FakeAmane()
    service.repo.save_settings(Settings(app_id="test", download_path="/云下载", amane_url="http://amane:8000", amane_token="tk"))
    monkeypatch.setattr(Service, "amane", lambda self: fake.client() if self.settings.amane_url else None)
    return fake


async def test_work_matches_exact_number() -> None:
    fake = FakeAmane()
    client = fake.client()
    w = await client.work("ABC-001")
    assert w.found and w.amane_id == 8 and w.title == "标题" and w.actors == ["演员甲", "演员乙"]
    assert w.poster == "/api/resources/p1" and w.thumb == "https://img.example/t.jpg" and w.file_count == 2 and w.date == "2026-09-01"
    assert not (await client.work("XYZ-999")).found
    assert fake.calls[0].headers["authorization"] == "Bearer tk"


async def test_actor_matches_name_ignoring_spaces() -> None:
    fake = FakeAmane()
    a = await fake.client().actor("演员 甲")
    assert fake.calls[0].url.params["search"] == "演员甲"
    assert a.found and a.amane_id == 3 and a.image == "/api/resources/a1"
    assert a.profile == {"生日": "2000-01-02", "身高": "158cm", "罩杯": "E"}


async def test_bad_token_is_reported() -> None:
    with pytest.raises(AmaneError, match="token"):
        await FakeAmane().client(token="wrong").work("ABC-001")


async def test_image_goes_through_amane() -> None:
    client = FakeAmane().client()
    assert client.image_request("/api/resources/p1") == ("/api/resources/p1", {})
    assert client.image_request("https://img.example/t.jpg") == ("/api/resources/proxy", {"url": "https://img.example/t.jpg"})


async def test_image_cache_for_amane(tmp_path: Path) -> None:
    fake = FakeAmane()
    cache = ImageCache(tmp_path)
    path = await cache.get_amane("https://img.example/t.jpg", fake.client())
    assert path.read_bytes() == b"\xff\xd8/api/resources/proxy"
    assert fake.calls[0].url.params["url"] == "https://img.example/t.jpg"
    assert await cache.get_amane("https://img.example/t.jpg", fake.client()) == path
    assert len(fake.calls) == 1  # 第二次走磁盘缓存


async def test_run_amane_caches_works_and_actors(service: Service, amane: FakeAmane) -> None:
    sub = service.repo.add_subscription(Subscription(source="javbus", kind="star", ident="1", uncensored=False, name="演员甲"))
    assert sub.id
    service.repo.add_release(Release(source="javbus", number="ABC-001", title="t", date="2026-09-01", cover=None, url="u"), sub.id)
    service.repo.add_resource(
        Resource(source="sukebei", tid=1, forum_id=0, forum_name="sukebei", type_name="r", title="XYZ-999", number="XYZ-999",
                 magnet="magnet:?xt=urn:btih:" + "a" * 40, info_hash="a" * 40)
    )
    service.add_numbers(["QQQ-100"])
    await service.run_amane()
    # 下载队列先查, 然后是磁力池, 最后是作品库
    numbers = [c.url.params["search"] for c in amane.calls if c.url.path == "/api/metadata"]
    assert numbers == ["QQQ-100", "XYZ-999", "ABC-001"]
    assert set(service.repo.works(["ABC-001", "XYZ-999", "QQQ-100"])) == {"ABC-001"}  # 没找到的也记下, 但不当作品返回

    group = service.actress_group(sub.id)
    assert group and group.amane and group.amane.found
    assert group.photo == "/img/amane?u=%2Fapi%2Fresources%2Fa1"
    assert group.full_profile["身高"] == "158cm"

    amane.calls.clear()
    await service.run_amane()
    assert amane.calls == []  # 刷新周期内不重复查询


async def test_run_amane_refreshes_old_entries(service: Service, amane: FakeAmane) -> None:
    service.add_numbers(["ABC-001"])
    service.repo.save_work(Work(number="ABC-001", found=False))
    service.repo._conn.execute("UPDATE amane_work SET checked_at = ?", (time.time() - 30 * 86400,))
    await service.run_amane()
    assert service.repo.works(["ABC-001"])["ABC-001"].title == "标题"


async def test_run_amane_without_config_does_nothing(service: Service) -> None:
    await service.run_amane()


async def test_run_amane_error_is_reported(service: Service, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeAmane()
    service.repo.save_settings(Settings(app_id="test", amane_url="http://amane:8000"))
    monkeypatch.setattr(Service, "amane", lambda self: fake.client(token="wrong"))
    service.add_numbers(["ABC-001"])
    await service.run_guarded("amane", service.run_amane)
    assert "token" in service.last_error["amane"]


def test_actress_photo_prefers_amane(service: Service) -> None:
    sub = service.repo.add_subscription(
        Subscription(source="javbus", kind="star", ident="1", uncensored=False, name="演员甲", avatar="https://www.javbus.com/a.jpg",
                     profile={"身高": "160cm"})
    )
    assert sub.id
    service.repo.save_amane_actor(AmaneActor(name="演员甲", found=True, image="/api/resources/a1", profile={"身高": "158cm", "罩杯": "E"}))
    g = service.actress_group(sub.id)
    # 头像 Amane 的优先 (资料站的只有 100~125 像素); 资料以资料站为准, Amane 补缺
    assert g and g.photo == "/img/amane?u=%2Fapi%2Fresources%2Fa1" and g.full_profile == {"身高": "160cm", "罩杯": "E"}
    service.repo.save_amane_actor(AmaneActor(name="演员甲", found=False))
    g = service.actress_group(sub.id)
    assert g and g.photo == "/img?u=https%3A%2F%2Fwww.javbus.com%2Fa.jpg"


def test_pages_use_amane_content(service: Service, amane: FakeAmane, tmp_path: Path) -> None:
    service.add_numbers(["ABC-001"])
    service.repo.save_work(
        Work(number="ABC-001", found=True, title="Amane 标题", actors=["演员甲"], thumb="/api/resources/t1", amane_id=8, file_count=1)
    )
    w = service.repo.wanted_by_number("ABC-001")
    assert w
    with TestClient(create_app(Env(data_dir=tmp_path, password=""), service=service)) as c:
        assert "Amane 标题" in c.get("/queue").text
        detail = c.get(f"/wanted/{w.id}").text
        assert "/img/amane?u=%2Fapi%2Fresources%2Ft1" in detail and "http://amane:8000/meta/8" in detail
        img = c.get("/img/amane", params={"u": "/api/resources/t1"})
        assert img.status_code == 200 and img.content == b"\xff\xd8/api/resources/t1"

        form = {"amane_url": "http://amane:8000/", "amane_token": "", "amane_refresh_days": "3"}
        assert "已保存" in c.post("/settings", data=form).text
        s = service.repo.settings()
        assert s.amane_url == "http://amane:8000" and s.amane_token == "tk" and s.amane_refresh_days == 3  # token 留空保持
        c.post("/settings", data={**form, "clear_amane_token": "on"})
        assert service.repo.settings().amane_token == ""


def test_amane_image_without_config(service: Service, tmp_path: Path) -> None:
    with TestClient(create_app(Env(data_dir=tmp_path, password=""), service=service)) as c:
        assert c.get("/img/amane", params={"u": "/api/resources/x"}).status_code == 404


def test_settings_amane_test_button(service: Service, amane: FakeAmane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(create_app(Env(data_dir=tmp_path, password=""), service=service)) as c:
        page = c.post("/settings/amane-test").text
        assert "Amane 连接正常, 媒体库里有" in page
        monkeypatch.setattr(Service, "amane", lambda self: amane.client(token="wrong"))
        assert "Amane 连接失败" in c.post("/settings/amane-test").text and "token" in c.post("/settings/amane-test").text
        monkeypatch.setattr(Service, "amane", lambda self: None)
        assert "还没有填写 Amane 地址" in c.post("/settings/amane-test").text


def test_work_page_with_amane(service: Service, amane: FakeAmane, tmp_path: Path) -> None:
    sub = service.repo.add_subscription(Subscription(source="javbus", kind="star", ident="1", uncensored=False, name="演员甲"))
    assert sub.id
    service.repo.add_release(Release(source="javbus", number="ABC-001", title="作品库标题", date="2026-09-01", cover=None,
                                     url="https://www.javbus.com/ABC-001"), sub.id)
    src = service.repo.add_source(MagnetSource(kind="sehuatang", name="高清中字", params={"fid": 103}))
    service.repo.add_resource(Resource(source="sehuatang", tid=555, forum_id=103, forum_name="高清中文字幕", type_name="",
                                       title="ABC-001 [新模型无码破解]", number="ABC-001", magnet="magnet:?xt=urn:btih:" + "a" * 40,
                                       info_hash="a" * 40, size=5 * 1024**3, posted="2026-10-07 06:00:00", source_id=src.id,
                                       images=["https://img.example/p.jpg"]))
    with TestClient(create_app(Env(data_dir=tmp_path, password=""), service=service)) as c:
        page = c.get("/works/ABC-001").text  # 没缓存过: 现查 Amane
        assert "标题" in page and "简介文字" in page and "某系列" in page and "某导演" in page and "97 分钟" in page
        assert f'href="/actresses/{sub.id}"' in page  # 订阅了的演员链到女优页
        assert "http://amane:8000/actors/4" in page  # 没订阅的链到 Amane
        assert "/img/amane?u=https%3A%2F%2Fimg.example%2Ff1.jpg" in page  # 剧照来自 Amane
        assert "/img/amane?u=https%3A%2F%2Fimg.example%2Ft.jpg" in page  # 主图用 Amane 的高清封面
        assert "https://www.sehuatang.net/thread-555-1-1.html" in page and "破解" in page and "2026-10-07" in page
        assert "http://amane:8000/meta/8" in page and "https://www.javbus.com/ABC-001" in page
        assert service.repo.works(["ABC-001"])["ABC-001"].amane_id == 8
        calls = len(amane.calls)
        c.get("/works/ABC-001")
        assert len([x for x in amane.calls[calls:] if x.url.path == "/api/metadata/8"]) == 0  # 详情缓存 10 分钟
        missing = c.get("/works/NOPE-999")
        assert missing.status_code == 404 and "都没有这个番号的记录" in missing.text and "加入下载队列" in missing.text
        assert 'href="/works/ABC-001"' in c.get("/").text


def test_work_page_without_amane(service: Service, tmp_path: Path) -> None:
    service.repo.add_resource(Resource(source="sehuatang", tid=556, forum_id=103, forum_name="", type_name="",
                                       title="XYZ-002 中文字幕", number="XYZ-002", magnet="magnet:?xt=urn:btih:" + "b" * 40,
                                       info_hash="b" * 40, images=["https://img.example/p.jpg", "https://img.example/q.jpg"]))
    with TestClient(create_app(Env(data_dir=tmp_path, password=""), service=service)) as c:
        page = c.get("/works/XYZ-002").text
        assert "XYZ-002 中文字幕" in page and "来自 98堂帖子" in page and "/img/res/1/1" in page and "加入下载队列" in page
