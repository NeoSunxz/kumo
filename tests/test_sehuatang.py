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
from kumo.models import MagnetSource, Resource, SourceFilter, Tag, WantedStatus
from kumo.sehuatang import Sehuatang, ThreadDetail, ThreadRef, forum_path, is_age_gate, parse_forum, parse_thread
from kumo.service import Service
from kumo.settings import Env, Settings
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
    assert d.images == [
        "https://tu.djhdhs.us/tupian/forum/202610/07/060350w3ir5qr3er4kni89.jpg",
        "https://tu.djhdhs.us/tupian/forum/202610/07/060350e50l0oam035qkqpm.jpg",
    ]


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


def forum_source(service: Service, fid: int = 103, typeid: int | None = None, *, enabled: bool = True,
                 auto: bool = False, **flt: object) -> MagnetSource:
    service.repo.save_settings(Settings(app_id="test", download_path="/云下载", sites=["sehuatang"], crawl_pages=3))
    src = service.repo.add_source(
        MagnetSource(kind="sehuatang", name=f"板块 {fid}", params={"fid": fid, "typeid": typeid}, enabled=enabled,
                     auto_download=auto, filter=SourceFilter(**flt))  # type: ignore[arg-type]
    )
    assert src.id
    return src


async def test_crawl_indexes_new_threads_and_stops_at_seen(service: Service, forum: type[FakeForum]) -> None:
    src = forum_source(service)
    forum.pages[103] = [[ref(3, "MNGS-005 新"), ref(2, "BF-042-C 新")], [ref(1, "OLD-001")]]
    forum.details = {3: detail(3, "MNGS-005 新"), 2: detail(2, "BF-042-C 新", magnet=False), 1: detail(1, "OLD-001")}
    await service.run_crawl()
    items = {r.tid: r for r in service.repo.resources()}
    assert set(items) == {1, 2, 3}
    assert items[3].number == "MNGS-005" and items[3].subtitle and items[3].info_hash == f"{3:040d}"
    assert items[3].type_id == 480 and items[3].source_id == src.id and Tag.SUBTITLE in items[3].tags
    assert items[2].magnet is None
    assert service.repo.list_wanted() == []  # 默认只入池, 等挑选
    assert (s := service.repo.source(src.id or 0)) and s.last_run and not s.last_error  # type: ignore[union-attr]

    # 第二轮: 第一页出现已爬过的帖子, 不再翻页.
    forum.page_calls.clear()
    forum.pages[103] = [[ref(4, "NEW-004"), ref(3, "MNGS-005 新")], [ref(2, "x")]]
    forum.details[4] = detail(4, "NEW-004")
    await service.run_crawl()
    assert forum.page_calls == [(103, 1)]
    assert service.repo.resource_exists("sehuatang", 4)


async def test_crawl_skips_failed_thread_and_retries_later(service: Service, forum: type[FakeForum]) -> None:
    forum_source(service)
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
    forum_source(service, 36, 369)
    await service.run_crawl()
    assert forum.page_calls == [(36, 1, 369)]


async def test_disabled_source_only_runs_when_asked(service: Service, forum: type[FakeForum]) -> None:
    src = forum_source(service, enabled=False)
    forum.pages[103] = [[ref(60, "ABC-060")]]
    forum.details[60] = detail(60, "ABC-060")
    await service.run_crawl()  # 定时任务只爬启用的
    assert forum.page_calls == []
    await service.run_crawl(source_id=src.id)  # 订阅上点「立即爬取」: 停用的也爬
    assert service.repo.resource_exists("sehuatang", 60)


async def test_filter_by_title_before_opening_thread(service: Service, forum: type[FakeForum]) -> None:
    """亚洲无码板块只要破解: 列表标题不符合的不打开帖子, 记为见过, 下次也不再看."""
    src = forum_source(service, 36, tags_any=["leak"])
    forum.pages[36] = [[
        ref(70, "HMN-904 [新模型无码破解]我的巨乳", "无码高清"),
        ref(71, "HEYZO-3381 [自提征用]责备和被责备", "无码高清"),
        ref(72, "[中文字幕]Vixen.26.09.24.Dolly V酒店", "无码高清"),
    ]]
    forum.details = {70: detail(70, "HMN-904 [新模型无码破解]我的巨乳"), 71: detail(71, "x"), 72: detail(72, "y")}
    opened: list[int] = []
    original = FakeForum.thread

    async def thread(self: FakeForum, fetcher: object, tid: int) -> ThreadDetail:
        opened.append(tid)
        return await original(self, fetcher, tid)

    FakeForum.thread = thread  # type: ignore[method-assign]
    try:
        await service.run_crawl()
    finally:
        FakeForum.thread = original  # type: ignore[method-assign]
    assert opened == [70]
    (r,) = service.repo.resources()
    assert r.number == "HMN-904" and r.tags == {"leak"} and r.brackets == ["新模型无码破解"]
    assert service.repo.is_seen(src.id or 0, "71") and service.repo.is_seen(src.id or 0, "72")


