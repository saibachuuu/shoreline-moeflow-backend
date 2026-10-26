"""项目占用记账：派生图增量、导出回退、幂等。"""

from app.models.output import Output
from app.models.project import Project
from tests import MoeTestCase


class DerivedBytesTestCase(MoeTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.create_project("usage-derived")
        self.image = self.project.create_file("a.png")
        self.image.update(save_name="a.png")

    def test_apply_derived_bytes_writes_project(self):
        self.image.apply_derived_bytes(2048)
        self.project.reload()
        self.assertEqual(2048, self.project.derived_bytes)
        # 项目 file_size 是 KB，顺手同步一个四舍五入的增量
        self.assertEqual(2, self.project.file_size)

    def test_regenerating_uses_delta_not_new_value(self):
        self.image.apply_derived_bytes(2048)
        # 重新生成后只剩 1024 字节：按差值回退，而不是再加 1024
        self.image.apply_derived_bytes(1024 - 2048)
        self.project.reload()
        self.assertEqual(1024, self.project.derived_bytes)
        self.assertEqual(1, self.project.file_size)

    def test_zero_delta_is_a_noop(self):
        self.image.apply_derived_bytes(0)
        self.project.reload()
        self.assertEqual(0, self.project.derived_bytes)


class OutputReleaseTestCase(MoeTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.create_project("usage-output")
        Project.objects(id=self.project.id).update(
            set__file_size=10, set__output_bytes=4096
        )
        target = self.project.targets().first()
        self.assertIsNotNone(target)
        self.output = Output(
            project=self.project,
            target=target,
            file_name="a.zip",
            storage_bytes=4096,
        ).save()

    def test_release_subtracts_bytes_and_kb(self):
        Output._release_project_bytes([self.output])
        self.project.reload()
        self.assertEqual(0, self.project.output_bytes)
        self.assertEqual(6, self.project.file_size)

    def test_release_is_idempotent(self):
        Output._release_project_bytes([self.output])
        # storage_bytes 已归零，重复清理不会再扣一次
        Output._release_project_bytes([self.output])
        self.project.reload()
        self.assertEqual(0, self.project.output_bytes)
        self.assertEqual(6, self.project.file_size)
