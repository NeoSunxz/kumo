import hashlib
import time
import xml.etree.ElementTree as ET

import pytest

from kumo.feeds import parse_feed, render_rss, torrent_to_magnet
from kumo.models import Wanted, WantedStatus

H = "61abb869985ebdf581c16347ae3d70aa68f7ab92"

RSS = f"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>t</title>
<item><title>MNGS-005 磁力在 enclosure</title><guid>g1</guid>
  <enclosure url="magnet:?xt=urn:btih:{H}&amp;dn=x" type="application/x-bittorrent"/></item>
<item><title>BF-042-C 只有种子</title><link>https://example.test/a.torrent</link></item>
<item><title>DDHH-006 只有番号</title><link>https://example.test/page</link></item>
<item><title>没有番号</title><description>magnet:?xt=urn:btih:{H}</description></item>
</channel></rss>"""

ATOM = f"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>
<entry><title>MNGS-005</title><id>urn:1</id><link href="magnet:?xt=urn:btih:{H}"/></entry>
</feed>"""


def test_parse_rss_variants() -> None:
    entries = parse_feed(RSS)
    assert [e.number for e in entries] == ["MNGS-005", "BF-042", "DDHH-006", None]
    assert entries[0].magnet and H in entries[0].magnet and entries[0].key == "g1"
    assert entries[1].magnet is None and entries[1].torrent_url == "https://example.test/a.torrent"
    assert entries[2].magnet is None and entries[2].torrent_url is None
    assert entries[3].magnet is not None  # 磁力在正文里也能识别


def test_parse_atom() -> None:
    (entry,) = parse_feed(ATOM)
    assert entry.number == "MNGS-005" and entry.magnet and entry.key == "urn:1"


@pytest.mark.parametrize("text", ["", "not xml", "<html><body>"])
def test_parse_feed_rejects_garbage(text: str) -> None:
    with pytest.raises(ValueError):
        parse_feed(text)


def test_torrent_to_magnet_hashes_raw_info_dict() -> None:
    info = b"d6:lengthi1024e4:name8:test.mp412:piece lengthi16384e6:pieces20:" + b"x" * 20 + b"e"
    torrent = b"d8:announce14:http://tracker4:info" + info + b"e"
    magnet = torrent_to_magnet(torrent, "test")
    assert magnet == f"magnet:?xt=urn:btih:{hashlib.sha1(info).hexdigest()}&dn=test"


@pytest.mark.parametrize("data", [b"", b"l1:ae", b"d8:announce3:abce", b"not bencode"])
def test_torrent_to_magnet_rejects_invalid(data: bytes) -> None:
    with pytest.raises((ValueError, IndexError)):
        torrent_to_magnet(data)


def test_render_rss_title_is_number_only_and_xml_escaped() -> None:
    now = time.time()
    w = Wanted(
        id=1, number="MNGS-005", status=WantedStatus.SUBMITTED, source="manual", note="", chosen_id=1, info_hash=H,
        percent=0, searched_at=now, submitted_at=now, done_at=None, created_at=now, updated_at=now,
    )
    xml = render_rss([(w, "标题 <&> 测试")], link="http://kumo/")
    root = ET.fromstring(xml)
    item = root.find("channel/item")
    assert item is not None
    assert item.findtext("title") == "MNGS-005"
    assert item.findtext("guid") == "kumo-MNGS-005"
    assert item.findtext("description") == "标题 <&> 测试"
