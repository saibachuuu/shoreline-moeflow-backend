import io
import os
from zipfile import ZipFile

from app import oss
from app.constants.output import OutputTypes
from app.models.language import Language
from app.models.team import Team
from tests import TEST_FILE_PATH, MoeAPITestCase


class OutputAPITestCase(MoeAPITestCase):
    def test_output_project1(self):
        """测试导出项目全部内容（含图片）"""
        project = self.create_project("p", target_languages=Language.by_code("en"))
        target = project.targets().first()
        token = self.get_creator(project).generate_token()
        with open(os.path.join(TEST_FILE_PATH, "2kb.png"), "rb") as file:
            project.upload("1.png", file)
            project.upload("2.png", file)
            project.upload("3.png", file)
        data = self.post(
            f"/v1/projects/{str(project.id)}/targets/{str(target.id)}/outputs",
            json={"type": OutputTypes.ALL},
            token=token,
        )
        self.assertErrorEqual(data)
        self.assertEqual(project.outputs().count(), 1)
        # 下载文件并查看是否正确
        output = project.outputs().first()
        output_zip_in_oss = oss.download(
            self.app.config["OSS_OUTPUT_PREFIX"] + str(output.id) + "/",
            output.file_name,
        )
        output_zip = ZipFile(io.BytesIO(output_zip_in_oss.read()))
        output_zip_namelist = output_zip.namelist()
        # self.assertIn("ps_script.jsx", output_zip_namelist)
        self.assertIn("translations.txt", output_zip_namelist)
        self.assertIn("images/1.png", output_zip_namelist)
        self.assertIn("images/2.png", output_zip_namelist)
        self.assertIn("images/3.png", output_zip_namelist)
        translations_txt = output_zip.read("translations.txt").decode("utf-8")
        self.assertIn("[1.png]", translations_txt)
        self.assertIn("[2.png]", translations_txt)
        self.assertIn("[3.png]", translations_txt)

    def test_output_project2(self):
        """测试导出项目全部内容（不含图片）"""
        project = self.create_project("p", target_languages=Language.by_code("en"))
        target = project.targets().first()
        token = self.get_creator(project).generate_token()
        with open(os.path.join(TEST_FILE_PATH, "2kb.png"), "rb") as file:
            project.upload("1.png", file)
            project.upload("2.png", file)
            project.upload("3.png", file)
        data = self.post(
            f"/v1/projects/{str(project.id)}/targets/{str(target.id)}/outputs",
            json={"type": OutputTypes.ONLY_TEXT},
            token=token,
        )
        self.assertErrorEqual(data)
        self.assertEqual(project.outputs().count(), 1)
        # 下载文件并查看是否正确
        output = project.outputs().first()
        output_txt_in_oss = oss.download(
            self.app.config["OSS_OUTPUT_PREFIX"] + str(output.id) + "/",
            output.file_name,
        )
        translations_txt = output_txt_in_oss.read().decode("utf-8")
        self.assertIn("[1.png]", translations_txt)
        self.assertIn("[2.png]", translations_txt)
        self.assertIn("[3.png]", translations_txt)

    def test_output_project3(self):
        """测试导出项目部分内容 include（含图片）"""
        project = self.create_project("p", target_languages=Language.by_code("en"))
        target = project.targets().first()
        token = self.get_creator(project).generate_token()
        with open(os.path.join(TEST_FILE_PATH, "2kb.png"), "rb") as file:
            file1 = project.upload("1.png", file)
            project.upload("2.png", file)
            file3 = project.upload("3.png", file)
        data = self.post(
            f"/v1/projects/{str(project.id)}/targets/{str(target.id)}/outputs",
            json={
                "type": OutputTypes.ALL,
                "file_ids_include": [str(file1.id), str(file3.id)],
            },
            token=token,
        )
        self.assertErrorEqual(data)
        self.assertEqual(project.outputs().count(), 1)
        # 下载文件并查看是否正确
        output = project.outputs().first()
        output_zip_in_oss = oss.download(
            self.app.config["OSS_OUTPUT_PREFIX"] + str(output.id) + "/",
            output.file_name,
        )
        output_zip = ZipFile(io.BytesIO(output_zip_in_oss.read()))
        output_zip_namelist = output_zip.namelist()
        # self.assertIn("ps_script.jsx", output_zip_namelist)
        self.assertIn("translations.txt", output_zip_namelist)
        self.assertIn("images/1.png", output_zip_namelist)
        self.assertNotIn("images/2.png", output_zip_namelist)
        self.assertIn("images/3.png", output_zip_namelist)
        translations_txt = output_zip.read("translations.txt").decode("utf-8")
        self.assertIn("[1.png]", translations_txt)
        self.assertNotIn("[2.png]", translations_txt)
        self.assertIn("[3.png]", translations_txt)

    def test_output_project4(self):
        """测试导出项目部分内容 include（不含图片）"""
        project = self.create_project("p", target_languages=Language.by_code("en"))
        target = project.targets().first()
        token = self.get_creator(project).generate_token()
        with open(os.path.join(TEST_FILE_PATH, "2kb.png"), "rb") as file:
            file1 = project.upload("1.png", file)
            project.upload("2.png", file)
            file3 = project.upload("3.png", file)
        data = self.post(
            f"/v1/projects/{str(project.id)}/targets/{str(target.id)}/outputs",
            json={
                "type": OutputTypes.ONLY_TEXT,
                "file_ids_include": [str(file1.id), str(file3.id)],
            },
            token=token,
        )
        self.assertErrorEqual(data)
        self.assertEqual(project.outputs().count(), 1)
        # 下载文件并查看是否正确
        output = project.outputs().first()
        output_txt_in_oss = oss.download(
            self.app.config["OSS_OUTPUT_PREFIX"] + str(output.id) + "/",
            output.file_name,
        )
        translations_txt = output_txt_in_oss.read().decode("utf-8")
        self.assertIn("[1.png]", translations_txt)
        self.assertNotIn("[2.png]", translations_txt)
        self.assertIn("[3.png]", translations_txt)

    def test_output_project5(self):
        """测试导出项目部分内容 exclude（含图片）"""
        project = self.create_project("p", target_languages=Language.by_code("en"))
        target = project.targets().first()
        token = self.get_creator(project).generate_token()
        with open(os.path.join(TEST_FILE_PATH, "2kb.png"), "rb") as file:
            file1 = project.upload("1.png", file)
            project.upload("2.png", file)
            file3 = project.upload("3.png", file)
        data = self.post(
            f"/v1/projects/{str(project.id)}/targets/{str(target.id)}/outputs",
            json={
                "type": OutputTypes.ALL,
                "file_ids_exclude": [str(file1.id), str(file3.id)],
            },
            token=token,
        )
        self.assertErrorEqual(data)
        self.assertEqual(project.outputs().count(), 1)
        # 下载文件并查看是否正确
        output = project.outputs().first()
        output_zip_in_oss = oss.download(
            self.app.config["OSS_OUTPUT_PREFIX"] + str(output.id) + "/",
            output.file_name,
        )
        output_zip = ZipFile(io.BytesIO(output_zip_in_oss.read()))
        output_zip_namelist = output_zip.namelist()
        # self.assertIn("ps_script.jsx", output_zip_namelist)
        self.assertIn("translations.txt", output_zip_namelist)
        self.assertNotIn("images/1.png", output_zip_namelist)
        self.assertIn("images/2.png", output_zip_namelist)
        self.assertNotIn("images/3.png", output_zip_namelist)
        translations_txt = output_zip.read("translations.txt").decode("utf-8")
        self.assertNotIn("[1.png]", translations_txt)
        self.assertIn("[2.png]", translations_txt)
        self.assertNotIn("[3.png]", translations_txt)

    def test_output_project6(self):
        """测试导出项目部分内容 exclude（不含图片）"""
        project = self.create_project("p", target_languages=Language.by_code("en"))
        target = project.targets().first()
        token = self.get_creator(project).generate_token()
        with open(os.path.join(TEST_FILE_PATH, "2kb.png"), "rb") as file:
            file1 = project.upload("1.png", file)
            project.upload("2.png", file)
            file3 = project.upload("3.png", file)
        data = self.post(
            f"/v1/projects/{str(project.id)}/targets/{str(target.id)}/outputs",
            json={
                "type": OutputTypes.ONLY_TEXT,
                "file_ids_exclude": [str(file1.id), str(file3.id)],
            },
            token=token,
        )
        self.assertErrorEqual(data)
        self.assertEqual(project.outputs().count(), 1)
        # 下载文件并查看是否正确
        output = project.outputs().first()
        output_txt_in_oss = oss.download(
            self.app.config["OSS_OUTPUT_PREFIX"] + str(output.id) + "/",
            output.file_name,
        )
        translations_txt = output_txt_in_oss.read().decode("utf-8")
        self.assertNotIn("[1.png]", translations_txt)
        self.assertIn("[2.png]", translations_txt)
        self.assertNotIn("[3.png]", translations_txt)

    def _add_worker_members(self, project):
        """为项目加入活跃工作人员（翻译/校对/嵌字）。"""
        from app.models.project_member import ProjectMember

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

    def _export_only_text(self, project, target, token):
        # Export labels render through lazy_gettext; without an explicit
        # Accept-Language the test client falls back to "en" (machine
        # translation), which would break the Chinese label assertions.
        # Production exports run in Celery workers without a request context
        # and always use the default "zh" locale.
        data = self.post(
            f"/v1/projects/{str(project.id)}/targets/{str(target.id)}/outputs",
            json={"type": OutputTypes.ONLY_TEXT},
            token=token,
            headers={"Accept-Language": "zh"},
        )
        self.assertErrorEqual(data)
        output = project.outputs().first()
        output_txt_in_oss = oss.download(
            self.app.config["OSS_OUTPUT_PREFIX"] + str(output.id) + "/",
            output.file_name,
        )
        return output_txt_in_oss.read().decode("utf-8")

    def test_output_staff_list_default_first_page(self):
        """导出时默认将人员名单植入第一页（框外居中）。"""
        project = self.create_project("p", target_languages=Language.by_code("en"))
        target = project.targets().first()
        token = self.get_creator(project).generate_token()
        self._add_worker_members(project)
        with open(os.path.join(TEST_FILE_PATH, "2kb.png"), "rb") as file:
            project.upload("1.png", file)
            project.upload("2.png", file)
            project.upload("3.png", file)
        translations_txt = self._export_only_text(project, target, token)
        staff_row = "----------------[0]----------------[0.5,0.5,2]"
        self.assertIn(staff_row, translations_txt)
        self.assertIn("翻译：甲", translations_txt)
        self.assertIn("校对：乙、丙", translations_txt)
        self.assertIn("嵌字：乙、丁", translations_txt)
        # 名单块位于第一个文件块内、第二个文件块之前
        self.assertLess(
            translations_txt.index(">>>>>>>>[1.png]<<<<<<<<"),
            translations_txt.index(staff_row),
        )
        self.assertLess(
            translations_txt.index(staff_row),
            translations_txt.index(">>>>>>>>[2.png]<<<<<<<<"),
        )

    def test_output_staff_list_last_page_by_project_setting(self):
        """项目设置 -1 时人员名单植入最后一页块内。"""
        project = self.create_project("p", target_languages=Language.by_code("en"))
        target = project.targets().first()
        token = self.get_creator(project).generate_token()
        self._add_worker_members(project)
        with open(os.path.join(TEST_FILE_PATH, "2kb.png"), "rb") as file:
            project.upload("1.png", file)
            project.upload("2.png", file)
            project.upload("3.png", file)
        project.update(staff_list_page=-1)
        translations_txt = self._export_only_text(project, target, token)
        staff_row = "----------------[0]----------------[0.5,0.5,2]"
        self.assertGreater(
            translations_txt.index(staff_row),
            translations_txt.index(">>>>>>>>[3.png]<<<<<<<<"),
        )

    def test_output_staff_list_team_default(self):
        """项目未设置时名单植入位置跟随团队设置。"""
        project = self.create_project("p", target_languages=Language.by_code("en"))
        target = project.targets().first()
        token = self.get_creator(project).generate_token()
        self._add_worker_members(project)
        with open(os.path.join(TEST_FILE_PATH, "2kb.png"), "rb") as file:
            project.upload("1.png", file)
            project.upload("2.png", file)
            project.upload("3.png", file)
        team = Team.objects(id=project.team.id).first()
        team.update(staff_list_page=-1)
        translations_txt = self._export_only_text(project, target, token)
        staff_row = "----------------[0]----------------[0.5,0.5,2]"
        self.assertGreater(
            translations_txt.index(staff_row),
            translations_txt.index(">>>>>>>>[3.png]<<<<<<<<"),
        )

    def test_output_staff_list_no_workers(self):
        """没有任何活跃工作人员时不植入名单。"""
        project = self.create_project("p", target_languages=Language.by_code("en"))
        target = project.targets().first()
        token = self.get_creator(project).generate_token()
        with open(os.path.join(TEST_FILE_PATH, "2kb.png"), "rb") as file:
            project.upload("1.png", file)
            project.upload("2.png", file)
        translations_txt = self._export_only_text(project, target, token)
        self.assertNotIn("----------------[0]----------------[0.5,0.5,2]", translations_txt)
        self.assertNotIn("翻译：", translations_txt)

    def test_output_project7(self):
        """测试导出项目时使用空数组仍然会导出全部"""
        project = self.create_project("p", target_languages=Language.by_code("en"))
        target = project.targets().first()
        token = self.get_creator(project).generate_token()
        with open(os.path.join(TEST_FILE_PATH, "2kb.png"), "rb") as file:
            project.upload("1.png", file)
            project.upload("2.png", file)
            project.upload("3.png", file)
        data = self.post(
            f"/v1/projects/{str(project.id)}/targets/{str(target.id)}/outputs",
            json={
                "type": OutputTypes.ALL,
                "file_ids_include": [],
                "file_ids_exclude": [],
            },
            token=token,
        )
        self.assertErrorEqual(data)
        self.assertEqual(project.outputs().count(), 1)
        # 下载文件并查看是否正确
        output = project.outputs().first()
        output_zip_in_oss = oss.download(
            self.app.config["OSS_OUTPUT_PREFIX"] + str(output.id) + "/",
            output.file_name,
        )
        output_zip = ZipFile(io.BytesIO(output_zip_in_oss.read()))
        output_zip_namelist = output_zip.namelist()
        # self.assertIn("ps_script.jsx", output_zip_namelist)
        self.assertIn("translations.txt", output_zip_namelist)
        self.assertIn("images/1.png", output_zip_namelist)
        self.assertIn("images/2.png", output_zip_namelist)
        self.assertIn("images/3.png", output_zip_namelist)
        translations_txt = output_zip.read("translations.txt").decode("utf-8")
        self.assertIn("[1.png]", translations_txt)
        self.assertIn("[2.png]", translations_txt)
        self.assertIn("[3.png]", translations_txt)
