from flask_babel import lazy_gettext

from app.constants.base import IntType


# 导出 translations.txt 时人员名单的植入页序号默认值（1 = 第一页）。
# 读取优先级：项目设置 > 团队设置 > 此默认值。
STAFF_LIST_DEFAULT_PAGE = 1


class ProjectStatus(IntType):
    """项目状态"""

    WORKING = 0  # 正常
    CLEARED = 1  # 内容已清空
    # Value 5 avoids colliding with the historical cleared value 1.
    COMPLETED = 5

    details = {
        "WORKING": {"name": lazy_gettext("正常")},
        "CLEARED": {"name": lazy_gettext("已清空")},
        "COMPLETED": {"name": lazy_gettext("已完成")},
    }


class ImportFromLabelplusStatus(IntType):
    PENDING = 0  # 排队中
    RUNNING = 1  # 进行中
    SUCCEEDED = 2  # 成功
    ERROR = 3  # 错误

    details = {
        "PENDING": {"name": lazy_gettext("排队中")},
        "RUNNING": {"name": lazy_gettext("进行中")},
        "SUCCEEDED": {"name": lazy_gettext("成功")},
        "ERROR": {"name": lazy_gettext("错误")},
    }


class ImportFromLabelplusErrorType(IntType):
    UNKNOWN = 0  # 未知
    NO_TARGET = 1  # 运行时，没有的翻译目标
    NO_CREATOR = 2  # 项目没有创建人
    PARSE_FAILED = 3  # 解析失败

    details = {
        "UNKNOWN": {
            "name": lazy_gettext(
                "从 Labelplus 文本导入中断，请重试，如仍出现同样错误，请联系开发团队"
            )
        },
        "NO_TARGET": {
            "name": lazy_gettext("从 Labelplus 文本导入时，没有有效的翻译目标语言")
        },
        "NO_CREATOR": {"name": lazy_gettext("从 Labelplus 文本导入时，项目没有创建人")},
        "PARSE_FAILED": {
            "name": lazy_gettext("Labelplus 文本解析失败，请联系开发团队")
        },
    }
