from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import fixture_text
from kumo import service as service_module
from kumo.magnets import JavBus, SiteError, is_javbus_verify, javbus_get
from kumo.models import Release, Subscription, WantedStatus
from kumo.service import Service
from kumo.settings import Env, Settings
from kumo.subscriptions import ListedRelease, ListPage, SubscriptionKey, parse_list, parse_subscription_url
from kumo.web import create_app

# --- 解析 ---


def test_parse_star_list() -> None:
    page = parse_list(fixture_text("javbus_star_1248.html"))
    items = page.items
    assert page.name == "春陽モカ" and page.more is True and len(items) == 30
    assert page.avatar == "https://www.javbus.com/pics/actress/1248_a.jpg"
    assert page.profile == {}  # 这位女优在 JavBus 上没有资料
    first = items[0]
    assert first.number == "VRKM-1902" and first.date == "2026-10-01"
    assert first.url == "https://www.javbus.com/VRKM-1902"
    assert first.cover == "https://www.javbus.com/pics/thumb/ckro.jpg"
    assert first.title.startswith("【VR】")
    dates = [i.date for i in items if i.date]
    assert dates == sorted(dates, reverse=True)  # 新作在前


def test_parse_studio_list() -> None:
    page = parse_list(fixture_text("javbus_studio_4v.html"))
    assert page.name == "ムーディーズ" and len(page.items) == 30 and page.items[0].number == "MDVR-444"
    assert page.avatar is None


def test_parse_list_of_unrelated_page() -> None:
    assert parse_list("<html><title>x</title></html>") == ListPage(name="", items=[], more=False)


def test_parse_javbus_profile() -> None:
    text = (
        '<title>某女优 - 女優 - 影片 - JavBus</title><div class="avatar-box"><div class="photo-frame">'
        '<img src="/pics/actress/abc_a.jpg" title="某女优"></div><div class="photo-info"><span class="pb10">某女优</span>'
        "<p>生日: 2000-01-01</p><p>身高: 160cm</p><p>罩杯: </p></div></div>"
    )
    page = parse_list(text)
    assert page.avatar == "https://www.javbus.com/pics/actress/abc_a.jpg"
    assert page.profile == {"生日": "2000-01-01", "身高": "160cm"}  # 空值不保留


def test_javbus_placeholder_avatar_is_ignored() -> None:
    text = '<div class="avatar-box"><div class="photo-frame"><img src="/pics/actress/nowprinting.gif"></div></div>'
    assert parse_list(text).avatar is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.javbus.com/star/1248", SubscriptionKey("star", "1248", False)),
        ("https://www.javbus.com/star/1248/3", SubscriptionKey("star", "1248", False)),
        ("https://www.javbus.com/uncensored/studio/3n", SubscriptionKey("studio", "3n", True)),
        (" https://javsee.me/series/b1x/ ", SubscriptionKey("series", "b1x", False)),
        ("https://www.javbus.com/label/8f6", SubscriptionKey("label", "8f6", False)),
        ("https://www.javbus.com/genre/4", SubscriptionKey("genre", "4", False)),
    ],
)
def test_parse_subscription_url(url: str, expected: SubscriptionKey) -> None:
    assert parse_subscription_url(url) == expected


@pytest.mark.parametrize(
    "url", ["", "https://www.javbus.com/MNGS-005", "https://www.javbus.com/actor/1", "javbus.com/star/1", "https://www.javbus.com/star/"]
)
def test_parse_subscription_url_rejects(url: str) -> None:
    with pytest.raises(ValueError):
        parse_subscription_url(url)


def test_subscription_key_urls() -> None:
    assert SubscriptionKey("star", "1248", False).page_url() == "https://www.javbus.com/star/1248"
    assert SubscriptionKey("studio", "3n", True).page_url(2) == "https://www.javbus.com/uncensored/studio/3n/2"
    assert SubscriptionKey("studio", "3n", True).label == "无码片商"


# --- JavBus 年龄验证 ---


