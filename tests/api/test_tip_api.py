from app.exceptions import (
    NeedTokenError,
    NoPermissionError,
    TipEmptyError,
)
from app.exceptions.base import ValidateError
from app.models.file import Tip
from app.models.project import Project, ProjectRole
from app.models.team import Team
from app.models.user import User
from tests import MoeAPITestCase


class TipAPITestCase(MoeAPITestCase):
    def test_create_tip(self):
        """测试新增原文批注"""
        # === 创建测试数据 ===
        token = self.create_user("11", "1@1.com", "111111").generate_token()
        user = User.objects(email="1@1.com").first()
        proofreader_token = self.create_user(
            "22", "2@2.com", "111111"
        ).generate_token()
        proofreader = User.objects(email="2@2.com").first()
        token2 = self.create_user("33", "3@3.com", "111111").generate_token()
        team = Team.create("t1", creator=user)
        project = Project.create("p1", team=team, creator=user)
        target = project.targets().first()
        project.create_file("f1.txt")
        image_file = project.create_file("f1.jpg")
        source = image_file.create_source("原文")
        proofreader.join(project, ProjectRole.by_system_code("proofreader"))
        with self.app.test_request_context():
            # === 错误测试 ===
            # 没登录不能创建
            data = self.post("/v1/sources/{}/tips".format(source.id))
            self.assertErrorEqual(data, NeedTokenError)
            # 非项目成员不能创建
            data = self.post(
                "/v1/sources/{}/tips".format(source.id),
                token=token2,
                json={"content": "批注", "target_id": str(target.id)},
            )
            self.assertErrorEqual(data, NoPermissionError)
            # 缺少参数
            data = self.post(
                "/v1/sources/{}/tips".format(source.id),
                token=token,
                json={},
            )
            self.assertErrorEqual(data, ValidateError)
            # 内容不能为空
            data = self.post(
                "/v1/sources/{}/tips".format(source.id),
                token=token,
                json={"content": "", "target_id": str(target.id)},
            )
            self.assertErrorEqual(data, TipEmptyError)
            # === 正常创建 ===
            data = self.post(
                "/v1/sources/{}/tips".format(source.id),
                token=token,
                json={"content": "第一条批注", "target_id": str(target.id)},
            )
            self.assertErrorEqual(data)
            self.assertEqual("第一条批注", data.json["content"])
            self.assertEqual(1, Tip.objects().count())
            # 校对者也能创建
            data = self.post(
                "/v1/sources/{}/tips".format(source.id),
                token=proofreader_token,
                json={"content": "第二条批注", "target_id": str(target.id)},
            )
            self.assertErrorEqual(data)
            self.assertEqual(2, Tip.objects().count())
            # === 原文列表里能取到批注 ===
            data = self.get(
                "/v1/files/{}/sources".format(image_file.id),
                query_string={"target_id": str(target.id)},
                token=token,
            )
            self.assertErrorEqual(data)
            self.assertEqual(2, len(data.json[0]["tips"]))

    def test_edit_and_delete_tip(self):
        """测试修改、删除原文批注"""
        # === 创建测试数据 ===
        token = self.create_user("11", "1@1.com", "111111").generate_token()
        user = User.objects(email="1@1.com").first()
        proofreader_token = self.create_user(
            "22", "2@2.com", "111111"
        ).generate_token()
        proofreader = User.objects(email="2@2.com").first()
        token2 = self.create_user("33", "3@3.com", "111111").generate_token()
        team = Team.create("t1", creator=user)
        project = Project.create("p1", team=team, creator=user)
        target = project.targets().first()
        image_file = project.create_file("f1.jpg")
        source = image_file.create_source("原文")
        proofreader.join(project, ProjectRole.by_system_code("proofreader"))
        tip = source.create_tip("原始批注", target=target, user=proofreader)
        with self.app.test_request_context():
            # === 错误测试 ===
            # 没登录不能修改
            data = self.put("/v1/tips/{}".format(tip.id))
            self.assertErrorEqual(data, NeedTokenError)
            # 非项目成员不能修改
            data = self.put(
                "/v1/tips/{}".format(tip.id),
                token=token2,
                json={"content": "改掉"},
            )
            self.assertErrorEqual(data, NoPermissionError)
            # 内容不能为空
            data = self.put(
                "/v1/tips/{}".format(tip.id),
                token=proofreader_token,
                json={"content": "  "},
            )
            self.assertErrorEqual(data, TipEmptyError)
            # === 正常修改 ===
            data = self.put(
                "/v1/tips/{}".format(tip.id),
                token=proofreader_token,
                json={"content": "改后的批注"},
            )
            self.assertErrorEqual(data)
            tip.reload()
            self.assertEqual("改后的批注", tip.content)
            # === 删除 ===
            # 没登录不能删除
            data = self.delete("/v1/tips/{}".format(tip.id))
            self.assertErrorEqual(data, NeedTokenError)
            # 非项目成员不能删除
            data = self.delete("/v1/tips/{}".format(tip.id), token=token2)
            self.assertErrorEqual(data, NoPermissionError)
            # 正常删除
            data = self.delete("/v1/tips/{}".format(tip.id), token=proofreader_token)
            self.assertErrorEqual(data)
            self.assertEqual(0, Tip.objects().count())
