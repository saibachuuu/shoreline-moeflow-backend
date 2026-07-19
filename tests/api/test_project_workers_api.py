from unittest import TestCase
from unittest.mock import call, patch

from app.constants.storage import StorageType
from app.exceptions import NeedTokenError, NoPermissionError
from app.models.project import Project, ProjectRole
from app.models.team import Team
from app.apis.project_workers import (
    extract_workers_from_text,
    normalize_workers,
)
from tests import MoeAPITestCase


class ProjectWorkerHelpersTestCase(TestCase):
    def test_normalize_workers_filters_invalid_values_and_duplicates(self):
        workers = normalize_workers(
            {
                "翻译": [" Alice ", "Alice", "", None, "Bob"],
                "校对": "Carol",
                "未知": ["Nobody"],
            }
        )

        self.assertEqual(workers, {"翻译": ["Alice", "Bob"]})

    def test_extract_workers_supports_full_width_colons_and_translator_checker(self):
        workers = extract_workers_from_text(
            "header\n翻译: Alice\n翻校：Bob\n嵌字： Carol \n翻译:Alice"
        )

        self.assertEqual(
            workers,
            {
                "翻译": ["Alice", "Bob"],
                "校对": ["Bob"],
                "嵌字": ["Carol"],
            },
        )


class ProjectWorkersAPITestCase(MoeAPITestCase):
    def create_project_with_users(self):
        creator = self.create_user("creator")
        outsider = self.create_user("outsider")
        member = self.create_user("member")
        team = Team.create("team", creator=creator)
        project = Project.create("project", team=team, creator=creator)
        member.join(project, role=ProjectRole.by_system_code("translator"))
        return project, creator, member, outsider

    def test_workers_crud_normalizes_data_and_checks_permissions(self):
        project, creator, member, outsider = self.create_project_with_users()
        url = f"/v1/projects/{project.id}/workers"

        response = self.get(url)
        self.assertErrorEqual(response, NeedTokenError)

        response = self.get(url, token=outsider.generate_token())
        self.assertErrorEqual(response, NoPermissionError)

        response = self.get(url, token=member.generate_token())
        self.assertErrorEqual(response)
        self.assertEqual(response.json["workers"], {})

        response = self.put(
            url,
            token=member.generate_token(),
            json={"workers": {"翻译": ["Member"]}},
        )
        self.assertErrorEqual(response, NoPermissionError)

        response = self.put(
            url,
            token=creator.generate_token(),
            json={
                "workers": {
                    "翻译": [" Alice ", "Alice", "Bob"],
                    "校对": [],
                    "unknown": ["Ignored"],
                }
            },
        )
        self.assertErrorEqual(response)
        self.assertEqual(response.json["workers"], {"翻译": ["Alice", "Bob"]})

        response = self.post(
            f"{url}/add",
            token=creator.generate_token(),
            json={"workers": {"翻译": ["Bob", "Carol"], "嵌字": ["Dave"]}},
        )
        self.assertErrorEqual(response)
        self.assertEqual(
            response.json["workers"],
            {"翻译": ["Alice", "Bob", "Carol"], "嵌字": ["Dave"]},
        )

        project.reload()
        self.assertIn('"Alice"', project.workers)

    def test_parse_workers_uses_selected_translation_and_proofread_content(self):
        project, creator, _, outsider = self.create_project_with_users()
        target = project.targets().first()
        image = project.create_file("page.webp")

        first_source = image.create_source("source 1")
        first_translation = first_source.create_translation(
            "翻译: Alice\n校对: Old checker",
            target,
            user=creator,
        )
        first_translation.proofread_content = "翻校：Bob\n嵌字: Carol"
        first_translation.save()
        first_translation.select(creator)

        second_source = image.create_source("source 2")
        second_source.create_translation(
            "图源: Scanner\n翻译：Alice",
            target,
            user=creator,
        )

        url = f"/v1/projects/{project.id}/workers/parse"
        response = self.post(url, token=outsider.generate_token())
        self.assertErrorEqual(response, NoPermissionError)

        response = self.post(url, token=creator.generate_token())
        self.assertErrorEqual(response)
        self.assertEqual(
            response.json["workers"],
            {
                "图源": ["Scanner"],
                "翻译": ["Bob", "Alice"],
                "校对": ["Bob"],
                "嵌字": ["Carol"],
            },
        )

    def test_invalid_stored_workers_are_returned_as_empty(self):
        project, creator, _, _ = self.create_project_with_users()
        project.update(workers="not-json")

        response = self.get(
            f"/v1/projects/{project.id}/workers",
            token=creator.generate_token(),
        )

        self.assertErrorEqual(response)
        self.assertEqual(response.json["workers"], {})

    @patch("app.tasks.thumbnail.create_thumbnail")
    def test_project_thumbnail_regeneration_filters_files_and_checks_permissions(
        self, create_thumbnail
    ):
        project, creator, member, _ = self.create_project_with_users()
        first_image = project.create_file("first.png")
        second_image = project.create_file("second.webp")
        second_image.create_revision()
        project.create_file("notes.txt")
        url = f"/v1/projects/{project.id}/thumbnails"

        response = self.post(url)
        self.assertErrorEqual(response, NeedTokenError)

        response = self.post(url, token=member.generate_token())
        self.assertErrorEqual(response, NoPermissionError)

        response = self.post(url, token=creator.generate_token())
        self.assertErrorEqual(response)
        self.assertEqual(response.json["count"], 2)
        create_thumbnail.assert_has_calls(
            [call(str(first_image.id)), call(str(second_image.id))],
            any_order=True,
        )
        self.assertEqual(create_thumbnail.call_count, 2)

    @patch("app.tasks.thumbnail.create_thumbnail")
    def test_project_thumbnail_regeneration_skips_non_local_storage(
        self, create_thumbnail
    ):
        project, creator, _, _ = self.create_project_with_users()
        project.create_file("first.png")
        self.app.config["STORAGE_TYPE"] = StorageType.OSS

        response = self.post(
            f"/v1/projects/{project.id}/thumbnails",
            token=creator.generate_token(),
        )

        self.assertErrorEqual(response)
        self.assertEqual(response.json["count"], 0)
        create_thumbnail.assert_not_called()
