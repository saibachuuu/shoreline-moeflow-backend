"""存储池：把久未使用的项目搬到另一个桶里，腾出默认存储的空间。

启用方式：目录存在即启用（见 ``docs/optional-modules.md``）。
本模块做三件事：

1. 注册管理接口（``/v1/admin/storage-pool/*``）；
2. 向核心里那张**空注册表**挂一个解析器 —— 核心只知道「这个项目有个来源 id」，
   不知道它是什么、存在哪，这正是 C1 约束要的边界；
3. 提供两个 celery 任务：每日巡查与逐项目搬家。
"""

import logging

from app.modules import ModuleSpec
from app.services import storage_router

logger = logging.getLogger(__name__)


def _resolve(source_id):
    """把来源 id 解析成后端。找不到来源就返回 None（核心会退化为默认存储）。"""
    from .backend import PoolBackend
    from .models import StoragePoolSource

    source = StoragePoolSource.objects(id=source_id).first()
    if source is None or not source.enabled:
        return None
    return PoolBackend(source)


def _guard_project_state(project, action):
    """项目完结/清空前的守卫：有进行中的迁移就拒绝。

    迁移侧也会在 ``run`` / ``_switch_pointer`` 检查项目状态和
    ``clear_in_progress``；两边一起保证「清空」与「搬家中切指针」互斥。
    """
    if action not in ("complete", "clear"):
        return

    from .api import TransferInProgressError
    from .constants import TransferStatus
    from .models import StorageTransfer

    if (
        StorageTransfer.objects(
            project=project, status__in=TransferStatus.ACTIVE
        ).first()
        is not None
    ):
        raise TransferInProgressError()


def _init(app):
    from .api import blueprint
    from . import models  # noqa: F401  确保模型被 import（mongoengine 需要）
    from . import tasks  # noqa: F401  确保任务被注册（beat 也要能找到它）
    from app.services.project_lifecycle import register_state_guard

    app.register_blueprint(blueprint)
    # 注册是幂等的，模块被重复初始化也不会叠加。
    storage_router.register(_resolve)
    register_state_guard(_guard_project_state)
    _register_beat_schedule()
    _check_temp_dir(app)


def _check_temp_dir(app):
    """启动自检：暂存目录配置有问题就记日志，不阻断模块加载。

    ``factory.py`` 对模块 ``init`` 的异常是「记日志后跳过整个模块」——
    在这里抛错会把存储池的管理接口一起弄没，排障面反而更大。真正的
    强校验在 ``backend._transfer_temp_dir()`` 的使用点。
    """
    import tempfile

    from .backend import _transfer_temp_dir

    try:
        with app.app_context():
            path = _transfer_temp_dir()
    except (RuntimeError, OSError) as error:
        logger.error("存储池暂存目录配置无效：%s", error)
        return
    if path:
        logger.info("存储池暂存目录已就绪：%s", path)
    else:
        logger.info("存储池暂存目录未配置，使用系统临时目录：%s", tempfile.gettempdir())


def _register_beat_schedule():
    """每日 03:00 巡查。模块自己往 beat 表里加一项，核心不认识它（C1）。"""
    from celery.schedules import crontab

    from app import celery

    celery.conf.beat_schedule = {
        **(celery.conf.beat_schedule or {}),
        "storage-pool-scan": {
            "task": "tasks.storage_pool_scan",
            "schedule": crontab(hour=3, minute=0),
        },
    }


MODULE = ModuleSpec(
    name="storage_pool",
    task_packages=("app.modules.storage_pool.tasks",),
    queue=None,  # 搬家是网络 IO，走 default 队列
    init=_init,
)
