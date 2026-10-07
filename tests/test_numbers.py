import pytest

from kumo.numbers import contains_number, extract_number, find_numbers, has_subtitle_suffix, parse_input


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("MNGS-005", "MNGS-005"),
        ("mngs005", "MNGS-005"),
        ("MNGS-5", "MNGS-005"),
        ("BF-042-C.mp4", "BF-042"),
        ("MNGS-042-破解-C.strm", "MNGS-042"),
        ("[H265 1080p] MNGS-005 【连线后视】", "MNGS-005"),
        ("+++ [FHDC] MNGS-005 【結合部】", "MNGS-005"),
        ("第一会所新片@SIS001@MNGS-005 高清", "MNGS-005"),
        ("300MIUM-123.mkv", "300MIUM-123"),
        ("FC2-PPV-1234567", "FC2-PPV-1234567"),
        ("fc2ppv_1234567 hd", "FC2-PPV-1234567"),
        ("FC2 1234567", "FC2-PPV-1234567"),
        ("DDHH-006 我的女友", "DDHH-006"),
        ("没有番号的标题", None),
        ("H265 1080P x264", None),
        ("", None),
    ],
)
def test_extract_number(text: str, expected: str | None) -> None:
    assert extract_number(text) == expected


def test_find_numbers_skips_noise_and_dedupes() -> None:
    assert find_numbers("SIS001@MNGS-005 MNGS005 HHD800") == ["MNGS-005"]


@pytest.mark.parametrize(
    ("text", "number", "expected"),
    [
        ("MNGS-005C 高清", "MNGS-005", True),
        ("MNGS-005-C.mp4", "MNGS-005", True),
        ("MNGS-005-UC.mp4", "MNGS-005", True),
        ("mngs005ch", "MNGS-005", True),
        ("MNGS-005.mp4", "MNGS-005", False),
        ("MNGS-005-CD1.mp4", "MNGS-005", False),
        ("MNGS-0051C", "MNGS-005", False),
    ],
)
def test_has_subtitle_suffix(text: str, number: str, expected: bool) -> None:
    assert has_subtitle_suffix(text, number) is expected


@pytest.mark.parametrize(
    ("text", "number", "expected"),
    [
        ("[FHDC] MNGS-005 xxx", "MNGS-005", True),
        ("MNGS-0050", "MNGS-005", False),
        ("MNGS-004", "MNGS-005", False),
        ("XMNGS-005", "MNGS-005", False),
    ],
)
def test_contains_number(text: str, number: str, expected: bool) -> None:
    assert contains_number(text, number) is expected


@pytest.mark.parametrize(
    ("line", "expected"),
    [("  mngs-005  ", "MNGS-005"), ("", None), ("   ", None), ("abc", "ABC")],
)
def test_parse_input(line: str, expected: str | None) -> None:
    assert parse_input(line) == expected
