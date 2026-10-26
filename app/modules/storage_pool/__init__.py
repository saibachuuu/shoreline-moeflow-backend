"""存储池：把久未使用的项目搬到另一个桶里，腾出默认存储的空间。

启用方式：目录存在即启用（见 ``docs/optional-modules.md``）。
本模块做三件事：

1. 注册管理接口（``/v1/admin/storage-pool/*``）；
2. 向核心里那张**空注册表**挂一个解析器 —— 核心只知道「这个项目有个来源 id」，
   不知道它是什么、存在哪，这正是 C1 约束要的边界；
3. 提供两个 celery 任务：每日巡查与逐项目搬家。
"""

from app.modules import ModuleSpec
from app.services import storage_router


def _resolve(source_id):
    """把来源 id 解析成后端。找不到来源就返回 None（核心会退化为默认存储）。"""
    from .backend import PoolBackend
    from .models import StoragePoolSource

    source = StoragePoolSource.objects(id=source_id).first()
    if source is None or not source.enabled:
        return None
    return PoolBackend(source)


def _init(app):
    from .api import blueprint
    from . import models  # noqa: F401  确保模型被 import（mongoengine 需要）
    from . import tasks  # noqa: F401  确保任务被注册（beat 也要能找到它）

    app.register_blueprint(blueprint)
    # 注册是幂等的，模块被重复初始化也不会叠加。
    storage_router.register(_resolve)
    _register_beat_schedule()


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
