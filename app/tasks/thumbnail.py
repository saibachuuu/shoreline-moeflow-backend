"""
生成缩略图
"""

import os
import shutil
import tempfile
from PIL import Image, ImageOps

from app import STORAGE_PATH, celery
from app.constants.storage import StorageType
from app.exceptions.file import FileNotExistError
from app import oss

from app.models import connect_db
from . import SyncResult
from celery.utils.log import get_task_logger

logger = get_task_logger(__name__)


@celery.task(name="tasks.create_thumbnail_task")
def create_thumbnail_task(image_id: str, image_path=None):
    """
    生成图片缩略图

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

    oss_file_prefix = celery.conf.app_config["OSS_FILE_PREFIX"]
    connect_db(celery.conf.app_config)
    oss.init(celery.conf.app_config)
    if celery.conf.app_config["STORAGE_TYPE"] != StorageType.LOCAL_STORAGE:
        return f"失败：创建缩略图失败，非本地模式 {image_id}"
    try:
        image = File.by_id(image_id)
        
        # 原图路径
        if image_path:
            # 使用传入的临时文件路径
            original_image_path = image_path
        else:
            # 从存储路径读取
            original_image_path = os.path.join(STORAGE_PATH, oss_file_prefix, image.save_name)
            
        save_name_prefix = image.save_name.rsplit(".", 1)[0]
        
        # 最终保存路径
        cover_image_path = os.path.join(
            STORAGE_PATH,
            oss_file_prefix,
            celery.conf.app_config["OSS_PROCESS_COVER_NAME"] + "-" + save_name_prefix + ".webp",
        )
        resample_image_path = os.path.join(
            STORAGE_PATH,
            oss_file_prefix,
            celery.conf.app_config["OSS_PROCESS_RESAMPLE_NAME"] + "-" + save_name_prefix + ".webp",
        )
        
        # 创建临时文件夹
        with tempfile.TemporaryDirectory() as temp_dir:
            # 临时文件路径
            temp_cover_path = os.path.join(temp_dir, f"cover-{save_name_prefix}.webp")
            temp_resample_path = os.path.join(temp_dir, f"resample-{save_name_prefix}.webp")
            
            # 读取原图到内存
            original = Image.open(original_image_path)
            
            # 生成缩略图到临时文件
            thumbnail = ImageOps.fit(original, (180, 140), Image.ANTIALIAS)
            if thumbnail.mode in ("RGBA", "P"):
                thumbnail = thumbnail.convert("RGB")
            thumbnail.save(temp_cover_path, "WEBP", quality=50)
            thumbnail.close()
            
            # 生成采样图到临时文件
            resample = original.copy()
            if resample.width > 1920:
                ratio = 1920 / resample.width
                new_height = int(resample.height * ratio)
                resample = resample.resize((1920, new_height), Image.LANCZOS)
            if resample.mode in ("RGBA", "P"):
                resample = resample.convert("RGB")
            resample.save(temp_resample_path, "WEBP", quality=50)
            resample.close()
            
            original.close()
            
            # 移动到最终位置（原子操作）
            shutil.move(temp_cover_path, cover_image_path)
            shutil.move(temp_resample_path, resample_image_path)
            
    except FileNotExistError:
        return f"失败：创建缩略图失败，原图不存在 {image_id}"
    except Exception:
        logger.exception(Exception)
        return f"失败：创建缩略图失败 {image_id}"
    return f"成功：创建缩略图成功 {image_id}"


def create_thumbnail(image_id, /, *, run_sync=False, image_path=None):
    alive_workers = celery.control.ping()
    if len(alive_workers) == 0 or run_sync:
        # 同步执行
        create_thumbnail_task(image_id, image_path)
        return SyncResult()
    else:
        # 异步执行
        return create_thumbnail_task.delay(image_id, image_path)
