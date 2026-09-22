"""「项目已创建」事件在**紫藤模块**上的集成验证。

本文件依赖紫藤模块，因此开头即 `requires_module`：模块不存在时在收集期整文件跳过
（§4 检查项 11）。**通用的**事件机制测试在 `tests/base/test_modules_generic_event.py`，
那份不依赖任何模块、始终运行。
"""

from unittest import mock

from app import modules as modules_pkg
from tests import MoeAPITestCase

# 模块不存在时在收集期整文件跳过
from tests.modules import requires_module

requires_module("ziteng_partner")

from app.modules.ziteng_partner.models import ZitengCheck


class ProjectCreatedIntegrationTest(MoeAPITestCase):
    """通过真实的项目创建接口验证钩子被触发。"""

    def setUp(self):
        super().setUp()
        ZitengCheck.drop_collection()

    def _create_project(self, name="テスト作品"):
        from app.models.project import Project

        team = self.create_team(f"{name}-t")
        user = self.get_creator(team)
        token = user.generate_token()
        project_set = team.default_project_set
        return self.post(
            f"/v1/teams/{team.id}/projects",
            token=token,
            json={
                "name": name,
                "intro": "",
                "project_set": str(project_set.id),
                "allow_apply_type": Project.allow_apply_type_cls.TEAM_USER,
                "application_check_type": Project.application_check_type_cls.ADMIN_CHECK,
                "default_role": str(Project.role_cls.by_system_code("translator").id),
                "source_language": "ja",
                "target_languages": ["zh-CN"],
            },
        )

    def test_project_creation_still_succeeds(self):
        """先确认基线：创建接口本身正常。"""
        resp = self._create_project()
        self.assertErrorEqual(resp)
        self.assertIn("project", resp.json)

    def test_hook_creates_check(self):
        resp = self._create_project()
        self.assertErrorEqual(resp)
        project_id = resp.json["project"]["id"]
        # TESTING 下任务同步执行，因此应已产生查重记录
        self.assertTrue(
            ZitengCheck.objects(project_id=str(project_id)).first() is not None,
            "项目创建后应触发查重任务",
        )

    def test_creation_survives_hook_failure(self):
        """模块钩子炸了也不能让项目创建失败。"""
        with mock.patch.object(
            modules_pkg, "discover", side_effect=RuntimeError("discovery exploded")
        ):
            resp = self._create_project("钩子失败也要成功")
        self.assertErrorEqual(resp)
        self.assertIn("project", resp.json)