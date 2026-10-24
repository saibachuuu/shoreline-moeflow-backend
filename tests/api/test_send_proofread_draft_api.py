from unittest.mock import patch
from flask import render_template
from app.apis.project import generate_diff_html
from app.constants.file import FileType
from app.exceptions.project import NoTranslatorMemberError, ProofreadDraftNoChangesError
from app.models.file import File, Source, Translation, Tip
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

        # 创建原文 Source（source1 带标记坐标，source2 用默认值验证跳过）
        source1 = Source(
            file=file1,
            rank=0,
            content="こんにちは",
            x=0.5,
            y=0.3,
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

        # 校对者在 source1（有修改的标签）留下批注，翻译者补一条，另一条挂在
        # source2（无修改的标签）上——验证批注在两种行下都会渲染且按时间排序
        Tip.create(
            content="语气再自然一点",
            source=source1,
            target=target,
            user=proofreader_user,
        )
        Tip.create(
            content="这里保持原译即可",
            source=source2,
            target=target,
            user=translator_user,
        )
        Tip.create(
            content="标点用全角",
            source=source1,
            target=target,
            user=translator_user,
        )

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
        self.assertIn("image_url", template_data["changed_pages"][0])

        # 批注数据：挂在标签上、按创建时间排序、计入页面与全局统计
        labels = template_data["changed_pages"][0]["labels"]
        label1 = next(l for l in labels if l["label_num"] == 1)
        label2 = next(l for l in labels if l["label_num"] == 2)
        self.assertEqual(
            [t["content"] for t in label1["tips"]],
            ["语气再自然一点", "标点用全角"],
        )
        self.assertEqual(
            [t["author"] for t in label1["tips"]],
            ["proofreader1", "translator1"],
        )
        self.assertEqual([t["content"] for t in label2["tips"]], ["这里保持原译即可"])
        self.assertEqual(template_data["changed_pages"][0]["tip_count"], 3)
        self.assertEqual(template_data["total_tips"], 3)

        # 渲染后的 HTML 与纯文本包含批注内容（自动转义，不需要 safe）。
        # 测试环境的邮件语言为英文（get_locale 的 best_match 默认 en）
        html_out = render_template("email/proofread_draft.html", **template_data)
        self.assertIn("语气再自然一点", html_out)
        self.assertIn("proofreader1：", html_out)
        self.assertIn("3 annotation(s) on this page", html_out)
        text_out = render_template("email/proofread_draft.txt", **template_data)
        self.assertIn("[N] translator1: 标点用全角", text_out)

        # 行式分组结构：[校]/[注] 徽章存在（英文环境 PR/N），三列表头已移除
        self.assertIn(">PR</span>", html_out)
        self.assertIn(">N</span>", html_out)
        self.assertNotIn("<th", html_out)

        # 定位标记：坐标与框内外字段进入数据；测试环境文件无真实 URL
        # （图片单元格被 {% if page.image_url %} 跳过），渲染断言需补地址
        self.assertEqual(label1["x"], 0.5)
        self.assertEqual(label1["y"], 0.3)
        self.assertEqual(label1["position_type"], 1)
        self.assertEqual(label2["x"], 0)
        self.assertEqual(label2["y"], 0)
        self.assertIn("marked_image_url", template_data["changed_pages"][0])
        template_data["changed_pages"][0]["image_url"] = "http://example.com/p01.png"
        template_data["changed_pages"][0]["marked_image_url"] = (
            "http://example.com/p01.marked.webp"
        )
        html_out = render_template("email/proofread_draft.html", **template_data)
        self.assertIn("In the box.", html_out)
        self.assertIn("Out of the box", html_out)
        # 邮件内嵌图用烤图，点开大图仍是原图
        self.assertIn('src="http://example.com/p01.marked.webp"', html_out)
        self.assertIn('href="http://example.com/p01.png"', html_out)

    @patch("app.apis.project.send_email")
    def test_send_proofread_draft_tip_only_page(self, mock_send_email):
        """只有批注、没有译文改动的页面也应纳入校对稿"""
        creator = self.create_user("creator", "creator@moeflow.com", "123456")
        team = Team.create(name="team_test", creator=creator)
        project = Project.create(name="proj_test", team=team)
        target = project.targets(language=Language.by_code("zh-CN")).first()

        translator_user = self.create_user("translator1", "trans@moeflow.com", "123456")
        ProjectMember.objects.create(
            project=project,
            user=translator_user,
            identity_key=f"{project.id}:{translator_user.id}",
            display_name="翻译甲",
            tags=["translator"],
            status="active",
        )
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

        file1 = project.create_file("p01.png")
        source1 = Source(file=file1, rank=0, content="こんにちは").save()
        Translation.create(
            content="你好呀",
            source=source1,
            target=target,
            user=translator_user,
        )
        # 无任何 proofread 修改，只有一条批注
        Tip.create(
            content="这句不用改，注意敬语",
            source=source1,
            target=target,
            user=proofreader_user,
        )

        res = self.post(
            f"/v1/projects/{project.id}/targets/{target.id}/send-proofread-draft",
            token=token_proofreader,
            json={"cc_myself": True},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json
        self.assertEqual(data["changed_pages_count"], 1)
        self.assertEqual(data["changed_labels_count"], 0)

        template_data = mock_send_email.call_args[1]["template_data"]
        page = template_data["changed_pages"][0]
        self.assertEqual(page["changed_count"], 0)
        self.assertEqual(page["tip_count"], 1)
        self.assertEqual(page["labels"][0]["tips"][0]["content"], "这句不用改，注意敬语")
        self.assertEqual(template_data["total_changed_labels"], 0)
        self.assertEqual(template_data["total_tips"], 1)