@pytest.mark.parametrize(
    ("flt", "title", "typ", "expected"),
    [
        ({}, "ABC-001", "有码高清", True),
        ({"types": ["无码高清"]}, "ABC-001", "有码高清", False),
        ({"include": ["中文"]}, "ABC-001 中文", "", True),
        ({"include": ["中文"]}, "ABC-001", "", False),
        ({"exclude": ["VR"]}, "ABC-001 vr版", "", False),
        ({"tags_any": ["leak"]}, "ABF-386 [新模型无码破解]x", "", True),
        ({"tags_any": ["leak"]}, "HEYZO-3381 [自提征用]x", "", False),
        ({"tags_none": ["western"]}, "[中文字幕]Blacked.26.09.25.Gizelle", "", False),
        ({"brackets": ["破解"]}, "ABF-386 [新模型无码破解]x", "", True),
        ({"brackets": ["破解"]}, "ABF-386 破解 x", "", False),
        ({"numbered": True}, "没有番号的合集", "", False),
    ],
)
def test_source_filter(flt: dict[str, object], title: str, typ: str, expected: bool) -> None:
    from kumo.numbers import extract_number
    from kumo.select import brackets, title_tags

    number = extract_number(title)
    assert SourceFilter(**flt).accepts(title, title_tags(title, number), brackets(title), typ, number) is expected  # type: ignore[arg-type]


async def test_auto_download_respects_filters_and_rules(service: Service, forum: type[FakeForum]) -> None:
    service.repo.save_settings(Settings(app_id="test", download_path="/云下载", size_max_gb=12))
    forum_source(service, auto=True, types=["有码高清"], exclude=["预告"])
    service.repo.save_settings(Settings(app_id="test", download_path="/云下载", size_max_gb=4))
    forum.pages[103] = [[ref(10, "AAA-010", "有码高清"), ref(11, "BBB-011", "无码高清"), ref(12, "CCC-012 预告")]]
    for tid, title in ((10, "AAA-010"), (11, "BBB-011"), (12, "CCC-012 预告")):
        forum.details[tid] = detail(tid, title)
    await service.run_crawl()
    assert service.repo.list_wanted() == []  # 5GB 超出选择规则的上限, 只入池
    assert [r.tid for r in service.repo.resources()] == [10]  # 其余不符合入池条件

    service.repo.save_settings(Settings(app_id="test", download_path="/云下载", size_max_gb=12))
    forum.pages[103] = [[ref(13, "DDD-013", "有码高清"), ref(10, "AAA-010", "有码高清")]]
    forum.details[13] = detail(13, "DDD-013")
    await service.run_crawl()
    (w,) = service.repo.list_wanted()
    assert w.number == "DDD-013" and w.status is WantedStatus.QUEUED and w.source == "sehuatang:103"


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


