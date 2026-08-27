"""画廊归档导入 API 测试（第三方网络层全部 mock）。"""

import io
import os
import tempfile
import zipfile
from unittest.mock import patch

from app.exceptions.base import ValidateError

from app.constants.archive_import import ArchiveImportStatus
from app.constants.project import ProjectStatus
from app.exceptions import NoPermissionError
from app.exceptions.project import ProjectFinishedError
from app.models.archive_import import ArchiveImportTask
from app.tasks import archive_import as archive_import_module
from app.utils.secrets import decrypt_secret
from tests import TEST_FILE_PATH, MoeAPITestCase


def _png_bytes() -> bytes:
    with open(os.path.join(TEST_FILE_PATH, "2kb.png"), "rb") as file:
        return file.read()


def _make_zip(entries) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buffer.getvalue()


def _write_temp_zip(payload: bytes) -> str:
    work_dir = tempfile.mkdtemp(prefix="archive-api-test-")
    path = os.path.join(work_dir, "archive.zip")
    with open(path, "wb") as file:
        file.write(payload)
    return path


class ArchiveImportAPITestCase(MoeAPITestCase):
    def setUp(self):
        super().setUp()
        self.app.config["ARCHIVE_PROVIDER_API_URL"] = "https://archive-api.example.com"
        self.app.config["ARCHIVE_PROVIDER_API_ALLOWED_HOSTS"] = (
            "team-api.example.cn",
        )

    def _project_with_keys(self, keys=None):
        project = self.create_project("p")
        team = project.team
        team.archive_api_keys = (
            keys
            if keys is not None
            else [{"id": "k1", "key": "secret-key-1", "enabled": True}]
        )
        team.save()
        return project

    def test_post_import_and_query_status(self):
        project = self._project_with_keys()
        token = self.get_creator(project).generate_token()
        zip_path = _write_temp_zip(
            _make_zip({"images/1.png": _png_bytes(), "images/2.png": _png_bytes()}),
        )
        with patch.object(
            archive_import_module,
            "resolve_archive_url",
            return_value="https://h1.hath.network/archive/123/aaaa/bbbb/2?start=1",
        ) as mock_resolve, patch.object(
            archive_import_module, "_download_zip", return_value=zip_path
        ):
            data = self.post(
                f"/v1/projects/{str(project.id)}/import-from-archive",
                json={"gid": "123", "token": "tok", "gallery_url": "https://exhentai.org/g/123/tok/"},
                token=token,
            )
        self.assertErrorEqual(data)
        self.assertTrue(mock_resolve.called)
        task = ArchiveImportTask.latest(project)
        self.assertIsNotNone(task)
        self.assertEqual(task.status, ArchiveImportStatus.SUCCEEDED)

        resp = self.get(
            f"/v1/projects/{str(project.id)}/import-task",
            token=token,
        )
        self.assertErrorEqual(resp)
        self.assertEqual(resp.json["task"]["status"], ArchiveImportStatus.SUCCEEDED)
        self.assertEqual(resp.json["task"]["completed_pages"], 2)

    def test_post_requires_add_file_permission(self):
        project = self._project_with_keys()
        outsider = self.create_user("outsider")
        outsider_token = outsider.generate_token()
        with patch.object(
            archive_import_module, "resolve_archive_url", return_value=None
        ):
            data = self.post(
                f"/v1/projects/{str(project.id)}/import-from-archive",
                json={"gid": "123", "token": "tok"},
                token=outsider_token,
            )
        self.assertErrorEqual(data, NoPermissionError)

    def test_post_rejects_finished_project(self):
        project = self._project_with_keys()
        project.update(status=ProjectStatus.COMPLETED)
        token = self.get_creator(project).generate_token()
        data = self.post(
            f"/v1/projects/{str(project.id)}/import-from-archive",
            json={"gid": "123", "token": "tok"},
            token=token,
        )
        self.assertErrorEqual(data, ProjectFinishedError)

    def test_post_rejects_duplicate_running_task(self):
        project = self._project_with_keys()
        token = self.get_creator(project).generate_token()
        ArchiveImportTask.create(
            project=project, gid="123", token="tok"
        ).set_progress(status=ArchiveImportStatus.DOWNLOADING, stage="下载归档")
        data = self.post(
            f"/v1/projects/{str(project.id)}/import-from-archive",
            json={"gid": "123", "token": "tok"},
            token=token,
        )
        self.assertErrorEqual(data, ValidateError)

    def test_rejects_without_team_key(self):
        project = self._project_with_keys(keys=[])
        token = self.get_creator(project).generate_token()
        data = self.post(
            f"/v1/projects/{str(project.id)}/import-from-archive",
            json={"gid": "123", "token": "tok"},
            token=token,
        )
        # 任务同步执行，API 仍返回成功；任务本身 FAILED
        self.assertErrorEqual(data)
        task = ArchiveImportTask.latest(project)
        self.assertEqual(task.status, ArchiveImportStatus.FAILED)
        self.assertIn("API key", task.error)

    def test_dismiss_import_task(self):
        project = self._project_with_keys()
        token = self.get_creator(project).generate_token()
        ArchiveImportTask.create(project=project, gid="old", token="t").set_progress(
            status=ArchiveImportStatus.SUCCEEDED, total=1, completed=1
        )
        ArchiveImportTask.create(project=project, gid="new", token="t").set_progress(
            status=ArchiveImportStatus.SUCCEEDED, total=2, completed=2
        )
        resp = self.post(
            f"/v1/projects/{str(project.id)}/import-task/dismiss",
            token=token,
        )
        self.assertErrorEqual(resp)
        self.assertTrue(resp.json["dismissed"])
        # 该项目所有非运行中任务全部标记已关闭
        for task in ArchiveImportTask.objects(project=project):
            self.assertTrue(task.dismissed)
        # 查询接口返回 dismissed=true（前端据此不再展示）
        resp = self.get(f"/v1/projects/{str(project.id)}/import-task", token=token)
        self.assertErrorEqual(resp)
        self.assertTrue(resp.json["task"]["dismissed"])

    def test_dismiss_keeps_running_task(self):
        project = self._project_with_keys()
        token = self.get_creator(project).generate_token()
        ArchiveImportTask.create(project=project, gid="done", token="t").set_progress(
            status=ArchiveImportStatus.SUCCEEDED, total=1, completed=1
        )
        running = ArchiveImportTask.create(project=project, gid="run", token="t")
        running.set_progress(status=ArchiveImportStatus.DOWNLOADING, stage="下载归档")
        resp = self.post(
            f"/v1/projects/{str(project.id)}/import-task/dismiss",
            token=token,
        )
        self.assertErrorEqual(resp)
        running.reload()
        self.assertFalse(running.dismissed)
        # 运行中的任务仍为最新，查询不被关闭影响
        resp = self.get(f"/v1/projects/{str(project.id)}/import-task", token=token)
        self.assertErrorEqual(resp)
        self.assertEqual(resp.json["task"]["status"], ArchiveImportStatus.DOWNLOADING)
        self.assertFalse(resp.json["task"]["dismissed"])

    def test_dismiss_requires_access(self):
        project = self._project_with_keys()
        outsider = self.create_user("outsider")
        outsider_token = outsider.generate_token()
        data = self.post(
            f"/v1/projects/{str(project.id)}/import-task/dismiss",
            token=outsider_token,
        )
        self.assertErrorEqual(data, NoPermissionError)


