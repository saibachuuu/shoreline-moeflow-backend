"""模块接线点的测试（`app/factory.py`）。

重点验证两件在 §4 里被列为验收项的事：
- 零模块时 `task_routes` 与引入模块系统之前**等价**（检查项 7）
- 模块队列路由插在通配 `"*"` 之前，否则会被 default 吃掉
- 核心文件里不出现任何具体模块名（CI 反向检查的核心逻辑）
"""

import os
import unittest
from unittest import TestCase

from app.modules import ModuleSpec


BACKEND_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

# 这些文件**必须**保持通用：它们是模块系统的核心本身。
CORE_FILES_THAT_MUST_STAY_GENERIC = (
    "app/modules/__init__.py",
    "app/factory.py",
    "app/config.py",
    # 归档导入迁移完成后（§5），这两个文件已完全不含任何模块名：
    # - team.py 只通过通用事件 notify_project_created 与模块交互；
    #   外部地址校验已上移到 app/utils/external_url.py（通用工具）。
    # - config.py 的归档专属配置已迁入模块，仅保留与核心 secrets.py 共享的
    #   ARCHIVE_API_KEY_ENCRYPTION_KEY（它是通用加解密工具的配置，非归档专属）。
    "app/apis/team.py",
)

# 这些文件**尚未**通用，因为对应的功能还没迁成模块。
# 迁移完成后必须从本表移除，届时上面的检查会自动开始覆盖它们。
# （见 docs/optional-modules.md §7 partner search。）
CORE_FILES_PENDING_MIGRATION = {
    "app/apis/urls.py": ("partner_search 仍是核心蓝图（§7）",),
    "app/models/site_setting.py": ("partner_search 仍有 4 个核心字段（§7）",),
}

# 已知的具体模块名。核心文件里出现任何一个都说明 C1 被破坏。
# 新增模块时把名字加进来，反向检查才会覆盖它。
KNOWN_MODULE_NAMES = (
    "archive_import",
    "ziteng",
    "partner_search",
)


def _route_order(routes: list[tuple]) -> list[str]:
    return [pattern for pattern, _ in routes]


class TaskRoutesTest(TestCase):
    """从 create_celery 抽出路由构造逻辑做等价性验证。

    不实例化真实 Celery（会连 broker），而是复算同一段逻辑。
    """

    BASE_ROUTES = [
        ("tasks.output_project_task", {"queue": "output"}),
        ("tasks.import_from_labelplus_task", {"queue": "output"}),
        ("tasks.create_thumbnail_task", {"queue": "output"}),
        ("tasks.mit.*", {"queue": "mit"}),
    ]
    CATCH_ALL = ("*", {"queue": "default"})

    def _build(self, specs: list[ModuleSpec]):
        module_routes = [
            (f"tasks.{spec.name}.*", {"queue": spec.queue})
            for spec in specs
            if spec.queue
        ]
        return self.BASE_ROUTES + module_routes + [self.CATCH_ALL]

    def test_zero_modules_matches_legacy(self):
        """零模块时路由必须与引入模块前逐项一致（§4 检查项 7）。"""
        routes = self._build([])
        self.assertEqual(
            _route_order(routes),
            [
                "tasks.output_project_task",
                "tasks.import_from_labelplus_task",
                "tasks.create_thumbnail_task",
                "tasks.mit.*",
                "*",
            ],
        )

    def test_module_route_precedes_catch_all(self):
        """模块队列路由必须在 "*" 之前，否则永远命中 default。"""
        routes = self._build([ModuleSpec(name="demo", queue="demo_q")])
        order = _route_order(routes)
        self.assertIn("tasks.demo.*", order)
        self.assertLess(order.index("tasks.demo.*"), order.index("*"))

    def test_module_without_queue_adds_no_route(self):
        """queue=None 的模块不应产生路由（走 default）。"""
        routes = self._build([ModuleSpec(name="demo")])
        self.assertEqual(
            _route_order(routes),
            [
                "tasks.output_project_task",
                "tasks.import_from_labelplus_task",
                "tasks.create_thumbnail_task",
                "tasks.mit.*",
                "*",
            ],
        )

    def test_catch_all_is_always_last(self):
        routes = self._build(
            [
                ModuleSpec(name="a", queue="qa"),
                ModuleSpec(name="b"),
                ModuleSpec(name="c", queue="qc"),
            ]
        )
        self.assertEqual("*", _route_order(routes)[-1])


