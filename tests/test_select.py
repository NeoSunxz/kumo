import pytest

from conftest import fixture_text
from kumo.magnets import parse_javbus_magnets, parse_sukebei_rss
from kumo.models import Candidate, Tag
from kumo.select import choose, detect_tags, evaluate, merge
from kumo.settings import Settings

GB = 1024**3


def cand(title: str, size_gb: float | None = 5, seeders: int | None = None, h: str = "a" * 40, tags: set[str] | None = None) -> Candidate:
    return Candidate(
        site="t", title=title, magnet=f"magnet:?xt=urn:btih:{h}", info_hash=h,
        size=int(size_gb * GB) if size_gb is not None else None, seeders=seeders, tags=set(tags or ()),
    )


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("MNGS-005C", {Tag.SUBTITLE}),
        ("MNGS-005-C.mp4", {Tag.SUBTITLE}),
        ("MNGS-005 中文字幕", {Tag.SUBTITLE}),
        ("[H265 1080p] MNGS-005", {Tag.H265, Tag.HD}),
        ("MNGS-005 4K 2160p", {Tag.UHD}),
        ("[Reducing Mosaic] MNGS-005", {Tag.LEAK}),
        ("MNGS-005-破解", {Tag.LEAK}),
        ("MNGS-005", set()),
        ("MNGS-005-CD1", set()),
    ],
)
def test_detect_tags(title: str, expected: set[str]) -> None:
    assert detect_tags(cand(title), "MNGS-005") == expected


@pytest.mark.parametrize(
    ("c", "settings", "reason"),
    [
        (cand("MNGS-004"), Settings(), "番号"),
        (cand("MNGS-005", size_gb=0.5), Settings(), "小于 1GB"),
        (cand("MNGS-005", size_gb=20), Settings(), "大于 12GB"),
        (cand("MNGS-005 sample"), Settings(exclude_keywords=["Sample"]), "排除词"),
        (cand("MNGS-005"), Settings(require_subtitle=True), "没有字幕"),
        (cand("MNGS-005 4K"), Settings(allow_4k=False), "4K"),
    ],
)
def test_hard_rules_reject(c: Candidate, settings: Settings, reason: str) -> None:
    evaluate(c, "MNGS-005", settings)
    assert reason in c.rejected


def test_hard_rules_pass_and_unknown_size_is_not_rejected() -> None:
    for c in (cand("MNGS-005"), cand("MNGS-005", size_gb=None), cand("MNGS-005 4K"), cand("MNGS-005", size_gb=30)):
        evaluate(c, "MNGS-005", Settings(allow_4k=True, size_max_gb=0))
        assert c.rejected == "", c.title


def test_choose_prefers_subtitle_then_avoids_h265() -> None:
    plain = cand("MNGS-005 1080p", h="1" * 40, seeders=40)
    sub = cand("MNGS-005-C", h="2" * 40)
    h265_sub = cand("[H265] MNGS-005-C", h="3" * 40, seeders=50)
    assert choose([plain, sub, h265_sub], "MNGS-005", Settings()) is sub
    assert choose([plain, h265_sub], "MNGS-005", Settings()) is h265_sub
    assert choose([plain, h265_sub], "MNGS-005", Settings(prefer_subtitle=False)) is plain


def test_choose_returns_none_when_everything_rejected() -> None:
    assert choose([cand("MNGS-005", size_gb=0.2), cand("OTHER-001", h="b" * 40)], "MNGS-005", Settings()) is None
    assert choose([], "MNGS-005", Settings()) is None


def test_merge_combines_same_hash_across_sites() -> None:
    a = cand("MNGS-005", seeders=3, tags={Tag.HD})
    a.site = "sukebei"
    b = cand("MNGS-005C", seeders=None, size_gb=None, tags={Tag.SUBTITLE})
    b.site = "javbus"
    merged = merge([a, b])
    assert len(merged) == 1
    assert merged[0].tags == {Tag.HD, Tag.SUBTITLE}
    assert merged[0].seeders == 3
    assert merged[0].site == "sukebei+javbus"


def test_choose_on_real_site_results() -> None:
    found = parse_sukebei_rss(fixture_text("sukebei_MNGS-005.xml")) + parse_javbus_magnets(fixture_text("javbus_magnets_MNGS-005.html"))
    candidates = merge(found)
    chosen = choose(candidates, "MNGS-005", Settings())
    assert chosen is not None
    # 真实数据里有带字幕的 4.98GB 版本, 应当被选中.
    assert Tag.SUBTITLE in chosen.tags
    assert chosen.info_hash == "61abb869985ebdf581c16347ae3d70aa68f7ab92"
