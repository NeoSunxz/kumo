"""番号识别与规范化.

规范形式: 字母前缀大写 + ``-`` + 数字 (至少 3 位, 不足补零), 例如 ``MNGS-005``;
FC2 统一为 ``FC2-PPV-1234567``. 文件名与磁力标题里的番号往往夹在别的标识中间
(``第一会所@SIS001@MNGS-005``、``[H265 1080p]``), 因此识别出全部候选, 由调用方决定取哪个.

无码厂牌与欧美的写法跟 Amane (``amane/parsing/file_info.py``) 保持一致, 两边按番号对得上:
一本道 / 加勒比 / 10musume 是六位日期加序号 (``061025_001``、``052725-001``, 分隔符照原样),
H4610 / C0930 / H0930 是 ``H4610-KI220417``, 欧美是 ``站点.YY.MM.DD`` (``Vixen.26.09.24``).
"""

from __future__ import annotations

import re

_FC2 = re.compile(r"FC2[\s_-]*(?:PPV[\s_-]*)?(\d{5,8})", re.I)
_H4610 = re.compile(r"(?<![A-Z0-9])(H4610|C0930|H0930)[\s_-]*([A-Z]+\d{4,})", re.I)
_HEYZO = re.compile(r"HEYZO[\s_-]*(\d{3,5})", re.I)
# 六位日期 + 序号: 一本道 061025_001 / 加勒比 052725-001 / 10musume 052425_01. 只在带厂牌标记时认, 以免把日期当番号.
_SIX_DIGIT = re.compile(r"(?<!\d)(\d{6}[-_]\d{2,4})(?!\d)")
_SIX_DIGIT_STUDIOS = re.compile(r"1pon|carib|10mu|paco|muramura|mura|heydouga|天然むすめ|一本道|加勒比|东京热", re.I)
# 欧美: 站点.YY.MM.DD 或 站点.20YY.MM.DD
_WESTERN = re.compile(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]{1,30})[.\s-](?:20)?(\d{2})[.-](\d{2})[.-](\d{2})(?!\d)")
_WESTERN_NAMES = {"tushyraw": "TushyRaw", "blackedraw": "BlackedRaw", "wgp": "WhenGirlsPlay", "x-art": "X-Art"}
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


def western_number(text: str) -> str | None:
    """欧美的 ``站点.YY.MM.DD``; 写法同 Amane: 站点名小写去掉符号后首字母大写."""
    m = _WESTERN.search(text)
    if not m:
        return None
    site = _WESTERN_NAMES.get(m.group(1).lower(), m.group(1))
    return (site.lower().replace("-", "").replace(".", "") + f".{m.group(2)}.{m.group(3)}.{m.group(4)}").capitalize()


def is_uncensored_number(number: str) -> bool:
    """无码厂牌的番号 (HEYZO、一本道等六位日期号、H4610 系列)."""
    upper = number.upper()
    return bool(
        upper.startswith(("HEYZO-", "H4610-", "C0930-", "H0930-", "KIN8-", "XXX-AV-")) or re.fullmatch(r"\d{6}[-_]\d{2,4}", number)
    )


def find_numbers(text: str) -> list[str]:
    """按出现顺序返回文本里的全部番号 (已规范化, 去重)."""
    found: list[str] = []
    for m in _FC2.finditer(text):
        found.append(f"FC2-PPV-{m.group(1)}")
    for m in _H4610.finditer(text):
        found.append(f"{m.group(1).upper()}-{m.group(2).upper()}")
    for m in _HEYZO.finditer(text):
        found.append(f"HEYZO-{int(m.group(1)):04d}")
    if _SIX_DIGIT_STUDIOS.search(text):
        found += [m.group(1) for m in _SIX_DIGIT.finditer(text)]
    # 已认出 FC2 / 无码厂牌番号时, 剩下的片段 (厂牌名、H4610 后半段) 不再当普通番号.
    cleaned = "" if found else text
    for m in _STD.finditer(cleaned):
        prefix = m.group(1).upper()
        if prefix.lstrip("0123456789") in _NOISE_PREFIXES or prefix in _NOISE_PREFIXES:
            continue
        found.append(normalize(prefix, m.group(2) or m.group(3)))
    if not found and (western := western_number(text)) is not None:
        # 欧美只在没有普通番号时认, 免得把「某词 2025.07.15」这类日期当成欧美番号.
        found.append(western)
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
