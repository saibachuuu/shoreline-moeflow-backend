"""紫藤（ziteng）合作方作品检索模块。

需求：创建项目时自动提取标题，查询对方接口，若发现疑似重复则提示。

**本模块是可选模块**（`docs/optional-modules.md`）：
- 目录存在即启用；没有前端开关，也没有隐藏的开关文件。
- 密钥缺失时模块自行跳过注册，站点核心功能不受影响。
- 模块自有集合，失效即重建，不写核心迁移。

关键设计（详见文档 §6）：
- 查询**异步**执行，不阻塞项目创建。
- 默认**只查已立项**（`published` + `in_progress`），不查 `withdrawn`。
- 三态严格区分：未查到 / 有疑似 / 查询失败——**查询失败不得降级为"未查到"**。
- 密钥只影响**查询能否执行**，不影响蓝图是否注册：
  没配密钥时接口仍在，`/v1/ziteng-partner/config` 会如实报告 `enabled: false`，
  查重请求会以明确的失败态落库（而不是 404 或静默"未查到"）。
  这样"未启用"与"配错了"可区分，且不依赖 import 顺序。
"""

from app.modules import ModuleSpec


def _init(app):
    """模块初始化：注册蓝图与自有索引。

    延迟到这里做重活，让模块的 `__init__.py` 保持轻量（`discover()` 只 import 它）。
    """
    from . import models  # noqa: F401  触发 mongoengine 模型注册
    from .api import blueprint

    app.register_blueprint(blueprint)
    models.ensure_indexes()


def _on_project_created(project) -> None:
    """项目创建后触发查重。

    必须轻量且不抛异常：它在项目创建的请求路径上。
    实际查询是异步任务（内部还会再兜底一次），入队失败也不会影响创建。
    """
    import logging

    try:
        from .tasks import enqueue_check

        enqueue_check(str(project.id))
    except Exception:  # pragma: no cover - 兜底，绝不拖垮项目创建
        logging.getLogger(__name__).exception(
            "触发查重失败 project=%s", getattr(project, "id", None)
        )


MODULE = ModuleSpec(
    name="ziteng_partner",
    task_packages=("app.modules.ziteng_partner.tasks",),
    queue=None,  # 走 default 队列，无需新增 worker
    init=_init,
    on_project_created=_on_project_created,
)