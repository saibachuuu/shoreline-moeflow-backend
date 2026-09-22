"""模块 API 的测试。

验证：蓝图自注册生效、权限校验、三态在 API 层的呈现、
以及**失败态不会被 API 伪装成"未查到"**。
"""

from unittest import mock

# 模块不存在时在收集期整文件跳过（§4 检查项 11：零模块时套件仍全绿）
from tests.modules import requires_module

requires_module("ziteng_partner")

from app.modules.ziteng_partner import config, service
from app.modules.ziteng_partner.client import PartnerApiError
from app.modules.ziteng_partner.models import (
    VERDICT_CLEAR,
    VERDICT_FAILED,
    VERDICT_SUSPECTED,
    ZitengCheck,
    ZitengQuery,
    ZitengWork,
)
from tests import MoeAPITestCase


class ZitengAPITestCase(MoeAPITestCase):
    def setUp(self):
        super().setUp()
        config.reset_cache()
        for model in (ZitengCheck, ZitengQuery, ZitengWork):
            model.drop_collection()
        self.project = self.create_project("テスト作品")
        self.token = self.get_creator(self.project).generate_token()

    def tearDown(self):
        config.reset_cache()
        super().tearDown()

    def url(self) -> str:
        return f"/v1/projects/{self.project.id}/ziteng-check"


class BlueprintRegistrationTest(ZitengAPITestCase):
    """模块蓝图必须已注册（否则断言 404 会掩盖问题）。"""

    def test_routes_registered(self):
        rules = [
            rule.rule for rule in self.app.url_map.iter_rules()  # type: ignore[attr-defined]
        ]
        self.assertIn("/v1/projects/<project_id>/ziteng-check", rules)
        self.assertIn("/v1/ziteng-partner/config", rules)

    def test_config_endpoint(self):
        resp = self.get("/v1/ziteng-partner/config", token=self.token)
        self.assertErrorEqual(resp)
        self.assertIn("enabled", resp.json)


class CheckEndpointTest(ZitengAPITestCase):
    def test_get_without_check_returns_null(self):
        resp = self.get(self.url(), token=self.token)
        self.assertErrorEqual(resp)
        self.assertIsNone(resp.json["check"])

    def test_get_requires_token(self):
        resp = self.get(self.url())
        self.assertIsNotNone(resp.json.get("code"))

    def test_get_404_for_unknown_project(self):
        resp = self.get(
            "/v1/projects/000000000000000000000000/ziteng-check", token=self.token
        )
        self.assertIsNotNone(resp.json.get("code"))

    def test_get_returns_clear_verdict(self):
        check = service.ensure_check(str(self.project.id), self.project.name)
        check.set_result(VERDICT_CLEAR, [])
        resp = self.get(self.url(), token=self.token)
        self.assertErrorEqual(resp)
        self.assertEqual(VERDICT_CLEAR, resp.json["check"]["verdict"])

    def test_get_returns_suspected_with_state(self):
        check = service.ensure_check(str(self.project.id), self.project.name)
        check.set_result(
            VERDICT_SUSPECTED,
            [
                {
                    "id": "w1",
                    "original_title": "作品",
                    "state": "in_progress",
                    "stage": "嵌字中",
                    "level": "exact",
                    "score": 1.0,
                }
            ],
        )
        resp = self.get(self.url(), token=self.token)
        self.assertErrorEqual(resp)
        payload = resp.json["check"]
        self.assertEqual(VERDICT_SUSPECTED, payload["verdict"])
        self.assertEqual(1, payload["suspect_count"])
        self.assertEqual("in_progress", payload["suspects"][0]["state"])

    def test_failed_verdict_is_exposed_as_failed(self):
        """**关键**：查询失败必须原样暴露，不得变成 clear。"""
        check = service.ensure_check(str(self.project.id), self.project.name)
        check.set_failed("对方服务不可用")
        resp = self.get(self.url(), token=self.token)
        self.assertErrorEqual(resp)
        payload = resp.json["check"]
        self.assertEqual(VERDICT_FAILED, payload["verdict"])
        self.assertNotEqual(VERDICT_CLEAR, payload["verdict"])
        self.assertIn("不可用", payload["error"])

    def test_post_triggers_check(self):
        with mock.patch.object(service, "search", return_value=mock.Mock(
            works=[], total=0, truncated=False
        )):
            resp = self.post(self.url(), token=self.token)
        self.assertErrorEqual(resp)
        self.assertIsNotNone(resp.json["check"])
        # TESTING 下任务同步执行，因此应已有结果
        self.assertEqual(VERDICT_CLEAR, resp.json["check"]["verdict"])

    def test_post_records_failure_without_pretending_clear(self):
        with mock.patch.object(
            service, "search", side_effect=PartnerApiError("网络超时")
        ):
            resp = self.post(self.url(), token=self.token)
        self.assertErrorEqual(resp)
        payload = resp.json["check"]
        self.assertEqual(VERDICT_FAILED, payload["verdict"])
        self.assertNotEqual(VERDICT_CLEAR, payload["verdict"])

    def test_api_shape_is_snake_case(self):
        """后端返回 snake_case；前端若读 camelCase 会拿不到值。

        （归档导入的进度组件就有这个问题，见 docs/optional-modules.md §6.3。）
        """
        check = service.ensure_check(str(self.project.id), self.project.name)
        check.set_result(VERDICT_CLEAR, [])
        resp = self.get(self.url(), token=self.token)
        payload = resp.json["check"]
        self.assertIn("suspect_count", payload)
        self.assertIn("project_id", payload)
        self.assertNotIn("suspectCount", payload)
        self.assertNotIn("projectId", payload)