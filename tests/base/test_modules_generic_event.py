"""「项目已创建」通用事件的测试。

**本文件属于核心，不属于任何模块**：它验证的是 `app.modules` 提供的通用机制，
即使一个模块都没有也应通过（§4 检查项 11）。因此**不要**在这里 import 任何具体模块。

模块侧的集成验证（例如「创建项目后真的触发了紫藤查重」）放在 `tests/modules/` 下，
那些文件会在模块不存在时于收集期跳过。
"""

from unittest import mock

from app import modules as modules_pkg
from app.modules import ModuleSpec, notify_project_created
from tests import MoeTestCase


class NotifyIsolationTest(MoeTestCase):
    """事件广播的失败隔离。"""

    def test_broken_callback_does_not_propagate(self):
        def boom(_project):
            raise RuntimeError("module exploded")

        specs = [
            ModuleSpec(name="broken", on_project_created=boom),
            ModuleSpec(name="fine", on_project_created=lambda _p: None),
        ]
        with mock.patch.object(modules_pkg, "discover", return_value=specs):
            # 不得抛出：模块炸了不能拖垮项目创建
            notify_project_created(object())

    def test_all_modules_are_notified_even_after_a_failure(self):
        seen = []

        def boom(_project):
            raise RuntimeError("boom")

        specs = [
            ModuleSpec(name="a", on_project_created=boom),
            ModuleSpec(name="b", on_project_created=lambda p: seen.append(p)),
        ]
        with mock.patch.object(modules_pkg, "discover", return_value=specs):
            notify_project_created("PROJECT")
        self.assertEqual(["PROJECT"], seen)

    def test_specs_without_callback_are_skipped(self):
        with mock.patch.object(
            modules_pkg, "discover", return_value=[ModuleSpec(name="x")]
        ):
            notify_project_created(object())  # 不抛错即可

    def test_zero_modules_is_a_noop(self):
        with mock.patch.object(modules_pkg, "discover", return_value=[]):
            notify_project_created(object())

    def test_discovery_failure_is_swallowed(self):
        """模块扫描本身失败也不能影响调用方（创建项目是关键路径）。"""
        with mock.patch.object(
            modules_pkg, "discover", side_effect=RuntimeError("discovery exploded")
        ):
            notify_project_created(object())


class ZeroModuleTestHygieneTest(MoeTestCase):
    """§4 检查项 11 的自守卫：模块测试必须能在模块缺失时**跳过**而非报错。

    这些断言不 import 任何具体模块，因此零模块时也照常运行。
    """

    def test_every_module_test_file_guards_its_module_import(self):
        """`tests/modules/test_*.py` 里凡 import 模块的，都必须先声明守卫。

        否则模块目录一删，pytest 会在 collection 阶段抛
        `ModuleNotFoundError` 并中断整个套件——「目录即开关」就没法安全使用。
        """
        import re
        from pathlib import Path

        backend_root = Path(__file__).resolve().parent.parent.parent
        modules_tests = backend_root / "tests" / "modules"
        offenders = []
        for path in sorted(modules_tests.glob("test_*.py")):
            source = path.read_text(encoding="utf-8")
            imports_module = re.search(
                r"^\s*from app\.modules\.[a-z_]+\b|^\s*import app\.modules\.[a-z_]+",
                source,
                re.MULTILINE,
            ) or re.search(r"^\s*from app\.modules\.ziteng_partner", source, re.MULTILINE)
            if not imports_module:
                continue
            # 必须出现 requires_module("<name>")，且位置早于模块 import
            guard = re.search(r"requires_module\(\s*['\"]([a-z_]+)['\"]\s*\)", source)
            if guard is None:
                offenders.append(f"{path.name}: 缺少 requires_module 守卫")
                continue
            if source.index("requires_module(") > source.index("app.modules."):
                # 允许 from tests.modules import requires_module 自身出现在前面
                first_module_import = min(
                    m.start()
                    for m in re.finditer(r"from app\.modules\.|import app\.modules\.", source)
                )
                if source.index("requires_module(") > first_module_import:
                    offenders.append(f"{path.name}: 守卫晚于模块 import，仍会报收集错误")
        self.assertEqual([], offenders, "\n".join(offenders))

    def test_requires_module_detects_installed_modules(self):
        """守卫的判定口径要与 discover() 一致：按目录存在性。

        ⚠️ 这里断言的是**不变式**，不是「当前装了哪些模块」。
        早期版本写死了 `assertTrue(module_exists("archive_import"))`，
        于是把两个模块目录一删，这个「零模块卫生」测试自己就红了——
        与它要守护的目标自相矛盾。改为「对目录里真实存在的每个模块都判 True」。
        """
        from pathlib import Path

        from tests.modules import module_exists

        # 不存在的模块必须判为不存在（否则守卫失效，无法跳过）
        self.assertFalse(module_exists("no_such_module_xyz"))

        # 对 app/modules 下实际存在的每个子包，判定都必须与文件系统一致。
        # 零模块时这个循环为空——同样正确（零模块本来就该全绿）。
        modules_dir = (
            Path(__file__).resolve().parent.parent.parent / "app" / "modules"
        )
        installed = [
            entry.name
            for entry in modules_dir.iterdir()
            if entry.is_dir() and not entry.name.startswith("__")
        ]
        for name in installed:
            self.assertTrue(
                module_exists(name), f"已安装的模块 {name} 应被判为存在"
            )

    def test_module_dir_entries_have_init(self):
        """判定口径依赖 `<module>/__init__.py` 存在，这里锁定该约定。"""
        from pathlib import Path

        modules_dir = (
            Path(__file__).resolve().parent.parent.parent / "app" / "modules"
        )
        missing = [
            entry.name
            for entry in modules_dir.iterdir()
            if entry.is_dir()
            and not entry.name.startswith("__")
            and not (entry / "__init__.py").is_file()
        ]
        self.assertEqual([], missing, f"模块目录缺少 __init__.py: {missing}")