def test_resources_page(service: Service, tmp_path: Path) -> None:
    r = add_res(service, 50, "ABC-050 [新模型无码破解]标题", "ABC-050")
    with TestClient(create_app(Env(data_dir=tmp_path, password=""), service=service)) as c:
        page = c.get("/resources")
        assert page.status_code == 200 and "ABC-050 [新模型无码破解]标题" in page.text and "还没有启用的磁力订阅" in page.text
        assert ">破解<" in page.text  # 旧数据也补上了标签
        assert "ABC-050" in c.get("/resources?tag=leak").text and "ABC-050" not in c.get("/resources?tag=western").text
        assert c.get("/resources?forum=103&q=ABC").status_code == 200
        resp = c.post(f"/resources/{r.id}/download", follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"].startswith("/wanted/")
        assert c.post("/resources/99999/download").status_code == 400


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


# --- 爬取出错 ---


async def test_source_error_is_recorded_and_others_continue(service: Service, forum: type[FakeForum]) -> None:
    bad = forum_source(service, 36)
    good = forum_source(service, 103)
    forum.pages[103] = [[ref(80, "ABC-080")]]
    forum.details[80] = detail(80, "ABC-080")
    original = FakeForum.forum_page

    async def forum_page(self: FakeForum, fetcher: object, fid: int, page: int, typeid: int | None = None) -> object:
        if fid == 36:
            raise SiteError("板块不存在")
        return await original(self, fetcher, fid, page, typeid)

    FakeForum.forum_page = forum_page  # type: ignore[method-assign]
    try:
        await service.run_guarded("crawl", service.run_crawl)
    finally:
        FakeForum.forum_page = original  # type: ignore[method-assign]
    assert service.repo.resource_exists("sehuatang", 80)
    assert (b := service.repo.source(bad.id or 0)) and b.last_error == "板块不存在"
    assert (g := service.repo.source(good.id or 0)) and not g.last_error
    assert "板块不存在" in service.last_error["crawl"]


def test_run_button_gives_feedback(service: Service, tmp_path: Path) -> None:
    with TestClient(create_app(Env(data_dir=tmp_path, password=""), service=service)) as c:
        r = c.post("/run/crawl", headers={"referer": "http://testserver/resources?forum=36"}, follow_redirects=False)
        assert "没有启用的论坛 sehuatang磁力订阅" in c.get(r.headers["location"]).text
        forum_source(service)
        r = c.post("/run/crawl", headers={"referer": "http://testserver/resources?forum=36&flash=old"}, follow_redirects=False)
        location = r.headers["location"]
        assert location.startswith("/resources?forum=36&flash=") and location.count("flash=") == 1
        assert "已开始论坛爬取" in c.get(location).text

        service.running["crawl"] = 0.0  # 模拟正在运行
        r = c.post("/run/crawl", headers={"referer": "http://testserver/resources"}, follow_redirects=False)
        page = c.get(r.headers["location"]).text
        assert "论坛爬取已经在运行" in page and "正在运行：论坛爬取" in page
        assert c.post("/run/bogus").status_code == 404


# --- 帖子图片与预览 ---


async def test_crawl_keeps_thread_images_and_serves_them_with_referer(
    service: Service, forum: type[FakeForum], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forum_source(service)
    forum.pages[103] = [[ref(90, "ABC-090")]]
    forum.details[90] = detail(90, "ABC-090")
    forum.details[90].images = ["https://img.example/a.jpg"]
    await service.run_crawl()
    (r,) = service.repo.resources()
    assert r.images == ["https://img.example/a.jpg"]

    calls: list[tuple[str, str]] = []

    async def get_with_referer(self: object, url: str, referer: str, *, proxy: str = "") -> Path:
        calls.append((url, referer))
        path = tmp_path / "a.jpg"
        path.write_bytes(b"\xff\xd8")
        return path

    monkeypatch.setattr("kumo.images.ImageCache.get_with_referer", get_with_referer)
    with TestClient(create_app(Env(data_dir=tmp_path, password=""), service=service)) as c:
        assert c.get(f"/img/res/{r.id}/0").content == b"\xff\xd8"
        assert calls == [("https://img.example/a.jpg", "https://www.sehuatang.net/")]
        assert c.get(f"/img/res/{r.id}/1").status_code == 404 and c.get("/img/res/999/0").status_code == 404
        # 没有资料站封面与 Amane 时, 挑选作品用帖子的图
        assert f"/img/res/{r.id}/0" in c.get("/works").text


async def test_preview_does_not_store_or_open_threads(service: Service, forum: type[FakeForum], tmp_path: Path) -> None:
    src = forum_source(service, 36, tags_any=["leak"])
    forum.pages[36] = [[ref(70, "HMN-904 [新模型无码破解]x", "无码高清"), ref(71, "HEYZO-3381 [自提征用]y", "无码高清")]]
    rows = await service.preview_source(src)
    assert [(r.number, r.reason) for r in rows] == [("HMN-904", ""), ("HEYZO-3381", "没有破解")]
    assert rows[0].brackets == ["新模型无码破解"] and rows[1].tags == {"uncensored"}
    assert service.repo.resources() == [] and not service.repo.is_seen(src.id or 0, "71")

    with TestClient(create_app(Env(data_dir=tmp_path, password=""), service=service)) as c:
        # 编辑表单里改了条件 (不要无码) 还没保存, 预览按改过的算
        page = c.post(f"/sources/{src.id}/preview", data={"kind": "sehuatang", "fid": "36", "tags_none": ["uncensored"]}).text
        assert "1</b> 条会入池" in page and "有无码" in page
        page = c.post("/sources/preview", data={"kind": "sehuatang", "fid": "36"}).text
        assert "2</b> 条会入池" in page and "没有保存" in page
        assert len(service.repo.sources()) == 1
        r = c.post("/sources/preview", data={"kind": "sehuatang", "fid": "x"}, follow_redirects=False)
        assert r.status_code == 303


async def test_subtitle_board_counts_as_subtitle_for_filters(service: Service, forum: type[FakeForum]) -> None:
    """字幕板块的帖子标题里不一定写「中文字幕」; 入池条件「必须有中字」不能因此挡掉."""
    src = forum_source(service, 103, tags_any=["subtitle"])
    forum.pages[103] = [[ref(95, "SERO-0124 [自提征用]空姐")]]
    forum.details[95] = detail(95, "SERO-0124 [自提征用]空姐")
    assert [r.reason for r in await service.preview_source(src)] == [""]
    await service.run_crawl()
    (r,) = service.repo.resources()
    assert r.number == "SERO-124" and "subtitle" in r.tags


async def test_preview_saved_source_from_list(service: Service, forum: type[FakeForum], tmp_path: Path) -> None:
    src = forum_source(service, 36, tags_any=["leak"])
    forum.pages[36] = [[ref(70, "HMN-904 [新模型无码破解]x", "无码高清"), ref(71, "HEYZO-3381 [自提征用]y", "无码高清")]]
    with TestClient(create_app(Env(data_dir=tmp_path, password=""), service=service)) as c:
        page = c.post(f"/sources/{src.id}/preview").text  # 不带表单: 按已保存的「只要破解」
        assert "1</b> 条会入池" in page and "没有破解" in page
