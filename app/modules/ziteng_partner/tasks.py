"""模块的 celery 任务。

任务名必须是 `tasks.<模块名>.*`，这样 `factory.py` 里通用的队列路由
（`tasks.<spec.name>.*`）才能命中；本模块 `queue=None`，因此走 default 队列。
"""

from __future__ import annotations

import logging

from app import celery
from app.tasks import _FORCE_SYNC_TASK

from . import service
from .models import CheckStatus, ZitengCheck

logger = logging.getLogger(__name__)


@celery.task(name="tasks.ziteng_partner.check_project")
def check_project_task(project_id: str) -> str:
    """对一个项目执行查重。"""
    check = ZitengCheck.objects(project_id=str(project_id)).first()
    if check is None:
        logger.warning("查重任务不存在，跳过: %s", project_id)
        return "missing"

    # 原子领取：并发查询会打爆对方 1 req/s 的全局配额
    if not check.claim():
        logger.info("查重任务已被领取或已完成，跳过: %s", project_id)
        return "skipped"

    service.run_check(project_id)
    return "ok"


def enqueue_check(project_id: str) -> None:
    """创建任务并入队（异步）。

    与归档导入同构：broker 不可达或 TESTING 时同步执行，
    这样测试无需 broker，生产也不因 broker 抖动丢失任务。
    """
    service.ensure_check(project_id, _project_name(project_id))

    if _FORCE_SYNC_TASK:
        # 同步执行；异常在 service 内已落库
        check = ZitengCheck.objects(project_id=str(project_id)).first()
        if check and check.claim():
            service.run_check(project_id)
        return

    try:
        check_project_task.delay(str(project_id))
    except Exception:
        logger.exception("入队失败，降级为同步执行: %s", project_id)
        check = ZitengCheck.objects(project_id=str(project_id)).first()
        if check and check.claim():
            service.run_check(project_id)


def _project_name(project_id: str) -> str:
    from app.models.project import Project

    project = Project.objects(id=project_id).first()
    return getattr(project, "name", "") or ""


def reset_for_retry(project_id: str) -> bool:
    """把已完成/失败的任务重置为可重跑。供"重新查重"按钮使用。"""
    updated = ZitengCheck.objects(project_id=str(project_id)).update_one(
        set__status=int(CheckStatus.QUEUED),
        set__verdict="",
        set__error="",
    )
    return bool(updated)