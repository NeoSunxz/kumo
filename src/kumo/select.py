"""磁力打分与选择.

流程: 合并各站点结果 (按 info hash 去重) → 识别标签 → 硬性条件 (番号匹配、体积、排除词、
必须字幕) 不满足的标记 ``rejected`` → 其余排序, 第一个自动选中.
没有任何候选通过硬性条件时返回 ``None``, 由调用方转为「待人工选择」.

排序 (``rank_key``) 是分层的, 前一层相同才比后一层:
1. 档次: 破解 + 中字 > 中字 > 破解 > 其它 (按设置的「优先中字」「优先破解」)
2. 来源: 磁力订阅按「磁力订阅」页的顺序, 按番号搜索各站的结果在所有订阅之后
3. 日期: 新的优先
4. 分数: 清晰度、避开 H265、做种数、体积
"""

from __future__ import annotations

import email.utils
import re

from .models import Candidate, Tag
from .numbers import contains_number, extract_number, has_subtitle_suffix, is_uncensored_number, western_number
from .settings import Settings

_GB = 1024**3
# 「无字幕」「無字幕」「没有字幕」不算.
_SUBTITLE_WORDS = re.compile(r"(?<![无無没])(?<!没有)(?:中文字幕|中字|字幕)|chinese\s*sub|\[ch\]|\bch\b", re.I)
_UHD_WORDS = re.compile(r"\b4k\b|2160p|\buhd\b", re.I)
_HD_WORDS = re.compile(r"1080p|\bfhd\b|\bhd\b|高清", re.I)
_H265_WORDS = re.compile(r"h\.?265|x265|hevc", re.I)
_LEAK_WORDS = re.compile(r"破解|流出|leak|reducing\s*mosaic|去码|去碼|无码化|無碼化", re.I)
_UNCENSORED_WORDS = re.compile(r"无码|無碼|uncensored|無修正", re.I)
_WESTERN_WORDS = re.compile(r"欧美|歐美|western")
_BRACKETS = re.compile(r"\[([^\[\]]{1,30})\]|【([^【】]{1,30})】")


def brackets(title: str) -> list[str]:
    """标题里方括号中的标记 (片头 ``[新模型无码破解]``、``[中文字幕]``、``[自提征用]``)."""
    return [a or b for a, b in _BRACKETS.findall(title)]


def title_tags(title: str, number: str | None = None) -> set[str]:
    """从标题识别标签; ``number`` 为空时自己从标题里识别番号."""
    number = number if number is not None else (extract_number(title) or "")
    return detect_tags(Candidate(site="", title=title, magnet="", info_hash=""), number)


def detect_tags(cand: Candidate, number: str) -> set[str]:
    tags = set(cand.tags)
    title = cand.title
    if _SUBTITLE_WORDS.search(title) or has_subtitle_suffix(title, number):
        tags.add(Tag.SUBTITLE)
    if _UHD_WORDS.search(title):
        tags.add(Tag.UHD)
    if _HD_WORDS.search(title):
        tags.add(Tag.HD)
    if _H265_WORDS.search(title):
        tags.add(Tag.H265)
    if _LEAK_WORDS.search(title):
        tags.add(Tag.LEAK)
    elif (number and is_uncensored_number(number)) or _UNCENSORED_WORDS.search(title):
        # 破解是有码作品去马赛克, 与无码厂牌原版分开; 「无码破解」只算破解.
        tags.add(Tag.UNCENSORED)
    if (number and "." in number and western_number(number)) or _WESTERN_WORDS.search(title):
        tags.add(Tag.WESTERN)
    if number.startswith("FC2-") or re.search(r"FC2[\s_-]*PPV", title, re.I):
        tags.add(Tag.FC2)
    return tags


