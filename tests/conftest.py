from __future__ import annotations

from pathlib import Path

import pytest

from kumo.db import Repo
from kumo.pan115 import FakePan115
from kumo.service import Service
from kumo.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path / "kumo.db")


@pytest.fixture
def fake115() -> FakePan115:
    pan = FakePan115()
    pan.mkdir("/云下载")
    return pan


@pytest.fixture
def service(repo: Repo, fake115: FakePan115) -> Service:
    repo.save_settings(Settings(app_id="test", download_path="/云下载", sites=["fake"]))
    return Service(repo, client_factory=lambda s, r: fake115)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")
