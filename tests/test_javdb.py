from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import fixture_text
from kumo import magnets as magnets_module
from kumo import service as service_module
from kumo.magnets import Fetcher, JavDB, SiteError, javdb_detail_path, parse_cookie_header, parse_javdb_magnets
from kumo.models import Release, Subscription, Tag
from kumo.service import Service
from kumo.settings import Env
from kumo.subscriptions import ListPage, SubscriptionKey, fetch_list, parse_javdb_list, parse_subscription_url
from kumo.web import create_app

# --- 订阅列表 ---


@pytest.mark.parametrize(
    ("fixture", "name", "first_number"),
    [
        ("javdb_actors_B85e9.html", "春陽モカ", "BMW-370"),
        ("javdb_makers_zKW.html", "MOODYZ", "MIDA-838"),
        ("javdb_video_codes_MNGS.html", "MNGS", "MNGS-079"),
    ],
)
def test_parse_javdb_list(fixture: str, name: str, first_number: str) -> None:
    page = parse_javdb_list(fixture_text(fixture))
    items = page.items
    assert page.name == name and page.more is True and len(items) == 40
    assert items[0].number == first_number
    assert items[0].url.startswith("https://javdb.com/v/")
    assert items[0].cover and items[0].cover.startswith("https://")
    assert all(i.date for i in items)
    assert len({i.number for i in items}) == 40


def test_javdb_list_includes_announced_titles() -> None:
    # 列表含已公布未发行的作品, 新作比 JavBus 出现得早.
    assert parse_javdb_list(fixture_text("javdb_actors_B85e9.html")).items[0].date == "2026-11-03"


def test_javdb_actor_avatar_and_count() -> None:
    page = parse_javdb_list(fixture_text("javdb_actors_B85e9.html"))
    assert page.avatar == "https://c0.jdbstatic.com/avatars/b8/B85e9.jpg"
    assert page.profile == {"作品数": "290"}
    maker = parse_javdb_list(fixture_text("javdb_makers_zKW.html"))
    assert maker.avatar is None and maker.profile == {}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://javdb.com/actors/B85e9", SubscriptionKey("star", "B85e9", source="javdb")),
        ("https://javdb.com/makers/zKW?page=3", SubscriptionKey("studio", "zKW", source="javdb")),
        ("https://javdb.com/publishers/abc", SubscriptionKey("label", "abc", source="javdb")),
        ("https://javdb.com/directors/kDe/", SubscriptionKey("director", "kDe", source="javdb")),
        ("https://javdb.com/series/X1y", SubscriptionKey("series", "X1y", source="javdb")),
        ("https://javdb.com/video_codes/MNGS", SubscriptionKey("code", "MNGS", source="javdb")),
        ("https://javdb521.com/actors/B85e9", SubscriptionKey("star", "B85e9", source="javdb")),  # 镜像域名
    ],
)
def test_parse_javdb_subscription_url(url: str, expected: SubscriptionKey) -> None:
    assert parse_subscription_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://javdb.com/actors/uncensored",
        "https://javdb.com/makers/western",
        "https://javdb.com/v/ZNdBQA",
        "https://javdb.com/tags",  # 没选标签
        "https://javdb.com/tags?page=2&sort=1",  # 只有无关参数
        "https://javdb.com/tags/foo?c2=67",  # 不存在的分区
    ],
)
def test_parse_javdb_subscription_url_rejects(url: str) -> None:
    with pytest.raises(ValueError):
        parse_subscription_url(url)


@pytest.mark.parametrize(
    ("url", "ident"),
    [
        ("https://javdb.com/tags?c2=67", "?c2=67"),
        # 组合标签: 参数排序后作为标识, 同一组合换个顺序粘贴也是同一个订阅; 分页等无关参数丢弃.
        ("https://javdb.com/tags?c4=57&c2=67&page=3", "?c2=67&c4=57"),
        ("https://javdb.com/tags?c2=67&c4=57", "?c2=67&c4=57"),
        ("https://javdb.com/tags?c7=28,168&c2=67", "?c2=67&c7=28,168"),  # 同一类多选
        ("https://javdb.com/tags/uncensored?c1=23", "uncensored?c1=23"),
        ("https://javdb.com/tags/western/?c10=1", "western?c10=1"),
        ("https://javdb.com/tags/fc2?c3=5", "fc2?c3=5"),
    ],
)
def test_parse_javdb_tag_url(url: str, ident: str) -> None:
    assert parse_subscription_url(url) == SubscriptionKey("tag", ident, source="javdb")


