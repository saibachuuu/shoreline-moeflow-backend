"""可选模块系统的核心注册表。

本文件**必须保持通用**：不得出现任何具体模块的名字、字段或开关。
（`docs/optional-modules.md` 的 C1 约束。）

模块 = `app/modules/<name>/` 目录存在，且其 `__init__.py` 导出 `MODULE`。
没有隐藏的开关文件，也没有环境变量开关——目录即启用。

`discover()` 是纯读取操作：不连库、不建蓝图、不注册任务，
因此可以被 create_celery 与 init_flask_app 安全地各调用一次。
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import logging
import pkgutil
from typing import Callable

logger = logging.getLogger(__name__)

__all__ = [
    "ModuleSpec",
    "discover",
    "log_enabled_modules",
    "notify_project_created",
]


@dataclass(frozen=True)
class ModuleSpec:
    """模块自述。由模块的 ``__init__.py`` 导出为 ``MODULE``。"""

    name: str
    """模块标识，建议等于目录名。"""

    task_packages: tuple[str, ...] = ()
    """供 celery ``autodiscover_tasks`` 使用的包路径。

    模块自己有 celery 任务时必须在这里登记——否则任务只在被 api 层 import 时
    才偶然注册（归档导入就是这个隐式耦合，见 docs/optional-modules.md §5）。
    """

    queue: str | None = None
    """期望的 celery 队列；``None`` 表示走 default。

    声明非 None 的队列意味着部署侧必须有同名 worker，否则任务会积压。
    """

    init: Callable | None = None
    """``init(app)`` 钩子，在 ``register_apis(app)`` 之后调用。

    重活（import models／建蓝图／写索引）都应延迟到这里，让模块的
    ``__init__.py`` 保持轻量——`discover()` 只 import 模块的 ``__init__.py``。
    """

    on_project_created: Callable | None = None
    """``on_project_created(project)`` 钩子，项目创建成功后调用。

    核心只发出这个通用事件，**不知道**谁会消费它（C1）。
    实现必须自行保证不抛异常、不阻塞请求（通常是入队一个异步任务）。
    """


def discover() -> list[ModuleSpec]:
    """扫描 ``app.modules`` 下的子包，返回启用（且自述有效）的模块。

    只 import 各模块的 ``__init__.py``，不触碰其 models/api/tasks。
    单个模块导入失败只记日志并跳过，不影响核心启动（C1 的一部分）。
    """
    package = importlib.import_module("app.modules")
    specs: list[ModuleSpec] = []
    for info in pkgutil.iter_modules(package.__path__):
        if not info.ispkg:
            continue
        try:
            module = importlib.import_module(f"{package.__name__}.{info.name}")
        except Exception:
            logger.exception("模块 %s 导入失败，已跳过", info.name)
            continue
        spec = getattr(module, "MODULE", None)
        if isinstance(spec, ModuleSpec):
            specs.append(spec)
        else:
            logger.warning("模块 %s 未导出 MODULE，已跳过", info.name)
    specs.sort(key=lambda s: s.name)
    return specs


def log_enabled_modules() -> None:
    """把已启用的模块打进启动日志。

    模块不生效时最难排查的就是「它到底有没有被加载」，
    因此这里主动输出一行 INFO（零模块时输出 none）。
    """
    try:
        names = [spec.name for spec in discover()]
    except Exception:  # pragma: no cover - 极早期启动失败
        logger.exception("模块扫描失败")
        return
    logger.info("可选模块: %s", ", ".join(names) if names else "none")


def notify_project_created(project) -> None:
    """广播「项目已创建」事件给所有模块。

    核心**只发事件**，不认识任何具体模块（C1）。模块若对项目创建感兴趣，
    在自述里提供 ``on_project_created``。

    单个模块的回调抛异常**不影响**项目创建，也不影响其它模块——
    创建项目是用户的关键路径，不能被模块拖垮。

    同理，模块扫描本身的失败也必须被吞掉。
    """
    try:
        specs = discover()
    except Exception:
        logger.exception("模块扫描失败，跳过 on_project_created 广播")
        return

    for spec in specs:
        callback = getattr(spec, "on_project_created", None)
        if callback is None:
            continue
        try:
            callback(project)
        except Exception:
            logger.exception("模块 %s 的 on_project_created 回调失败", spec.name)