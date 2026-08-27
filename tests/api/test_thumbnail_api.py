from unittest.mock import patch

from app.constants.storage import StorageType
from app.exceptions import FileTypeNotSupportError, NoPermissionError
from app.models.project import Project, ProjectRole
from app.models.team import Team
from app.tasks import thumbnail as thumbnail_module
from tests import MoeAPITestCase

import os
import tempfile
import unittest


class PublishTestCase(MoeAPITestCase):
    @unittest.skipIf(os.name != "posix", "POSIX 权限位仅在 Linux 上可验证")
    def test_publish_output_is_world_readable(self):
        """_publish 发布的缩略图必须是 0644，否则 nginx(www-data) 静态服务 403。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = os.path.join(tmpdir, "source.webp")
            with open(source, "wb") as handle:
                handle.write(b"webp-bytes")
            destination = os.path.join(tmpdir, "cover-x.webp")
            thumbnail_module._publish(source, destination)
            self.assertTrue(os.path.exists(destination))
            self.assertEqual(os.stat(destination).st_mode & 0o777, 0o644)


class FileThumbnailAPITestCase(MoeAPITestCase):
    def create_project_with_image(self):
        creator = self.create_user("creator")
        member = self.create_user("member")
        outsider = self.create_user("outsider")
        team = Team.create("team", creator=creator)
        project = Project.create("project", team=team, creator=creator)
        member.join(project, role=ProjectRole.by_system_code("translator"))
        image = project.create_file("image.webp")
        text = project.create_file("notes.txt")
        return project, image, text, creator, member, outsider

    @patch("app.tasks.thumbnail.create_thumbnail")
    def test_single_thumbnail_requires_project_access_and_image_type(
        self, create_thumbnail
    ):
        _, image, text, creator, member, outsider = self.create_project_with_image()

        response = self.post(
            f"/v1/files/{image.id}/thumbnail",
            token=outsider.generate_token(),
        )
        self.assertErrorEqual(response, NoPermissionError)

        response = self.post(
            f"/v1/files/{text.id}/thumbnail",
            token=creator.generate_token(),
        )
        self.assertErrorEqual(response, FileTypeNotSupportError)

        response = self.post(
            f"/v1/files/{image.id}/thumbnail",
            token=member.generate_token(),
        )
        self.assertErrorEqual(response)
        create_thumbnail.assert_called_once_with(str(image.id))

    @patch("app.tasks.thumbnail.create_thumbnail")
    def test_single_thumbnail_is_noop_for_oss_storage(self, create_thumbnail):
        _, image, _, creator, _, _ = self.create_project_with_image()
        self.app.config["STORAGE_TYPE"] = StorageType.OSS

        response = self.post(
            f"/v1/files/{image.id}/thumbnail",
            token=creator.generate_token(),
        )

        self.assertErrorEqual(response)
        self.assertEqual(response.json["count"], 0)
        create_thumbnail.assert_not_called()