def test_javdb_page_url() -> None:
    key = SubscriptionKey("star", "B85e9", source="javdb")
    assert key.page_url() == "https://javdb.com/actors/B85e9"
    assert key.page_url(2) == "https://javdb.com/actors/B85e9?page=2"
    assert SubscriptionKey("code", "MNGS", source="javdb").page_url() == "https://javdb.com/video_codes/MNGS"
    tag = SubscriptionKey("tag", "?c2=67&c4=57", source="javdb")
    assert tag.page_url() == "https://javdb.com/tags?c2=67&c4=57"
    assert tag.page_url(2) == "https://javdb.com/tags?c2=67&c4=57&page=2"
    assert SubscriptionKey("tag", "uncensored?c1=23", source="javdb").page_url() == "https://javdb.com/tags/uncensored?c1=23"
    assert tag.label == "标签"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("_jdb_session=abc; locale=zh", [("_jdb_session", "abc"), ("locale", "zh")]),
        ("Cookie: _jdb_session=abc", [("_jdb_session", "abc")]),
        ("abc123", [("_jdb_session", "abc123")]),  # 只粘贴了值
        ("  ", []),
        ("a=1;;=2; b = 3 ", [("a", "1"), ("b", "3")]),
    ],
)
def test_parse_cookie_header(text: str, expected: list[tuple[str, str]]) -> None:
    assert parse_cookie_header(text) == expected


async def test_fetcher_sends_javdb_cookie_only_to_javdb(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = request.headers.get("cookie", "")
        return httpx.Response(200, text="ok")

    real = httpx.AsyncClient
    monkeypatch.setattr(magnets_module.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **{k: v for k, v in kw.items() if k != "proxy"}))
    f = Fetcher(javdb_cookie="_jdb_session=secret")
    await f.get_text("https://javdb.com/tags?c2=67")
    await f.get_text("https://sukebei.nyaa.si/")
    await f.aclose()
    assert "_jdb_session=secret" in seen["javdb.com"]
    assert "_jdb_session" not in seen["sukebei.nyaa.si"]


async def test_tag_page_without_login_reports_clearly() -> None:
    f = PageFetcher({"/tags": (200, "window.location.href='/login';")})
    with pytest.raises(SiteError, match="JavDB Cookie"):
        await fetch_list(f, SubscriptionKey("tag", "?c2=67", source="javdb"))  # type: ignore[arg-type]


