"""模块自有集合。

按模块契约（C2）：**模块数据必须可重建**，因此不写核心迁移，
索引在模块 `init` 时 `ensure_indexes()`。

三个集合：
- `ZitengQuery`  —— 关键词查询缓存（含 TTL），避免重复消耗 1 req/s 配额
- `ZitengWork`   —— 对方作品记录（按对方 id 去重）
- `ZitengCheck`  —— 每个项目的查重任务与结果（前端轮询这个）
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import IntEnum

import mongoengine as db


class CheckStatus(IntEnum):
    """查重任务状态。

    注意 `SUCCEEDED` 之下还有"未查到 / 有疑似"之分，由 `suspect_count` 表达。
    `FAILED` **必须**与"未查到"区分——前者是没查成，后者是查到了但没有。
    """

    QUEUED = 0
    RUNNING = 1
    SUCCEEDED = 2
    FAILED = 3


# 三态（前端据此渲染）
VERDICT_CLEAR = "clear"  # 未查到（不代表不存在）
VERDICT_SUSPECTED = "suspected"  # 有疑似
VERDICT_FAILED = "failed"  # 查询失败——不得降级为 clear


class ZitengWork(db.Document):
    """对方的一条作品记录。按对方 id 唯一。"""

    meta = {
        "collection": "ziteng_work",
        "indexes": ["work_id", "state"],
    }

    work_id = db.StringField(required=True, unique=True)  # 对方的 id
    reference = db.StringField(default="")
    display_title = db.StringField(default="")
    original_title = db.StringField(default="")
    author = db.StringField(default="")
    circle = db.StringField(default="")
    state = db.StringField(default="")
    stage = db.StringField(default="")
    # 归一化后的检索用标题（本地匹配用，避免每次重算）
    normalized_title = db.StringField(default="")
    fetched_at = db.DateTimeField(default=datetime.utcnow)

    def to_api(self) -> dict:
        return {
            "id": self.work_id,
            "reference": self.reference,
            "display_title": self.display_title,
            "original_title": self.original_title,
            "author": self.author,
            "circle": self.circle,
            "state": self.state,
            "stage": self.stage,
        }


class ZitengQuery(db.Document):
    """关键词查询缓存。

    同一关键词在 TTL 内不重复发请求——这是懒查询策略的核心，
    让请求量与"新建项目数"成正比，而不是"查询次数"。
    """

    meta = {
        "collection": "ziteng_query",
        "indexes": ["keyword"],
    }

    keyword = db.StringField(required=True, unique=True)
    # 命中的对方 work_id 列表
    work_ids = db.ListField(db.StringField(), default=list)
    total = db.IntField(default=0)
    truncated = db.BooleanField(default=False)
    queried_at = db.DateTimeField(default=datetime.utcnow)

    def is_fresh(self, ttl_seconds: int) -> bool:
        if self.queried_at is None:
            return False
        return datetime.utcnow() - self.queried_at < timedelta(seconds=ttl_seconds)


class ZitengCheck(db.Document):
    """某个项目的查重任务与结果。前端轮询这个文档。"""

    meta = {
        "collection": "ziteng_check",
        "indexes": ["project_id", "status"],
    }

    project_id = db.StringField(required=True, unique=True)
    # 提取出的检索词（便于排查"为什么没查到"）
    keyword = db.StringField(default="")
    status = db.IntField(default=int(CheckStatus.QUEUED))
    # 三态之一
    verdict = db.StringField(default="")
    # 疑似列表（精简后的展示数据）
    suspects = db.ListField(db.DictField(), default=list)
    suspect_count = db.IntField(default=0)
    error = db.StringField(default="")
    created_at = db.DateTimeField(default=datetime.utcnow)
    updated_at = db.DateTimeField(default=datetime.utcnow)

    def claim(self) -> bool:
        """原子领取任务，保证同一时刻只有一个 worker 在跑。

        对方配额是全局 1 req/s，并发查询必然触发 429。
        """
        updated = type(self).objects(
            id=self.id, status=int(CheckStatus.QUEUED)
        ).update_one(status=int(CheckStatus.RUNNING), updated_at=datetime.utcnow())
        if updated:
            self.reload()
        return bool(updated)

    def set_result(self, verdict: str, suspects: list[dict]) -> None:
        """写入结果。用 update() 避免 save 并发覆盖。"""
        type(self).objects(id=self.id).update_one(
            verdict=verdict,
            suspects=suspects,
            suspect_count=len(suspects),
            status=int(CheckStatus.SUCCEEDED),
            error="",
            updated_at=datetime.utcnow(),
        )
        self.reload()

    def set_failed(self, message: str) -> None:
        """标记查询失败。

        **不得**把失败写成 `verdict=clear`——那会让用户以为没有撞车。
        """
        type(self).objects(id=self.id).update_one(
            status=int(CheckStatus.FAILED),
            verdict=VERDICT_FAILED,
            error=message,
            updated_at=datetime.utcnow(),
        )
        self.reload()

    def to_api(self) -> dict:
        return {
            "project_id": self.project_id,
            "keyword": self.keyword,
            "status": self.status,
            "verdict": self.verdict,
            "suspects": self.suspects,
            "suspect_count": self.suspect_count,
            "error": self.error,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


def ensure_indexes() -> None:
    """建模块自有索引。失败不致命（索引缺失只影响性能）。"""
    import logging

    logger = logging.getLogger(__name__)
    for model in (ZitengWork, ZitengQuery, ZitengCheck):
        try:
            model.ensure_indexes()
        except Exception:  # pragma: no cover - 索引建立失败不应阻断启动
            logger.exception("建立 %s 索引失败", model.__name__)