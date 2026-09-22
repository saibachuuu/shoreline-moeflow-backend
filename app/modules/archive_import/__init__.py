"""模块实例一：画廊归档导入（自核心平移，见 docs/optional-modules.md §5）。

**目录存在即启用**，没有额外开关。

平移范围（§5 决策）：
- ✅ 视图、模型、任务、校验器、常量、模块配置 → 全部移入本目录
- ✅ 任务显式登记到 `ModuleSpec.task_packages`——**修掉了原先的隐性缺陷**：
  此前 `app.tasks.archive_import` 不在 `autodiscover_tasks`，也不在 `task_routes`，
  只靠 `urls.py` 的 import 副作用才注册；删掉那一行 import 任务就会在 worker 里静默消失。
- ❌ **不**迁 `Team` 的 `archive_api_keys` / `archive_api_url` 字段。
  这是需求方拍板的取舍（§5 决策 2）：这两项支撑团队设置页里的多 key 轮换，
  是已上线且用户可见的能力，迁到模块本地配置会使其丢失。

因此本模块仍会读取核心 `Team` 上的两个只读字段——这是**共享数据形状**，
不是共享领域接口（C2 允许）。

URL 保持与平移前完全一致（§5 方案 A：自建蓝图、前缀 `/v1/projects`），
因此前端与既有测试无需改动。
"""

from __future__ import annotations

from flask import Blueprint

from app.modules import ModuleSpec

blueprint = Blueprint("archive_import", __name__)


def _init(app) -> None:
    """注册路由与模块自有索引。

    视图在 `api.py`，为保持 `app/modules/<name>/__init__.py` 轻量，
    放到这里才 import。
    """
    from . import models  # noqa: F401  触发 mongoengine 模型注册

    # 把本模块的配置默认值注入 app.config（原先是核心 app/config.py 的一部分）
    from . import config as module_config

    module_config.apply_to_app(app)

    _register_routes(blueprint)
    app.register_blueprint(blueprint)


def _register_routes(blueprint: Blueprint) -> None:
    """三条路由的 URL 与迁移前保持一致（前端零改动）。"""
    from .api import ArchiveImportAPI, ArchiveImportTaskAPI

    blueprint.add_url_rule(
        "/v1/projects/<project_id>/import-from-archive",
        methods=["POST", "OPTIONS"],
        view_func=ArchiveImportAPI.as_view("archive_import"),
    )
    blueprint.add_url_rule(
        "/v1/projects/<project_id>/import-task",
        methods=["GET", "OPTIONS"],
        view_func=ArchiveImportTaskAPI.as_view("archive_import_task"),
    )
    blueprint.add_url_rule(
        "/v1/projects/<project_id>/import-task/dismiss",
        methods=["POST", "OPTIONS"],
        view_func=ArchiveImportTaskAPI.as_view("archive_import_task_dismiss"),
    )


def _on_project_created(project) -> None:
    """归档导入不需要在项目创建时做事。

    保留显式定义（而非省略）是为了表明：这是有意的「无操作」，
    不是忘了接线。
    """
    return None


MODULE = ModuleSpec(
    name="archive_import",
    task_packages=("app.modules.archive_import.tasks",),
    # queue=None（走 default）——这是**刻意保持与平移前一致**的行为。
    #
    # 为什么不是 "output"：本模块的任务名是 `tasks.archive_import_task`
    # （历史命名，点号后没有后缀），而模块路由的匹配模式是
    # `tasks.<模块名>.*`，即 `tasks.archive_import.*` —— 它**匹配不到**这个任务名。
    # 因此声明 queue="output" 只会新增一条永远不命中的路由，任务仍会落到
    # 通配 `*` → default。与其留下一个误导性的声明，不如如实标 None。
    #
    # 平移前的行为就是 default（因为它既不在 task_routes 也不在通配之前），
    # 所以这里保持 default 才是真正的「平移」。若要改到 output，那是一次
    # 独立的运维决策（涉及哪个 worker 消费），不属于本次迁移。
    queue=None,
    init=_init,
)