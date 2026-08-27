"""人员名单植入导出的模型级测试。"""

import os

from app.constants.project import STAFF_LIST_DEFAULT_PAGE
from app.models.language import Language
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.team import Team
from app.utils.labelplus import load_from_labelplus
from tests import TEST_FILE_PATH, MoeTestCase

STAFF_ROW = "----------------[0]----------------[0.5,0.5,2]"


class StaffListPageTestCase(MoeTestCase):
    def _project_with_members(self):
        """创建项目并加入活跃/非活跃成员，返回 (project, target)。"""
        owner = self.create_user("owner")
        team = Team.create("t1", creator=owner)
        project = Project.create(
            name="p1",
            team=team,
            creator=owner,
            target_languages=Language.by_code("en"),
        )
        for name, tags in [
            ("甲", ["translator"]),
            ("乙", ["proofreader", "typesetter"]),
            ("丙", ["proofreader"]),
            ("丁", ["typesetter"]),
        ]:
            ProjectMember(
                project=project,
                user=self.create_user(name),
                display_name=name,
                tags=tags,
                status="active",
            ).save()
        # 非活跃成员不计入名单
        ProjectMember(
            project=project,
            user=self.create_user("invited-user"),
            display_name="被邀请",
            tags=["translator"],
            status="invited",
        ).save()
        ProjectMember(
            project=project,
            user=self.create_user("removed-user"),
            display_name="已移除",
            tags=["translator"],
            status="removed",
        ).save()
        return project, project.targets().first()

    def _upload_images(self, project, names):
        with open(os.path.join(TEST_FILE_PATH, "2kb.png"), "rb") as file:
            for name in names:
                project.upload(name, file)

    def test_resolve_precedence(self):
        """解析优先级：项目设置 > 团队设置 > 默认第一页。"""
        owner = self.create_user("owner")
        team = Team.create("t1", creator=owner)
        project = Project.create(name="p1", team=team, creator=owner)
        # 都未设置 -> 默认
        self.assertEqual(STAFF_LIST_DEFAULT_PAGE, project._staff_list_page_resolved())
        self.assertEqual(1, project._staff_list_page_resolved())
        # 仅团队设置 -> 使用团队设置
        team.update(staff_list_page=-1)
        project = Project.objects(id=project.id).first()
        self.assertEqual(-1, project._staff_list_page_resolved())
        # 项目设置优先于团队设置
        project.update(staff_list_page=2)
        project = Project.objects(id=project.id).first()
        self.assertEqual(2, project._staff_list_page_resolved())
        # 项目取消设置后回落到团队设置
        project.update(staff_list_page=None)
        project = Project.objects(id=project.id).first()
        self.assertEqual(-1, project._staff_list_page_resolved())

    def test_clamp_target_index(self):
        """超出页数时回退到最近的一页。"""
        target_index = Project._staff_list_target_index
        self.assertEqual(0, target_index(3, 1))
        self.assertEqual(1, target_index(3, 2))
        self.assertEqual(2, target_index(3, 3))
        # 正数超出 -> 最后一页
        self.assertEqual(2, target_index(3, 5))
        # 负数从后往前
        self.assertEqual(2, target_index(3, -1))
        self.assertEqual(1, target_index(3, -2))
        self.assertEqual(0, target_index(3, -3))
        # 负数超出 -> 第一页
        self.assertEqual(0, target_index(3, -5))
        # 没有文件 -> 不植入
        self.assertEqual(-1, target_index(0, 1))
        self.assertEqual(-1, target_index(0, -1))

    def test_staff_block_none_without_workers(self):
        """没有任何活跃工作人员时不植入名单。"""
        owner = self.create_user("owner")
        project = Project.create(
            name="p1", team=Team.create("t1", creator=owner), creator=owner
        )
        self.assertIsNone(project._staff_list_block())

    def test_staff_block_content(self):
        """名单内容：只列有活跃成员的职位，按固定顺序，多人用、连接。"""
        project, _ = self._project_with_members()
        block = project._staff_list_block()
        self.assertIsNotNone(block)
        self.assertTrue(block.startswith(STAFF_ROW + "\r\n"))
        self.assertIn("翻译：甲\r\n", block)
        self.assertIn("校对：乙、丙\r\n", block)
        self.assertIn("嵌字：乙、丁\r\n", block)
        # 没有活跃成员的职位不出现
        self.assertNotIn("图源：", block)
        self.assertNotIn("扫图：", block)
        self.assertNotIn("被邀请", block)
        self.assertNotIn("已移除", block)

    def _labelplus_text(self, project, target):
        return project.to_labelplus(target=target)

    def test_first_page_default(self):
        """默认植入第一页：名单块紧跟第一个文件标记。"""
        project, target = self._project_with_members()
        self._upload_images(project, ["1.png", "2.png", "3.png"])
        text = self._labelplus_text(project, target)
        self.assertLess(
            text.index(">>>>>>>>[1.png]<<<<<<<<"),
            text.index(STAFF_ROW),
        )
        # 名单块在第一页块内、第二页之前
        self.assertLess(text.index(STAFF_ROW), text.index(">>>>>>>>[2.png]<<<<<<<<"))
        self.assertIn("翻译：甲", text)

    def test_last_page_negative(self):
        """staff_list_page=-1 植入最后一页块内。"""
        project, target = self._project_with_members()
        self._upload_images(project, ["1.png", "2.png", "3.png"])
        project.update(staff_list_page=-1)
        project = Project.objects(id=project.id).first()
        text = self._labelplus_text(project, target)
        # 名单块在最后一个文件标记之后
        self.assertGreater(text.index(STAFF_ROW), text.index(">>>>>>>>[3.png]<<<<<<<<"))
        self.assertIn("校对：乙、丙", text)

    def test_positive_index_page(self):
        """staff_list_page=2 植入第二页块内。"""
        project, target = self._project_with_members()
        self._upload_images(project, ["1.png", "2.png", "3.png"])
        project.update(staff_list_page=2)
        project = Project.objects(id=project.id).first()
        text = self._labelplus_text(project, target)
        self.assertGreater(
            text.index(STAFF_ROW), text.index(">>>>>>>>[2.png]<<<<<<<<")
        )
        self.assertLess(text.index(STAFF_ROW), text.index(">>>>>>>>[3.png]<<<<<<<<"))

    def test_clamp_overflow(self):
        """超出页数自动回退最近一页：5 -> 最后一页，-5 -> 第一页。"""
        project, target = self._project_with_members()
        self._upload_images(project, ["1.png", "2.png", "3.png"])
        project.update(staff_list_page=5)
        project = Project.objects(id=project.id).first()
        text = self._labelplus_text(project, target)
        self.assertGreater(
            text.index(STAFF_ROW), text.index(">>>>>>>>[3.png]<<<<<<<<")
        )
        project.update(staff_list_page=-5)
        project = Project.objects(id=project.id).first()
        text = self._labelplus_text(project, target)
        self.assertLess(text.index(STAFF_ROW), text.index(">>>>>>>>[2.png]<<<<<<<<"))

    def test_team_default_fallback_export(self):
        """项目未设置时跟随团队默认值。"""
        project, target = self._project_with_members()
        self._upload_images(project, ["1.png", "2.png", "3.png"])
        team = Team.objects(id=project.team.id).first()
        team.update(staff_list_page=-1)
        # 重新拉取项目，确保团队引用是新鲜的
        project = Project.objects(id=project.id).first()
        text = self._labelplus_text(project, target)
        self.assertGreater(
            text.index(STAFF_ROW), text.index(">>>>>>>>[3.png]<<<<<<<<")
        )

    def test_round_trip_parse(self):
        """生成的名单块可被 load_from_labelplus 无损解析回标签。"""
        project, target = self._project_with_members()
        self._upload_images(project, ["1.png", "2.png", "3.png"])
        text = self._labelplus_text(project, target)
        files = load_from_labelplus(text)
        self.assertEqual(3, len(files))
        staff_label = files[0]["labels"][0]
        self.assertEqual(0.5, staff_label["x"])
        self.assertEqual(0.5, staff_label["y"])
        self.assertEqual(2, staff_label["position_type"])
        self.assertTrue(staff_label["translation"].startswith("翻译：甲"))
        self.assertIn("校对：乙、丙", staff_label["translation"])
        self.assertIn("嵌字：乙、丁", staff_label["translation"])