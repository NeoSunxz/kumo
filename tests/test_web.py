from __future__ import annotations

import base64
from pathlib import Path

from fastapi.testclient import TestClient

from kumo.models import WantedStatus
from kumo.service import Service
from kumo.settings import Env
from kumo.web import create_app


def client_for(service: Service, tmp_path: Path, password: str = "") -> TestClient:
    return TestClient(create_app(Env(data_dir=tmp_path, password=password), service=service))


def test_pages_render(service: Service, tmp_path: Path) -> None:
    with client_for(service, tmp_path) as c:
        for path in ("/", "/settings", "/auth", "/library", "/events", "/health"):
            assert c.get(path).status_code == 200, path


def test_add_numbers_and_detail(service: Service, tmp_path: Path) -> None:
    with client_for(service, tmp_path) as c:
        r = c.post("/wanted", data={"numbers": "mngs-005\nBF-042-C.mp4, ddhh006\n\n"}, follow_redirects=False)
        assert r.status_code == 303
        numbers = {w.number for w in service.repo.list_wanted()}
        assert numbers == {"MNGS-005", "BF-042", "DDHH-006"}
        w = service.repo.wanted_by_number("MNGS-005")
        assert w
        assert "MNGS-005" in c.get(f"/wanted/{w.id}").text
        assert c.get("/wanted/99999").status_code == 404
        assert c.post(f"/wanted/{w.id}/bogus").status_code == 400
        assert c.post(f"/wanted/{w.id}/magnet", data={"magnet": "nope"}).status_code == 400
        c.post(f"/wanted/{w.id}/ignore")
        assert service.repo.wanted(w.id).status is WantedStatus.IGNORED  # type: ignore[union-attr]
        c.post(f"/wanted/{w.id}/delete")
        assert service.repo.wanted(w.id) is None


def test_settings_roundtrip_and_validation(service: Service, tmp_path: Path) -> None:
    with client_for(service, tmp_path) as c:
        form = {
            "app_id": " 123 ", "download_path": "/云下载", "sync_paths": "/影片\n\n/其它\n",
            "site_sukebei": "on", "size_min_gb": "1", "size_max_gb": "10", "prefer_subtitle": "on",
            "exclude_keywords": "sample, 预告", "input_feeds": "https://a/rss | 中文 | 4K\nhttps://b/rss",
            "submit_batch": "3",
        }
        r = c.post("/settings", data=form)
        assert "已保存" in r.text
        s = service.repo.settings()
        assert s.app_id == "123" and s.sync_paths == ["/影片", "/其它"] and s.sites == ["sukebei"]
        assert s.exclude_keywords == ["sample", "预告"] and not s.avoid_h265
        assert s.input_feeds[0].include_keywords == ["中文"] and s.input_feeds[0].exclude_keywords == ["4K"]
        assert s.input_feeds[1].url == "https://b/rss" and s.submit_batch == 3

        r = c.post("/settings", data={**form, "submit_batch": "999"})
        assert "保存失败" in r.text
        assert service.repo.settings().submit_batch == 3


def test_rss_output(service: Service, tmp_path: Path) -> None:
    service.add_numbers(["MNGS-005", "BF-042"])
    w = service.repo.wanted_by_number("MNGS-005")
    assert w
    service.repo.update_wanted(w.id, status=WantedStatus.SUBMITTED, submitted_at=1.0)
    with client_for(service, tmp_path) as c:
        r = c.get("/rss.xml")
        assert r.status_code == 200 and "application/rss+xml" in r.headers["content-type"]
        assert "<title>MNGS-005</title>" in r.text
        assert "BF-042" not in r.text  # 还没提交的不输出


def test_password_protects_ui_but_rss_accepts_key(service: Service, tmp_path: Path) -> None:
    with client_for(service, tmp_path, password="pw") as c:
        assert c.get("/").status_code == 401
        assert c.get("/health").status_code == 200
        assert c.get("/rss.xml").status_code == 401
        assert c.get("/rss.xml?key=wrong").status_code == 401
        assert c.get("/rss.xml?key=pw").status_code == 200
        good = base64.b64encode(b"any:pw").decode()
        bad = base64.b64encode(b"any:nope").decode()
        assert c.get("/", headers={"Authorization": f"Basic {good}"}).status_code == 200
        assert c.get("/", headers={"Authorization": f"Basic {bad}"}).status_code == 401
        assert c.get("/", headers={"Authorization": "Basic !!!"}).status_code == 401


def test_auth_start_requires_app_id(service: Service, tmp_path: Path) -> None:
    # 测试服务用的是内存 115 客户端, 不是开放平台客户端, 因此不能发起扫码.
    with client_for(service, tmp_path) as c:
        assert c.post("/auth/start").status_code == 400
        assert c.get("/auth/poll").json()["status"] == -1
