"""Small database-isolated regressions for the performance fixes."""

from io import BytesIO
import os
import shutil
import tempfile
from unittest.mock import Mock, patch

from PIL import Image

from app.constants.file import ThumbnailStatus
from app.constants.storage import StorageType
from app.models.file import File
from app.tasks import thumbnail
from tests import MoeTestCase


class PerformanceRegressionTestCase(MoeTestCase):
    def test_empty_search_term_is_not_a_name_filter(self):
        project = self.create_project("empty-word")
        project.create_file("one.png")
        project.create_file("two.png")

        self.assertEqual(2, project.files().count())
        self.assertEqual(2, project.files(word="").count())
        self.assertEqual(2, project.files(word="   ").count())

    def test_succeeded_thumbnail_skips_the_storage_probe(self):
        """SUCCEEDED 是热路径：列表页每一行都不应再去问存储。"""
        project = self.create_project("thumbnail-state")
        image = project.create_file("image.png")
        image.save_name = "image.png"

        image.thumbnail_status = ThumbnailStatus.SUCCEEDED
        with (
            patch("app.models.file.oss.is_exist") as is_exist,
            patch(
                "app.models.file.oss.sign_url", return_value="/storage/cover-image.webp"
            ) as sign_url,
        ):
            self.assertEqual("/storage/cover-image.webp", image.cover_url)
        is_exist.assert_not_called()
        sign_url.assert_called_once()

        data = image.to_api()
        self.assertEqual(ThumbnailStatus.SUCCEEDED, data["thumbnail_status"])
        self.assertEqual("", data["thumbnail_error"])

    def test_unfinished_status_still_serves_a_thumbnail_that_exists(self):
        """状态不能单独决定结果，否则缩略图会被永久隐藏。

        Celery 默认提前 ack 且本任务没有 acks_late/重试：worker 被 OOM 杀掉后
        文档会永远停在 GENERATING。若此时只看状态，一张已经生成好、就躺在磁盘
        上的缩略图将再也不会被返回。批量重建把整个项目刷成 QUEUING 时同理。
        """
        project = self.create_project("thumbnail-stuck")
        image = project.create_file("image.png")
        image.save_name = "image.png"

        for status in (
            ThumbnailStatus.QUEUING,
            ThumbnailStatus.GENERATING,
            ThumbnailStatus.FAILED,
            ThumbnailStatus.UNKNOWN,
        ):
            with self.subTest(status=status):
                image.thumbnail_status = status
                # 文件在 —— 必须返回真实 URL，而不是 "generating"。
                with (
                    patch("app.models.file.oss.is_exist", return_value=True),
                    patch(
                        "app.models.file.oss.sign_url",
                        return_value="/storage/cover-image.webp",
                    ),
                ):
                    self.assertEqual("/storage/cover-image.webp", image.cover_url)
                # 文件不在 —— 才回落到 "generating"。
                with patch("app.models.file.oss.is_exist", return_value=False):
                    self.assertEqual("generating", image.cover_url)

    def test_replacement_upload_persists_before_cleanup_and_reports_timing(self):
        project = self.create_project("replacement-upload")
        image = project.create_file("image.png")
        image.save_name = "old.png"
        image.file_size = 3
        image.save()
        self.app.config["STORAGE_TYPE"] = StorageType.LOCAL_STORAGE
        self.app.config["OSS_FILE_PREFIX"] = "files/"

        call_order = []

        def record_upload(*args, **kwargs):
            call_order.append("upload")

        def record_delete(*args, **kwargs):
            call_order.append("delete")

        with (
            patch("app.models.file.oss.upload", side_effect=record_upload),
            patch("app.models.file.oss.delete", side_effect=record_delete) as delete,
            patch("app.models.file.create_thumbnail") as queue_thumbnail,
            patch("app.models.file.logger.info") as logger_info,
        ):
            image.upload_real_file(BytesIO(b"replacement bytes"))

        image.reload()
        self.assertNotEqual("old.png", image.save_name)
        self.assertEqual(["upload", "delete"], call_order)
        queue_thumbnail.assert_called_once_with(str(image.id))
        deleted_names = delete.call_args.args[1]
        self.assertEqual("old.png", deleted_names[0])
        self.assertIn("cover-old.webp", deleted_names)
        self.assertIn("resample-old.webp", deleted_names)
        self.assertEqual(
            "upload_performance file_id=%s bytes=%s stage_ms=%s upload_ms=%s total_ms=%s",
            logger_info.call_args.args[0],
        )

    def test_failed_replacement_upload_keeps_old_object(self):
        project = self.create_project("failed-replacement")
        image = project.create_file("image.png")
        image.save_name = "old.png"
        image.file_size = 3
        image.save()

        with (
            patch(
                "app.models.file.oss.upload", side_effect=OSError("storage unavailable")
            ),
            patch("app.models.file.oss.delete") as delete,
            patch("app.models.file.create_thumbnail") as queue_thumbnail,
        ):
            with self.assertRaisesRegex(OSError, "storage unavailable"):
                image.upload_real_file(BytesIO(b"replacement bytes"))

        image.reload()
        self.assertEqual("old.png", image.save_name)
        self.assertEqual(3, image.file_size)
        delete.assert_not_called()
        queue_thumbnail.assert_not_called()

    def test_local_thumbnail_task_updates_lifecycle_and_logs_segments(self):
        project = self.create_project("local-thumbnail")
        image = project.create_file("image.png")
        image.save_name = "original.png"
        image.save()
        config = dict(self.app.config)
        config.update(
            {
                "STORAGE_TYPE": StorageType.LOCAL_STORAGE,
                "OSS_FILE_PREFIX": "files/",
                "OSS_PROCESS_COVER_NAME": "cover",
                "OSS_PROCESS_RESAMPLE_NAME": "resample",
            }
        )

        with tempfile.TemporaryDirectory() as storage_path:
            source_dir = os.path.join(storage_path, "files")
            os.makedirs(source_dir)
            Image.new("RGB", (20, 20), "white").save(
                os.path.join(source_dir, "original.png")
            )
            with (
                patch.object(thumbnail, "celery", Mock(conf=Mock(app_config=config))),
                patch.object(thumbnail, "STORAGE_PATH", storage_path),
                patch.object(thumbnail, "connect_db"),
                patch.object(thumbnail.oss, "init"),
                patch.object(thumbnail.logger, "info") as logger_info,
            ):
                result = thumbnail.create_thumbnail_task.run(str(image.id))

            self.assertIn("成功", result)
            self.assertTrue(
                os.path.isfile(os.path.join(source_dir, "cover-original.webp"))
            )
            self.assertTrue(
                os.path.isfile(os.path.join(source_dir, "resample-original.webp"))
            )

        image.reload()
        self.assertEqual(ThumbnailStatus.SUCCEEDED, image.thumbnail_status)
        self.assertEqual("", image.thumbnail_error)
        self.assertEqual(
            "thumbnail_performance file_id=%s download_ms=%s transform_ms=%s persist_ms=%s total_ms=%s",
            logger_info.call_args.args[0],
        )

    def test_missing_local_thumbnail_source_is_recorded_as_failed(self):
        project = self.create_project("missing-thumbnail")
        image = project.create_file("image.png")
        image.save_name = "missing.png"
        image.save()
        config = dict(self.app.config)
        config.update(
            {"STORAGE_TYPE": StorageType.LOCAL_STORAGE, "OSS_FILE_PREFIX": "files/"}
        )

        with (
            tempfile.TemporaryDirectory() as storage_path,
            patch.object(thumbnail, "celery", Mock(conf=Mock(app_config=config))),
            patch.object(thumbnail, "STORAGE_PATH", storage_path),
            patch.object(thumbnail, "connect_db"),
            patch.object(thumbnail.oss, "init"),
        ):
            result = thumbnail.create_thumbnail_task.run(str(image.id))

        self.assertIn("原图不存在", result)
        image.reload()
        self.assertEqual(ThumbnailStatus.FAILED, image.thumbnail_status)
        self.assertEqual("source file is missing", image.thumbnail_error)

    def test_r2_thumbnail_task_downloads_and_uploads_derivatives(self):
        config = dict(self.app.config)
        config.update(
            {
                "STORAGE_TYPE": StorageType.OSS,
                "OSS_BUCKET_STYLE": "R2",
                "OSS_FILE_PREFIX": "files/",
                "OSS_PROCESS_COVER_NAME": "cover",
                "OSS_PROCESS_RESAMPLE_NAME": "resample",
            }
        )
        image = Mock(save_name="remote.png")

        def write_source(*, local_path):
            Image.new("RGB", (20, 20), "white").save(local_path, format="PNG")

        image.download_real_file.side_effect = write_source
        with (
            patch.object(thumbnail, "celery", Mock(conf=Mock(app_config=config))),
            patch("app.models.file.File.by_id", return_value=image),
            patch.object(thumbnail, "connect_db"),
            patch.object(thumbnail.oss, "init"),
            patch.object(thumbnail.oss, "upload") as upload,
            patch.object(thumbnail.logger, "info") as logger_info,
        ):
            result = thumbnail.create_thumbnail_task.run("remote-image")

        self.assertIn("成功", result)
        self.assertEqual(2, upload.call_count)
        self.assertEqual("files/", upload.call_args_list[0].args[0])
        self.assertEqual("cover-remote.webp", upload.call_args_list[0].args[1])
        self.assertEqual("resample-remote.webp", upload.call_args_list[1].args[1])
        # 状态写入走带围栏的 queryset（``_qs.filter(pk=..., save_name=...)``），
        # 而不是 ``Document.update`` —— 后者会把 save_name 当成赋值而非条件。
        update_one = image._qs.filter.return_value.update_one
        statuses = [
            call.kwargs["thumbnail_status"] for call in update_one.call_args_list
        ]
        self.assertEqual(
            [ThumbnailStatus.GENERATING, ThumbnailStatus.SUCCEEDED], statuses
        )
        # 每次写入都必须围栏在本轮任务开始时的那个对象上。
        for call in image._qs.filter.call_args_list:
            self.assertEqual("remote.png", call.kwargs["save_name"])
        self.assertEqual(
            "thumbnail_performance file_id=%s download_ms=%s transform_ms=%s persist_ms=%s total_ms=%s",
            logger_info.call_args.args[0],
        )

    def test_a_stale_task_cannot_overwrite_a_newer_upload(self):
        """重传后，上一轮任务的迟到写入必须无效。

        任务是为某一个具体存储对象排队的。若用户在任务执行途中重传，文档已经
        指向新对象、并且已经为它排了新任务。此时旧任务写 FAILED 会把一张已经
        生成好的新缩略图打成永久 "generating"，写 SUCCEEDED 则是在为刚被删掉的
        文件背书。按 save_name 加围栏后，迟到的写入直接落空。
        """
        project = self.create_project("stale-task")
        image = project.create_file("image.png")
        image.save_name = "old.png"
        image.thumbnail_status = ThumbnailStatus.GENERATING
        image.save()

        # 重传发生：文档已经指向新对象，并由新任务负责。
        File.objects(id=image.id).update(
            save_name="new.png", thumbnail_status=ThumbnailStatus.SUCCEEDED
        )

        # 旧任务这时才收尾，围栏在 old.png 上。
        thumbnail._set_thumbnail_state(
            image, ThumbnailStatus.FAILED, "source file is missing", save_name="old.png"
        )

        image.reload()
        self.assertEqual("new.png", image.save_name)
        self.assertEqual(ThumbnailStatus.SUCCEEDED, image.thumbnail_status)
        self.assertEqual("", image.thumbnail_error)

        # 而围栏匹配时，写入必须照常生效。
        thumbnail._set_thumbnail_state(
            image, ThumbnailStatus.FAILED, "boom", save_name="new.png"
        )
        image.reload()
        self.assertEqual(ThumbnailStatus.FAILED, image.thumbnail_status)
        self.assertEqual("boom", image.thumbnail_error)

    def test_publish_uses_a_unique_staging_name_per_call(self):
        """暂存文件名必须按调用唯一，不能只按 pid。

        文档推荐的 worker 是 ``-P eventlet``，同一进程内所有并发任务共享 pid。
        同一张图的两个任务（批量重建撞上重传、或 broker 重投）会算出同一个暂存
        路径，一个的 copy 与另一个的 rename 交错就会发布出被截断的 webp ——
        正是这个函数要避免的事。
        """
        with tempfile.TemporaryDirectory() as storage:
            destination = os.path.join(storage, "cover-x.webp")
            observed = []
            real_copyfile = shutil.copyfile

            def record(src, dst, *args, **kwargs):
                observed.append(dst)
                return real_copyfile(src, dst, *args, **kwargs)

            for index in range(2):
                source = os.path.join(storage, f"src{index}")
                with open(source, "wb") as handle:
                    handle.write(b"payload")
                with patch.object(thumbnail.shutil, "copyfile", side_effect=record):
                    thumbnail._publish(source, destination)

            self.assertEqual(2, len(observed))
            self.assertNotEqual(
                observed[0], observed[1], "staging paths collided between calls"
            )
            for path in observed:
                self.assertEqual(storage, os.path.dirname(path))
                self.assertFalse(os.path.exists(path), "staging file was left behind")
            self.assertTrue(os.path.isfile(destination))
            with open(destination, "rb") as handle:
                self.assertEqual(b"payload", handle.read())
