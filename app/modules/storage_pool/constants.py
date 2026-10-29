"""存储池的状态与方向常量。

状态顺序即安全性：**切指针在删源之前**，任何一步失败都停在上一步，
不会出现「两边都读不到」的窗口。
"""

from app.constants.base import IntType, StrType


class TransferStatus(IntType):
    """迁移任务状态。"""

    PENDING = 0  # 已排定，等 worker 取
    COPYING = 1  # 正在逐对象搬运
    VERIFIED = 2  # 已逐个校验（size / etag）
    SWITCHED = 3  # 指针已切换（★ 从这里开始读的是新位置）
    DONE = 4  # 源端已清理
    FAILED = 5  # 出错，可重试
    ABORTED = 6  # 并发写入等原因主动中止，可重试

    details = {
        "PENDING": {"name": "待执行"},
        "COPYING": {"name": "搬运中"},
        "VERIFIED": {"name": "已校验"},
        "SWITCHED": {"name": "已切换"},
        "DONE": {"name": "已完成"},
        "FAILED": {"name": "失败"},
        "ABORTED": {"name": "已中止"},
    }

    #: 还占着「这个项目正在迁移」这个坑的状态。
    ACTIVE = (PENDING, COPYING, VERIFIED, SWITCHED)

    #: 可以重试的状态。
    RETRYABLE = (FAILED, ABORTED)


class TransferDirection(IntType):
    """迁移方向。"""

    IN = 0  # 默认存储 → 池来源
    OUT = 1  # 池来源 → 默认存储（回迁）
    MOVE = 2  # 池来源 → 另一池来源（直转，不经过默认存储）

    details = {
        "IN": {"name": "迁入存储池"},
        "OUT": {"name": "回迁到默认存储"},
        "MOVE": {"name": "转到其他来源"},
    }


class TransferObjectStatus(IntType):
    """单个对象的搬运状态（断点续传靠它）。"""

    PENDING = 0
    COPIED = 1
    VERIFIED = 2
    SOURCE_DELETED = 3
    FAILED = 4
    SKIPPED = 5  # 源对象不存在（如派生图还没生成），不搬、不删源、不算失败

    details = {
        "PENDING": {"name": "待搬运"},
        "COPIED": {"name": "已复制"},
        "VERIFIED": {"name": "已校验"},
        "SOURCE_DELETED": {"name": "源端已删"},
        "FAILED": {"name": "失败"},
        "SKIPPED": {"name": "已跳过"},
    }


class StorageObjectKind(StrType):
    """参与迁移的对象种类。

    只搬三类：原图（IMAGE）、重采样图（resample）与导出产物。
    文本原文、小封面、校对标记图与头像永远留在默认存储。
    """

    ORIGINAL = "original"
    OUTPUT = "output"
    RESAMPLE = "resample"
    TEXT = "text"  # 保留常量以兼容旧任务；新任务不再产生

    details = {
        "ORIGINAL": {"name": "原图"},
        "OUTPUT": {"name": "导出产物"},
        "RESAMPLE": {"name": "重采样图"},
        "TEXT": {"name": "文本原文"},
    }


#: 连通性测试写入的探针对象名（测完即删）。
PROBE_OBJECT_NAME = ".moeflow-storage-probe"

#: 连续多少轮「一个对象都没搬成、但还有剩余」就判死。
#: 没有这道闸，自动续跑会在桶不可用时无休止地空转。
MAX_STALLS = 3

#: 自动续跑的延迟（秒）。给上一轮的状态写入留出可见性时间。
CONTINUE_COUNTDOWN = 30

#: S3 ``delete_objects`` 单次 key 上限（协议规定 1000）。删源必须按这个分批，
#: 否则超过一千个对象的项目会整批失败，源端对象永远删不掉。
DELETE_BATCH_SIZE = 1000