def merge(candidates: list[Candidate]) -> list[Candidate]:
    """同一个 info hash 在多个站点出现时合并: 标签取并集, 做种数取最大, 大小与标题取先到的非空值."""
    merged: dict[str, Candidate] = {}
    for cand in candidates:
        cur = merged.get(cand.info_hash)
        if cur is None:
            merged[cand.info_hash] = cand
            continue
        cur.tags |= cand.tags
        cur.seeders = max(filter(None, (cur.seeders, cand.seeders)), default=None)
        cur.size = cur.size or cand.size
        cur.published = cur.published or cand.published
        if cand.rank < cur.rank:
            cur.rank, cur.origin = cand.rank, cand.origin
        if cand.site not in cur.site.split("+"):
            cur.site = f"{cur.site}+{cand.site}"
    return list(merged.values())


def evaluate(cand: Candidate, number: str, s: Settings) -> None:
    """就地写入 ``tags``、``rejected`` 与 ``score``."""
    cand.tags = detect_tags(cand, number)
    reasons: list[str] = []
    if not contains_number(cand.title, number):
        reasons.append("标题与番号不符")
    if cand.size is not None:
        if s.size_min_gb and cand.size < s.size_min_gb * _GB:
            reasons.append(f"小于 {s.size_min_gb:g}GB")
        if s.size_max_gb and cand.size > s.size_max_gb * _GB:
            reasons.append(f"大于 {s.size_max_gb:g}GB")
    lowered = cand.title.lower()
    hit = next((k for k in s.exclude_keywords if k and k.lower() in lowered), None)
    if hit:
        reasons.append(f"含排除词「{hit}」")
    if s.require_subtitle and Tag.SUBTITLE not in cand.tags:
        reasons.append("没有字幕")
    if not s.allow_4k and Tag.UHD in cand.tags:
        reasons.append("4K (未允许)")
    cand.rejected = "；".join(reasons)

    score = 0.0
    if s.prefer_subtitle and Tag.SUBTITLE in cand.tags:
        score += 100
    if Tag.UHD in cand.tags:
        score += 40
    elif Tag.HD in cand.tags:
        score += 30
    if s.avoid_h265 and Tag.H265 in cand.tags:
        score -= 40
    if s.prefer_leak and Tag.LEAK in cand.tags:
        score += 50
    if cand.seeders is not None:
        score += min(cand.seeders, 50) * 0.5
    if cand.size:
        # 同等条件下偏向体积大的 (码率高), 但影响很小.
        score += min(cand.size / _GB, 10)
    cand.score = round(score, 1)


def tier(cand: Candidate, s: Settings) -> int:
    """档次: 破解 + 中字 (3) > 中字 (2) > 破解 (1) > 其它 (0); 对应偏好关掉时不算."""
    return 2 * (s.prefer_subtitle and Tag.SUBTITLE in cand.tags) + (s.prefer_leak and Tag.LEAK in cand.tags)


def rank_key(cand: Candidate, s: Settings) -> tuple[bool, int, int, str, float]:
    """越大越好; 按它降序排列, 第一个就是自动选择的."""
    return (not cand.rejected, tier(cand, s), -cand.rank, day(cand.published) or "", cand.score)


def ranked(candidates: list[Candidate], s: Settings) -> list[Candidate]:
    return sorted(candidates, key=lambda c: rank_key(c, s), reverse=True)


def choose(candidates: list[Candidate], number: str, s: Settings) -> Candidate | None:
    for cand in candidates:
        evaluate(cand, number, s)
    accepted = [c for c in candidates if not c.rejected]
    return max(accepted, key=lambda c: rank_key(c, s)) if accepted else None


def day(text: str | None) -> str | None:
    """各处的日期写法统一成 ``2026-10-07``: 98堂的 ``2026-10-7 06:00``, sukebei / RSS 的 ``Tue, 07 Oct 2026 ...``."""
    if not text:
        return None
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    try:
        return email.utils.parsedate_to_datetime(text).date().isoformat()
    except (TypeError, ValueError):
        return None
