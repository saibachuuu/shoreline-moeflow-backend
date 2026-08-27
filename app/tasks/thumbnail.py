"""Generate local WebP cover and resample images."""

import os
import shutil
import tempfile
import time
from datetime import datetime
from PIL import Image, ImageOps

from app import STORAGE_PATH, celery
from app.constants.storage import StorageType
from app.exceptions.file import FileNotExistError, SourceFileNotExist
from app import oss
from oss2.exceptions import NoSuchKey
from celery.exceptions import MaxRetriesExceededError

from app.models import connect_db
from . import SyncResult, _FORCE_SYNC_TASK
from celery.utils.log import get_task_logger

logger = get_task_logger(__name__)


def _set_thumbnail_state(image, status, error="", save_name=None):
    """Record thumbnail state, but only while the file still has ``save_name``.

    A task is queued for one specific stored object. If the user re-uploads
    while that task is running, the document already points at a new object and
    a second task is queued for it, so anything the older task has to say is
    stale: writing FAILED over a newer, complete generation would strand the
    image, and writing SUCCEEDED would vouch for files that were just deleted.
    Fencing on ``save_name`` makes the late write a no-op instead.

    ``Document.update`` cannot express the guard -- it forwards every keyword
    as an update operator, so passing ``save_name`` there would overwrite the
    field rather than filter on it. Go through the queryset instead.
    """
    fields = dict(
        thumbnail_status=status,
        thumbnail_error=error[:500],
        thumbnail_update_time=datetime.utcnow(),
    )
    if save_name is None:
        return image.update(**fields)
    return image._qs.filter(pk=image.pk, save_name=save_name).update_one(**fields)


def _publish(temp_path, destination):
    """Move a generated file into the storage directory atomically.

    ``os.replace`` alone raises EXDEV when the temp dir and the storage dir live
    on different filesystems, which is the normal layout once storage is a
    bind-mounted volume. Copying straight to the destination would instead let
    nginx serve a half-written image, so copy to a sibling of the destination
    first and rename within the same filesystem.

    The staging name must be unique per *task*, not per process: the documented
    worker runs ``-P eventlet``, where every concurrent task shares one pid, and
    two tasks for the same image do occur (a project-wide rebuild racing a
    re-upload, or a broker redelivery). Sharing one staging path would let one
    task's copy interleave with the other's rename and publish a truncated
    ``.webp`` -- exactly what this helper exists to prevent.
    """
    directory, name = os.path.split(destination)
    staging_fd, staging_path = tempfile.mkstemp(
        dir=directory or ".", prefix=f".{name}.tmp-"
    )
    os.close(staging_fd)
    try:
        shutil.copyfile(temp_path, staging_path)
        # mkstemp 创建的文件是 0600，nginx (www-data) 无法读取，
        # 静态 /storage 服务会 403。发布前显式改为 0644。
        os.chmod(staging_path, 0o644)
        os.replace(staging_path, destination)
    except BaseException:
        try:
            os.unlink(staging_path)
        except OSError:
            pass
        raise
    else:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