async def test_add_tag_subscription_uses_given_name(service: Service, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fetch(fetcher: object, key: SubscriptionKey, page: int = 1) -> ListPage:
        return ListPage(name="標籤", items=[], more=False)

    monkeypatch.setattr(service_module, "fetch_list", fetch)
    named = await service.add_subscription("https://javdb.com/tags?c4=57&c2=67", name=" 辣妹+美臀 ")
    assert named.kind == "tag" and named.ident == "?c2=67&c4=57" and named.name == "辣妹+美臀"
    unnamed = await service.add_subscription("https://javdb.com/tags/uncensored?c1=23")
    assert unnamed.name == "uncensored?c1=23"  # 标签页没有可用名称, 用参数代替
    renamed = await service.add_subscription("https://www.javbus.com/star/1", name="自定义")
    assert renamed.name == "自定义"


def test_settings_keep_javdb_cookie_when_left_blank(service: Service, tmp_path: Path) -> None:
    with TestClient(create_app(Env(data_dir=tmp_path, password=""), service=service)) as c:
        c.post("/settings", data={"javdb_cookie": "_jdb_session=abc"})
        assert service.repo.settings().javdb_cookie == "_jdb_session=abc"
        page = c.get("/settings").text
        assert "已填写" in page and "_jdb_session=abc" not in page  # 页面上不回显
        c.post("/settings", data={"javdb_cookie": ""})
        assert service.repo.settings().javdb_cookie == "_jdb_session=abc"  # 留空保持
        c.post("/settings", data={"clear_javdb_cookie": "on"})
        assert service.repo.settings().javdb_cookie == ""


def test_rename_subscription(service: Service, tmp_path: Path) -> None:
    sub = service.repo.add_subscription(Subscription(source="javdb", kind="tag", ident="?c2=67", uncensored=False, name="?c2=67"))
    with TestClient(create_app(Env(data_dir=tmp_path, password=""), service=service)) as c:
        c.post(f"/subscriptions/{sub.id}/rename", data={"name": "辣妹"})
        assert service.repo.subscription(sub.id).name == "辣妹"  # type: ignore[arg-type,union-attr]
        c.post(f"/subscriptions/{sub.id}/rename", data={"name": "  "})
        assert service.repo.subscription(sub.id).name == "辣妹"  # type: ignore[arg-type,union-attr]
        assert c.post("/subscriptions/9999/rename", data={"name": "x"}).status_code == 404


# --- 磁力 ---


def test_parse_javdb_magnets() -> None:
    items = parse_javdb_magnets(fixture_text("javdb_detail_MNGS-005.html"))
    assert len(items) == 8
    first = items[0]
    assert first.site == "javdb" and first.title == "MNGS-005"
    assert first.info_hash == "8aaad85af95c1b03ff614e23a8ff15f29c461555"
    assert first.magnet.startswith("magnet:?xt=urn:btih:8aaad85af95c1b03ff614e23a8ff15f29c461555&dn=")
    assert first.size == int(4.97 * 1024**3) and first.published == "2025-07-11"
    assert first.tags == {Tag.HD}
    assert all(len(i.info_hash) == 40 for i in items)


def test_javdb_detail_path_requires_exact_number() -> None:
    page = fixture_text("javdb_search_MNGS-005.html")
    assert javdb_detail_path(page, "MNGS-005") == "/v/ZNdBQA"
    assert javdb_detail_path(page, "ZZZZ-999") is None


class PageFetcher:
    def __init__(self, pages: dict[str, tuple[int, str]]) -> None:
        self.pages = pages
        self.urls: list[str] = []

    async def get_text(self, url: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
        self.urls.append(url)
        for key, value in self.pages.items():
            if key in url:
                return value
        return 404, ""


async def test_javdb_search() -> None:
    f = PageFetcher(
        {
            "/search?": (200, fixture_text("javdb_search_MNGS-005.html")),
            "/v/ZNdBQA": (200, fixture_text("javdb_detail_MNGS-005.html")),
        }
    )
    items = await JavDB().search(f, "MNGS-005")  # type: ignore[arg-type]
    assert len(items) == 8
    assert f.urls == ["https://javdb.com/search?q=MNGS-005&f=all", "https://javdb.com/v/ZNdBQA"]
    assert await JavDB().search(f, "ZZZZ-999") == []  # type: ignore[arg-type]


@pytest.mark.parametrize(("status", "message"), [(429, "频繁"), (503, "503")])
async def test_javdb_search_errors(status: int, message: str) -> None:
    with pytest.raises(SiteError, match=message):
        await JavDB().search(PageFetcher({"/search?": (status, "")}), "MNGS-005")  # type: ignore[arg-type]


# --- 跨站去重 ---


def test_same_number_from_two_sites_is_one_release(service: Service) -> None:
    jb = service.repo.add_subscription(Subscription(source="javbus", kind="star", ident="1248", uncensored=False, name="春陽モカ"))
    jd = service.repo.add_subscription(Subscription(source="javdb", kind="star", ident="B85e9", uncensored=False, name="春陽モカ"))
    assert jb.id and jd.id
    a, new_a = service.repo.add_release(
        Release(source="javdb", number="MNGS-083", title="JavDB 标题", date="2026-10-20", cover=None, url="https://javdb.com/v/x"), jd.id
    )
    b, new_b = service.repo.add_release(
        Release(source="javbus", number="MNGS-083", title="JavBus 标题", date=None, cover="c.jpg", url="https://www.javbus.com/MNGS-083"), jb.id
    )
    assert new_a and new_b and a.id == b.id
    (row,) = service.repo.releases()
    rel, subs, _, _ = row
    assert rel.source == "javdb" and rel.url == "https://javdb.com/v/x"  # 先见到的站点为准
    assert rel.title == "JavDB 标题" and rel.date == "2026-10-20" and rel.cover == "c.jpg"  # 只补缺
    assert subs == "春陽モカ"
    assert service.repo.subscription(jb.id).release_count == 1  # type: ignore[union-attr]
