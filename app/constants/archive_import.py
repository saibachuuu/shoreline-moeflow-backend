from flask_babel import lazy_gettext

from app.constants.base import IntType


class ArchiveImportStatus(IntType):
    """画廊归档导入任务状态"""

    QUEUED = 0  # 排队中
    RESOLVING = 1  # 解析归档链接
    DOWNLOADING = 2  # 下载归档
    VALIDATING = 3  # 校验归档
    IMPORTING = 4  # 导入图片
    SUCCEEDED = 5  # 已完成
    FAILED = 6  # 失败

    details = {
        "QUEUED": {"name": lazy_gettext("排队中")},
        "RESOLVING": {"name": lazy_gettext("解析归档链接")},
        "DOWNLOADING": {"name": lazy_gettext("下载归档中")},
        "VALIDATING": {"name": lazy_gettext("校验归档中")},
        "IMPORTING": {"name": lazy_gettext("导入图片中")},
        "SUCCEEDED": {"name": lazy_gettext("已完成")},
        "FAILED": {"name": lazy_gettext("导入失败")},
    }

    RUNNING = (QUEUED, RESOLVING, DOWNLOADING, VALIDATING, IMPORTING)