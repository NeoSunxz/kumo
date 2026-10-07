from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from kumo import images as images_module
from kumo import service as service_module
from kumo.db import Repo
from kumo.images import ImageCache, ImageError, referer_for
from kumo.models import Release, Subscription
from kumo.service import Service
from kumo.settings import Env
from kumo.subscriptions import ListPage, SubscriptionKey
from kumo.web import create_app

TODAY = date.today()


def day(offset: int) -> str:
    return (TODAY + timedelta(days=offset)).isoformat()


def star(
    service: Service, ident: str, name: str, source: str = "javbus", avatar: str | None = None, profile: dict[str, str] | None = None
) -> Subscription:
    sub = service.repo.add_subscription(
        Subscription(source=source, kind="star", ident=ident, uncensored=False, name=name, avatar=avatar, profile=profile or {})
    )
    assert sub.id
    return sub


def rel(service: Service, sub: Subscription, number: str, when: str | None) -> Release:
    assert sub.id
    r, _ = service.repo.add_release(
        Release(
            source=sub.source, number=number, title=f"{number} 标题", date=when,
            cover=f"https://www.javbus.com/pics/thumb/{number}.jpg", url=f"https://www.javbus.com/{number}",
        ),
        sub.id,
    )
    return r


def test_actress_groups_merge_same_name_across_sites(service: Service) -> None:
    jb = star(service, "1248", "春陽モカ", avatar="https://www.javbus.com/pics/actress/1248_a.jpg", profile={"身高": "155cm"})
    jd = star(service, "B85e9", "春陽 モカ", source="javdb", avatar="https://c0.jdbstatic.com/avatars/b8/B85e9.jpg", profile={"作品数": "290", "身高": "156cm"})
    other = star(service, "9", "别的女优")
    studio = service.repo.add_subscription(Subscription(source="javbus", kind="studio", ident="4v", uncensored=False, name="ムーディーズ"))
    rel(service, jb, "AAA-001", day(-100))
    rel(service, jb, "AAA-002", day(-3))
    rel(service, jd, "AAA-002", day(-3))  # 两站都有, 只算一部
    rel(service, jd, "AAA-003", day(+20))  # 待发行
    rel(service, other, "BBB-001", day(-400))
    assert studio.id
    service.add_numbers(["AAA-001"])

    groups = service.actress_groups()
    assert [g.name for g in groups] == ["春陽モカ", "别的女优"]  # 女优之外的订阅不出现, 有新作的在前
    g = groups[0]
    assert sorted(s.source for s in g.subs) == ["javbus", "javdb"]
    assert g.total == 3 and g.recent == 1 and g.upcoming == 1 and g.queued == 1
    assert g.latest and g.latest.number == "AAA-003"
    assert g.avatar == "https://www.javbus.com/pics/actress/1248_a.jpg"
    assert g.profile == {"身高": "155cm", "作品数": "290"}  # 先到的为准, 只补缺
    assert service.actress_group(jd.id) == g  # type: ignore[arg-type]
    assert service.actress_group(studio.id) is None
    assert service.actress_group(99999) is None