class ArchiveImportTeamKeyAPITestCase(MoeAPITestCase):
    def setUp(self):
        super().setUp()
        self.app.config["ARCHIVE_PROVIDER_API_ALLOWED_HOSTS"] = (
            "team-api.example.cn",
        )

    def _team_and_tokens(self):
        team = self.create_team("key-team")
        admin_token = self.get_creator(team).generate_token()
        outsider = self.create_team("other-team")
        outsider_token = self.get_creator(outsider).generate_token()
        return team, admin_token, outsider_token

    def test_team_key_add_and_masked_response(self):
        team, admin_token, _ = self._team_and_tokens()
        resp = self.put(
            f"/v1/teams/{str(team.id)}",
            json={
                "archive_api_keys": [
                    {"key": "abcd-secret-key-88", "remark": "主 key"},
                ]
            },
            token=admin_token,
        )
        self.assertErrorEqual(resp)
        team.reload()
        self.assertEqual(len(team.archive_api_keys), 1)
        self.assertTrue(team.archive_api_keys[0]["key"].startswith("fernet:"))
        self.assertEqual(
            decrypt_secret(team.archive_api_keys[0]["key"]), "abcd-secret-key-88"
        )
        # 响应脱敏：不含明文，只有尾号
        masked = resp.json["team"]["archive_api_keys"]
        self.assertEqual(len(masked), 1)
        self.assertNotIn("key", masked[0])
        self.assertEqual(masked[0]["key_tail"], "y-88")
        self.assertEqual(masked[0]["remark"], "主 key")

    def test_team_key_update_and_remove(self):
        team, admin_token, _ = self._team_and_tokens()
        team.archive_api_keys = [
            {"id": "k1", "key": "key-one", "remark": "a", "enabled": True},
            {"id": "k2", "key": "key-two", "remark": "b", "enabled": True},
        ]
        team.save()
        # 更新 k1 备注 + 新增 k3 + 删除 k2（不引用）
        resp = self.put(
            f"/v1/teams/{str(team.id)}",
            json={
                "archive_api_keys": [
                    {"id": "k1", "remark": "a-updated"},
                    {"key": "key-three", "remark": "c"},
                ]
            },
            token=admin_token,
        )
        self.assertErrorEqual(resp)
        team.reload()
        keys = team.archive_api_keys
        self.assertEqual(len(keys), 2)
        ids = {item["id"] for item in keys}
        self.assertIn("k1", ids)
        self.assertNotIn("k2", ids)
        by_id = {item["id"]: item for item in keys}
        self.assertEqual(decrypt_secret(by_id["k1"]["key"]), "key-one")
        self.assertEqual(by_id["k1"]["remark"], "a-updated")
        self.assertEqual(by_id["k1"]["enabled"], True)
        by_key = {decrypt_secret(item["key"]): item for item in keys}
        self.assertEqual(by_key["key-three"]["remark"], "c")
        self.assertEqual(by_key["key-three"]["enabled"], True)

    def test_team_key_requires_admin(self):
        team, _, outsider_token = self._team_and_tokens()
        resp = self.put(
            f"/v1/teams/{str(team.id)}",
            json={"archive_api_keys": [{"key": "x"}]},
            token=outsider_token,
        )
        self.assertErrorEqual(resp, NoPermissionError)

    def test_team_key_rejects_invalid_payload(self):
        team, admin_token, _ = self._team_and_tokens()
        resp = self.put(
            f"/v1/teams/{str(team.id)}",
            json={"archive_api_keys": "not-a-list"},
            token=admin_token,
        )
        self.assertErrorEqual(resp, ValidateError)

    def test_team_key_rejects_empty_key(self):
        team, admin_token, _ = self._team_and_tokens()
        resp = self.put(
            f"/v1/teams/{str(team.id)}",
            json={"archive_api_keys": [{"key": "   "}]},
            token=admin_token,
        )
        self.assertErrorEqual(resp, ValidateError)

    def test_team_key_rejects_unknown_id(self):
        team, admin_token, _ = self._team_and_tokens()
        resp = self.put(
            f"/v1/teams/{str(team.id)}",
            json={"archive_api_keys": [{"id": "missing-id"}]},
            token=admin_token,
        )
        self.assertErrorEqual(resp, ValidateError)

    def test_team_api_does_not_leak_keys_to_members(self):
        team = self.create_team("leak-team")
        team.archive_api_keys = [{"id": "k1", "key": "top-secret-key", "enabled": True}]
        team.save()
        member = self.create_user("plain-member")
        member.join(team, role=team.role_cls.by_system_code("member"))
        member_token = member.generate_token()
        resp = self.get(f"/v1/teams/{str(team.id)}", token=member_token)
        self.assertErrorEqual(resp)
        masked = resp.json["archive_api_keys"]
        self.assertEqual(len(masked), 1)
        self.assertNotIn("key", masked[0])
        self.assertEqual(masked[0]["key_tail"], "top-secret-key"[-4:])

    def test_team_archive_api_url_save_and_clear(self):
        team, admin_token, _ = self._team_and_tokens()
        resp = self.put(
            f"/v1/teams/{str(team.id)}",
                json={"archive_api_url": "  https://team-api.example.cn/  "},
            token=admin_token,
        )
        self.assertErrorEqual(resp)
        team.reload()
        self.assertEqual(team.archive_api_url, "https://team-api.example.cn")
        self.assertEqual(resp.json["team"]["archive_api_url"], "https://team-api.example.cn")
        # 置空 = 回退到系统默认
        resp = self.put(
            f"/v1/teams/{str(team.id)}",
            json={"archive_api_url": ""},
            token=admin_token,
        )
        self.assertErrorEqual(resp)
        team.reload()
        self.assertEqual(team.archive_api_url, "")

    def test_team_archive_api_url_member_denied(self):
        team, _, outsider_token = self._team_and_tokens()
        resp = self.put(
            f"/v1/teams/{str(team.id)}",
            json={"archive_api_url": "http://x.example.com"},
            token=outsider_token,
        )
        self.assertErrorEqual(resp, NoPermissionError)

    def test_team_archive_api_url_too_long_rejected(self):
        team, admin_token, _ = self._team_and_tokens()
        resp = self.put(
            f"/v1/teams/{str(team.id)}",
            json={"archive_api_url": "x" * 600},
            token=admin_token,
        )
        self.assertErrorEqual(resp, ValidateError)

    def test_team_archive_api_url_requires_https_and_allowlist(self):
        team, admin_token, _ = self._team_and_tokens()
        for value in ("http://team-api.example.cn", "https://untrusted.example.com"):
            resp = self.put(
                f"/v1/teams/{str(team.id)}",
                json={"archive_api_url": value},
                token=admin_token,
            )
            self.assertErrorEqual(resp, ValidateError)
