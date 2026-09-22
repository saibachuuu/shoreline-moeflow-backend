"""画廊归档导入任务的单元测试（不访问真实网络）。"""

import io
import os
import shutil
import tempfile
import zipfile
from unittest.mock import patch

from app.constants.file import FileType

# 模块不存在时在收集期整文件跳过（§4 检查项 11：零模块时套件仍全绿）
from tests.modules import requires_module

requires_module("archive_import")

from app.modules.archive_import.constants import ArchiveImportStatus
from app.modules.archive_import.models import ArchiveImportTask
from app.modules.archive_import import tasks as archive_import_module
from app.utils.secrets import decrypt_secret
from tests import TEST_FILE_PATH, MoeTestCase


def _png_bytes() -> bytes:
    with open(os.path.join(TEST_FILE_PATH, "2kb.png"), "rb") as file:
        return file.read()


def _make_zip(entries) -> bytes:
    """entries: {name: bytes}，按给定顺序写入 zip。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buffer.getvalue()


class ArchiveImportTaskUnitTestCase(MoeTestCase):
    def test_canonicalize_archive_url(self):
        canonical = archive_import_module.canonicalize_archive_url
        self.assertEqual(
            canonical(
                "https://h1.hath.network/archive/123/aaaa/bbbb/",
                "123",
            ),
            "https://h1.hath.network/archive/123/aaaa/bbbb/2?start=1",
        )
        self.assertIsNone(canonical("https://evil.example.com/x", "123"))
        self.assertIsNone(canonical("not a url", "123"))

    def test_stream_test_zip_valid_and_corrupt(self):
        png = _png_bytes()
        valid = _make_zip({"images/1.png": png, "images/2.png": png})
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp.write(valid)
            path = tmp.name
        try:
            self.assertEqual(
                archive_import_module._stream_test_zip(path), len(png) * 2
            )
        finally:
            os.unlink(path)

    def test_stream_test_zip_enforces_entry_and_uncompressed_budgets(self):
        png = _png_bytes()
        payload = _make_zip({"images/1.png": png, "images/2.png": png})
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp.write(payload)
            path = tmp.name
        try:
            with self.assertRaisesRegex(ValueError, "解压后大小"):
                archive_import_module._stream_test_zip(
                    path,
                    max_entries=10,
                    max_uncompressed_bytes=len(png),
                    max_entry_bytes=len(png),
                )
            with self.assertRaisesRegex(ValueError, "条目数量"):
                archive_import_module._stream_test_zip(
                    path,
                    max_entries=1,
                    max_uncompressed_bytes=len(png) * 10,
                    max_entry_bytes=len(png) * 2,
                )
        finally:
            os.unlink(path)

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp.write(b"this is not a zip at all")
            path = tmp.name
        try:
            with self.assertRaises(ValueError):
                archive_import_module._stream_test_zip(path)
        finally:
            os.unlink(path)

    def test_zip_image_entries_filtering(self):
        png = _png_bytes()
        payload = _make_zip(
            {
                "images/1.png": png,
                "images/2.jpg": png,
                "images/sub/3.png": png,  # 嵌套目录 → 取 basename
                "translations.txt": "[1.png]",
                "images/.hidden": png,  # 隐藏文件 → 跳过
                "images/notes.txt": "text",  # 非图片 → 跳过
                "cover.webp": png,  # 根目录平铺图片 → 保留
            }
        )
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp.write(payload)
            path = tmp.name
        try:
            entries = archive_import_module._zip_image_entries(path, max_entries=100)
            names = [name for _, name in entries]
            self.assertEqual(
                sorted(names), ["1.png", "2.jpg", "3.png", "cover.webp"]
            )
        finally:
            os.unlink(path)

    def test_zip_image_entries_max(self):
        png = _png_bytes()
        payload = _make_zip({f"images/{i}.png": png for i in range(5)})
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp.write(payload)
            path = tmp.name
        try:
            with self.assertRaises(ValueError):
                archive_import_module._zip_image_entries(path, max_entries=3)
        finally:
            os.unlink(path)

    def test_download_zip_creates_missing_tmp_dir(self):
        """TMP_PATH 缺失时 _download_zip 自动创建，而不是毛糙崩溃。"""
        fake_tmp = os.path.join(
            tempfile.gettempdir(), "archive-fake-tmp-missing"
        )
        shutil.rmtree(fake_tmp, ignore_errors=True)
        self.assertFalse(os.path.isdir(fake_tmp))

        class FakeResponse:
            url = "https://h1.hath.network/archive/1/a/b/2?start=1"

            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size):
                yield b"PK\x03\x04fakezip"

        with patch.object(
            archive_import_module, "TMP_PATH", fake_tmp
        ), patch.object(
            archive_import_module.requests, "get", return_value=FakeResponse()
        ):
            path = archive_import_module._download_zip(
                "https://h1.hath.network/archive/1/a/b/2?start=1", 10 * 1024 * 1024
            )
        try:
            self.assertTrue(os.path.isdir(fake_tmp))
            self.assertTrue(os.path.exists(path))
        finally:
            shutil.rmtree(fake_tmp, ignore_errors=True)
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)

    def test_download_zip_rejects_https_redirect_to_untrusted_host(self):
        class FakeResponse:
            url = "https://evil.example.com/archive.zip"

            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size):
                yield b"not-used"

        with patch.object(
            archive_import_module.requests, "get", return_value=FakeResponse()
        ):
            with self.assertRaisesRegex(ValueError, "hath.network"):
                archive_import_module._download_zip(
                    "https://h1.hath.network/archive/1/a/b/2?start=1",
                    1024,
                )


class ArchiveImportTaskFlowTestCase(MoeTestCase):
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

    @staticmethod
    def _write_temp_zip(payload: bytes) -> str:
        work_dir = tempfile.mkdtemp(prefix="archive-test-")
        path = os.path.join(work_dir, "archive.zip")
        with open(path, "wb") as file:
            file.write(payload)
        return path

    def test_full_flow_imports_images(self):
        png = _png_bytes()
        payload = _make_zip(
            {
                "images/1.png": png,
                "images/2.png": png,
                "images/sub/3.png": png,
                "translations.txt": "ignored",
            }
        )
        project = self._project_with_keys()
        task = ArchiveImportTask.create(
            project=project, gid="123", token="tok", gallery_url="https://exhentai.org/g/123/tok/"
        )
        zip_path = self._write_temp_zip(payload)
        with patch.object(
            archive_import_module,
            "resolve_archive_url",
            return_value="https://h1.hath.network/archive/123/aaaa/bbbb/2?start=1",
        ), patch.object(
            archive_import_module, "_download_zip", return_value=zip_path
        ):
            result = archive_import_module.archive_import_task(
                str(project.id), "123", "tok"
            )
        task.reload()
        self.assertIn("成功", result)
        self.assertEqual(task.status, ArchiveImportStatus.SUCCEEDED)
        self.assertEqual(task.completed_pages, 3)
        names = sorted(
            file.name for file in project.files(type_only=FileType.IMAGE)
        )
        self.assertEqual(names, ["1.png", "2.png", "3.png"])

    def test_failure_no_api_key(self):
        project = self._project_with_keys(keys=[])
        task = ArchiveImportTask.create(project=project, gid="1", token="t")
        result = archive_import_module.archive_import_task(str(project.id), "1", "t")
        task.reload()
        self.assertIn("失败", result)
        self.assertEqual(task.status, ArchiveImportStatus.FAILED)
        self.assertIn("API key", task.error)

    def test_failure_resolve_returns_none(self):
        project = self._project_with_keys()
        task = ArchiveImportTask.create(project=project, gid="1", token="t")
        with patch.object(archive_import_module, "resolve_archive_url", return_value=None):
            archive_import_module.archive_import_task(str(project.id), "1", "t")
        task.reload()
        self.assertEqual(task.status, ArchiveImportStatus.FAILED)
        self.assertIn("归档链接", task.error)

    def test_failure_corrupt_zip(self):
        project = self._project_with_keys()
        task = ArchiveImportTask.create(project=project, gid="1", token="t")
        zip_path = self._write_temp_zip(b"not a real zip")
        with patch.object(
            archive_import_module,
            "resolve_archive_url",
            return_value="https://h1.hath.network/archive/1/a/b/2?start=1",
        ), patch.object(archive_import_module, "_download_zip", return_value=zip_path):
            archive_import_module.archive_import_task(str(project.id), "1", "t")
        task.reload()
        self.assertEqual(task.status, ArchiveImportStatus.FAILED)
        self.assertIn("zip", task.error.lower())

    def test_failure_empty_zip(self):
        project = self._project_with_keys()
        task = ArchiveImportTask.create(project=project, gid="1", token="t")
        zip_path = self._write_temp_zip(_make_zip({"translations.txt": "x"}))
        with patch.object(
            archive_import_module,
            "resolve_archive_url",
            return_value="https://h1.hath.network/archive/1/a/b/2?start=1",
        ), patch.object(archive_import_module, "_download_zip", return_value=zip_path):
            archive_import_module.archive_import_task(str(project.id), "1", "t")
        task.reload()
        self.assertEqual(task.status, ArchiveImportStatus.FAILED)
        self.assertIn("图片", task.error)

    def test_multi_key_fallback_order(self):
        project = self._project_with_keys(
            keys=[
                {"id": "k1", "key": "bad-key", "enabled": True},
                {"id": "k2", "key": "good-key", "enabled": True},
            ]
        )
        png = _png_bytes()
        payload = _make_zip({"images/1.png": png})
        zip_path = self._write_temp_zip(payload)
        calls = []

        def fake_resolve(api_url, gid, token, api_key):
            calls.append(api_key)
            return (
                "https://h1.hath.network/archive/1/a/b/2?start=1"
                if api_key == "good-key"
                else None
            )

        ArchiveImportTask.create(project=project, gid="1", token="t")
        with patch.object(
            archive_import_module, "resolve_archive_url", side_effect=fake_resolve
        ), patch.object(archive_import_module, "_download_zip", return_value=zip_path):
            result = archive_import_module.archive_import_task(str(project.id), "1", "t")
        self.assertIn("成功", result)
        self.assertEqual(calls, ["bad-key", "good-key"])

    def test_team_archive_api_url_takes_precedence(self):
        """团队自己配置的档案 API 基址优先于全局配置。"""
        project = self._project_with_keys()
        team = project.team
        team.archive_api_url = "https://team-api.example.cn"
        team.save()
        png = _png_bytes()
        zip_path = self._write_temp_zip(_make_zip({"images/1.png": png}))
        seen_urls = []

        def fake_resolve(api_url, gid, token, api_key):
            seen_urls.append(api_url)
            return "https://h1.hath.network/archive/1/a/b/2?start=1"

        ArchiveImportTask.create(project=project, gid="1", token="t")
        with patch.object(
            archive_import_module, "resolve_archive_url", side_effect=fake_resolve
        ), patch.object(archive_import_module, "_download_zip", return_value=zip_path):
            result = archive_import_module.archive_import_task(str(project.id), "1", "t")
        self.assertIn("成功", result)
        self.assertEqual(seen_urls, ["https://team-api.example.cn"])

    def test_team_archive_api_url_falls_back_to_global(self):
        """团队未配置基址时使用全局 ARCHIVE_PROVIDER_API_URL。"""
        project = self._project_with_keys()
        png = _png_bytes()
        zip_path = self._write_temp_zip(_make_zip({"images/1.png": png}))
        seen_urls = []

        def fake_resolve(api_url, gid, token, api_key):
            seen_urls.append(api_url)
            return "https://h1.hath.network/archive/1/a/b/2?start=1"

        ArchiveImportTask.create(project=project, gid="1", token="t")
        with patch.object(
            archive_import_module, "resolve_archive_url", side_effect=fake_resolve
        ), patch.object(archive_import_module, "_download_zip", return_value=zip_path):
            result = archive_import_module.archive_import_task(str(project.id), "1", "t")
        self.assertIn("成功", result)
        self.assertEqual(seen_urls, ["https://archive-api.example.com"])

    def test_failure_when_no_api_url_configured(self):
        """团队与全局都未配置档案 API 基址 → FAILED。"""
        self.app.config["ARCHIVE_PROVIDER_API_URL"] = ""
        project = self._project_with_keys()
        task = ArchiveImportTask.create(project=project, gid="1", token="t")
        result = archive_import_module.archive_import_task(str(project.id), "1", "t")
        task.reload()
        self.assertIn("失败", result)
        self.assertEqual(task.status, ArchiveImportStatus.FAILED)
        self.assertIn("档案 API 地址", task.error)

    def test_download_oserror_marks_failed(self):
        """下载阶段遇到 OSError（如临时目录缺失）必须落 FAILED，不能停在 DOWNLOADING。"""
        project = self._project_with_keys()
        task = ArchiveImportTask.create(project=project, gid="1", token="t")
        with patch.object(
            archive_import_module,
            "resolve_archive_url",
            return_value="https://h1.hath.network/archive/1/a/b/2?start=1",
        ), patch.object(
            archive_import_module,
            "_download_zip",
            side_effect=FileNotFoundError("no such dir"),
        ):
            result = archive_import_module.archive_import_task(str(project.id), "1", "t")
        task.reload()
        self.assertIn("失败", result)
        self.assertEqual(task.status, ArchiveImportStatus.FAILED)
        self.assertIn("临时目录", task.error)

    def test_task_updates_latest_record(self):
        """存在多个历史记录时，任务必须更新最新一条而不是最旧的。"""
        project = self._project_with_keys()
        old = ArchiveImportTask.create(project=project, gid="1", token="t")
        latest = ArchiveImportTask.create(project=project, gid="2", token="t2")
        zip_path = self._write_temp_zip(_make_zip({"images/1.png": _png_bytes()}))
        with patch.object(
            archive_import_module,
            "resolve_archive_url",
            return_value="https://h1.hath.network/archive/1/a/b/2?start=1",
        ), patch.object(archive_import_module, "_download_zip", return_value=zip_path):
            result = archive_import_module.archive_import_task(
                str(project.id), "2", "t2"
            )
        old.reload()
        latest.reload()
        self.assertIn("成功", result)
        self.assertEqual(old.status, ArchiveImportStatus.QUEUED)  # 旧记录保持排队态
        self.assertEqual(latest.status, ArchiveImportStatus.SUCCEEDED)
        self.assertEqual(latest.completed_pages, 1)

    def test_task_id_correlates_worker_updates_to_exact_record(self):
        """旧消息不能通过项目查询拿到后创建的新任务。"""
        project = self._project_with_keys()
        old = ArchiveImportTask.create(project=project, gid="1", token="old")
        latest = ArchiveImportTask.create(project=project, gid="2", token="new")
        zip_path = self._write_temp_zip(_make_zip({"images/1.png": _png_bytes()}))
        with patch.object(
            archive_import_module,
            "resolve_archive_url",
            return_value="https://h1.hath.network/archive/1/a/b/2?start=1",
        ), patch.object(archive_import_module, "_download_zip", return_value=zip_path):
            archive_import_module.archive_import_task(
                str(project.id), "1", "old", str(old.id)
            )
        old.reload()
        latest.reload()
        self.assertEqual(old.status, ArchiveImportStatus.SUCCEEDED)
        self.assertEqual(latest.status, ArchiveImportStatus.QUEUED)

    def test_team_api_key_is_encrypted_on_settings_write(self):
        project = self._project_with_keys(keys=[])
        team = project.team
        from app.apis.team import _apply_archive_api_keys

        stored = _apply_archive_api_keys(
            team, [{"key": "plain-key", "remark": "test"}]
        )
        self.assertTrue(stored[0]["key"].startswith("fernet:"))
        self.assertEqual(decrypt_secret(stored[0]["key"]), "plain-key")
