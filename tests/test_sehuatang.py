from __future__ import annotations

import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import fixture_text
from kumo import service as service_module
from kumo.db import Repo
from kumo.magnets import SiteError
from kumo.models import Resource, Tag, WantedStatus
from kumo.sehuatang import Sehuatang, ThreadDetail, ThreadRef, forum_path, is_age_gate, parse_forum, parse_thread
from kumo.service import Service, _accepts
from kumo.settings import CrawlForum, Env, Settings
from kumo.web import create_app

# --- 解析 ---


def test_parse_forum_page() -> None:
    name, threads = parse_forum(fixture_text("sehuatang_forum_103.html"))
    assert name == "高清中文字幕"
    assert len(threads) == 30
    oppw = next(t for t in threads if t.tid == 3815699)
    assert oppw.title.startswith("OPPW-160")
    assert oppw.type_name == "有码高清" and oppw.type_id == 480
    assert oppw.posted == "2026-10-07"
    assert len({t.tid for t in threads}) == 30


@pytest.mark.parametrize(
    ("fid", "page", "typeid", "expected"),
    [
        (103, 1, None, "forum.php?mod=forumdisplay&fid=103&orderby=dateline&page=1&filter=author"),
        (36, 2, 369, "forum.php?mod=forumdisplay&fid=36&orderby=dateline&page=2&filter=typeid&typeid=369"),
    ],
)
def test_forum_path_orders_by_post_time(fid: int, page: int, typeid: int | None, expected: str) -> None:
    assert forum_path(fid, page, typeid) == expected


def test_parse_thread_page() -> None:
    d = parse_thread(3815699, fixture_text("sehuatang_thread_3815699.html"))
    assert d.title.startswith("OPPW-160")
    assert d.magnet == "magnet:?xt=urn:btih:3E8389A91363F917750393CB25A26DF5C02AC833"
    assert d.size == int(5.29 * 1024**3)
    assert d.censored == "有码"
    assert d.posted == "2026-10-07 06:04:06"


def test_parse_thread_without_magnet() -> None:
    d = parse_thread(1, '<span id="thread_subject">ABC-001 标题</span><td class="t_f" id="postmessage_1">没有磁力</td>')
    assert d.title == "ABC-001 标题" and d.magnet is None and d.size is None


def test_age_gate_detection() -> None:
    assert is_age_gate(fixture_text("sehuatang_age_gate.html"))
    assert not is_age_gate(fixture_text("sehuatang_forum_103.html"))


