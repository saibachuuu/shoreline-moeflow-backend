"""从项目名提取检索标题。

校准依据（见 docs/optional-modules.md §6.2.1）：紫藤的 `original_title`
会剥离编号与 `[DL版]` 等版本标签，但**保留作品系列括号**（如 `(ブルーアーカイブ)`）。
本模块沿用同一套规则，使两侧标题形态可比。

模块自持，核心不感知。
"""

from __future__ import annotations

import re

# 编号：`【385】`、`[263]`、`#385`、`（385）`
_LEADING_INDEX = re.compile(
    r"^\s*[\[【（(#]\s*(?:no\.?\s*)?\d{1,5}\s*[\]】）)]\s*",
    re.IGNORECASE,
)

# 社团/作者方括号：`[ShiBoo! (Ixy)]`、`[DL版]`、`[中国翻訳]`
# 只处理方括号（圆括号通常是系列名，必须保留）
_BRACKET_TAG = re.compile(r"[\[【][^\[\]【】]{1,60}[\]】]")

# 版本/语言标签（可能出现在任意位置，含圆括号形式）
_VERSION_TAGS = re.compile(
    r"[（(]\s*(?:DL版|中国翻訳|中国翻译|漢化|汉化|無修正|无修正|カラー|彩页)\s*[)）]",
    re.IGNORECASE,
)

# 结尾的作者/社团圆括号：`作品名 (作者)` 中的单个短词
# 注意只在该括号不是系列名时才应剥离；系列名通常含作品关键词。
# 这里保守处理：**不自动剥离圆括号**，因为无法可靠区分作者与系列。
# 若实测发现噪声，再按具体数据调整。

_WHITESPACE = re.compile(r"\s+")


def extract_title(project_name: str) -> str:
    """从项目名提取用于检索的作品标题。

    剥离：编号前后缀、方括号标签、版本/语言标签。
    保留：圆括号内容（可能是系列名，是重要区分信息）、卷号。

    >>> extract_title("【385】[ShiBoo! (Ixy)] 魔法少女敗北実現委員会 (ブルーアーカイブ) [DL版]")
    '魔法少女敗北実現委員会 (ブルーアーカイブ)'
    """
    if not project_name:
        return ""

    text = project_name.strip()
    text = _LEADING_INDEX.sub("", text)
    text = _VERSION_TAGS.sub(" ", text)
    text = _BRACKET_TAG.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    return text


def is_searchable(title: str) -> bool:
    """标题是否值得发起查询。

    过短或纯符号的标题查不出东西，白耗配额。
    紫藤要求 q 长度 1–240。
    """
    if not title:
        return False
    if len(title) > 240:
        return False
    # 至少要有一个字母或数字（含 CJK）
    return any(char.isalnum() for char in title)