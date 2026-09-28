import datetime
import io
import os
from zipfile import ZipFile
from unittest.mock import patch

from mongomock.collection import BulkOperationBuilder

from app import oss
from app.constants.output import OutputTypes
from app.constants.project import ProjectStatus
from app.exceptions import NeedTokenError, NoPermissionError, InvalidObjectIdError
from app.exceptions.base import ValidateError
from app.models.file import File
from app.models.language import Language
from app.models.project_member import ProjectMember
from app.models.team_member import TeamMember
from app.services.file_order import export_file_names, order_snapshot, save_order
from tests import MoeAPITestCase, TEST_FILE_PATH


class FileOrderAPITestCase(MoeAPITestCase):
    def setUp(self):
        super().setUp()
        # mongomock 4.3 lacks PyMongo 4.17's optional bulk sort argument.
        # These writes use no sort; retain the real mock bulk execution.
        original = BulkOperationBuilder.add_update

        def add_update(builder, *args, sort=None, **kwargs):
            return original(builder, *args, **kwargs)

        compatibility = patch.object(BulkOperationBuilder, "add_update", add_update)
        compatibility.start()
        self.addCleanup(compatibility.stop)
        self.project = self.create_project(
            "ordered-pages", target_languages=Language.by_code("en")
        )
        self.owner = self.get_creator(self.project)
        self.token = self.owner.generate_token()
        self.files = [
            self.project.create_file(name) for name in ["10.png", "2.png", "1.png"]
        ]
        self.url = f"/v1/projects/{self.project.id}/files/order"

    def snapshot(self):
        response = self.get(self.url, token=self.token)
        self.assertErrorEqual(response)
        return response.json

    def reorder(self):
        snapshot = self.snapshot()
        ids = [str(f.id) for f in self.files]
        response = self.put(
            self.url,
            json={"file_ids": ids, "version": snapshot["version"]},
            token=self.token,
        )
        self.assertErrorEqual(response)
        return ids

    def test_legacy_order_and_noop_keep_order_null(self):
        snapshot = self.snapshot()
        self.assertEqual(
            [f["name"] for f in snapshot["files"]], ["1.png", "2.png", "10.png"]
        )
        self.assertTrue(all(f["manual_order"] is None for f in snapshot["files"]))
        result = self.put(
            self.url,
            json={
                "file_ids": [f["id"] for f in snapshot["files"]],
                "version": snapshot["version"],
            },
            token=self.token,
        )
        self.assertErrorEqual(result)
        self.assertEqual(export_file_names(list(self.project.files())), {})
        self.assertTrue(all(f.manual_order is None for f in self.project.files()))

    def test_save_order_list_pagination_and_neighbors(self):
        ids = self.reorder()
        snapshot = self.snapshot()
        self.assertEqual([f["id"] for f in snapshot["files"]], ids)
        self.assertEqual([f["manual_order"] for f in snapshot["files"]], [1, 2, 3])
        result = self.get(
            f"/v1/projects/{self.project.id}/files",
            query_string={"page": 2, "limit": 1},
            token=self.token,
        )
        self.assertErrorEqual(result)
        self.assertEqual([f["id"] for f in result.json], [ids[1]])
        result = self.get(f"/v1/files/{ids[1]}", token=self.token)
        self.assertErrorEqual(result)
        self.assertEqual(result.json["prev_image"]["id"], ids[0])
        self.assertEqual(result.json["next_image"]["id"], ids[2])
        self.assertNotIn(
            "prev_image", self.get(f"/v1/files/{ids[0]}", token=self.token).json
        )
        self.assertNotIn(
            "next_image", self.get(f"/v1/files/{ids[2]}", token=self.token).json
        )

    def test_duplicate_missing_foreign_and_stale_ids_rejected(self):
        snapshot = self.snapshot()
        ids = [str(f.id) for f in self.files]
        other = self.create_project("other-order").create_file("foreign.png")
        for invalid in [
            ids[:2],
            [ids[0], ids[0], ids[1]],
            [ids[0], ids[1], str(other.id)],
            ["bad"],
        ]:
            result = self.put(
                self.url,
                json={"file_ids": invalid, "version": snapshot["version"]},
                token=self.token,
            )
            self.assertErrorEqual(
                result, InvalidObjectIdError if invalid == ["bad"] else ValidateError
            )
        self.assertTrue(all(f.manual_order is None for f in self.project.files()))
        self.reorder()
        result = self.put(
            self.url,
            json={"file_ids": ids, "version": snapshot["version"]},
            token=self.token,
        )
        self.assertErrorEqual(result, ValidateError)
        self.assertIsNone(self.project.reload().file_order_lock)

    def test_upload_during_edit_requires_reload(self):
        snapshot = self.snapshot()
        self.project.create_file("new.png")
        result = self.put(
            self.url,
            json={
                "file_ids": [str(f.id) for f in self.files],
                "version": snapshot["version"],
            },
            token=self.token,
        )
        self.assertErrorEqual(result, ValidateError)

    def test_save_lock_and_expired_lock(self):
        self.project.update(
            file_order_lock=datetime.datetime.utcnow() + datetime.timedelta(minutes=1)
        )
        files, version = order_snapshot(self.project)
        with self.assertRaises(ValidateError):
            save_order(
                self.project, self.owner, [str(f.id) for f in reversed(files)], version
            )
        self.project.update(
            file_order_lock=datetime.datetime.utcnow() - datetime.timedelta(minutes=1)
        )
        self.reorder()
        self.assertIsNone(self.project.reload().file_order_lock)

    def test_only_active_builtin_creators_and_admins_can_edit(self):
        self.assertTrue(self.project.to_api(user=self.owner)["can_order_files"])
        self.assertErrorEqual(self.get(self.url), NeedTokenError)
        outsider = self.create_user("order-outsider")
        self.assertErrorEqual(
            self.get(self.url, token=outsider.generate_token()), NoPermissionError
        )
        for index, (scope, tag, status, allowed) in enumerate(
            [
                ("project", "admin", "active", True),
                ("project", "creator", "active", True),
                ("project", "translator", "active", False),
                ("project", "admin", "invited", False),
                ("project", "admin", "removed", False),
                ("team", "admin", "active", True),
                ("team", "creator", "active", True),
                ("team", "member", "active", False),
                ("team", "admin", "removed", False),
            ]
        ):
            user = self.create_user(f"order-user-{index}")
            if scope == "project":
                ProjectMember(
                    project=self.project,
                    user=user,
                    display_name=user.name,
                    tags=[tag],
                    status=status,
                ).save()
            else:
                TeamMember(
                    team=self.project.team, user=user, base_tag=tag, status=status
                ).save()
            self.assertEqual(self.project.to_api(user=user)["can_order_files"], allowed)
            token = user.generate_token()
            self.assertErrorEqual(
                self.get(self.url, token=token), None if allowed else NoPermissionError
            )
            snapshot = self.snapshot()
            result = self.put(
                self.url,
                json={
                    "file_ids": [f["id"] for f in snapshot["files"]],
                    "version": snapshot["version"],
                },
                token=token,
            )
            self.assertErrorEqual(result, None if allowed else NoPermissionError)

    def test_removed_team_admin_and_completed_project_cannot_edit(self):
        admin = self.create_user("removed-order-admin")
        TeamMember(team=self.project.team, user=admin, base_tag="admin").save()
        ProjectMember(
            project=self.project,
            user=admin,
            display_name=admin.name,
            tags=["admin"],
            status="removed",
        ).save()
        self.assertFalse(self.project.can_order_files(admin))
        self.project.update(status=ProjectStatus.COMPLETED)
        self.assertFalse(self.project.reload().can_order_files(self.owner))
        self.assertErrorEqual(self.get(self.url, token=self.token), NoPermissionError)
        self.assertErrorEqual(
            self.put(self.url, json={"file_ids": [], "version": "x"}, token=self.token),
            NoPermissionError,
        )

    def test_rename_overwrite_append_move_and_revision(self):
        self.reorder()
        first = self.files[0].reload()
        first.rename("renamed.png")
        self.assertEqual(first.reload().manual_order, 1)
        self.assertEqual(self.project.create_file("renamed.png").manual_order, 1)
        new_file = self.project.create_file("0.png")
        self.assertEqual(new_file.manual_order, 4)
        revision = first.create_revision()
        self.assertEqual(revision.manual_order, 1)
        revision.activate_revision()
        self.assertEqual(revision.reload().manual_order, 1)
        folder = self.project.create_folder("folder")
        new_file.move_to(folder)
        self.assertIsNone(new_file.reload().manual_order)
        new_file.move_to(None)
        self.assertEqual(new_file.reload().manual_order, 4)

    def test_export_zip_and_labelplus_share_numbered_names(self):
        # Real image storage, synchronous export, and read back the resulting ZIP.
        self.app.config["OUTPUT_WAIT_SECONDS"] = 0
        for file in self.files:
            with open(os.path.join(TEST_FILE_PATH, "2kb.png"), "rb") as image:
                self.project.upload(file.name, image)
        self.reorder()
        target = self.project.targets().first()
        for output_type, include in [
            (OutputTypes.ALL, None),
            (OutputTypes.ALL, [str(self.files[0].id), str(self.files[2].id)]),
            (OutputTypes.ONLY_TEXT, None),
        ]:
            payload = {"type": output_type}
            if include:
                payload["file_ids_include"] = include
            response = self.post(
                f"/v1/projects/{self.project.id}/targets/{target.id}/outputs",
                json=payload,
                token=self.token,
            )
            self.assertErrorEqual(response)
            output = self.project.outputs().first()
            blob = oss.download(
                self.app.config["OSS_OUTPUT_PREFIX"] + str(output.id) + "/",
                output.file_name,
            ).read()
            names = (
                ["0001_10.png", "0002_1.png"]
                if include
                else ["0001_10.png", "0002_2.png", "0003_1.png"]
            )
            if output_type == OutputTypes.ALL:
                archive = ZipFile(io.BytesIO(blob))
                text = archive.read("translations.txt").decode("utf-8")
                for name in names:
                    self.assertIn("images/" + name, archive.namelist())
            else:
                text = blob.decode("utf-8")
            for name in names:
                self.assertIn(f">>>>>>>>[{name}]<<<<<<<<", text)
            self.assertEqual(
                sorted(f.name for f in self.project.files(type_only=2)),
                ["1.png", "10.png", "2.png"],
            )

    def test_mixed_directories_export_uses_unique_names_matching_headers(self):
        self.reorder()
        folder = self.project.create_folder("nested")
        self.project.create_file("10.png", parent=folder)
        files = list(self.project.files(type_only=2))
        mapping = export_file_names(files)
        self.assertEqual(len(set(mapping.values())), len(files))
        text = self.project.to_labelplus(
            target=self.project.targets().first(), files=files, export_names=mapping
        )
        for name in mapping.values():
            self.assertIn(f">>>>>>>>[{name}]<<<<<<<<", text)
        self.assertNotIn("nested/", text)

    def test_auto_sort_clears_order_and_restores_legacy_behavior(self):
        ids = self.reorder()
        snapshot = self.snapshot()
        self.assertEqual(snapshot["default_file_ids"], list(reversed(ids)))
        result = self.put(
            self.url,
            json={
                "file_ids": snapshot["default_file_ids"],
                "version": snapshot["version"],
                "reset_to_default": True,
            },
            token=self.token,
        )
        self.assertErrorEqual(result)
        files = list(self.project.files(parent=None))
        self.assertEqual([f.name for f in files], ["1.png", "2.png", "10.png"])
        self.assertTrue(all(f.manual_order is None for f in files))
        self.assertEqual(export_file_names(files), {})
        text = self.project.to_labelplus(target=self.project.targets().first())
        self.assertIn(">>>>>>>>[1.png]<<<<<<<<", text)
        self.assertNotIn("0001_", text)
        new_file = self.project.create_file("0.png")
        self.assertIsNone(new_file.manual_order)
        self.assertEqual(self.project.files(parent=None).first().id, new_file.id)
        result = self.get(f"/v1/files/{ids[1]}", token=self.token)
        self.assertEqual(result.json["prev_image"]["id"], ids[2])
        self.assertEqual(result.json["next_image"]["id"], ids[0])

    def test_auto_sort_clears_ranks_even_when_visible_order_is_unchanged(self):
        files, _ = order_snapshot(self.project)
        for position, file in enumerate(files, 1):
            file.update(manual_order=position)
        folder = self.project.create_folder("nested-reset")
        nested = self.project.create_file("nested.png", parent=folder)
        nested.update(manual_order=5)
        snapshot = self.snapshot()
        self.assertEqual(
            snapshot["default_file_ids"], [f["id"] for f in snapshot["files"]]
        )
        result = self.put(
            self.url,
            json={
                "file_ids": snapshot["default_file_ids"],
                "version": snapshot["version"],
                "reset_to_default": True,
            },
            token=self.token,
        )
        self.assertErrorEqual(result)
        self.assertTrue(
            all(f.manual_order is None for f in self.project.files(parent=None))
        )
        self.assertEqual(nested.reload().manual_order, 5)

    def test_auto_sort_rejects_unauthorized_incomplete_and_stale_requests(self):
        self.reorder()
        snapshot = self.snapshot()
        payload = {
            "file_ids": snapshot["default_file_ids"],
            "version": snapshot["version"],
            "reset_to_default": True,
        }
        outsider = self.create_user("reset-outsider")
        result = self.put(self.url, json=payload, token=outsider.generate_token())
        self.assertErrorEqual(result, NoPermissionError)
        result = self.put(
            self.url,
            json={**payload, "file_ids": payload["file_ids"][:1]},
            token=self.token,
        )
        self.assertErrorEqual(result, ValidateError)
        self.project.create_file("new-during-reset.png")
        result = self.put(self.url, json=payload, token=self.token)
        self.assertErrorEqual(result, ValidateError)
        self.assertTrue(
            all(file.reload().manual_order is not None for file in self.files)
        )

    def test_manual_order_can_be_saved_again_after_reset(self):
        self.reorder()
        snapshot = self.snapshot()
        result = self.put(
            self.url,
            json={
                "file_ids": snapshot["default_file_ids"],
                "version": snapshot["version"],
                "reset_to_default": True,
            },
            token=self.token,
        )
        self.assertErrorEqual(result)
        self.reorder()
        self.assertEqual(
            [f.name for f in self.project.files()], ["10.png", "2.png", "1.png"]
        )
        self.assertTrue(all(f.manual_order is not None for f in self.project.files()))
