"""Generate local WebP cover and resample images."""

import os
import tempfile
from PIL import Image, ImageOps

from app import STORAGE_PATH, celery
from app.constants.storage import StorageType
from app.exceptions.file import FileNotExistError
from app import oss

from app.models import connect_db
from . import SyncResult, _FORCE_SYNC_TASK
from celery.utils.log import get_task_logger

logger = get_task_logger(__name__)


@celery.task(name="tasks.create_thumbnail_task")
def create_thumbnail_task(image_id: str, image_path=None):
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
    try:
        image = File.by_id(image_id)

        if image_path is None:
            if is_r2:
                tmp = tempfile.NamedTemporaryFile(delete=False)
                try:
                    image.download_real_file(local_path=tmp.name)
                except FileNotExistError:
                    os.unlink(tmp.name)
                    return f"失败：创建缩略图失败，原图不存在 {image_id}"
                image_path = tmp.name
            else:
                image_path = os.path.join(
                    STORAGE_PATH, oss_file_prefix, image.save_name
                )

        if not os.path.isfile(image_path):
            return f"失败：创建缩略图失败，原图不存在 {image_id}"

        save_name_prefix = image.save_name.rsplit(".", 1)[0]
        resampling_filter = getattr(Image, "Resampling", Image).LANCZOS

        with tempfile.TemporaryDirectory(prefix=".thumbnail-") as temp_dir:
            temp_cover_path = os.path.join(temp_dir, f"cover-{save_name_prefix}.webp")
            temp_resample_path = os.path.join(
                temp_dir, f"resample-{save_name_prefix}.webp"
            )

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

            cover_name = f"{config['OSS_PROCESS_COVER_NAME']}-{save_name_prefix}.webp"
            resample_name = (
                f"{config['OSS_PROCESS_RESAMPLE_NAME']}-{save_name_prefix}.webp"
            )

            if is_r2:
                with open(temp_cover_path, "rb") as f:
                    oss.upload(oss_file_prefix, cover_name, f)
                with open(temp_resample_path, "rb") as f:
                    oss.upload(oss_file_prefix, resample_name, f)
            else:
                output_dir = os.path.join(STORAGE_PATH, oss_file_prefix)
                os.makedirs(output_dir, exist_ok=True)
                os.replace(temp_cover_path, os.path.join(output_dir, cover_name))
                os.replace(
                    temp_resample_path,
                    os.path.join(output_dir, resample_name),
                )
    except FileNotExistError:
        return f"失败：创建缩略图失败，原图不存在 {image_id}"
    except Exception:
        logger.exception("Failed to create thumbnails for %s", image_id)
        return f"失败：创建缩略图失败 {image_id}"
    finally:
        if is_r2 and image_path and image_path.startswith(tempfile.gettempdir()):
            try:
                os.unlink(image_path)
            except OSError:
                pass
    return f"成功：创建缩略图成功 {image_id}"


def create_thumbnail(image_id, /, *, run_sync=False, image_path=None):
    if run_sync or _FORCE_SYNC_TASK:
        create_thumbnail_task(image_id, image_path)
        return SyncResult()

    alive_workers = celery.control.ping()
    if len(alive_workers) == 0:
        create_thumbnail_task(image_id, image_path)
        return SyncResult()
    return create_thumbnail_task.delay(image_id, image_path)