async def test_check_refreshes_avatar(service: Service, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fetch(fetcher: object, key: SubscriptionKey, page: int = 1) -> ListPage:
        return ListPage(name="新名字", items=[], more=False, avatar="https://www.javbus.com/new.jpg", profile={"生日": "2000-01-01"})

    monkeypatch.setattr(service_module, "fetch_list", fetch)
    sub = await service.add_subscription("https://www.javbus.com/star/1248")
    assert sub.avatar == "https://www.javbus.com/new.jpg" and sub.profile == {"生日": "2000-01-01"}
    service.repo.update_subscription(sub.id, avatar=None, profile={})  # type: ignore[arg-type]
    await service.check_subscription(service.repo.subscription(sub.id))  # type: ignore[arg-type]
    again = service.repo.subscription(sub.id)  # type: ignore[arg-type]
    assert again and again.avatar == "https://www.javbus.com/new.jpg" and again.profile == {"生日": "2000-01-01"}


def test_actress_pages(service: Service, tmp_path: Path) -> None:
    jb = star(service, "1248", "春陽モカ", avatar="https://www.javbus.com/pics/actress/1248_a.jpg")
    rel(service, jb, "AAA-001", day(-1))
    rel(service, jb, "AAA-009", day(+9))
    service.add_numbers(["AAA-001"])
    with TestClient(create_app(Env(data_dir=tmp_path, password=""), service=service)) as c:
        wall = c.get("/actresses").text
        assert "春陽モカ" in wall and f"/actresses/{jb.id}" in wall
        assert "/img?u=https%3A%2F%2Fwww.javbus.com%2Fpics%2Factress%2F1248_a.jpg" in wall
        assert "春陽モカ" not in c.get("/actresses?q=别人").text

        detail = c.get(f"/actresses/{jb.id}").text
        assert "AAA-001" in detail and "AAA-009" in detail and "待发行" in detail
        assert f"rss/releases.xml?sub={jb.id}" in detail
        todo = c.get(f"/actresses/{jb.id}?todo=1").text
        assert "AAA-009" in todo and "AAA-001" not in todo
        assert c.get("/actresses/99999").status_code == 404

        ids = [r.id for r, *_ in service.repo.releases()]
        r = c.post(
            "/releases/queue", data={"ids": [str(i) for i in ids]},
            headers={"referer": f"http://testserver/actresses/{jb.id}?todo=1"}, follow_redirects=False,
        )
        assert r.headers["location"].startswith(f"/actresses/{jb.id}?todo=1&message=")
        r = c.post("/releases/queue", data={"ids": []}, headers={"referer": "http://evil.example/x"}, follow_redirects=False)
        assert r.headers["location"].startswith("/releases?")


@pytest.mark.parametrize(
    ("url", "referer"),
    [
        ("https://www.javbus.com/pics/thumb/a.jpg", "https://www.javbus.com/"),
        ("https://c0.jdbstatic.com/covers/a.jpg", "https://javdb.com/"),
        ("https://javdb.com/x.jpg", "https://javdb.com/"),
        ("https://evil.example/a.jpg", None),
        ("https://javbus.com.evil.example/a.jpg", None),
        ("file:///etc/passwd", None),
        ("http://192.168.100.110:8000/api", None),
    ],
)
def test_image_proxy_whitelist(url: str, referer: str | None) -> None:
    assert referer_for(url) == referer


async def test_image_cache_fetches_once_with_referer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.headers))
        if "bad" in request.url.path:
            return httpx.Response(403, headers={"content-type": "text/html"}, content=b"no")
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"\xff\xd8jpeg")

    real = httpx.AsyncClient
    monkeypatch.setattr(images_module.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler)))
    cache = ImageCache(tmp_path / "img")
    path = await cache.get("https://www.javbus.com/pics/thumb/a.jpg")
    assert path.suffix == ".jpg" and path.read_bytes() == b"\xff\xd8jpeg"
    assert calls[0]["referer"] == "https://www.javbus.com/"
    assert await cache.get("https://www.javbus.com/pics/thumb/a.jpg") == path
    assert len(calls) == 1  # 第二次走磁盘缓存
    with pytest.raises(ImageError):
        await cache.get("https://www.javbus.com/pics/bad.jpg")
    with pytest.raises(ImageError):
        await cache.get("https://evil.example/a.jpg")
    assert len(calls) == 2  # 白名单外的地址不发请求


def test_image_route_rejects_non_whitelisted(service: Service, tmp_path: Path) -> None:
    with TestClient(create_app(Env(data_dir=tmp_path, password=""), service=service)) as c:
        assert c.get("/img?u=https%3A%2F%2Fevil.example%2Fa.jpg").status_code == 404
        assert c.get("/img").status_code == 422


def test_old_database_gets_avatar_columns(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE subscription (id INTEGER PRIMARY KEY, source TEXT NOT NULL, kind TEXT NOT NULL, ident TEXT NOT NULL,"
        " uncensored INTEGER NOT NULL DEFAULT 0, name TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,"
        " auto_search INTEGER NOT NULL DEFAULT 0, checked_at REAL, last_error TEXT NOT NULL DEFAULT '',"
        " release_count INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, UNIQUE (source, kind, ident, uncensored))"
    )
    conn.execute("INSERT INTO subscription(source, kind, ident, name, created_at) VALUES('javbus', 'star', '1', 'A', 0)")
    conn.commit()
    conn.close()
    (old,) = Repo(db).subscriptions()
    assert old.name == "A" and old.avatar is None and old.profile == {}
