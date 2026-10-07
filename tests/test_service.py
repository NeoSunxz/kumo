from __future__ import annotations

import pytest

from kumo import magnets
from kumo.magnets import Fetcher, MagnetSite, SiteError
from kumo.models import Candidate, WantedStatus
from kumo.pan115 import FakePan115, OfflineTask
from kumo.service import Service
from kumo.settings import Settings

GB = 1024**3


class FakeSite(MagnetSite):
    name = "fake"

    def __init__(self) -> None:
        self.results: dict[str, list[Candidate]] = {}
        self.fail = False

    async def search(self, fetcher: Fetcher, number: str) -> list[Candidate]:
        if self.fail:
            raise SiteError("站点挂了")
        return [Candidate(**vars(c)) for c in self.results.get(number, [])]


def mag(h: str, title: str, size_gb: float = 5) -> Candidate:
    return Candidate(site="fake", title=title, magnet=f"magnet:?xt=urn:btih:{h}", info_hash=h, size=int(size_gb * GB))


@pytest.fixture
def site(monkeypatch: pytest.MonkeyPatch) -> FakeSite:
    s = FakeSite()
    monkeypatch.setitem(magnets.SITES, "fake", s)
    return s


async def test_full_lifecycle(service: Service, fake115: FakePan115, site: FakeSite) -> None:
    site.results["MNGS-005"] = [mag("1" * 40, "MNGS-005"), mag("2" * 40, "MNGS-005-C")]
    assert service.add_numbers(["MNGS-005", "MNGS-005"]) == (1, 1)

    await service.run_search()
    w = service.repo.wanted_by_number("MNGS-005")
    assert w and w.status is WantedStatus.QUEUED
    chosen = service.repo.candidate(w.chosen_id)
    assert chosen and chosen.info_hash == "2" * 40  # 字幕版优先

    await service.run_submit()
    w = service.repo.wanted_by_number("MNGS-005")
    assert w and w.status is WantedStatus.SUBMITTED and w.info_hash == "2" * 40
    assert fake115.added == [(chosen.magnet, fake115.folders["/云下载"])]

    fake115.task_list["2" * 40] = OfflineTask(info_hash="2" * 40, name="x", status=1, percent=42.5, file_id="", size=0)
    await service.run_tasks()
    w = service.repo.wanted_by_number("MNGS-005")
    assert w and w.status is WantedStatus.DOWNLOADING and w.percent == 42.5

    fake115.task_list["2" * 40].status = 2
    await service.run_tasks()
    w = service.repo.wanted_by_number("MNGS-005")
    assert w and w.status is WantedStatus.DONE and w.done_at


async def test_no_candidates_and_site_errors(service: Service, site: FakeSite) -> None:
    service.add_numbers(["ABC-001"])
    await service.run_search()
    w = service.repo.wanted_by_number("ABC-001")
    assert w and w.status is WantedStatus.NO_MAGNET and w.note == "未搜到磁力"

    site.fail = True
    service.repo.update_wanted(w.id, status=WantedStatus.PENDING)
    await service.run_search()
    w = service.repo.wanted_by_number("ABC-001")
    assert w and w.status is WantedStatus.NO_MAGNET and "站点挂了" in w.note


async def test_need_choice_then_manual_choice(service: Service, site: FakeSite, fake115: FakePan115) -> None:
    site.results["ABC-002"] = [mag("3" * 40, "ABC-002", size_gb=0.3)]
    service.add_numbers(["ABC-002"])
    await service.run_search()
    w = service.repo.wanted_by_number("ABC-002")
    assert w and w.status is WantedStatus.NEED_CHOICE and w.chosen_id is None
    (c,) = service.repo.candidates(w.id)
    assert "小于" in c.rejected

    assert c.id is not None
    service.choose_candidate(w.id, c.id)
    await service.run_submit()
    w = service.repo.wanted_by_number("ABC-002")
    assert w and w.status is WantedStatus.SUBMITTED


async def test_mismatched_titles_only_means_no_magnet(service: Service, site: FakeSite) -> None:
    site.results["ABC-003"] = [mag("4" * 40, "ABC-0031 other")]
    service.add_numbers(["ABC-003"])
    await service.run_search()
    w = service.repo.wanted_by_number("ABC-003")
    assert w and w.status is WantedStatus.NO_MAGNET


