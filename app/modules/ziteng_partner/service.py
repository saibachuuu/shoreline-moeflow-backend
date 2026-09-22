"""查重服务：串起「提取标题 → 查询（带缓存）→ 本地匹配 → 落结果」。

放在 service 里而不是 task 里，是为了让 HTTP 层与测试都能直接调用，
不必依赖 celery broker。
"""

from __future__ import annotations

from datetime import datetime
import logging

from . import config
from .client import PartnerApiError, PartnerWork, search
from .matching import find_suspects
from .models import (
    VERDICT_CLEAR,
    VERDICT_FAILED,
    VERDICT_SUSPECTED,
    ZitengCheck,
    ZitengQuery,
    ZitengWork,
)
from .title import extract_title, is_searchable

logger = logging.getLogger(__name__)


def ensure_check(project_id: str, project_name: str) -> ZitengCheck:
    """取得（或创建）某项目的查重任务。已存在则复用。

    复用而非重建：重复触发不应重复消耗配额。
    """
    keyword = extract_title(project_name)
    existing = ZitengCheck.objects(project_id=str(project_id)).first()
    if existing:
        # 项目改名后重新触发时更新检索词
        if existing.keyword != keyword:
            ZitengCheck.objects(id=existing.id).update_one(
                keyword=keyword, updated_at=datetime.utcnow()
            )
            existing.reload()
        return existing

    check = ZitengCheck(project_id=str(project_id), keyword=keyword)
    check.save()
    return check


def _fetch_cached(keyword: str) -> tuple[list[PartnerWork], bool] | None:
    """从缓存取结果。命中新鲜缓存返回 (works, truncated)，否则 None。"""
    cached = ZitengQuery.objects(keyword=keyword).first()
    if not cached or not cached.is_fresh(config.cache_ttl()):
        return None

    works = list(ZitengWork.objects(work_id__in=cached.work_ids))
    # 保持缓存里的顺序
    by_id = {work.work_id: work for work in works}
    ordered = [by_id[wid] for wid in cached.work_ids if wid in by_id]
    return ordered, cached.truncated


def _store(keyword: str, works: list[PartnerWork], total: int, truncated: bool) -> None:
    """把查询结果写进缓存。upsert 语义。"""
    work_ids: list[str] = []
    for work in works:
        ZitengWork.objects(work_id=work.id).update_one(
            set__reference=work.reference,
            set__display_title=work.display_title,
            set__original_title=work.original_title,
            set__author=work.author,
            set__circle=work.circle,
            set__state=work.state,
            set__stage=work.stage,
            set__fetched_at=datetime.utcnow(),
            upsert=True,
        )
        work_ids.append(work.id)

    ZitengQuery.objects(keyword=keyword).update_one(
        set__work_ids=work_ids,
        set__total=total,
        set__truncated=truncated,
        set__queried_at=datetime.utcnow(),
        upsert=True,
    )


def run_check(project_id: str, *, use_cache: bool = True) -> ZitengCheck:
    """执行一次查重。

    三态结果写入 `ZitengCheck`：
    - `clear`     —— 可查询范围内无条目（**不代表不存在**）
    - `suspected` —— 有疑似
    - `failed`    —— 查询失败（**不得**降级为 clear）

    本函数不抛异常给调用方（任务失败要落库，而不是静默丢失）。
    """
    check = ZitengCheck.objects(project_id=str(project_id)).first()
    if check is None:
        raise ValueError(f"查重任务不存在: {project_id}")

    keyword = check.keyword
    if not is_searchable(keyword):
        # 标题不可检索：这是"没查到"，不是"没有"。
        # 但也不该报错——直接记 clear 并保留 keyword 供排查。
        check.set_result(VERDICT_CLEAR, [])
        return check

    try:
        cached = _fetch_cached(keyword) if use_cache else None
        if cached is not None:
            works, _truncated = cached
        else:
            result = search(keyword)
            _store(keyword, result.works, result.total, result.truncated)
            works = result.works
    except PartnerApiError as exc:
        # 关键：查询失败绝不等于"未查到"
        logger.warning("查重失败 project=%s keyword=%r: %s", project_id, keyword, exc)
        check.set_failed(str(exc))
        return check
    except Exception as exc:  # pragma: no cover - 兜底，避免任务静默死亡
        logger.exception("查重异常 project=%s", project_id)
        check.set_failed(f"内部错误: {exc}")
        return check

    project = _load_project(project_id)
    project_title = getattr(project, "name", "") or keyword
    suspects = find_suspects(project_title, works, limit=config.max_results())

    payload = [
        {
            "id": suspect.work.id,
            "reference": suspect.work.reference,
            "display_title": suspect.work.display_title,
            "original_title": suspect.work.original_title,
            "author": suspect.work.author,
            "circle": suspect.work.circle,
            "state": suspect.work.state,
            "stage": suspect.work.stage,
            "level": suspect.level,
            "score": round(suspect.score, 3),
        }
        for suspect in suspects
    ]

    verdict = VERDICT_SUSPECTED if payload else VERDICT_CLEAR
    check.set_result(verdict, payload)
    return check


def _load_project(project_id: str):
    """读取项目（只读，不改核心数据）。

    单独抽出来便于测试打桩。
    """
    from app.models.project import Project

    return Project.objects(id=project_id).first()


def mark_failed(project_id: str, message: str) -> None:
    """把任务标记为失败（供任务层在异常路径调用）。"""
    check = ZitengCheck.objects(project_id=str(project_id)).first()
    if check:
        check.set_failed(message)