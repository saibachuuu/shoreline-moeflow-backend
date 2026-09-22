"""紫藤合作方检索 API 的客户端。

只负责 HTTP：分页、限流间隔、错误分类。
**不做匹配、不落库、不判断业务语义。**

最关键的一点：**查询失败必须能被上层区分出来**。
`total: 0` 是"查到了但没有"，而超时/429/503 是"没查成"。
紫藤文档明确要求调用方自行区分这两者；把它们混为一谈会让用户误以为安全。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import time
from typing import Any

import requests

from . import config

logger = logging.getLogger(__name__)


class PartnerApiError(Exception):
    """查询失败（网络、超时、限流、服务端错误）。

    注意：这是"没查成"，**不是**"没有结果"。上层必须区别对待。
    """


@dataclass
class PartnerWork:
    """对方返回的一条作品记录（8 个字段）。"""

    id: str
    reference: str
    display_title: str
    original_title: str
    author: str
    circle: str
    state: str
    stage: str


@dataclass
class SearchResult:
    """一次关键词查询的结果。"""

    query: str
    works: list[PartnerWork] = field(default_factory=list)
    total: int = 0
    # 是否因为翻页上限而截断（结果可能不完整）
    truncated: bool = False


def _parse_work(raw: dict) -> PartnerWork:
    return PartnerWork(
        id=str(raw.get("id") or ""),
        reference=str(raw.get("reference") or ""),
        display_title=str(raw.get("display_title") or ""),
        original_title=str(raw.get("original_title") or ""),
        author=str(raw.get("author") or ""),
        circle=str(raw.get("circle") or ""),
        state=str(raw.get("state") or ""),
        stage=str(raw.get("stage") or ""),
    )


class _Throttle:
    """全局串行节流：保证两次请求之间至少间隔 `min_interval`。

    对方配额是**全接口共享每秒 1 次**，因此这里按进程内单一时间戳节流。
    模块的查询任务本身是单实例串行的（见 tasks.claim），两者配合即可守住配额。
    """

    def __init__(self, interval: float):
        self.interval = interval
        self._last = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last
        remaining = self.interval - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def mark(self) -> None:
        self._last = time.monotonic()


_throttle = _Throttle(1.1)


def reset_throttle() -> None:
    """重置节流状态。仅供测试使用。"""
    global _throttle
    _throttle = _Throttle(config.min_interval())


def _request(params: dict, *, timeout: float) -> dict:
    """发一次请求，处理 429 重试。任何失败都抛 PartnerApiError。"""
    headers = {
        "Authorization": f"Bearer {config.api_key()}",
        "Accept": "application/json",
        "User-Agent": "MoeFlow/ZitengPartnerSearch",
    }

    for attempt in range(2):
        _throttle.wait()
        try:
            response = requests.get(
                config.api_url(), params=params, headers=headers, timeout=timeout
            )
            _throttle.mark()
        except requests.RequestException as exc:
            _throttle.mark()
            raise PartnerApiError(f"请求失败: {exc}") from exc

        if response.status_code == 429:
            # 尊重 Retry-After；没有就退避 1 秒后重试一次
            retry_after = response.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else 1.0
            except ValueError:
                delay = 1.0
            if attempt == 0:
                logger.warning("被限流，等待 %.1fs 后重试", delay)
                time.sleep(delay)
                continue
            raise PartnerApiError("被限流且重试后仍失败")

        if response.status_code == 409:
            # catalog_changed：翻页期间索引变了，须丢弃本轮分页从第一页重查
            raise _CatalogChanged()

        if response.status_code >= 500:
            # 503 等：索引不可用 ≠ 无结果
            raise PartnerApiError(f"对方服务不可用: HTTP {response.status_code}")

        if response.status_code >= 400:
            # 400 invalid_query / invalid_page 等属于调用方问题，重试无意义
            raise PartnerApiError(
                f"请求被拒绝: HTTP {response.status_code} {response.text[:200]}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise PartnerApiError("返回内容不是合法 JSON") from exc

    raise PartnerApiError("被限流且重试后仍失败")


class _CatalogChanged(Exception):
    """内部信号：409 catalog_changed，需要重头再来一次。"""


def search(query: str) -> SearchResult:
    """查询一个关键词，返回候选作品。

    失败时抛 `PartnerApiError`（**不是**返回空结果）。
    遇到 409 会从第一页重查一次。
    """
    query = (query or "").strip()
    if not query:
        raise PartnerApiError("检索词为空")
    if len(query) > 240:
        raise PartnerApiError("检索词超过 240 字符上限")

    for _attempt in range(2):
        try:
            return _search_pages(query)
        except _CatalogChanged:
            logger.info("索引已变化（catalog_changed），从第一页重查: %r", query)

    raise PartnerApiError("索引持续变化，未能取得稳定结果")


def _search_pages(query: str) -> SearchResult:
    """按页拉取，直到 `next_page` 为 null 或达到翻页上限。"""
    from .config import api_key  # 延迟导入便于测试打桩

    if not api_key():
        raise PartnerApiError("未配置 API 密钥")

    params: dict[str, Any] = {"q": query, "page": 1}
    # 默认只查已立项；include_inactive 默认关闭（需求方确认）
    if config.include_inactive():
        params["include_inactive"] = "true"

    works: list[PartnerWork] = []
    total = 0
    revision: str | None = None
    truncated = False
    max_pages = config.max_pages()
    timeout = config.timeout()

    for page in range(1, max_pages + 1):
        params["page"] = page
        if revision:
            # 携带第一页的 revision，锁定同一索引版本
            params["revision"] = revision

        payload = _request(params, timeout=timeout)
        revision = revision or payload.get("revision")
        total = int(payload.get("total") or 0)
        works.extend(_parse_work(item) for item in payload.get("results") or [])

        if not payload.get("next_page"):
            break
        if page == max_pages:
            truncated = True
            logger.info("关键词 %r 命中 %d 条，达到翻页上限 %d 页", query, total, max_pages)

    return SearchResult(query=query, works=works, total=total, truncated=truncated)