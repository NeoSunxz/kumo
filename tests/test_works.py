"""磁力订阅 (sukebei / RSS)、挑选作品 (片源与磁力按番号合并)、磁力订阅页与分散的设置表单."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import fixture_text
from kumo import service as service_module
from kumo.db import Repo
from kumo.magnets import Fetcher, Sukebei, parse_sukebei_items
from kumo.models import LibraryFile, MagnetSource, Release, Resource, SourceFilter, Subscription, WantedStatus, Work
from kumo.service import Service
from kumo.settings import CrawlForum, Env, InputFeed, Settings, SukebeiRule
from kumo.web import create_app

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


def source(service: Service, kind: str = "sukebei", *, auto: bool = False, **params: object) -> MagnetSource:
    flt = params.pop("filter", SourceFilter())
    src = service.repo.add_source(MagnetSource(kind=kind, name=f"{kind}-测试", params=params, auto_download=auto, filter=flt))  # type: ignore[arg-type]
    assert src.id
    return src


def add_res(service: Service, tid: int, title: str, number: str | None, *, src: MagnetSource | None = None,
            size: int = 5 * GiB, seeders: int | None = None, magnet: bool = True) -> Resource:
    return service.repo.add_resource(
        Resource(
            source="sukebei", tid=tid, forum_id=0, forum_name="sukebei", type_name="规则", title=title, number=number,
            magnet=f"magnet:?xt=urn:btih:{tid:040x}" if magnet else None, info_hash=f"{tid:040x}" if magnet else None,
            size=size, seeders=seeders, source_id=src.id if src else None, posted="Tue, 07 Oct 2026 08:00:00 -0000",
        )
    )


def release(service: Service, number: str, date: str, sub_name: str = "某人") -> Subscription:
    sub = service.repo.add_subscription(Subscription(source="javbus", kind="star", ident=sub_name, uncensored=False, name=sub_name))
    assert sub.id
    service.repo.add_release(Release(source="javbus", number=number, title=f"{number} 作品", date=date, cover=None, url="u"), sub.id)
    return sub


# --- sukebei ---


def test_parse_sukebei_items_keeps_id_and_trusted() -> None:
    items = parse_sukebei_items(fixture_text("sukebei_MNGS-005.xml"))
    assert items[0].id == 4344176 and items[0].trusted and items[0].category == "Real Life - Videos"
    assert items[1].id == 4343914 and not items[1].trusted


def test_rss_url() -> None:
    assert Sukebei.rss_url(query="中文字幕", user="abc", category="2_2", trusted_only=True) == (
        "https://sukebei.nyaa.si/?page=rss&q=%E4%B8%AD%E6%96%87%E5%AD%97%E5%B9%95&c=2_2&f=2&u=abc"
    )


async def test_sukebei_source_fills_pool_with_filters(service: Service, sukebei: dict[str, str]) -> None:
    src = source(service, query="中文字幕", min_seeders=2, filter=SourceFilter(tags_none=["western"], exclude=["4K"]))
    sukebei[Sukebei.rss_url(query="中文字幕", category="2_2")] = rss(
        (101, "[中文字幕] ABC-001 好片", 5, "5.0 GiB", True),
        (102, "ABC-002 4K", 9, "20.0 GiB", True),  # 排除词
        (103, "ABC-003", 1, "4.0 GiB", False),  # 做种不够
        (104, "Blacked.26.09.25.Gizelle [中文字幕]", 9, "2.0 GiB", True),  # 不要欧美
        (105, "没有番号的合集", 9, "40.0 GiB", True),
    )
    await service.run_sukebei()
    pool = {r.tid: r for r in service.repo.resources(sources=["sukebei"])}
    assert sorted(pool) == [101, 105]
    assert pool[101].seeders == 5 and pool[101].subtitle and pool[101].source_id == src.id and pool[101].size == 5 * GiB
    assert service.repo.list_wanted() == []  # 没开自动: 入池等挑选
    assert all(service.repo.is_seen(src.id or 0, str(t)) for t in (102, 103, 104))

    await service.run_sukebei()  # 再爬一次不重复入库
    assert len(service.repo.resources(sources=["sukebei"])) == 2


async def test_sukebei_auto_download_only_accepts_matching_magnets(service: Service, sukebei: dict[str, str]) -> None:
    service.repo.save_settings(Settings(app_id="test", download_path="/云下载", size_max_gb=12))
    source(service, auto=True, query="x")
    sukebei[Sukebei.rss_url(query="x", category="2_2")] = rss(
        (201, "ABC-011 1080p", 5, "5.0 GiB", True),
        (202, "ABC-012 超大", 5, "30.0 GiB", True),  # 超出体积上限, 留在磁力池等挑选
    )
    await service.run_sukebei()
    w = service.repo.wanted_by_number("ABC-011")
    assert w and w.status is WantedStatus.QUEUED
    assert service.repo.wanted_by_number("ABC-012") is None
    assert [i.number for i in service.works(origin="magnet")] == ["ABC-012"]


async def test_number_search_uses_sukebei_pool(service: Service) -> None:
    service.repo.save_settings(Settings(app_id="test", download_path="/云下载", sites=["sehuatang"]))
    add_res(service, 301, "ABC-021 中文字幕", "ABC-021", seeders=4)
    service.add_numbers(["ABC-021"])
    w = service.repo.wanted_by_number("ABC-021")
    assert w and await service.search(w) is WantedStatus.QUEUED
    (cand,) = service.repo.candidates(w.id)
    assert cand.site == "sukebei" and cand.seeders == 4


# --- RSS ---


async def test_rss_source(service: Service, monkeypatch: pytest.MonkeyPatch) -> None:
    feed = """<rss version="2.0"><channel>
      <item><title>ABC-031 中文字幕</title><guid>a</guid><link>magnet:?xt=urn:btih:{h}</link></item>
      <item><title>ABC-032</title><guid>b</guid><link>https://x/view/2</link></item>
      <item><title>ABC-033 预告</title><guid>c</guid><link>magnet:?xt=urn:btih:{h}</link></item>
    </channel></rss>""".replace("{h}", "c" * 40)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=feed)

    real = httpx.AsyncClient
    monkeypatch.setattr(service_module.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    src = source(service, "rss", url="https://feed/rss", filter=SourceFilter(exclude=["预告"]))
    await service.run_feeds()
    pool = {r.number: r for r in service.repo.resources(sources=["rss"])}
    assert set(pool) == {"ABC-031", "ABC-032"}
    assert pool["ABC-031"].magnet and "subtitle" in pool["ABC-031"].tags and pool["ABC-032"].magnet is None
    assert {i.number for i in service.works(origin="magnet")} == {"ABC-031", "ABC-032"}

    service.repo.update_source(src.id or 0, auto_download=True)
    await service.run_feeds()  # 见过的条目不再处理
    assert service.repo.list_wanted() == []


# --- 挑选作品: 两边按番号合并 ---


def test_works_merge_release_and_pool(service: Service) -> None:
    src = source(service)
    release(service, "ABC-041", "2026-10-01", "女优甲")
    add_res(service, 1, "abc-041 [中文字幕]", "ABC-041", src=src)
    add_res(service, 2, "ABC-041 4K", "ABC-041", src=src)
    add_res(service, 3, "ABF-386 [新模型无码破解]x", "ABF-386", src=src)
    release(service, "ABC-042", "2026-09-01", "女优乙")
    service.repo.save_work(Work(number="ABC-041", found=True, title="Amane 标题", actors=["女优甲"]))

    items = {i.number: i for i in service.works()}
    assert set(items) == {"ABC-041", "ABF-386", "ABC-042"}
    a = items["ABC-041"]
    assert a.release and a.subs_name == "女优甲" and len(a.magnets) == 2 and a.sources == ["sukebei-测试"]
    assert a.title == "Amane 标题" and a.tags >= {"subtitle", "4k"}
    assert items["ABF-386"].release is None and items["ABF-386"].tags == {"leak"}

    assert {i.number for i in service.works(origin="subs")} == {"ABC-041", "ABC-042"}
    assert {i.number for i in service.works(origin="magnet")} == {"ABC-041", "ABF-386"}
    assert [i.number for i in service.works(tags=["leak"])] == ["ABF-386"]
    assert {i.number for i in service.works(source_ids=[src.id or 0])} == {"ABC-041", "ABF-386"}
    assert [i.number for i in service.works(search="386")] == ["ABF-386"]


def test_works_todo_hides_handled(service: Service) -> None:
    for n, d in (("ABC-051", "2026-10-01"), ("ABC-052", "2026-10-02"), ("ABC-053", "2026-10-03"), ("ABC-054", "2026-10-04")):
        release(service, n, d)
    service.add_numbers(["ABC-051"])
    service.repo.replace_library_root("/影片", [LibraryFile(fid="f", name="ABC-052.mp4", number="ABC-052")])
    service.repo.dismiss_numbers(["ABC-053"])
    assert [i.number for i in service.works()] == ["ABC-054"]
    every = {i.number: i for i in service.works(todo=False)}
    assert [i.number for i in service.works(todo=False)] == ["ABC-054", "ABC-053", "ABC-052", "ABC-051"]  # 按日期新到旧
    assert every["ABC-051"].wanted and every["ABC-052"].owned and every["ABC-053"].dismissed
    service.repo.undismiss_numbers(["ABC-053"])
    assert {i.number for i in service.works()} == {"ABC-053", "ABC-054"}


def test_works_page_queue_and_dismiss(service: Service, tmp_path: Path) -> None:
    src = source(service)
    release(service, "ABC-061", "2026-10-01")
    add_res(service, 1, "ABC-062 [中文字幕]", "ABC-062", src=src)
    add_res(service, 2, "ABC-063", "ABC-063", src=src)
    with TestClient(create_app(Env(data_dir=tmp_path, password=""), service=service)) as c:
        page = c.get("/works").text
        assert "ABC-061" in page and "ABC-062" in page and "片源" in page and "磁力" in page
        assert "ABC-061" not in c.get("/works?origin=magnet").text
        assert "ABC-062" in c.get("/works?tag=subtitle").text and "ABC-063" not in c.get("/works?tag=subtitle").text
        assert c.get("/works?sub=x").status_code == 400

        r = c.post("/works/queue", data={"ids": ["ABC-061", "ABC-062"]}, headers={"referer": "http://testserver/works?origin=subs"},
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/works?origin=subs&message=")
        assert service.repo.wanted_by_number("ABC-061") and service.repo.wanted_by_number("ABC-062")
        c.post("/works/dismiss", data={"only": "ABC-063"})
        assert service.works() == []
        assert "取消忽略" in c.get("/works?todo=0").text
        c.post("/works/undismiss", data={"only": "ABC-063"})
        assert [i.number for i in service.works()] == ["ABC-063"]
        assert c.post("/works/nope").status_code == 400

        # 旧地址跳到挑选作品
        assert c.get("/pool", follow_redirects=False).headers["location"] == "/works?origin=magnet"
        assert c.get("/releases?sub=3", follow_redirects=False).headers["location"] == "/works?origin=subs&sub=3"


def test_new_unhandled_count_in_nav(service: Service, tmp_path: Path) -> None:
    release(service, "ABC-071", "2026-10-01")
    add_res(service, 1, "ABC-072", "ABC-072")
    add_res(service, 2, "ABC-071", "ABC-071")
    assert service.repo.new_unhandled_count(0) == 2
    service.add_numbers(["ABC-071"])
    assert service.repo.new_unhandled_count(0) == 1


# --- 磁力订阅页 ---


def test_sources_page_crud(service: Service, tmp_path: Path) -> None:
    with TestClient(create_app(Env(data_dir=tmp_path, password=""), service=service)) as c:
        assert "还没有磁力订阅" in c.get("/sources").text
        r = c.post("/sources", data={"kind": "sehuatang", "fid": "36", "tags_any": ["leak"], "brackets": "破解，新模型",
                                     "exclude": "预告", "auto_download": "on"}, follow_redirects=False)
        assert r.status_code == 303
        (src,) = service.repo.sources()
        assert src.name == "板块 36" and src.params == {"fid": 36, "typeid": None} and src.auto_download
        assert src.filter == SourceFilter(tags_any=["leak"], brackets=["破解", "新模型"], exclude=["预告"])
        page = c.get("/sources").text
        assert "要破解" in page and "片头[破解]" in page and "-预告" in page

        c.post(f"/sources/{src.id}", data={"kind": "sehuatang", "name": "无码破解", "fid": "36", "typeid": "369", "tags_none": ["western"]})
        src = service.repo.source(src.id or 0)
        assert src and src.name == "无码破解" and src.params["typeid"] == 369 and src.filter.tags_none == ["western"]
        assert not src.auto_download  # 编辑表单没勾就是关

        c.post(f"/sources/{src.id}/toggle")
        c.post(f"/sources/{src.id}/auto")
        src = service.repo.source(src.id or 0)
        assert src and not src.enabled and src.auto_download

        for bad in ({"kind": "sehuatang", "fid": "abc"}, {"kind": "sukebei", "min_seeders": "x"}, {"kind": "rss", "url": "ftp://x"},
                    {"kind": "nope"}):
            r = c.post("/sources", data=bad, follow_redirects=False)
            assert "%E5%A4%B1%E8%B4%A5" in r.headers["location"], bad  # 「失败」
        c.post("/sources", data={"kind": "sukebei", "query": "中文字幕", "trusted_only": "on", "min_seeders": "3"})
        c.post("/sources", data={"kind": "rss", "url": "https://feed.example/rss"})
        kinds = {s.kind: s for s in service.repo.sources()}
        assert kinds["sukebei"].params == {"query": "中文字幕", "user": "", "category": "2_2", "trusted_only": True, "min_seeders": 3}
        assert kinds["sukebei"].name == "中文字幕" and kinds["rss"].name == "feed.example"

        add_res(service, 1, "ABC-081", "ABC-081", src=src)
        c.post(f"/sources/{src.id}/delete")
        assert service.repo.source(src.id or 0) is None and service.repo.resources()[0].source_id is None  # 磁力保留
        assert c.post("/sources/999/toggle").status_code == 404


def test_crawl_params_saved_from_sources_page(service: Service, tmp_path: Path) -> None:
    service.repo.save_settings(Settings(app_id="keep", size_max_gb=8, prefer_subtitle=False))
    with TestClient(create_app(Env(data_dir=tmp_path, password=""), service=service)) as c:
        r = c.post("/settings", data={"_back": "/sources", "crawl_pages": "5", "crawl_base_url": "https://new.example/"},
                   follow_redirects=False)
        assert r.headers["location"].startswith("/sources?flash=")
        s = service.repo.settings()
        # 只改表单里的字段, 其余保持
        assert s.crawl_pages == 5 and s.crawl_base_url == "https://new.example" and s.app_id == "keep" and s.size_max_gb == 8
        assert not s.prefer_subtitle
        r = c.post("/settings", data={"_back": "/sources", "crawl_pages": "99"}, follow_redirects=False)
        assert "%E5%A4%B1%E8%B4%A5" in r.headers["location"] and service.repo.settings().crawl_pages == 5


# --- 迁移 ---


def test_old_settings_become_sources(repo: Repo) -> None:
    repo.save_settings(Settings(
        crawl_enabled=True, crawl_forums=[CrawlForum(fid=103), CrawlForum(fid=36, typeid=369, auto_download=True, exclude_keywords=["VR"])],
        sukebei_rules=[SukebeiRule(name="中字", query="中文字幕", min_seeders=3, exclude_keywords=["4K"])],
        input_feeds=[InputFeed(url="https://a/rss", include_keywords=["中文"])],
    ))
    service = Service(repo)
    srcs = service.repo.sources()
    # 按优先顺序 (先建的在前)
    assert [(s.kind, s.name) for s in srcs] == [("sehuatang", "板块 103"), ("sehuatang", "板块 36:369"), ("sukebei", "中字"), ("rss", "a/rss")]
    assert srcs[1].auto_download and srcs[1].filter.exclude == ["VR"] and srcs[0].filter == SourceFilter()
    assert srcs[2].params["min_seeders"] == 3 and srcs[2].filter.exclude == ["4K"]
    assert srcs[3].auto_download and srcs[3].filter.include == ["中文"]
    Service(repo)  # 只迁移一次
    assert len(repo.sources()) == 4


def test_default_forums_not_migrated_when_crawl_was_off(repo: Repo) -> None:
    repo.save_settings(Settings())
    assert Service(repo).repo.sources() == []


def test_old_database_gets_pool_columns_and_tags(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE resource (id INTEGER PRIMARY KEY, source TEXT NOT NULL, tid INTEGER NOT NULL, forum_id INTEGER NOT NULL,"
        " forum_name TEXT NOT NULL DEFAULT '', type_id INTEGER, type_name TEXT NOT NULL DEFAULT '', title TEXT NOT NULL,"
        " number TEXT, magnet TEXT, info_hash TEXT, size INTEGER, posted TEXT, subtitle INTEGER NOT NULL DEFAULT 0,"
        " wanted_id INTEGER, crawled_at REAL NOT NULL, UNIQUE (source, tid))"
    )
    conn.execute("INSERT INTO resource(source, tid, forum_id, title, number, magnet, crawled_at)"
                 " VALUES('sehuatang', 1, 103, 'HMN-904 [新模型无码破解]x', 'HMN-904', 'm', 0)")
    conn.commit()
    conn.close()
    (r,) = Repo(path).resources()
    assert r.seeders is None and r.tags == {"leak"} and r.brackets == ["新模型无码破解"] and r.source_id is None


async def test_sukebei_preview_explains_each_item(service: Service, sukebei: dict[str, str]) -> None:
    src = source(service, query="x", trusted_only=True, min_seeders=3, filter=SourceFilter(tags_any=["subtitle"]))
    sukebei[Sukebei.rss_url(query="x", category="2_2", trusted_only=True)] = rss(
        (1, "ABC-001 中文字幕", 5, "5.0 GiB", True),
        (2, "ABC-002 中文字幕", 1, "5.0 GiB", True),
        (3, "ABC-003 中文字幕", 9, "5.0 GiB", False),
        (4, "ABC-004", 9, "5.0 GiB", True),
    )
    rows = await service.preview_source(src)
    assert [r.reason for r in rows] == ["", "做种不足 3", "上传者不可信", "没有中字"]
    assert rows[0].seeders == 5 and rows[0].size == 5 * GiB
    assert service.repo.resources() == []


# --- 磁力选择的排序: 档次 › 来源顺序 › 日期 › 分数 ---


async def test_choose_order_tier_then_source_then_date(service: Service, tmp_path: Path) -> None:
    service.repo.save_settings(Settings(app_id="test", download_path="/云下载", sites=["sehuatang"]))
    t98 = service.repo.add_source(MagnetSource(kind="sehuatang", name="98堂", params={"fid": 103}))
    suk = service.repo.add_source(MagnetSource(kind="sukebei", name="sukebei", params={}))

    def res(tid: int, src: MagnetSource, title: str, posted: str) -> None:
        service.repo.add_resource(Resource(
            source=src.kind, tid=tid, forum_id=0, forum_name="", type_name="", title=title, number="ABC-100",
            magnet=f"magnet:?xt=urn:btih:{tid:040x}", info_hash=f"{tid:040x}", size=5 * GiB, posted=posted, source_id=src.id,
        ))

    res(1, suk, "ABC-100 中文字幕", "Tue, 07 Oct 2026 08:00:00 -0000")
    res(2, t98, "ABC-100 [中文字幕]", "2026-10-01 06:00:00")
    res(3, suk, "ABC-100 [无码破解] 中文字幕", "Tue, 07 Oct 2026 08:00:00 -0000")
    res(4, t98, "ABC-100 [新模型无码破解] 中文字幕 旧", "2026-09-01 06:00:00")
    res(5, t98, "ABC-100 [新模型无码破解] 中文字幕 新", "2026-10-05 06:00:00")
    res(6, t98, "ABC-100 无字幕", "2026-10-06 06:00:00")
    service.add_numbers(["ABC-100"])
    w = service.repo.wanted_by_number("ABC-100")
    assert w
    await service.search(w)
    from kumo.select import ranked

    order = [c.info_hash for c in ranked(service.repo.candidates(w.id), service.settings)]
    # 98 破解中字 (新的在前) › sukebei 破解中字 › 98 中字 › sukebei 中字 › 其它
    assert order == [f"{t:040x}" for t in (5, 4, 3, 2, 1, 6)]
    w = service.repo.wanted(w.id)
    assert w and (chosen := service.repo.candidate(w.chosen_id or 0)) and chosen.info_hash == f"{5:040x}"
    assert chosen.origin == "98堂" and chosen.rank == 0

    # 调换订阅顺序后, 同档次里 sukebei 优先
    service.repo.move_source(suk.id or 0, -1)
    await service.search(w)
    order = [c.info_hash for c in ranked(service.repo.candidates(w.id), service.settings)]
    assert order[:2] == [f"{3:040x}", f"{5:040x}"]

    with TestClient(create_app(Env(data_dir=tmp_path, password=""), service=service)) as c:
        page = c.get(f"/wanted/{w.id}").text
        assert "破解+中字" in page and "2026-10-07" in page and "2026-10-05" in page


def test_day_normalizes_dates() -> None:
    from kumo.select import day

    assert day("2026-10-7 06:00") == "2026-10-07"
    assert day("Tue, 07 Oct 2026 08:00:00 -0000") == "2026-10-07"
    assert day("") is None and day("not a date") is None


def test_tags_all_filter_and_move(service: Service, tmp_path: Path) -> None:
    flt = SourceFilter(tags_all=["subtitle", "leak"])
    assert flt.reject_reason("ABF-386 [无码破解]x", {"leak"}, ["无码破解"]) == "没有中字"
    assert flt.accepts("x", {"leak", "subtitle"}, [])
    assert flt.summary() == ["要中字+破解"]
    with TestClient(create_app(Env(data_dir=tmp_path, password=""), service=service)) as c:
        c.post("/sources", data={"kind": "sehuatang", "fid": "103", "tags_all": ["subtitle", "leak"]})
        c.post("/sources", data={"kind": "sukebei", "query": "x"})
        a, b = service.repo.sources()
        assert a.filter.tags_all == ["subtitle", "leak"]
        c.post(f"/sources/{b.id}/up")
        assert [s.id for s in service.repo.sources()] == [b.id, a.id]
        c.post(f"/sources/{b.id}/up")  # 已经在最前, 不变
        assert [s.id for s in service.repo.sources()] == [b.id, a.id]
        assert "#1" in c.get("/sources").text


def test_empty_filter_values_mean_all(service: Service, tmp_path: Path) -> None:
    """表单里选「全部」会提交空串, 不能报 422."""
    release(service, "ABC-110", "2026-10-01")
    with TestClient(create_app(Env(data_dir=tmp_path, password=""), service=service)) as c:
        for url in ("/works?origin=&sub=&src=&days=&todo=1&q=", "/resources?forum=&typeid=&src=&q=",
                    "/rss/releases.xml?days=&sub=", "/works?days=7"):
            r = c.get(url)
            assert r.status_code == 200, url
        assert "ABC-110" in c.get("/works?days=").text


def test_delete_resources_by_source_and_subscription(service: Service, tmp_path: Path) -> None:
    a, b = source(service), source(service)
    add_res(service, 1, "ABC-201", "ABC-201", src=a)
    add_res(service, 2, "ABC-202", "ABC-202", src=b)
    service.repo.add_resource(Resource(source="sehuatang", tid=3, forum_id=103, forum_name="", type_name="", title="ABC-203", number="ABC-203"))
    service.repo.mark_seen(a.id or 0, "99")
    service.add_numbers(["ABC-201"])
    with TestClient(create_app(Env(data_dir=tmp_path, password=""), service=service)) as c:
        assert "全部磁力" in c.get(f"/resources?src={a.id}").text and "的全部磁力</button>" in c.get(f"/resources?src={a.id}").text
        r = c.post("/resources/delete", data={"src": str(a.id)}, follow_redirects=False)
        assert r.status_code == 303 and "1" in r.headers["location"]
        assert {x.tid for x in service.repo.resources()} == {2, 3}
        assert not service.repo.is_seen(a.id or 0, "99")  # 清掉见过的记录, 下次重新爬
        assert service.repo.wanted_by_number("ABC-201")  # 队列不受影响
        c.post("/resources/delete", data={"source": "sehuatang"})
        assert {x.tid for x in service.repo.resources()} == {2}
        assert c.post("/resources/delete", data={}).status_code == 400


def test_works_page_shows_more(service: Service, tmp_path: Path) -> None:
    for i in range(130):
        add_res(service, 1000 + i, f"ABC-{300 + i}", f"ABC-{300 + i}")
    with TestClient(create_app(Env(data_dir=tmp_path, password=""), service=service)) as c:
        page = c.get("/?origin=magnet").text
        assert page.count('name="ids"') == 120 and "显示更多" in page and "n=240" in page
        page = c.get("/?origin=magnet&n=240").text
        assert page.count('name="ids"') == 130 and "显示更多</a>" not in page