class GateFetcher:
    """第一次返回年龄确认页, 带上正确 Cookie 后返回正文."""

    def __init__(self, page: str, *, gate_forever: bool = False) -> None:
        self.page = page
        self.cookies: dict[str, str] = {}
        self.gate_forever = gate_forever
        self.calls = 0

    def set_cookie(self, name: str, value: str, domain: str) -> None:
        self.cookies[name] = value
        self.domain = domain

    async def get_text(self, url: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
        self.calls += 1
        if self.gate_forever or self.cookies.get("_safe") != "VB0o5VctlO09O9ct":
            return 200, fixture_text("sehuatang_age_gate.html")
        return 200, self.page


async def test_passes_age_gate_with_safe_cookie() -> None:
    f = GateFetcher(fixture_text("sehuatang_forum_103.html"))
    name, threads = await Sehuatang("https://www.sehuatang.net/").forum_page(f, 103, 1)  # type: ignore[arg-type]
    assert name == "高清中文字幕" and len(threads) == 30
    assert f.calls == 2 and f.domain == ".sehuatang.net"


async def test_age_gate_that_never_passes_raises() -> None:
    f = GateFetcher("", gate_forever=True)
    with pytest.raises(SiteError, match="年龄确认"):
        await Sehuatang("https://sehuatang.net").forum_page(f, 103, 1)  # type: ignore[arg-type]


# --- 爬取流程 ---


class FakeForum:
    """模拟站点: 每个板块按页给出帖子, 帖子详情可配置为失败."""

    pages: dict[int, list[list[ThreadRef]]] = {}
    details: dict[int, ThreadDetail] = {}
    failing: set[int] = set()
    page_calls: list[tuple[int, int]] = []

    def __init__(self, base_url: str) -> None:
        pass

    async def forum_page(self, fetcher: object, fid: int, page: int, typeid: int | None = None) -> tuple[str, list[ThreadRef]]:
        FakeForum.page_calls.append((fid, page) if typeid is None else (fid, page, typeid))
        pages = FakeForum.pages.get(fid, [])
        return ("高清中文字幕" if fid == 103 else "亚洲无码原创"), (pages[page - 1] if page <= len(pages) else [])

    async def thread(self, fetcher: object, tid: int) -> ThreadDetail:
        if tid in FakeForum.failing:
            raise SiteError("超时")
        return FakeForum.details[tid]


def ref(tid: int, title: str, typ: str = "有码高清") -> ThreadRef:
    return ThreadRef(tid=tid, title=title, type_name=typ, posted="2026-10-07", type_id=480 if typ == "有码高清" else 481)


def detail(tid: int, title: str, magnet: bool = True) -> ThreadDetail:
    return ThreadDetail(
        tid=tid, title=title, magnet=f"magnet:?xt=urn:btih:{tid:040d}" if magnet else None,
        size=5 * 1024**3, censored="有码", posted="2026-10-07 06:00:00",
    )


@pytest.fixture
def forum(monkeypatch: pytest.MonkeyPatch) -> type[FakeForum]:
    FakeForum.pages, FakeForum.details, FakeForum.failing, FakeForum.page_calls = {}, {}, set(), []
    monkeypatch.setattr(service_module, "Sehuatang", FakeForum)

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(service_module.asyncio, "sleep", no_sleep)
    return FakeForum


def crawl_settings(**forum_kw: object) -> Settings:
    return Settings(
        app_id="test", download_path="/云下载", sites=["sehuatang"], crawl_enabled=True, crawl_pages=3,
        crawl_forums=[CrawlForum(fid=103, **forum_kw)],  # type: ignore[arg-type]
    )


async def test_crawl_indexes_new_threads_and_stops_at_seen(service: Service, forum: type[FakeForum]) -> None:
    service.repo.save_settings(crawl_settings())
    forum.pages[103] = [[ref(3, "MNGS-005 新"), ref(2, "BF-042-C 新")], [ref(1, "OLD-001")]]
    forum.details = {3: detail(3, "MNGS-005 新"), 2: detail(2, "BF-042-C 新", magnet=False), 1: detail(1, "OLD-001")}
    await service.run_crawl()
    items = {r.tid: r for r in service.repo.resources()}
    assert set(items) == {1, 2, 3}
    assert items[3].number == "MNGS-005" and items[3].subtitle and items[3].info_hash == f"{3:040d}"
    assert items[3].type_id == 480
    assert items[2].magnet is None
    assert service.repo.list_wanted() == []  # 默认只入库, 不下载

    # 第二轮: 第一页出现已爬过的帖子, 不再翻页.
    forum.page_calls.clear()
    forum.pages[103] = [[ref(4, "NEW-004"), ref(3, "MNGS-005 新")], [ref(2, "x")]]
    forum.details[4] = detail(4, "NEW-004")
    await service.run_crawl()
    assert forum.page_calls == [(103, 1)]
    assert service.repo.resource_exists("sehuatang", 4)


async def test_crawl_skips_failed_thread_and_retries_later(service: Service, forum: type[FakeForum]) -> None:
    service.repo.save_settings(crawl_settings())
    forum.pages[103] = [[ref(5, "ABC-005")]]
    forum.details[5] = detail(5, "ABC-005")
    forum.failing = {5}
    await service.run_crawl()
    assert not service.repo.resource_exists("sehuatang", 5)
    assert any("读取失败" in e["message"] for e in service.repo.events())
    forum.failing = set()
    await service.run_crawl()
    assert service.repo.resource_exists("sehuatang", 5)


async def test_crawl_target_with_typeid(service: Service, forum: type[FakeForum]) -> None:
    s = crawl_settings()
    s.crawl_forums = [CrawlForum(fid=36, typeid=369)]
    service.repo.save_settings(s)
    await service.run_crawl()
    assert forum.page_calls == [(36, 1, 369)]


async def test_crawl_disabled_does_nothing(service: Service, forum: type[FakeForum]) -> None:
    service.repo.save_settings(Settings(crawl_enabled=False))
    await service.run_crawl()
    assert forum.page_calls == []


async def test_auto_download_respects_filters(service: Service, forum: type[FakeForum]) -> None:
    service.repo.save_settings(crawl_settings(auto_download=True, types=["有码高清"], exclude_keywords=["预告"]))
    forum.pages[103] = [[ref(10, "AAA-010", "有码高清"), ref(11, "BBB-011", "无码高清"), ref(12, "CCC-012 预告"), ref(13, "西洋片 无番号")]]
    for tid, title in ((10, "AAA-010"), (11, "BBB-011"), (12, "CCC-012 预告"), (13, "西洋片 无番号")):
        forum.details[tid] = detail(tid, title)
    await service.run_crawl()
    wanted = {w.number: w for w in service.repo.list_wanted()}
    assert set(wanted) == {"AAA-010"}
    assert wanted["AAA-010"].status is WantedStatus.QUEUED
    assert wanted["AAA-010"].source == "sehuatang:103"


@pytest.mark.parametrize(
    ("forum_cfg", "title", "typ", "expected"),
    [
        (CrawlForum(fid=1), "ABC-001", "有码高清", True),
        (CrawlForum(fid=1, types=["无码高清"]), "ABC-001", "有码高清", False),
        (CrawlForum(fid=1, include_keywords=["中文"]), "ABC-001 中文", "", True),
        (CrawlForum(fid=1, include_keywords=["中文"]), "ABC-001", "", False),
        (CrawlForum(fid=1, exclude_keywords=["VR"]), "ABC-001 vr版", "", False),
    ],
)
def test_accepts(forum_cfg: CrawlForum, title: str, typ: str, expected: bool) -> None:
    r = Resource(source="sehuatang", tid=1, forum_id=1, forum_name="", type_name=typ, title=title)
    assert _accepts(forum_cfg, r) is expected


# --- 资源库用于搜索与手动下载 ---


def add_res(service: Service, tid: int, title: str, number: str | None, magnet: bool = True, subtitle: bool = False) -> Resource:
    return service.repo.add_resource(
        Resource(
            source="sehuatang", tid=tid, forum_id=103, forum_name="高清中文字幕", type_name="有码高清", title=title,
            number=number, magnet=f"magnet:?xt=urn:btih:{tid:040d}" if magnet else None,
            info_hash=f"{tid:040d}" if magnet else None, size=5 * 1024**3, subtitle=subtitle,
        )
    )


async def test_search_uses_crawled_index(service: Service) -> None:
    service.repo.save_settings(Settings(app_id="test", download_path="/云下载", sites=["sehuatang"]))
    add_res(service, 20, "MNGS-005 字幕版", "MNGS-005", subtitle=True)
    add_res(service, 21, "MNGS-005 没磁力", "MNGS-005", magnet=False)
    service.add_numbers(["MNGS-005"])
    await service.run_search()
    w = service.repo.wanted_by_number("MNGS-005")
    assert w and w.status is WantedStatus.QUEUED
    chosen = service.repo.candidate(w.chosen_id)
    assert chosen and chosen.site == "sehuatang" and Tag.SUBTITLE in chosen.tags


def test_download_resource(service: Service) -> None:
    r = add_res(service, 30, "ABC-030", "ABC-030")
    no_magnet = add_res(service, 31, "ABC-031", "ABC-031", magnet=False)
    no_number = add_res(service, 32, "西洋片", None)
    assert r.id and no_magnet.id and no_number.id
    wid = service.download_resource(r.id)
    w = service.repo.wanted(wid)
    assert w and w.status is WantedStatus.QUEUED and service.repo.resource(r.id).wanted_id == wid  # type: ignore[union-attr]
    for bad in (no_magnet.id, no_number.id, 99999):
        with pytest.raises(ValueError):
            service.download_resource(bad)


def test_download_resource_does_not_override_submitted(service: Service) -> None:
    service.add_numbers(["ABC-040"])
    w = service.repo.wanted_by_number("ABC-040")
    assert w
    service.repo.update_wanted(w.id, status=WantedStatus.SUBMITTED, info_hash="x" * 40)
    r = add_res(service, 40, "ABC-040", "ABC-040")
    assert r.id
    assert service.download_resource(r.id) == w.id
    w = service.repo.wanted(w.id)
    assert w and w.status is WantedStatus.SUBMITTED and w.info_hash == "x" * 40


# --- 网页 ---


def test_resources_page_and_settings(service: Service, tmp_path: Path) -> None:
    r = add_res(service, 50, "ABC-050 标题", "ABC-050")
    with TestClient(create_app(Env(data_dir=tmp_path, password=""), service=service)) as c:
        page = c.get("/resources")
        assert page.status_code == 200 and "ABC-050 标题" in page.text and "论坛爬取未启用" in page.text
        assert c.get("/resources?forum=103&q=ABC").status_code == 200
        resp = c.post(f"/resources/{r.id}/download", follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"].startswith("/wanted/")
        assert c.post("/resources/99999/download").status_code == 400

        form = {"crawl_enabled": "on", "crawl_forums": "103 | 自动 | 有码高清 | | 预告\n36\n36:369 | 入库\n", "crawl_pages": "3"}
        assert "已保存" in c.post("/settings", data=form).text
        s = service.repo.settings()
        assert s.crawl_enabled and s.crawl_pages == 3
        assert s.crawl_forums[0] == CrawlForum(fid=103, auto_download=True, types=["有码高清"], exclude_keywords=["预告"])
        assert s.crawl_forums[1] == CrawlForum(fid=36)
        assert s.crawl_forums[2] == CrawlForum(fid=36, typeid=369)
        assert "36:369 | 入库" in c.get("/settings").text
        for bad in ("abc | 自动", "36:x", "36:"):
            assert "保存失败" in c.post("/settings", data={**form, "crawl_forums": bad}).text, bad


# --- 按参数输出 RSS ---


def add_typed(
    service: Service, tid: int, fid: int, typeid: int | None, title: str, number: str | None,
    subtitle: bool = False, magnet: bool = True,
) -> None:
    service.repo.add_resource(
        Resource(
            source="sehuatang", tid=tid, forum_id=fid, forum_name=f"板块{fid}", type_id=typeid, type_name=f"分类{typeid}",
            title=title, number=number, magnet=f"magnet:?xt=urn:btih:{tid:040d}&dn=a" if magnet else None,
            info_hash=f"{tid:040d}" if magnet else None, size=2 * 1024**3, posted="2026-10-07 06:04:06", subtitle=subtitle,
        )
    )


def rss_items(text: str) -> list[ET.Element]:
    return list(ET.fromstring(text).iter("item"))


def test_resource_rss_filters(service: Service, tmp_path: Path) -> None:
    add_typed(service, 1, 36, 369, "HEYZO-1234 标题", "HEYZO-1234")
    add_typed(service, 2, 36, 370, "加勒比 123456-789", None)
    add_typed(service, 3, 103, 480, "OPPW-160 [自提征用]字幕版", "OPPW-160", subtitle=True)
    add_typed(service, 4, 103, 480, "ABC-004 没磁力", "ABC-004", magnet=False)
    with TestClient(create_app(Env(data_dir=tmp_path, password=""), service=service)) as c:

        def guids(query: str) -> list[str]:
            r = c.get(f"/rss/sehuatang.xml{query}")
            assert r.status_code == 200 and "application/rss+xml" in r.headers["content-type"]
            return [i.findtext("guid") or "" for i in rss_items(r.text)]

        assert guids("") == ["sehuatang-3", "sehuatang-1"]  # 默认: 有磁力且有番号, 新帖在前
        assert guids("?numbered=0") == ["sehuatang-3", "sehuatang-2", "sehuatang-1"]
        assert guids("?fid=36") == ["sehuatang-1"]
        assert guids("?fid=36,103&typeid=480") == ["sehuatang-3"]
        assert guids("?subtitle=1") == ["sehuatang-3"]
        assert guids("?q=HEYZO") == ["sehuatang-1"]
        assert guids("?limit=1") == ["sehuatang-3"]
        assert c.get("/rss/sehuatang.xml?fid=abc").status_code == 400

        item = rss_items(c.get("/rss/sehuatang.xml?fid=36").text)[0]
        assert item.findtext("title") == "HEYZO-1234 标题"
        assert item.findtext("link") == "https://www.sehuatang.net/thread-1-1-1.html"
        enclosure = item.find("enclosure")
        assert enclosure is not None and enclosure.get("url") == f"magnet:?xt=urn:btih:{1:040d}&dn=a"
        assert item.findtext("pubDate") == "Tue, 06 Oct 2026 22:04:06 GMT"  # 北京时间 06:04 → UTC

        page = c.get("/resources?forum=36&typeid=369").text
        assert "HEYZO-1234" in page and "加勒比" not in page
        assert "/rss/sehuatang.xml?fid=36&amp;typeid=369" in page


def test_resource_rss_title_gets_number_prefix_when_missing(service: Service, tmp_path: Path) -> None:
    add_typed(service, 5, 36, 368, "【無】素人 FC2PPV-4988506", "FC2-PPV-4988506")
    with TestClient(create_app(Env(data_dir=tmp_path, password=""), service=service)) as c:
        (item,) = rss_items(c.get("/rss/sehuatang.xml").text)
        assert item.findtext("title") == "FC2-PPV-4988506 【無】素人 FC2PPV-4988506"


def test_resource_rss_requires_key_with_password(service: Service, tmp_path: Path) -> None:
    with TestClient(create_app(Env(data_dir=tmp_path, password="pw"), service=service)) as c:
        assert c.get("/rss/sehuatang.xml").status_code == 401
        assert c.get("/rss/sehuatang.xml?fid=36&key=pw").status_code == 200


def test_old_database_gets_type_id_column(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE resource (id INTEGER PRIMARY KEY, source TEXT NOT NULL, tid INTEGER NOT NULL,"
        " forum_id INTEGER NOT NULL, forum_name TEXT NOT NULL DEFAULT '', type_name TEXT NOT NULL DEFAULT '',"
        " title TEXT NOT NULL, number TEXT, magnet TEXT, info_hash TEXT, size INTEGER, posted TEXT,"
        " subtitle INTEGER NOT NULL DEFAULT 0, wanted_id INTEGER, crawled_at REAL NOT NULL, UNIQUE (source, tid))"
    )
    conn.execute("INSERT INTO resource(source, tid, forum_id, title, crawled_at) VALUES('sehuatang', 9, 36, 'old', 0)")
    conn.commit()
    conn.close()
    (old,) = Repo(db).resources()
    assert old.title == "old" and old.type_id is None