def test_verify_page_detection() -> None:
    assert is_javbus_verify(fixture_text("javbus_driver_verify.html"))
    for name in ("javbus_detail_MNGS-005.html", "javbus_star_1248.html", "javbus_magnets_MNGS-005.html"):
        assert not is_javbus_verify(fixture_text(name)), name


class VerifyFetcher:
    """前 ``gate`` 次 GET 返回验证页; 记录提交的表单."""

    def __init__(self, page: str, gate: int) -> None:
        self.page, self.gate = page, gate
        self.posts: list[tuple[str, dict[str, str]]] = []

    async def get_text(self, url: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
        if self.gate > 0:
            self.gate -= 1
            return 200, fixture_text("javbus_driver_verify.html")
        return 200, self.page

    async def post_form(self, url: str, data: dict[str, str], headers: dict[str, str] | None = None) -> int:
        self.posts.append((url, data))
        return 200


async def test_javbus_get_submits_verify_form() -> None:
    f = VerifyFetcher("ok", gate=1)
    assert await javbus_get(f, "https://www.javbus.com/MNGS-005") == (200, "ok")  # type: ignore[arg-type]
    (url, data), = f.posts
    assert url == "https://www.javbus.com/doc/driver-verify?referer=https%3A%2F%2Fwww.javbus.com%2FMNGS-005"
    assert data == {"Submit": "確認"}


async def test_javbus_get_no_verify_no_post() -> None:
    f = VerifyFetcher("ok", gate=0)
    await javbus_get(f, "https://www.javbus.com/x")  # type: ignore[arg-type]
    assert f.posts == []


async def test_javbus_magnet_search_reports_unpassed_verify() -> None:
    # 以前验证页被当成「没有该番号」静默返回空列表, 现在必须报错.
    with pytest.raises(SiteError, match="年龄验证"):
        await JavBus().search(VerifyFetcher("", gate=99), "MNGS-005")  # type: ignore[arg-type]


# --- 订阅流程 ---


def listed(number: str, date: str) -> ListedRelease:
    return ListedRelease(number=number, title=f"{number} 标题", date=date, cover=None, url=f"https://www.javbus.com/{number}")


class FakeLists:
    pages: dict[tuple[str, str], list[tuple[list[ListedRelease], bool]]] = {}
    calls: list[tuple[str, int]] = []
    fail = False

    avatars: dict[str, str] = {}

    @classmethod
    async def fetch(cls, fetcher: object, key: SubscriptionKey, page: int = 1) -> ListPage:
        cls.calls.append((key.ident, page))
        if cls.fail:
            raise SiteError("javbus 返回 HTTP 500")
        pages = cls.pages.get((key.kind, key.ident), [])
        items, more = pages[page - 1] if page <= len(pages) else ([], False)
        avatar = cls.avatars.get(key.ident)
        return ListPage(name=f"名字{key.ident}", items=items, more=more, avatar=avatar, profile={"身高": "160cm"} if avatar else {})


@pytest.fixture
def lists(monkeypatch: pytest.MonkeyPatch) -> type[FakeLists]:
    FakeLists.pages, FakeLists.calls, FakeLists.fail, FakeLists.avatars = {}, [], False, {}
    monkeypatch.setattr(service_module, "fetch_list", FakeLists.fetch)

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(service_module.asyncio, "sleep", no_sleep)
    return FakeLists


async def test_add_and_check_subscription(service: Service, lists: type[FakeLists]) -> None:
    service.repo.save_settings(Settings(subs_initial_pages=3, subs_pages=1))
    lists.pages[("star", "1248")] = [
        ([listed("AAA-003", "2026-10-03"), listed("AAA-002", "2026-10-02")], True),
        ([listed("AAA-001", "2026-10-01")], False),
    ]
    sub = await service.add_subscription("https://www.javbus.com/star/1248")
    assert sub.name == "名字1248" and sub.kind == "star" and not sub.auto_search
    assert await service.check_subscription(sub) == 3
    assert lists.calls == [("1248", 1), ("1248", 1), ("1248", 2)]  # 添加时取一次名称, 首次回填翻到最后一页
    assert service.repo.list_wanted() == []  # 默认不自动搜磁力

    # 第二次: 第一页里出现已见过的作品, 不再翻页.
    lists.calls.clear()
    lists.pages[("star", "1248")] = [([listed("AAA-004", "2026-10-04"), listed("AAA-003", "2026-10-03")], True)]
    stored = service.repo.subscription(sub.id)  # type: ignore[arg-type]
    assert stored and stored.release_count == 3
    assert await service.check_subscription(stored) == 1
    assert lists.calls == [("1248", 1)]
    assert service.repo.subscription(sub.id).release_count == 4  # type: ignore[arg-type,union-attr]

    # 同一页面重复添加, 返回已有订阅.
    again = await service.add_subscription("https://www.javbus.com/star/1248/2")
    assert again.id == sub.id


async def test_auto_search_adds_new_releases_to_queue(service: Service, lists: type[FakeLists]) -> None:
    lists.pages[("studio", "4v")] = [([listed("BBB-001", "2026-10-01")], False)]
    sub = await service.add_subscription("https://www.javbus.com/studio/4v", auto_search=True)
    await service.check_subscription(sub)
    (w,) = service.repo.list_wanted()
    assert w.number == "BBB-001" and w.status is WantedStatus.PENDING and w.source == "订阅:名字4v"


async def test_check_failure_is_recorded(service: Service, lists: type[FakeLists]) -> None:
    sub = await service.add_subscription("https://www.javbus.com/star/9")
    lists.fail = True
    with pytest.raises(SiteError):
        await service.check_subscription(sub)
    stored = service.repo.subscription(sub.id)  # type: ignore[arg-type]
    assert stored and "500" in stored.last_error and stored.checked_at
    # 调度入口吞掉错误继续下一个订阅, 只记日志.
    await service.run_subscriptions(force=True)
    assert any("检查失败" in e["message"] for e in service.repo.events())


async def test_run_subscriptions_respects_interval_and_enabled(service: Service, lists: type[FakeLists]) -> None:
    service.repo.save_settings(Settings(subs_interval_hours=12))
    a = await service.add_subscription("https://www.javbus.com/star/1")
    b = await service.add_subscription("https://www.javbus.com/star/2")
    service.repo.update_subscription(b.id, enabled=False)  # type: ignore[arg-type]
    lists.calls.clear()
    await service.run_subscriptions()
    assert lists.calls == [("1", 1)]
    lists.calls.clear()
    await service.run_subscriptions()  # 刚检查过, 未到期
    assert lists.calls == []
    await service.run_subscriptions(force=True)
    assert lists.calls == [("1", 1)]
    assert a.id is not None


def test_delete_subscription_keeps_shared_releases(service: Service) -> None:
    a = service.repo.add_subscription(Subscription(source="javbus", kind="star", ident="1", uncensored=False, name="A"))
    b = service.repo.add_subscription(Subscription(source="javbus", kind="studio", ident="2", uncensored=False, name="B"))
    assert a.id and b.id
    rel = Release(source="javbus", number="CCC-001", title="t", date="2026-10-01", cover=None, url="u")
    service.repo.add_release(rel, a.id)
    service.repo.add_release(rel, b.id)
    service.repo.add_release(Release(source="javbus", number="CCC-002", title="t", date=None, cover=None, url="u"), a.id)
    service.repo.delete_subscription(a.id)
    assert [r.number for r, *_ in service.repo.releases()] == ["CCC-001"]


def test_queue_releases(service: Service) -> None:
    sub = service.repo.add_subscription(Subscription(source="javbus", kind="star", ident="1", uncensored=False, name="A"))
    assert sub.id
    r1, _ = service.repo.add_release(Release(source="javbus", number="DDD-001", title="", date=None, cover=None, url=""), sub.id)
    r2, _ = service.repo.add_release(Release(source="javbus", number="DDD-002", title="", date=None, cover=None, url=""), sub.id)
    service.add_numbers(["DDD-002"])
    assert service.queue_releases([r1.id, r2.id, 99999]) == 1  # type: ignore[list-item]
    rows = {r.number: status for r, _, _, status in service.repo.releases()}
    assert rows == {"DDD-001": "pending", "DDD-002": "pending"}
    assert [r.number for r, *_ in service.repo.releases(not_queued=True)] == []


# --- 网页与 RSS ---


def seed(service: Service) -> tuple[Subscription, Subscription]:
    a = service.repo.add_subscription(Subscription(source="javbus", kind="star", ident="1248", uncensored=False, name="春陽モカ"))
    b = service.repo.add_subscription(Subscription(source="javbus", kind="studio", ident="4v", uncensored=False, name="ムーディーズ"))
    assert a.id and b.id
    service.repo.add_release(Release(source="javbus", number="MNGS-070", title="タイトル <&>", date="2026-10-02", cover=None, url="https://www.javbus.com/MNGS-070"), a.id)
    service.repo.add_release(Release(source="javbus", number="MNGS-070", title="タイトル <&>", date="2026-10-02", cover=None, url="https://www.javbus.com/MNGS-070"), b.id)
    service.repo.add_release(Release(source="javbus", number="MDVR-444", title="VR", date="2026-10-06", cover=None, url="https://www.javbus.com/MDVR-444"), b.id)
    return a, b


def test_subscription_and_release_pages(service: Service, tmp_path: Path, lists: type[FakeLists]) -> None:
    a, _ = seed(service)
    lists.pages[("star", "77")] = [([listed("EEE-001", "2026-10-01")], False)]
    with TestClient(create_app(Env(data_dir=tmp_path, password=""), service=service)) as c:
        page = c.get("/subscriptions").text
        assert "春陽モカ" in page
        assert "添加失败" in c.post("/subscriptions", data={"url": "https://example.com/x"}).text
        assert c.post("/subscriptions", data={"url": "https://www.javbus.com/star/77"}, follow_redirects=False).status_code == 303
        assert any(s.ident == "77" for s in service.repo.subscriptions())

        c.post(f"/subscriptions/{a.id}/auto")
        assert service.repo.subscription(a.id).auto_search is True  # type: ignore[arg-type,union-attr]
        c.post(f"/subscriptions/{a.id}/toggle")
        assert service.repo.subscription(a.id).enabled is False  # type: ignore[arg-type,union-attr]
        assert c.post(f"/subscriptions/{a.id}/bogus").status_code == 400
        assert c.post("/subscriptions/9999/check").status_code == 404

        releases = c.get(f"/releases?sub={a.id}").text
        assert "MNGS-070" in releases and "MDVR-444" not in releases
        assert c.get("/releases?kind=studio&days=30&todo=1&q=M").status_code == 200

        rel_ids = [r.id for r, *_ in service.repo.releases()]
        r = c.post("/releases/queue", data={"ids": [str(i) for i in rel_ids]}, headers={"referer": "http://testserver/works?sub=1&message=old"}, follow_redirects=False)
        assert r.status_code == 303
        location = r.headers["location"]
        assert location.startswith("/works?sub=1&message=") and location.count("message=") == 1
        assert {w.number for w in service.repo.list_wanted()} >= {"MNGS-070", "MDVR-444"}

        c.post(f"/subscriptions/{a.id}/delete")
        assert service.repo.subscription(a.id) is None  # type: ignore[arg-type]


def test_subscription_settings_saved(service: Service, tmp_path: Path) -> None:
    with TestClient(create_app(Env(data_dir=tmp_path, password=""), service=service)) as c:
        text = c.post("/settings", data={"subs_interval_hours": "6", "subs_pages": "2", "subs_initial_pages": "5"}).text
        assert "已保存" in text
        s = service.repo.settings()
        assert (s.subs_interval_hours, s.subs_pages, s.subs_initial_pages) == (6, 2, 5)
        assert "保存失败" in c.post("/settings", data={"subs_pages": "99"}).text