def test_choose_candidate_rejects_foreign_candidate(service: Service) -> None:
    service.add_numbers(["ABC-004"])
    w = service.repo.wanted_by_number("ABC-004")
    assert w
    with pytest.raises(ValueError):
        service.choose_candidate(w.id, 99999)


def test_add_magnet_validates(service: Service) -> None:
    service.add_numbers(["ABC-005"])
    w = service.repo.wanted_by_number("ABC-005")
    assert w
    with pytest.raises(ValueError):
        service.add_magnet(w.id, "http://not-a-magnet")
    service.add_magnet(w.id, "magnet:?xt=urn:btih:" + "5" * 40)
    w = service.repo.wanted_by_number("ABC-005")
    assert w and w.status is WantedStatus.QUEUED


async def test_submit_waits_without_authorization_or_folder(repo, site: FakeSite) -> None:  # type: ignore[no-untyped-def]
    class Unauthorized(FakePan115):
        def authorized(self) -> bool:
            return False

    repo.save_settings(Settings(app_id="x", download_path="/云下载", sites=["fake"]))
    svc = Service(repo, client_factory=lambda s, r: Unauthorized())
    svc.add_numbers(["ABC-006"])
    w = repo.wanted_by_number("ABC-006")
    svc.add_magnet(w.id, "magnet:?xt=urn:btih:" + "6" * 40)
    await svc.run_submit()
    assert repo.wanted_by_number("ABC-006").status is WantedStatus.QUEUED
    assert "未授权" in svc.last_error["submit"]

    repo.save_settings(Settings(app_id="x", download_path="", sites=["fake"]))
    svc2 = Service(repo, client_factory=lambda s, r: FakePan115())
    await svc2.run_submit()
    assert repo.wanted_by_number("ABC-006").status is WantedStatus.QUEUED
    assert "保存目录" in svc2.last_error["submit"]


async def test_duplicate_task_counts_as_submitted(service: Service, fake115: FakePan115) -> None:
    h = "7" * 40
    fake115.task_list[h] = OfflineTask(info_hash=h, name="x", status=0, percent=0, file_id="", size=0)
    service.add_numbers(["ABC-007"])
    w = service.repo.wanted_by_number("ABC-007")
    assert w
    service.add_magnet(w.id, f"magnet:?xt=urn:btih:{h}")
    await service.run_submit()
    w = service.repo.wanted_by_number("ABC-007")
    assert w and w.status is WantedStatus.SUBMITTED and "已有" in w.note


async def test_failed_task(service: Service, fake115: FakePan115) -> None:
    service.add_numbers(["ABC-008"])
    w = service.repo.wanted_by_number("ABC-008")
    assert w
    service.add_magnet(w.id, "magnet:?xt=urn:btih:" + "8" * 40)
    await service.run_submit()
    fake115.task_list["8" * 40].status = -1
    await service.run_tasks()
    assert service.repo.wanted_by_number("ABC-008").status is WantedStatus.FAILED  # type: ignore[union-attr]


async def test_sync_marks_owned(repo, site: FakeSite) -> None:  # type: ignore[no-untyped-def]
    pan = FakePan115()
    pan.mkdir("/云下载")
    pan.mkdir("/影片", ["MNGS-005-C.mp4", "readme.txt", "BF-042.mkv"])
    repo.save_settings(Settings(app_id="x", download_path="/云下载", sync_paths=["/影片"], sites=["fake"]))
    svc = Service(repo, client_factory=lambda s, r: pan)
    svc.add_numbers(["MNGS-005", "DDHH-006"])
    await svc.run_sync()
    assert repo.wanted_by_number("MNGS-005").status is WantedStatus.OWNED
    assert repo.wanted_by_number("DDHH-006").status is WantedStatus.PENDING
    (f,) = repo.owned("MNGS-005")
    assert f.subtitle is True and f.root == "/影片"
    assert repo.owned("README") == []
    # 新加入的已拥有番号直接标记, 不再搜索
    svc.add_numbers(["BF-042"])
    assert repo.wanted_by_number("BF-042").status is WantedStatus.OWNED

    # 目录里的文件被删掉后, 下次同步不再算已拥有
    pan.files[pan.folders["/影片"]] = []
    await svc.run_sync()
    assert repo.owned("MNGS-005") == []


async def test_guarded_job_records_error(service: Service) -> None:
    async def boom() -> None:
        raise RuntimeError("坏了")

    await service.run_guarded("search", boom)
    assert "坏了" in service.last_error["search"]
    assert any("坏了" in e["message"] for e in service.repo.events())
