"""番号识别与规范化.

规范形式: 字母前缀大写 + ``-`` + 数字 (至少 3 位, 不足补零), 例如 ``MNGS-005``;
FC2 统一为 ``FC2-PPV-1234567``. 文件名与磁力标题里的番号往往夹在别的标识中间
(``第一会所@SIS001@MNGS-005``、``[H265 1080p]``), 因此识别出全部候选, 由调用方决定取哪个.
"""

from __future__ import annotations

import re

_FC2 = re.compile(r"FC2[\s_-]*(?:PPV[\s_-]*)?(\d{5,8})", re.I)
# 前缀允许带 1~3 位数字 (300MIUM-123); 前后不能紧贴字母数字, 避免从长串中间截出番号.
# 有分隔符时数字可以只有 1 位 (MNGS-5), 没有分隔符至少 2 位, 以免把 ABC5 之类的串当成番号.
_STD = re.compile(r"(?<![A-Z0-9])(\d{0,3}[A-Z]{2,8})(?:[\s_-](\d{1,5})|(\d{2,5}))(?![0-9])", re.I)
# 常见的非番号片段: 编码、清晰度、论坛标识等.
_NOISE_PREFIXES = frozenset(
    {
        "SIS", "HHD", "FHD", "UHD", "HD", "MP", "AAC", "DTS", "AC", "BD", "WEB", "WEBDL", "HEVC", "AVC",
        "CD", "PART", "DISC", "VOL", "EP", "SE", "MP4", "MKV", "AVI", "TS", "CH", "HDR", "SDR", "BIT",
        "FPS", "KBPS", "MBPS", "GB", "MB", "PPV", "FHDC", "HJD", "HHB", "BIG",
    }
)
# 番号后的字幕 / 破解标记: MNGS-005-C, MNGS-005C, MNGS-005-UC, MNGS-005ch.
_SUBTITLE_SUFFIX = re.compile(r"^[\s_-]?(?:U?C|CH)(?![A-Z0-9])", re.I)


def normalize(prefix: str, digits: str) -> str:
    return f"{prefix.upper()}-{int(digits):03d}"


def find_numbers(text: str) -> list[str]:
    """按出现顺序返回文本里的全部番号 (已规范化, 去重)."""
    found: list[str] = []
    for m in _FC2.finditer(text):
        found.append(f"FC2-PPV-{m.group(1)}")
    cleaned = _FC2.sub(" ", text)
    for m in _STD.finditer(cleaned):
        prefix = m.group(1).upper()
        if prefix.lstrip("0123456789") in _NOISE_PREFIXES or prefix in _NOISE_PREFIXES:
            continue
        found.append(normalize(prefix, m.group(2) or m.group(3)))
    seen: set[str] = set()
    return [n for n in found if not (n in seen or seen.add(n))]


def extract_number(text: str) -> str | None:
    """取文本里最像番号的一个; 识别不到返回 ``None``."""
    numbers = find_numbers(text)
    return numbers[0] if numbers else None


def parse_input(text: str) -> str | None:
    """用户手动输入的一行: 先按番号识别, 识别不到时原样大写 (允许添加暂不认识的格式)."""
    text = text.strip()
    if not text:
        return None
    return extract_number(text) or text.upper()


def contains_number(text: str, number: str) -> bool:
    return number in find_numbers(text)


def has_subtitle_suffix(text: str, number: str) -> bool:
    """文件名或标题里, 番号后是否紧跟字幕标记 (``-C`` / ``C`` / ``-UC`` / ``ch``)."""
    prefix, _, digits = number.rpartition("-")
    pattern = re.compile(rf"(?<![A-Z0-9]){re.escape(prefix)}[\s_-]?0*{int(digits) if digits.isdigit() else digits}", re.I)
    for m in pattern.finditer(text):
        if _SUBTITLE_SUFFIX.match(text[m.end() :]):
            return True
    return False
