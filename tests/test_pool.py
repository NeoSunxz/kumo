"""磁力池: sukebei 规则爬取、按番号分组的「确认影片」、与片源共享的数据."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kumo import service as service_module
from kumo.magnets import Fetcher, Sukebei, parse_sukebei_items
from kumo.models import LibraryFile, Release, Resource, Subscription, WantedStatus, Work
from kumo.service import Service
from kumo.settings import Env, Settings, SukebeiRule
from kumo.web import _parse_sukebei_rule, create_app, format_sukebei_rule

from conftest import fixture_text

GiB = 1024**3


def rss(*items: tuple[int, str, int, str, bool]) -> str:
    """(种子编号, 标题, 做种, 大小, 可信) → sukebei RSS."""
    body = "".join(
        f"""<item><title>{title}</title><link>https://sukebei.nyaa.si/download/{tid}.torrent</link>
        <guid isPermaLink="true">https://sukebei.nyaa.si/view/{tid}</guid><pubDate>Tue, 07 Oct 2026 08:00:00 -0000</pubDate>
        <nyaa:seeders>{seeders}</nyaa:seeders><nyaa:infoHash>{tid:040x}</nyaa:infoHash><nyaa:category>Real Life - Videos</nyaa:category>
        <nyaa:size>{size}</nyaa:size><nyaa:trusted>{'Yes' if trusted else 'No'}</nyaa:trusted></item>"""
        for tid, title, seeders, size, trusted in items
    )
    return f'<rss xmlns:nyaa="https://sukebei.nyaa.si/xmlns/nyaa" version="2.0"><channel>{body}</channel></rss>'


@pytest.fixture
def sukebei(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """按 RSS 地址返回预设内容, 没设的返回空 RSS."""
    responses: dict[str, str] = {}

    async def get_text(self: Fetcher, url: str, *, headers: dict[str, str] | None = None) -> tuple[int, str]:
        return 200, responses.get(url, rss())

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(Fetcher, "get_text", get_text)
    monkeypatch.setattr(service_module.asyncio, "sleep", no_sleep)
    return responses


def add_res(service: Service, tid: int, title: str, number: str | None, *, source: str = "sehuatang", size: int = 5 * GiB,
            seeders: int | None = None) -> Resource:
    return service.repo.add_resource(
        Resource(
            source=source, tid=tid, forum_id=0 if source == "sukebei" else 103, forum_name=source, type_name="规则",
            title=title, number=number, magnet=f"magnet:?xt=urn:btih:{tid:040x}", info_hash=f"{tid:040x}", size=size,
            seeders=seeders,
        )
    )


# --- sukebei 解析与规则 ---


def test_parse_sukebei_items_keeps_id_and_trusted() -> None:
    items = parse_sukebei_items(fixture_text("sukebei_MNGS-005.xml"))
    assert items[0].id == 4344176 and items[0].trusted and items[0].category == "Real Life - Videos"
    assert items[1].id == 4343914 and not items[1].trusted


def test_rss_url() -> None:
    assert Sukebei.rss_url(query="中文字幕", user="abc", category="2_2", trusted_only=True) == (
        "https://sukebei.nyaa.si/?page=rss&q=%E4%B8%AD%E6%96%87%E5%AD%97%E5%B9%95&c=2_2&f=2&u=abc"
    )


def test_rule_text_roundtrip() -> None:
    rule = _parse_sukebei_rule("中字 | 中文字幕 | 自动 可信 做种>=3 上传者:abc 分类:2_0 +1080p -4K")
    assert rule == SukebeiRule(
        name="中字", query="中文字幕", auto_download=True, trusted_only=True, min_seeders=3, user="abc", category="2_0",
        include_keywords=["1080p"], exclude_keywords=["4K"],
    )
    assert _parse_sukebei_rule(format_sukebei_rule(rule)) == rule
    assert format_sukebei_rule(SukebeiRule(name="全部")) == "全部"
    with pytest.raises(ValueError):
        _parse_sukebei_rule("x | y | 不认识")
    with pytest.raises(ValueError):
        _parse_sukebei_rule(" | y")


# --- sukebei 爬取 ---


async def test_sukebei_crawl_fills_pool_with_filters(service: Service, sukebei: dict[str, str]) -> None:
    rule = SukebeiRule(name="中字", query="中文字幕", min_seeders=2, exclude_keywords=["4K"])
    service.repo.save_settings(Settings(app_id="test", download_path="/云下载", sukebei_rules=[rule]))
    url = Sukebei.rss_url(query="中文字幕", category="2_2")
    sukebei[url] = rss(
        (101, "[中文字幕] ABC-001 好片", 5, "5.0 GiB", True),
        (102, "ABC-002 4K", 9, "20.0 GiB", True),  # 排除词
        (103, "ABC-003", 1, "4.0 GiB", False),  # 做种不够
        (104, "没有番号的合集", 9, "40.0 GiB", True),
    )
    await service.run_sukebei()
    pool = service.repo.resources(sources=["sukebei"])
    assert sorted((r.tid, r.number) for r in pool) == [(101, "ABC-001"), (104, None)]
    first = next(r for r in pool if r.tid == 101)
    assert first.seeders == 5 and first.subtitle and first.type_name == "中字" and first.size == 5 * GiB
    assert service.repo.list_wanted() == []  # 没开自动: 只进磁力池

    await service.run_sukebei()  # 再爬一次不重复入库
    assert len(service.repo.resources(sources=["sukebei"])) == 2


async def test_sukebei_auto_download_only_accepts_matching_magnets(service: Service, sukebei: dict[str, str]) -> None:
    rule = SukebeiRule(name="自动", query="x", auto_download=True)
    service.repo.save_settings(Settings(app_id="test", download_path="/云下载", sukebei_rules=[rule], size_max_gb=12))
    sukebei[Sukebei.rss_url(query="x", category="2_2")] = rss(
        (201, "ABC-011 1080p", 5, "5.0 GiB", True),
        (202, "ABC-012 超大", 5, "30.0 GiB", True),  # 超出体积上限, 留在磁力池
    )
    await service.run_sukebei()
    w = service.repo.wanted_by_number("ABC-011")
    assert w and w.status is WantedStatus.QUEUED
    assert service.repo.wanted_by_number("ABC-012") is None
    assert [g.number for g in service.pool_groups()] == ["ABC-012"]


async def test_sukebei_without_rules(service: Service, sukebei: dict[str, str]) -> None:
    await service.run_sukebei()  # 定时任务: 没规则就不做事
    with pytest.raises(service_module.SiteError):
        await service.run_sukebei(force=True)


async def test_number_search_uses_sukebei_pool(service: Service) -> None:
    service.repo.save_settings(Settings(app_id="test", download_path="/云下载", sites=["sehuatang"]))
    add_res(service, 301, "ABC-021 中文字幕", "ABC-021", source="sukebei", seeders=4)
    service.add_numbers(["ABC-021"])
    w = service.repo.wanted_by_number("ABC-021")
    assert w and await service.search(w) is WantedStatus.QUEUED
    (cand,) = service.repo.candidates(w.id)
    assert cand.site == "sukebei" and cand.seeders == 4


# --- 确认影片 ---


def test_pool_groups_by_number_and_excludes_handled(service: Service) -> None:
    service.repo.save_settings(Settings(app_id="test", download_path="/云下载", size_max_gb=12))
    add_res(service, 1, "ABC-031 1080p", "ABC-031", seeders=2)
    add_res(service, 2, "ABC-031 中文字幕", "ABC-031", source="sukebei", seeders=9)
    add_res(service, 3, "ABC-031 巨大", "ABC-031", size=40 * GiB)
    add_res(service, 4, "ABC-032", "ABC-032")  # 已在队列
    add_res(service, 5, "ABC-033", "ABC-033")  # 115 已有
    add_res(service, 6, "ABC-034", "ABC-034")  # 被忽略
    add_res(service, 7, "合集", None)
    service.add_numbers(["ABC-032"])
    service.repo.replace_library_root("/影片", [LibraryFile(fid="f", name="ABC-033.mp4", number="ABC-033")])
    service.repo.dismiss_numbers(["ABC-034"])

    groups = service.pool_groups()
    assert [g.number for g in groups] == ["ABC-031"]
    (g,) = groups
    assert [m.resource.tid for m in g.magnets] == [2, 1, 3]  # 字幕优先, 超体积的排最后
    assert g.magnets[-1].rejected and g.best.resource.tid == 2
    assert service.repo.pool_pending_count() == 1
    assert [g.number for g in service.pool_groups(sources=["sukebei"])] == ["ABC-031"]
    assert service.pool_groups(search="不存在") == []


def test_pool_shares_work_and_release(service: Service) -> None:
    add_res(service, 1, "abc-041 磁力标题", "ABC-041")
    sub = service.repo.add_subscription(Subscription(source="javbus", kind="star", ident="1", uncensored=False, name="某人"))
    assert sub.id
    service.repo.add_release(Release(source="javbus", number="ABC-041", title="作品标题", date="2026-10-01", cover=None, url="u"), sub.id)
    service.repo.save_work(Work(number="ABC-041", found=True, title="Amane 标题", actors=["某人"], thumb="/api/resources/abc"))
    (g,) = service.pool_groups()
    assert g.work and g.work.actors == ["某人"] and g.release and g.release.title == "作品标题"
    assert g.title == "Amane 标题"
    # 作品那边反过来看得到磁力池
    assert service.repo.pool_counts(["ABC-041", "XYZ-1"]) == {"ABC-041": 1}


def test_confirm_pool_uses_picked_magnet(service: Service) -> None:
    add_res(service, 1, "ABC-051 A", "ABC-051")
    b = add_res(service, 2, "ABC-051 B", "ABC-051")
    assert b.id
    assert service.confirm_pool([b.id, 99999]) == 1
    w = service.repo.wanted_by_number("ABC-051")
    assert w and w.status is WantedStatus.QUEUED
    chosen = service.repo.candidate(w.chosen_id or 0)
    assert chosen and chosen.info_hash == f"{2:040x}"
    assert service.pool_groups() == []


def test_pool_page_confirm_and_dismiss(service: Service, tmp_path: Path) -> None:
    add_res(service, 1, "ABC-061 A", "ABC-061")
    b = add_res(service, 2, "ABC-061 B", "ABC-061")
    add_res(service, 3, "ABC-062", "ABC-062")
    add_res(service, 4, "ABC-063", "ABC-063")
    with TestClient(create_app(Env(data_dir=tmp_path, password=""), service=service)) as c:
        page = c.get("/pool").text
        assert "ABC-061" in page and "ABC-062" in page and f'name="pick_ABC-061" value="{b.id}"' in page
        assert "/pool" in c.get("/").text  # 导航里有磁力池与待确认数
        # 单张卡片上的「下载」, 用选中的那条磁力
        r = c.post("/pool/confirm", data={"only": "ABC-061", "pick_ABC-061": str(b.id)}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/pool?")
        w = service.repo.wanted_by_number("ABC-061")
        assert w and (cand := service.repo.candidate(w.chosen_id or 0)) and cand.title == "ABC-061 B"
        # 批量忽略勾选的
        c.post("/pool/dismiss", data={"ids": ["ABC-062"]})
        assert [g.number for g in service.pool_groups()] == ["ABC-063"]
        assert "已忽略" in c.get("/resources").text
        assert c.post("/pool/nope", data={}).status_code == 400


def test_releases_page_shows_shared_flags(service: Service, tmp_path: Path) -> None:
    sub = service.repo.add_subscription(Subscription(source="javbus", kind="star", ident="1", uncensored=False, name="某人"))
    assert sub.id
    service.repo.add_release(Release(source="javbus", number="ABC-071", title="t", date="2026-10-01", cover=None, url="u"), sub.id)
    add_res(service, 1, "ABC-071", "ABC-071")
    service.repo.save_work(Work(number="ABC-071", found=True, title="t", thumb="https://example.com/a.jpg", file_count=1))
    with TestClient(create_app(Env(data_dir=tmp_path, password=""), service=service)) as c:
        page = c.get("/releases").text
        assert "磁力池 1" in page and "Amane 已入库" in page
        assert "/img/amane?u=https%3A%2F%2Fexample.com%2Fa.jpg" in page  # 没有资料站封面时用 Amane 的
        detail = c.get(f"/actresses/{sub.id}").text
        assert "磁力池 1" in detail


def test_settings_save_sukebei_rules(service: Service, tmp_path: Path) -> None:
    with TestClient(create_app(Env(data_dir=tmp_path, password=""), service=service)) as c:
        form = {"sukebei_rules": "中字 | 中文字幕 | 可信 做种>=3\n全部 | \n", "sukebei_interval_minutes": "90"}
        assert "已保存" in c.post("/settings", data=form).text
        s = service.repo.settings()
        assert s.sukebei_rules == [SukebeiRule(name="中字", query="中文字幕", trusted_only=True, min_seeders=3), SukebeiRule(name="全部")]
        assert s.sukebei_interval_minutes == 90
        assert "中字 | 中文字幕 | 可信 做种&gt;=3" in c.get("/settings").text
        assert "保存失败" in c.post("/settings", data={"sukebei_rules": "x | y | 乱写"}).text


def test_old_database_gets_pool_columns(tmp_path: Path) -> None:
    import sqlite3

    from kumo.db import Repo

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE resource (id INTEGER PRIMARY KEY, source TEXT NOT NULL, tid INTEGER NOT NULL, forum_id INTEGER NOT NULL,"
        " forum_name TEXT NOT NULL DEFAULT '', type_id INTEGER, type_name TEXT NOT NULL DEFAULT '', title TEXT NOT NULL,"
        " number TEXT, magnet TEXT, info_hash TEXT, size INTEGER, posted TEXT, subtitle INTEGER NOT NULL DEFAULT 0,"
        " wanted_id INTEGER, crawled_at REAL NOT NULL, UNIQUE (source, tid))"
    )
    conn.execute("INSERT INTO resource(source, tid, forum_id, title, number, magnet, crawled_at) VALUES('sehuatang', 1, 103, 't', 'ABC-1', 'm', 0)")
    conn.commit()
    conn.close()
    (r,) = Repo(path).resources()
    assert r.seeders is None and not r.dismissed
