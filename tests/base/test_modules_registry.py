"""可选模块系统核心注册表的测试。

覆盖 `docs/optional-modules.md` §4 的通用性验收中可自动化的部分：
零模块时 `discover()` 为空、契约校验、失败隔离、接线不引入模块名。

这些用例**不依赖数据库**：注册表是纯读取操作，用 stub 包即可验证。
"""

import sys
import types
from unittest import TestCase

from app.modules import ModuleSpec, discover, log_enabled_modules


MODULES_PACKAGE = "app.modules"


def _make_package(name: str, attributes: dict) -> types.ModuleType:
    """构造一个假的模块包，用于替代真实的 app.modules."""
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


class ModuleSpecTest(TestCase):
    """ModuleSpec 的默认值与不可变性。"""

    def test_defaults(self):
        spec = ModuleSpec(name="demo")
        self.assertEqual("demo", spec.name)
        self.assertEqual((), spec.task_packages)
        self.assertIsNone(spec.queue)
        self.assertIsNone(spec.init)

    def test_is_frozen(self):
        spec = ModuleSpec(name="demo")
        with self.assertRaises(Exception):
            spec.name = "other"  # type: ignore[misc]


class DiscoverTest(TestCase):
    """discover() 的扫描行为。

    通过替换 sys.modules 中的 app.modules 包来注入 stub 子包，
    避免依赖真实模块目录的内容。
    """

    def setUp(self):
        self._saved = sys.modules.get(MODULES_PACKAGE)
        self._injected: list[str] = []

    def tearDown(self):
        for name in self._injected:
            sys.modules.pop(name, None)
        if self._saved is not None:
            sys.modules[MODULES_PACKAGE] = self._saved
        else:
            sys.modules.pop(MODULES_PACKAGE, None)

    def _install(self, child_names: list[str], attrs_by_name: dict | None = None):
        """把 app.modules 换成一个只有给定子包的 stub（真实目录、可被 pkgutil 扫描）。"""
        import os
        import tempfile

        # pkgutil.iter_modules 需要一个真实目录才能枚举；建一个临时目录放空子包。
        tmpdir = tempfile.mkdtemp(prefix="moeflow-modules-test-")
        path = [tmpdir]
        for child in child_names:
            child_dir = os.path.join(tmpdir, child)
            os.makedirs(child_dir, exist_ok=True)
            with open(os.path.join(child_dir, "__init__.py"), "w", encoding="utf-8"):
                pass

            full = f"{MODULES_PACKAGE}.{child}"
            attrs = (attrs_by_name or {}).get(child)
            if attrs is None:
                # 不给 MODULE 的模块：应被跳过并告警
                package = _make_package(full, {})
            elif isinstance(attrs, Exception):
                package = attrs  # 不是模块；下面用 import 失败路径处理
            else:
                package = _make_package(full, attrs)
            sys.modules[full] = package
            self._injected.append(full)

        stub = _make_package(MODULES_PACKAGE, {})
        stub.__path__ = path
        sys.modules[MODULES_PACKAGE] = stub
        return tmpdir

    def test_empty_package_yields_no_modules(self):
        """零模块：必须返回空列表（§4 检查项 5）。"""
        self._install([])
        self.assertEqual([], discover())

    def test_returns_specs_sorted_by_name(self):
        self._install(
            ["zeta", "alpha"],
            {
                "zeta": {"MODULE": ModuleSpec(name="zeta")},
                "alpha": {"MODULE": ModuleSpec(name="alpha")},
            },
        )
        self.assertEqual(["alpha", "zeta"], [s.name for s in discover()])

    def test_module_without_MODULE_is_skipped(self):
        """没导出 MODULE 的包应被跳过，而不是让 discover() 抛错。"""
        self._install(["nospec"], {"nospec": {}})
        self.assertEqual([], discover())

    def test_non_ModuleSpec_MODULE_is_skipped(self):
        """导出 MODULE 但类型不对（例如写成了 dict）应被跳过。"""
        self._install(["bogus"], {"bogus": {"MODULE": {"name": "bogus"}}})
        self.assertEqual([], discover())

    def test_init_and_task_packages_are_preserved(self):
        calls = []

        def _init(_app):
            calls.append(_app)

        self._install(
            ["demo"],
            {
                "demo": {
                    "MODULE": ModuleSpec(
                        name="demo",
                        task_packages=("app.modules.demo.tasks",),
                        queue="demo_queue",
                        init=_init,
                    )
                }
            },
        )
        specs = discover()
        self.assertEqual(1, len(specs))
        spec = specs[0]
        self.assertEqual(("app.modules.demo.tasks",), spec.task_packages)
        self.assertEqual("demo_queue", spec.queue)
        # discover() 本身**不得**调用 init——init 由 factory 在正确时机调用。
        self.assertEqual([], calls)


class DiscoverFailureIsolationTest(TestCase):
    """单个模块导入失败不能影响核心启动（C1）。"""

    def setUp(self):
        self._saved = sys.modules.get(MODULES_PACKAGE)
        self._injected: list[str] = []
        import os
        import tempfile

        tmpdir = tempfile.mkdtemp(prefix="moeflow-modules-fail-")
        for child in ("good", "broken"):
            child_dir = os.path.join(tmpdir, child)
            os.makedirs(child_dir, exist_ok=True)
            with open(os.path.join(child_dir, "__init__.py"), "w", encoding="utf-8"):
                pass
            self._injected.append(f"{MODULES_PACKAGE}.{child}")

        self._tmpdir = tmpdir
        sys.modules[f"{MODULES_PACKAGE}.good"] = _make_package(
            f"{MODULES_PACKAGE}.good", {"MODULE": ModuleSpec(name="good")}
        )

        class _Boom(types.ModuleType):
            def __getattr__(self, item):  # pragma: no cover - 仅用于触发
                raise RuntimeError("boom")

        # 让 import 该模块时抛错：注册一个 None 会让 importlib 抛 ImportError
        sys.modules[f"{MODULES_PACKAGE}.broken"] = None  # type: ignore[assignment]

        stub = _make_package(MODULES_PACKAGE, {})
        stub.__path__ = [tmpdir]
        sys.modules[MODULES_PACKAGE] = stub

    def tearDown(self):
        for name in self._injected:
            sys.modules.pop(name, None)
        if self._saved is not None:
            sys.modules[MODULES_PACKAGE] = self._saved
        else:
            sys.modules.pop(MODULES_PACKAGE, None)

    def test_broken_module_is_skipped_but_others_load(self):
        names = [s.name for s in discover()]
        self.assertEqual(["good"], names)


class LogEnabledModulesTest(TestCase):
    """启动日志必须能在零模块时正常输出。"""

    def test_logs_none_without_exception(self):
        with self.assertLogs("app.modules", level="INFO") as captured:
            log_enabled_modules()
        self.assertTrue(
            any("可选模块" in line for line in captured.output),
            captured.output,
        )