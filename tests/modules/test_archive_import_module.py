"""归档导入模块迁移后的接线测试（docs/optional-modules.md §5）。

重点验证「平移」的三个承诺：
1. **URL 完全不变**（前端与既有测试零改动）。
2. 蓝图由**模块自己**注册，核心 `urls.py` 不再 import 它。
3. 模块的配置默认值注入到 `app.config`，任务读取方式不变。
"""

from unittest import mock

# 模块不存在时在收集期整文件跳过（§4 检查项 11：零模块时套件仍全绿）
from tests.modules import requires_module

requires_module("archive_import")

from app.modules.archive_import import config as archive_config
from app.modules.archive_import.constants import ArchiveImportStatus
from app.modules.archive_import.models import ArchiveImportTask
from tests import MoeAPITestCase, MoeTestCase


class RouteParityTest(MoeAPITestCase):
    """三条路由必须与迁移前逐字一致。"""

    EXPECTED = (
        "/v1/projects/<project_id>/import-from-archive",
        "/v1/projects/<project_id>/import-task",
        "/v1/projects/<project_id>/import-task/dismiss",
    )

    def test_routes_registered_by_module(self):
        rules = {rule.rule for rule in self.app.url_map.iter_rules()}  # type: ignore[attr-defined]
        for rule in self.EXPECTED:
            self.assertIn(rule, rules, f"路由缺失：{rule}")

    def test_no_duplicate_routes(self):
        """模块注册后不应出现重复规则（蓝图被注册两次的典型症状）。"""
        rules = [rule.rule for rule in self.app.url_map.iter_rules()]  # type: ignore[attr-defined]
        for rule in self.EXPECTED:
            self.assertEqual(1, rules.count(rule), f"路由重复：{rule}")

    def test_blueprint_is_owned_by_module(self):
        """蓝图归属模块，而非核心 urls.py。"""
        from app.apis import urls as core_urls
        from app.modules.archive_import import blueprint

        self.assertEqual("archive_import", blueprint.name)
        core_blueprint_names = {
            value.name
            for value in vars(core_urls).values()
            if hasattr(value, "name") and hasattr(value, "deferred_functions")
        }
        self.assertNotIn(
            "archive_import",
            core_blueprint_names,
            "归档蓝图不应再出现在核心 urls.py 中（C1）",
        )


class ConfigInjectionTest(MoeTestCase):
    """模块配置注入 app.config，且不覆盖显式设置的值。"""

    EXPECTED_KEYS = (
        "ARCHIVE_PROVIDER_API_URL",
        "ARCHIVE_PROVIDER_API_ALLOWED_HOSTS",
        "ARCHIVE_MAX_ZIP_BYTES",
        "ARCHIVE_MAX_ZIP_UNCOMPRESSED_BYTES",
        "ARCHIVE_MAX_ZIP_ENTRIES",
        "ARCHIVE_MAX_ENTRY_BYTES",
    )

    def test_keys_present_after_app_init(self):
        for key in self.EXPECTED_KEYS:
            self.assertIn(key, self.app.config, f"缺少配置键：{key}")

    def test_does_not_override_existing_value(self):
        """测试与显式配置优先于模块默认值。"""
        self.app.config["ARCHIVE_PROVIDER_API_URL"] = "https://explicit.example.com"
        archive_config.apply_to_app(self.app)
        self.assertEqual(
            "https://explicit.example.com",
            self.app.config["ARCHIVE_PROVIDER_API_URL"],
        )

    def test_defaults_are_typed_correctly(self):
        """数值项必须是 int（任务里直接参与算术与比较）。"""
        for key in (
            "ARCHIVE_MAX_ZIP_BYTES",
            "ARCHIVE_MAX_ZIP_UNCOMPRESSED_BYTES",
            "ARCHIVE_MAX_ZIP_ENTRIES",
            "ARCHIVE_MAX_ENTRY_BYTES",
        ):
            self.assertIsInstance(self.app.config[key], int, key)
        self.assertIsInstance(self.app.config["ARCHIVE_PROVIDER_API_ALLOWED_HOSTS"], tuple)

    def test_env_var_units_preserved(self):
        """环境变量以 MB 计、配置值是字节——平移时最容易搞错的地方。

        若把单位统一掉，线上已设置的值会静默变成原来的 1/1048576。
        """
        with mock.patch.dict(
            "os.environ", {"ARCHIVE_MAX_ZIP_BYTES": "7"}, clear=False
        ):
            self.assertEqual(7 * 1024 * 1024, archive_config.max_zip_bytes())

        # 这一项的**环境变量名**与键名不同（..._UNCOMPRESSED_MB）
        with mock.patch.dict(
            "os.environ", {"ARCHIVE_MAX_ZIP_UNCOMPRESSED_MB": "3"}, clear=False
        ):
            self.assertEqual(3 * 1024 * 1024, archive_config.max_zip_uncompressed_bytes())

        with mock.patch.dict(
            "os.environ", {"ARCHIVE_MAX_ENTRY_BYTES": "5"}, clear=False
        ):
            self.assertEqual(5 * 1024 * 1024, archive_config.max_entry_bytes())

    def test_entries_is_not_megabytes(self):
        """条目数是纯计数，不能乘 MB。"""
        with mock.patch.dict(
            "os.environ", {"ARCHIVE_MAX_ZIP_ENTRIES": "42"}, clear=False
        ):
            self.assertEqual(42, archive_config.max_zip_entries())

    def test_invalid_int_falls_back_to_default(self):
        """非法值回落到默认，不让笔误使模块启动失败。"""
        with mock.patch.dict(
            "os.environ", {"ARCHIVE_MAX_ZIP_ENTRIES": "not-a-number"}, clear=False
        ):
            self.assertEqual(5000, archive_config.max_zip_entries())

    def test_plaintext_encryption_key_stays_in_core(self):
        """与核心 secrets.py 共享的加密密钥**不**迁入模块（§5）。"""
        import app.config as core_config

        self.assertTrue(hasattr(core_config, "ARCHIVE_API_KEY_ENCRYPTION_KEY"))


class TaskRegistrationTest(MoeTestCase):
    """任务显式登记——修掉迁移前的隐性缺陷（§5(3)）。"""

    def test_task_is_importable_from_module(self):
        from app.modules.archive_import.tasks import archive_import_task

        self.assertTrue(callable(archive_import_task))

    def test_import_archive_from_gallery_exported(self):
        from app.modules.archive_import.tasks import import_archive_from_gallery

        self.assertTrue(callable(import_archive_from_gallery))

    def test_model_collection_name_unchanged(self):
        """集合名由类名决定，与模块路径无关——已落库数据不受迁移影响。"""
        self.assertEqual("archive_import_task", ArchiveImportTask._get_collection_name())

    def test_status_constants_intact(self):
        """常量未在迁移中走样。

        `ArchiveImportStatus` 是项目的 `IntType`（不是 Python Enum），
        因此用属性访问断言，而不是 `__members__`。
        """
        self.assertEqual(0, int(ArchiveImportStatus.QUEUED))
        self.assertEqual(6, int(ArchiveImportStatus.FAILED))
        self.assertEqual(5, int(ArchiveImportStatus.SUCCEEDED))
        # RUNNING 是运行中状态的集合，任务层用它判断「进行中」
        self.assertEqual(
            (0, 1, 2, 3, 4), tuple(int(item) for item in ArchiveImportStatus.RUNNING)
        )