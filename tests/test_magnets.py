import base64

import pytest

from conftest import fixture_text
from kumo.magnets import SiteError, parse_javbus_magnets, parse_sukebei_rss
from kumo.models import Tag, info_hash_of, parse_size


def test_parse_sukebei_rss() -> None:
    items = parse_sukebei_rss(fixture_text("sukebei_MNGS-005.xml"))
    assert len(items) >= 3
    first = items[0]
    assert first.site == "sukebei"
    assert first.info_hash == "66ece2e0f9beea24e6647ab0b5ab26328ee8eb53"
    assert first.magnet.startswith("magnet:?xt=urn:btih:66ece2e0f9beea24e6647ab0b5ab26328ee8eb53")
    assert first.size == int(5.0 * 1024**3)
    assert first.seeders == 1
    assert "MNGS-005" in first.title
    assert all(len(i.info_hash) == 40 for i in items)


@pytest.mark.parametrize("text", ["<html>not rss", "", "<<<"])
def test_parse_sukebei_rejects_garbage(text: str) -> None:
    with pytest.raises(SiteError):
        parse_sukebei_rss(text)


def test_parse_sukebei_skips_items_without_hash() -> None:
    xml = '<rss xmlns:nyaa="https://sukebei.nyaa.si/xmlns/nyaa"><channel><item><title>x</title></item></channel></rss>'
    assert parse_sukebei_rss(xml) == []


def test_parse_javbus_magnets() -> None:
    items = parse_javbus_magnets(fixture_text("javbus_magnets_MNGS-005.html"))
    assert len(items) >= 6
    first = items[0]
    assert first.info_hash == "61abb869985ebdf581c16347ae3d70aa68f7ab92"
    assert first.title == "MNGS-005C"  # 标签按钮文字已从标题里去掉
    assert first.tags == {Tag.HD, Tag.SUBTITLE}
    assert first.size == int(4.98 * 1024**3)
    assert first.published == "2025-09-14"
    plain = next(i for i in items if i.info_hash == "6408923680fedce286d3e0395a1364834080bfc1")
    assert plain.tags == set()


def test_parse_javbus_empty() -> None:
    assert parse_javbus_magnets("<table></table>") == []


_HEX = "61abb869985ebdf581c16347ae3d70aa68f7ab92"
_B32 = base64.b32encode(bytes.fromhex(_HEX)).decode()


@pytest.mark.parametrize(
    ("magnet", "expected"),
    [
        (f"magnet:?xt=urn:btih:{_HEX.upper()}&dn=x", _HEX),
        (f"magnet:?xt=urn:btih:{_B32}", _HEX),
        (f"magnet:?xt=urn:btih:{_B32.lower()}&tr=udp://x", _HEX),
        ("magnet:?xt=urn:sha1:abc", None),
        ("not a magnet", None),
    ],
)
def test_info_hash_of(magnet: str, expected: str | None) -> None:
    assert info_hash_of(magnet) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("5.0 GiB", 5 * 1024**3), ("4.98GB", int(4.98 * 1024**3)), ("700 MiB", 700 * 1024**2), ("??", None)],
)
def test_parse_size(text: str, expected: int | None) -> None:
    assert parse_size(text) == expected
