from __future__ import annotations

import pytest

from conftest import fixture_text
from kumo.magnets import JavDB, SiteError, javdb_detail_path, parse_javdb_magnets
from kumo.models import Release, Subscription, Tag
from kumo.service import Service
from kumo.subscriptions import SubscriptionKey, parse_javdb_list, parse_subscription_url

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
    ["https://javdb.com/actors/uncensored", "https://javdb.com/makers/western", "https://javdb.com/v/ZNdBQA", "https://javdb.com/tags?c1=23"],
)
def test_parse_javdb_subscription_url_rejects(url: str) -> None:
    with pytest.raises(ValueError):
        parse_subscription_url(url)


def test_javdb_page_url() -> None:
    key = SubscriptionKey("star", "B85e9", source="javdb")
    assert key.page_url() == "https://javdb.com/actors/B85e9"
    assert key.page_url(2) == "https://javdb.com/actors/B85e9?page=2"
    assert SubscriptionKey("code", "MNGS", source="javdb").page_url() == "https://javdb.com/video_codes/MNGS"


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