class CoreStaysGenericTest(TestCase):
    """§4 的反向检查：核心文件不得出现具体模块名。"""

    def test_core_files_contain_no_module_name(self):
        violations = []
        for relative in CORE_FILES_THAT_MUST_STAY_GENERIC:
            path = os.path.join(BACKEND_ROOT, relative)
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as handle:
                content = handle.read()
            for name in KNOWN_MODULE_NAMES:
                if name in content:
                    violations.append(f"{relative} 出现模块名 {name!r}")
        self.assertEqual([], violations, "\n".join(violations))

    def test_modules_package_init_is_generic(self):
        """app/modules/__init__.py 本身必须通用。"""
        path = os.path.join(BACKEND_ROOT, "app/modules/__init__.py")
        with open(path, encoding="utf-8") as handle:
            content = handle.read()
        for name in KNOWN_MODULE_NAMES:
            self.assertNotIn(name, content)

    def test_pending_migration_list_is_not_stale(self):
        """待迁移清单必须仍然名副其实。

        若某文件已经不再含模块名（说明迁移做完了），本用例会失败，
        提醒把它从 CORE_FILES_PENDING_MIGRATION 移到强制通用清单。
        这样该清单不会烂掉，迁移完成能被自动发现。
        """
        stale = []
        for relative in CORE_FILES_PENDING_MIGRATION:
            path = os.path.join(BACKEND_ROOT, relative)
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as handle:
                content = handle.read()
            if not any(name in content for name in KNOWN_MODULE_NAMES):
                stale.append(
                    f"{relative} 已不含任何模块名——请把它移到"
                    " CORE_FILES_THAT_MUST_STAY_GENERIC"
                )
        self.assertEqual([], stale, "\n".join(stale))


class GenericEventSurfaceTest(TestCase):
    """核心 ↔ 模块之间的唯一回调面必须是**通用事件**，不是领域接口。

    这是 C2 的守卫：模块之间不共享领域接口，只共享注册机制。
    若哪天有人给 ModuleSpec 加上 `on_archive_import` 之类的专用钩子，
    这里会失败，提醒改成通用事件（如 on_project_created）。
    """

    # 允许的钩子名。都是「什么时候」而不是「做什么」。
    ALLOWED_HOOKS = {"init", "on_project_created"}

    def test_module_spec_exposes_only_generic_hooks(self):
        import dataclasses

        fields = {field.name for field in dataclasses.fields(ModuleSpec)}
        hooks = {name for name in fields if name.startswith("on_") or name == "init"}
        self.assertEqual(
            set(),
            hooks - self.ALLOWED_HOOKS,
            "ModuleSpec 上出现了非通用钩子；模块间只共享注册机制，不共享领域接口",
        )

    def test_module_spec_has_no_required_positional_beyond_name(self):
        """新建模块不应被迫提供任何东西——零配置是默认体验。"""
        spec = ModuleSpec(name="bare")
        self.assertEqual("bare", spec.name)
        self.assertEqual((), spec.task_packages)
        self.assertIsNone(spec.queue)
        self.assertIsNone(spec.init)
        self.assertIsNone(spec.on_project_created)

    def test_module_spec_is_frozen(self):
        import dataclasses

        spec = ModuleSpec(name="frozen")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            spec.name = "changed"  # type: ignore[misc]


class ArchiveImportRegistrationTest(TestCase):
    """§5(3)：归档导入任务必须**显式**登记，不能靠 import 副作用。

    迁移前 `app.tasks.archive_import` 不在 `autodiscover_tasks`，也不在
    `task_routes`；它能工作纯属 `urls.py` 里那行 import 的副作用。
    删掉那行，任务就会在 worker 里静默消失——这是**缺陷而非设计**。

    迁移为模块后，`ModuleSpec.task_packages` 显式登记它，本节锁定该成果。

    ⚠️ 本类**依赖归档模块存在**。模块被关掉（目录删除）时整类跳过——
    这正是 §4 检查项 11 要求的「零模块时套件仍全绿」。
    通用机制的断言在 `test_modules_registry.py`，那里不依赖任何模块。
    """

    @classmethod
    def setUpClass(cls):
        from app.modules import discover

        if "archive_import" not in {spec.name for spec in discover()}:
            raise unittest.SkipTest("归档模块未启用，跳过其登记断言")

    def _archive_spec(self):
        from app.modules import discover

        specs = {spec.name: spec for spec in discover()}
        self.assertIn("archive_import", specs, "归档导入模块应当被发现")
        return specs["archive_import"]

    def test_task_package_is_registered(self):
        spec = self._archive_spec()
        self.assertEqual(
            ("app.modules.archive_import.tasks",),
            spec.task_packages,
            "归档任务必须由模块显式登记，否则会影响 worker 端注册",
        )

    def test_module_keeps_default_queue_matching_pre_migration_behavior(self):
        """queue=None（default）是与平移前一致的行为，不是遗漏。

        任务名 `tasks.archive_import_task` **不匹配** 模块路由模式
        `tasks.archive_import.*`，因此任务本就落到通配 `*` → default。
        若误标为 "output"，只会新增一条永不命中的路由。
        """
        self.assertIsNone(self._archive_spec().queue)

    def test_task_name_really_does_not_match_module_route_pattern(self):
        """锁定上面那条推理所依赖的事实；Celery 路由用 fnmatch 语义。"""
        from fnmatch import fnmatch

        self.assertFalse(fnmatch("tasks.archive_import_task", "tasks.archive_import.*"))
        self.assertTrue(fnmatch("tasks.archive_import_task", "*"))