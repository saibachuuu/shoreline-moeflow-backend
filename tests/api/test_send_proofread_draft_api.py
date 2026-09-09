from unittest.mock import patch
from app.apis.project import generate_diff_html
from app.constants.file import FileType
from app.exceptions.project import NoTranslatorMemberError, ProofreadDraftNoChangesError
from app.models.file import File, Source, Translation
from app.models.language import Language
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.team import Team
from app.models.user import User
from tests import MoeAPITestCase


class SendProofreadDraftAPITestCase(MoeAPITestCase):
    def test_generate_diff_html(self):
        """测试文本差异高亮生成"""
        orig = "你好世界"
        proof = "你好萌翻世界"
        html_diff = generate_diff_html(orig, proof)
        self.assertIn("你好", html_diff)
        self.assertIn("<ins", html_diff)
        self.assertIn("萌翻", html_diff)
        self.assertIn("世界", html_diff)

        # 替换测试
        diff2 = generate_diff_html("原内容", "新内容")
        self.assertIn("<del", diff2)
        self.assertIn("原", diff2)
        self.assertIn("<ins", diff2)
        self.assertIn("新", diff2)
        self.assertIn("内容", diff2)

    @patch("app.apis.project.send_email")
    def test_send_proofread_draft_workflow(self, mock_send_email):
        """测试寄送校对稿完整业务流程"""
        # 创建团队和项目创建者
        creator = self.create_user("creator", "creator@moeflow.com", "123456")
        token_creator = creator.generate_token()
        team = Team.create(name="team_test", creator=creator)
        project = Project.create(name="proj_test", team=team)
        target = project.targets(language=Language.by_code("zh-CN")).first()

        # 创建翻译成员
        translator_user = self.create_user("translator1", "trans@moeflow.com", "123456")
        ProjectMember.objects.create(
            project=project,
            user=translator_user,
            identity_key=f"{project.id}:{translator_user.id}",
            display_name="翻译甲",
            tags=["translator"],
            status="active",
        )

        # 创建校对成员
        proofreader_user = self.create_user("proofreader1", "proof@moeflow.com", "123456")
        token_proofreader = proofreader_user.generate_token()
        ProjectMember.objects.create(
            project=project,
            user=proofreader_user,
            identity_key=f"{project.id}:{proofreader_user.id}",
            display_name="校对乙",
            tags=["proofreader"],
            status="active",
        )

        # 创建页面图片文件
        file1 = project.create_file("p01.png")

        # 创建原文 Source
        source1 = Source(
            file=file1,
            rank=0,
            content="こんにちは",
        ).save()
        source2 = Source(
            file=file1,
            rank=1,
            content="ありがとう",
        ).save()

        # 此时还没有翻译和校对，调用接口应抛出无修改异常
        res = self.post(
            f"/v1/projects/{project.id}/targets/{target.id}/send-proofread-draft",
            token=token_proofreader,
            json={"cc_myself": True},
        )
        self.assertErrorEqual(res, ProofreadDraftNoChangesError)

        # 翻译人员录入翻译
        trans1 = Translation.create(
            content="你好呀",
            source=source1,
            target=target,
            user=translator_user,
        )
        trans2 = Translation.create(
            content="谢谢你",
            source=source2,
            target=target,
            user=translator_user,
        )

        # 只有原译，没有校对修改，依旧抛出无修改异常
        res = self.post(
            f"/v1/projects/{project.id}/targets/{target.id}/send-proofread-draft",
            token=token_proofreader,
            json={"cc_myself": True},
        )
        self.assertErrorEqual(res, ProofreadDraftNoChangesError)

        # 校对人员修改了 source1 的翻译
        trans1.proofread_content = "你好啊！"
        trans1.proofreader = proofreader_user
        trans1.save()

        # 成功调用寄送校对稿
        res = self.post(
            f"/v1/projects/{project.id}/targets/{target.id}/send-proofread-draft",
            token=token_proofreader,
            json={"cc_myself": True},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json
        self.assertEqual(data["changed_pages_count"], 1)
        self.assertEqual(data["changed_labels_count"], 1)
        self.assertIn("trans@moeflow.com", data["recipients"])
        self.assertIn("proof@moeflow.com", data["recipients"])

        # 验证 send_email 被调用且参数正确
        self.assertTrue(mock_send_email.called)
        call_kwargs = mock_send_email.call_args[1]
        self.assertEqual(call_kwargs["to_address"], ["trans@moeflow.com"])
        self.assertEqual(call_kwargs["cc_address"], ["proof@moeflow.com"])
        self.assertIn("proj_test", call_kwargs["subject"])
        self.assertEqual(call_kwargs["template"], "email/proofread_draft")
        template_data = call_kwargs["template_data"]
        self.assertEqual(len(template_data["changed_pages"]), 1)
        self.assertEqual(template_data["changed_pages"][0]["page_number"], 1)
        self.assertEqual(template_data["changed_pages"][0]["changed_count"], 1)
        self.assertEqual(template_data["changed_pages"][0]["changed_label_nums"], [1])
