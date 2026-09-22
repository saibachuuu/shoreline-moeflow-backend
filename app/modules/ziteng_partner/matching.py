"""本地匹配：从候选里判断哪些是"疑似重复"。

紫藤只做关键词检索、不给结论（文档明确要求调用方自行实现撞车判断），
且缓存让这里可以零请求地反复调参。

匹配强度分级（见 docs/optional-modules.md §6.2.4）：
- `in_progress`（对方在制）→ 最高优先级，最直接的撞车信号
- `published`（对方已发布）→ 可能重复翻译
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from .client import PartnerWork
from .title import extract_title


# 匹配等级
MATCH_EXACT = "exact"
MATCH_CONTAINS = "contains"
MATCH_NONE = "none"

# 状态排序权重（越小越靠前）
_STATE_ORDER = {"in_progress": 0, "published": 1, "withdrawn": 2}


@dataclass
class Suspect:
    """一条疑似重复。"""

    work: PartnerWork
    level: str
    score: float

    @property
    def state_rank(self) -> int:
        return _STATE_ORDER.get(self.work.state, 99)


def _normalize(text: str) -> str:
    """归一化：NFKC + 小写 + 去空白 + 去常见装饰符。

    用于比较，不改变展示用原文。
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).lower()
    # 去掉空白与常见分隔装饰
    text = re.sub(r"[\s\u3000]+", "", text)
    text = re.sub(r"[~〜～\-—–_・:：!！?？。.,，、'\"“”‘’()（）\[\]【】]", "", text)
    return text


def _core(title: str) -> str:
    """取出标题的核心部分：剥离系列括号后再归一化。

    `魔法少女敗北実現委員会 (ブルーアーカイブ)` -> `魔法少女敗北実現委員会`
    这样同作品的不同系列标注不会导致漏匹配。
    """
    base = re.sub(r"[（(][^（()）]*[)）]", "", title or "")
    return _normalize(base)


def score(project_title: str, candidate: PartnerWork) -> tuple[str, float]:
    """算匹配程度。返回 (等级, 分数 0~1)。"""
    left = _core(extract_title(project_title))
    right = _core(candidate.original_title)
    if not left or not right:
        return MATCH_NONE, 0.0

    if left == right:
        return MATCH_EXACT, 1.0

    # 一方包含另一方（且较短方足够长，避免短串误命中）
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    if len(shorter) >= 4 and shorter in longer:
        # 长度比越高越可信
        return MATCH_CONTAINS, len(shorter) / len(longer)

    return MATCH_NONE, 0.0


def find_suspects(
    project_title: str, candidates: list[PartnerWork], limit: int = 5
) -> list[Suspect]:
    """从候选里挑出疑似重复，按优先级排序后截断。

    排序：状态优先（在制 > 已发布 > 已终止），同级按匹配分数降序。
    """
    suspects: list[Suspect] = []
    for work in candidates:
        level, value = score(project_title, work)
        if level == MATCH_NONE:
            continue
        suspects.append(Suspect(work=work, level=level, score=value))

    suspects.sort(key=lambda item: (item.state_rank, -item.score))
    return suspects[:limit]