@celery.task(
    name="tasks.create_thumbnail_task",
    bind=True,
    # Ack only after the run finishes so a killed worker (OOM, redeploy)
    # redelivers instead of stranding the document at GENERATING forever.
    acks_late=True,
    # Transient failures (broker/OSS hiccups) retry; the document stays at
    # GENERATING between attempts and cover_url falls back to the storage
    # probe, so a retry never hides an already-good thumbnail.
    max_retries=3,
    default_retry_delay=30,
)
def create_thumbnail_task(self, image_id: str, image_path=None):
    """
    生成图片缩略图（缩略封面和采样图）

    支持 LOCAL_STORAGE 和 OSS (R2/S3-compatible) 模式。
    R2 模式不支持阿里云 OSS 的服务端图片处理（x-oss-process），
    因此需要预先生成并上传处理后的文件。

    :param image_id: 图片ID
    :param image_path: 可选的图片路径，用于从临时文件生成
    :return:
    """
    from app.models.file import File
    from app.models.project import Project
    from app.models.output import Output
    from app.models.team import Team
    from app.models.target import Target
    from app.models.user import User

    (File, Project, Team, Target, User, Output)

    config = celery.conf.app_config
    oss_file_prefix = config["OSS_FILE_PREFIX"]
    connect_db(config)
    oss.init(config)

    is_local = config["STORAGE_TYPE"] == StorageType.LOCAL_STORAGE
    is_r2 = (
        config["STORAGE_TYPE"] == StorageType.OSS
        and config.get("OSS_BUCKET_STYLE") == "R2"
    )

    # S3-style OSS uses server-side image processing (x-oss-process)
    if not is_local and not is_r2:
        return f"失败：创建缩略图失败，非本地模式 {image_id}"
    started_at = time.monotonic()
    download_ms = 0
    transform_ms = 0
    persist_ms = 0
    image = None
    fenced_save_name = None
    downloaded_tmp = None
    try:
        image = File.by_id(image_id)
        from app.constants.file import ThumbnailStatus

        # Pin the object this run is about. Every later state write is
        # conditional on the document still pointing at it, so a re-upload that
        # lands mid-run silently wins instead of being overwritten by us.
        fenced_save_name = image.save_name
        _set_thumbnail_state(
            image, ThumbnailStatus.GENERATING, save_name=fenced_save_name
        )

        if image_path is None:
            if is_r2:
                tmp = tempfile.NamedTemporaryFile(delete=False)
                downloaded_tmp = tmp.name
                try:
                    image.download_real_file(local_path=tmp.name)
                except (FileNotExistError, SourceFileNotExist, NoSuchKey):
                    # R2 (and any S3-compatible) storage reports a missing
                    # source as NoSuchKey; SourceFileNotExist covers an empty
                    # save_name.  Both mean "source image is gone", not a
                    # transient error, so this is terminal, not retryable.
                    _set_thumbnail_state(
                        image,
                        ThumbnailStatus.FAILED,
                        "source file is missing",
                        save_name=fenced_save_name,
                    )
                    return f"失败：创建缩略图失败，原图不存在 {image_id}"
                image_path = tmp.name
                download_ms = round((time.monotonic() - started_at) * 1000)
            else:
                image_path = os.path.join(
                    STORAGE_PATH, oss_file_prefix, image.save_name
                )

        if not os.path.isfile(image_path):
            _set_thumbnail_state(
                image,
                ThumbnailStatus.FAILED,
                "source file is missing",
                save_name=fenced_save_name,
            )
            return f"失败：创建缩略图失败，原图不存在 {image_id}"

        save_name_prefix = image.save_name.rsplit(".", 1)[0]
        resampling_filter = getattr(Image, "Resampling", Image).LANCZOS

        with tempfile.TemporaryDirectory(prefix=".thumbnail-") as temp_dir:
            temp_cover_path = os.path.join(temp_dir, f"cover-{save_name_prefix}.webp")
            temp_resample_path = os.path.join(
                temp_dir, f"resample-{save_name_prefix}.webp"
            )

            transform_started_at = time.monotonic()
            with Image.open(image_path) as original:
                thumbnail = ImageOps.fit(original, (180, 140), resampling_filter)
                if thumbnail.mode in ("RGBA", "P"):
                    thumbnail = thumbnail.convert("RGB")
                thumbnail.save(temp_cover_path, "WEBP", quality=50)

                resample = original.copy()
                if resample.width > 1920:
                    ratio = 1920 / resample.width
                    resample = resample.resize(
                        (1920, int(resample.height * ratio)), resampling_filter
                    )
                if resample.mode in ("RGBA", "P"):
                    resample = resample.convert("RGB")
                resample.save(temp_resample_path, "WEBP", quality=50)
            transform_ms = round((time.monotonic() - transform_started_at) * 1000)

            cover_name = f"{config['OSS_PROCESS_COVER_NAME']}-{save_name_prefix}.webp"
            resample_name = (
                f"{config['OSS_PROCESS_RESAMPLE_NAME']}-{save_name_prefix}.webp"
            )

            persist_started_at = time.monotonic()
            if is_r2:
                with open(temp_cover_path, "rb") as f:
                    oss.upload(oss_file_prefix, cover_name, f)
                with open(temp_resample_path, "rb") as f:
                    oss.upload(oss_file_prefix, resample_name, f)
            else:
                output_dir = os.path.join(STORAGE_PATH, oss_file_prefix)
                os.makedirs(output_dir, exist_ok=True)
                # The temp dir and the storage dir are usually on different
                # filesystems (tmpfs/overlay vs. a bind-mounted volume), where
                # ``os.replace`` fails with EXDEV. Stage the copy inside the
                # destination directory so readers never observe a partial file,
                # then rename within that filesystem.
                _publish(temp_cover_path, os.path.join(output_dir, cover_name))
                _publish(
                    temp_resample_path,
                    os.path.join(output_dir, resample_name),
                )
            persist_ms = round((time.monotonic() - persist_started_at) * 1000)
        _set_thumbnail_state(
            image, ThumbnailStatus.SUCCEEDED, save_name=fenced_save_name
        )
        logger.info(
            "thumbnail_performance file_id=%s download_ms=%s transform_ms=%s persist_ms=%s total_ms=%s",
            image_id,
            download_ms,
            transform_ms,
            persist_ms,
            round((time.monotonic() - started_at) * 1000),
        )
    except (FileNotExistError, SourceFileNotExist, NoSuchKey):
        if image is not None:
            _set_thumbnail_state(
                image,
                ThumbnailStatus.FAILED,
                "source file is missing",
                save_name=fenced_save_name,
            )
        return f"失败：创建缩略图失败，原图不存在 {image_id}"
    except Exception as error:
        # Transient failures retry with backoff; the state stays GENERATING
        # (cover_url's storage probe keeps working) until retries are spent.
        # Nothing is marked FAILED on the first attempt: a late second task
        # must never overwrite a SUCCEEDED run of the same save_name.
        try:
            raise self.retry(exc=error, countdown=30)
        except MaxRetriesExceededError:
            if image is not None:
                _set_thumbnail_state(
                    image,
                    ThumbnailStatus.FAILED,
                    str(error)[:500],
                    save_name=fenced_save_name,
                )
            logger.exception("Failed to create thumbnails for %s", image_id)
            return f"失败：创建缩略图失败 {image_id}"
    finally:
        if downloaded_tmp is not None:
            try:
                os.unlink(downloaded_tmp)
            except OSError:
                pass
    return f"成功：创建缩略图成功 {image_id}"


def create_thumbnail(image_id, /, *, run_sync=False, image_path=None):
    from app.constants.file import ThumbnailStatus
    from app.models.file import File

    File.objects(id=image_id).update(
        thumbnail_status=ThumbnailStatus.QUEUING,
        thumbnail_error="",
        thumbnail_update_time=datetime.utcnow(),
    )
    if run_sync or _FORCE_SYNC_TASK:
        create_thumbnail_task(image_id, image_path)
        return SyncResult()

    # A Celery broadcast ping on every upload adds a broker round trip to the
    # request path and may wait for its timeout.  Queue the work directly; an
    # explicit opt-in remains available for installations that require the old
    # synchronous fallback when no worker answers.
    if celery.conf.app_config.get("THUMBNAIL_SYNC_FALLBACK", False):
        alive_workers = celery.control.ping()
        if len(alive_workers) == 0:
            create_thumbnail_task(image_id, image_path)
            return SyncResult()
    try:
        return create_thumbnail_task.delay(image_id, image_path)
    except Exception as error:
        File.objects(id=image_id).update(
            thumbnail_status=ThumbnailStatus.FAILED,
            thumbnail_error=str(error)[:500],
            thumbnail_update_time=datetime.utcnow(),
        )
        raise
