"""存储池的数据模型。

三张表：来源（桶配置）、迁移任务、逐对象进度。

字段全部用短 ``db_field``：这些文档会随项目量增长，Mongo 的字段名重复存一遍
没有意义，与仓内既有模型保持一致。
"""

import datetime
import logging

from mongoengine import (
    BooleanField,
    DateTimeField,
    Document,
    EmbeddedDocument,
    EmbeddedDocumentField,
    IntField,
    LongField,
    NotUniqueError,
    ObjectIdField,
    ReferenceField,
    StringField,
)

from app.models.project import Project
from .constants import (
    TransferDirection,
    TransferObjectStatus,
    TransferStatus,
)

logger = logging.getLogger(__name__)


def normalize_sub_path(value) -> str:
    """子路径归一：保证「无前导斜杠、有尾斜杠（或为空）」。

    用户在界面上可能填 ``storage``、``/storage/``、``storage//``，
    统一成 ``storage/``；填 ``/`` 或空视为「不要前缀」。
    """
    raw = (value or "").strip().lstrip("/").strip("/")
    return f"{raw}/" if raw else ""


class StoragePoolSource(Document):
    """一个可迁移进去的存储来源（一个桶 + 一个子路径）。"""

    name = StringField(db_field="n", required=True)
    enabled = BooleanField(db_field="e", default=True)
    endpoint = StringField(db_field="ep", required=True)
    bucket = StringField(db_field="b", required=True)
    # ★ 子路径 == 该来源的 key 总前缀。默认 storage/ 与站点默认存储一致，
    #   这样迁移时对象 key 一字不改，只换桶。
    sub_path = StringField(db_field="sp", default="storage/")
    region = StringField(db_field="r", default="auto")
    addressing_style = StringField(db_field="as", default="path")
    verify_ssl = BooleanField(db_field="vs", default=True)
    access_key_id = StringField(db_field="ak", required=True)
    # ⚠️ 明文入库（站长决定，见规划「不做的事」）。拿到 Mongo 读权限即握有
    #   池内桶的读写凭据；对外 API 永不返回本字段。
    secret_access_key = StringField(db_field="sk", required=True)
    # 可选：读 URL 用的 CDN 域名；空则用 S3 预签名 URL。
    public_domain = StringField(db_field="pd", default="")
    remark = StringField(db_field="rm", default="")
    # 超过多少天没动过的项目才允许迁入
    # ⚠️ 不要用 db_field="id" —— mongoengine 会把 "id" 映射到 _id，
    # 这个字段会变成主键（实测踩过：新建来源的 _id 变成 90）。
    inactive_days = IntField(db_field="iad", default=90)
    # 单项目单轮最多搬多少个对象（防止一次任务跑太久）
    max_objects_per_run = IntField(db_field="mo", default=2000)
    # 是否已有项目迁入。为 True 时禁止改 sub_path —— 改了等于让已迁入的
    # 项目全部失联。
    has_data = BooleanField(db_field="hd", default=False)

    create_time = DateTimeField(db_field="ct", default=datetime.datetime.utcnow)
    update_time = DateTimeField(db_field="ut", default=datetime.datetime.utcnow)

    meta = {
        "collection": "storage_pool_source",
        "indexes": ["enabled"],
    }

    def normalized_sub_path(self) -> str:
        """子路径归一（见模块级 :func:`normalize_sub_path`）。"""
        return normalize_sub_path(self.sub_path)

    def masked_access_key_id(self) -> str:
        """凭据脱敏：只回前 4 后 4。"""
        key = self.access_key_id or ""
        if len(key) <= 8:
            return "*" * len(key)
        return f"{key[:4]}...{key[-4:]}"

    def to_api(self):
        """对外表示。**永不返回 secret_access_key。**"""
        return {
            "id": str(self.id),
            "name": self.name,
            "enabled": self.enabled,
            "endpoint": self.endpoint,
            "bucket": self.bucket,
            "sub_path": self.normalized_sub_path(),
            "region": self.region,
            "addressing_style": self.addressing_style,
            "verify_ssl": self.verify_ssl,
            "access_key_id": self.masked_access_key_id(),
            "public_domain": self.public_domain,
            "remark": self.remark,
            "inactive_days": self.inactive_days,
            "max_objects_per_run": self.max_objects_per_run,
            "has_data": self.has_data,
            "create_time": self.create_time.isoformat() if self.create_time else None,
            "update_time": self.update_time.isoformat() if self.update_time else None,
        }


class TransferBreakdown(EmbeddedDocument):
    """这一趟实际搬了多少字节，按类别分开。

    只统计**被搬运的**三类：原图（``files/`` 下的 IMAGE，记 ``origin_bytes``）、
    重采样图（``files/resample-*``，记 ``origin_bytes``）与导出产物
    （``outputs/``，记 ``output_bytes``）。
    文本、小封面、校对标记图、头像不搬，所以不出现在这里。

    用途是给后台看「这个项目到底占在哪」，以及和项目账做交叉对账；
    **不用它覆盖 ``Project.file_size``** —— 后者还包含派生图等，
    直接覆盖会把那部分抹掉。
    """

    origin_bytes = LongField(db_field="ob", default=0)
    output_bytes = LongField(db_field="ov", default=0)
    object_count = IntField(db_field="oc", default=0)

    def to_api(self):
        return {
            "origin_bytes": self.origin_bytes,
            "output_bytes": self.output_bytes,
            "object_count": self.object_count,
        }


class StorageTransfer(Document):
    """一次迁移任务（一个项目一趟搬家）。"""

    source = ReferenceField(StoragePoolSource, db_field="s", required=True)
    #: 直转（MOVE）的目标来源；IN 时与 source 相同语义（目标池源，可为空
    #: 兼容旧数据）、OUT 时为空。只在 MOVE 时必填。
    target_source = ReferenceField(
        StoragePoolSource, db_field="ts", required=False, null=True
    )
    project = ReferenceField(Project, db_field="p", required=True)
    status = IntField(db_field="st", default=TransferStatus.PENDING)
    direction = IntField(db_field="dir", default=TransferDirection.IN)
    # 开始时的 project.edit_time，用于检测「搬运期间有人动了文件」
    snapshot_edit_time = DateTimeField(db_field="set")
    total_objects = IntField(db_field="to", default=0)
    copied_objects = IntField(db_field="co", default=0)
    total_bytes = LongField(db_field="tb", default=0)
    copied_bytes = LongField(db_field="cb", default=0)
    breakdown = EmbeddedDocumentField(TransferBreakdown, db_field="bd", default=None)
    #: 连续「一轮搬了 0 个但还有剩余」的次数。到 ``MAX_STALLS`` 就停，
    #: 否则自动续跑会无休止地空转，白烧 worker。
    stall_count = IntField(db_field="npc", default=0)
    start_time = DateTimeField(db_field="stt")
    finish_time = DateTimeField(db_field="ftt")
    last_error = StringField(db_field="le", default="")
    create_time = DateTimeField(db_field="ct", default=datetime.datetime.utcnow)

    meta = {
        "collection": "storage_transfer",
        # 索引写**字段名**而不是 db_field；复合索引是字段名元组，升序。
        "indexes": [
            ("project", "source"),
            "status",
        ],
    }

    @property
    def progress_percent(self) -> int:
        if not self.total_objects:
            return 0
        return min(100, int(self.copied_objects * 100 / self.total_objects))

    def to_api(self):
        project = self.project
        source = self.source
        # MOVE 时 source=源、target_source=目标；OUT 时目标是默认存储（None）；
        # IN 时目标就是 source，本字段与 source 相同。
        target = (
            self.target_source if self.direction == TransferDirection.MOVE else None
        )
        if self.direction == TransferDirection.IN:
            target = source
        return {
            "id": str(self.id),
            "project": {
                "id": str(project.id) if project else None,
                "name": project.name if project else "",
            },
            "source": {
                "id": str(source.id) if source else None,
                "name": source.name if source else "",
            },
            "target_source": {
                "id": str(target.id) if target else None,
                "name": target.name if target else "",
            }
            if target is not None
            else None,
            "status": self.status,
            "status_name": TransferStatus.get_detail_by_value(self.status, "name"),
            "direction": self.direction,
            "direction_name": TransferDirection.get_detail_by_value(
                self.direction, "name"
            ),
            "total_objects": self.total_objects,
            "copied_objects": self.copied_objects,
            "total_bytes": self.total_bytes,
            "copied_bytes": self.copied_bytes,
            "breakdown": self.breakdown.to_api()
            if self.breakdown
            else {"origin_bytes": 0, "output_bytes": 0, "object_count": 0},
            "progress_percent": self.progress_percent,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "finish_time": self.finish_time.isoformat() if self.finish_time else None,
            "create_time": self.create_time.isoformat() if self.create_time else None,
            "last_error": self.last_error,
        }


class StorageTransferObject(Document):
    """单个对象的搬运进度。断点续传就靠这张表。"""

    transfer = ReferenceField(StorageTransfer, db_field="t", required=True)
    src_key = StringField(db_field="sk", required=True)
    dst_key = StringField(db_field="dk", required=True)
    #: 对象种类（``StorageObjectKind``）：原图 / 导出产物 / 文本原文。
    #: 文本原文的 key 也在 ``files/`` 下，仅靠前缀分不出，
    #: 所以建任务时写死，记账（breakdown）按它归类。
    #: 旧任务没有本字段，读到空时按前缀回退判断。
    kind = StringField(db_field="kd", default="")
    size = LongField(db_field="sz", default=0)
    etag = StringField(db_field="et", default="")
    status = IntField(db_field="st", default=TransferObjectStatus.PENDING)
    attempts = IntField(db_field="at", default=0)
    last_error = StringField(db_field="le", default="")

    meta = {
        "collection": "storage_transfer_object",
        "indexes": [
            ("transfer", "status"),
            "src_key",
        ],
    }


class StoragePoolSetting(Document):
    """存储池的运行开关（单例）。

    为什么是数据库文档而不是环境变量：开关要能**随时热切换**。
    环境变量改完必须重启，beat 还是独立容器，等于没有热切换。

    为什么闸门在任务体而不是动态改 ``celery.conf.beat_schedule``：
    beat 是另一个进程，运行时改那个 dict 只改当前进程的内存，beat 根本看不到。
    所以定时条目常驻、每天照响，任务醒来的第一件事是读这里——关着就直接返回。
    """

    type = StringField(primary_key=True, db_field="_id", default="default")
    #: 总闸：是否允许自动巡查挑选新项目。**默认关闭** —— 自动迁移会真的搬数据、
    #: 并在校验通过后删掉源端对象，第一次应当由人手动触发并盯着。
    scan_enabled = BooleanField(db_field="se", default=False)
    #: 第二道闸：是否允许 worker 领取新批次。关掉后已排队任务停在 PENDING，
    #: 恢复后从断点续跑（用于"发现桶配错了，先刹车"）。
    run_enabled = BooleanField(db_field="re", default=True)
    # == 单轮工作量上限 ==
    #: 字节预算。语义是「这一轮允许开工哪几个项目」，不是「这一轮允许搬多少
    #: 字节」—— 预算不足只影响排队，永不切分单个项目。
    run_max_bytes = LongField(db_field="rmb", default=20 * 1024**3)
    #: 单次任务调用的对象数安全阀（防一次调用跑几十分钟被 worker 超时杀掉）。
    run_max_objects = IntField(db_field="rmo", default=2000)
    #: 单轮最多启动几个项目的迁移。
    run_max_projects = IntField(db_field="rmp", default=20)
    updated_by = ObjectIdField(db_field="ub", null=True)
    update_time = DateTimeField(db_field="ut", default=datetime.datetime.utcnow)
    #: 只读回填，给后台卡片看「上次到底跑没跑」。
    #: 只在**真的执行了巡查**时更新；被开关跳过时不覆盖，避免把上一次的
    #: 真实结果冲掉。
    last_scan_time = DateTimeField(db_field="lst", null=True)
    last_scan_result = StringField(db_field="lsr", default="")
    last_scan_queued = IntField(db_field="lsq", default=0)

    meta = {"collection": "storage_pool_setting"}

    @classmethod
    def get(cls) -> "StoragePoolSetting":
        """取单例；不存在就按默认值建一个（首访不会 500）。"""
        setting = cls.objects(type="default").first()
        if setting is not None:
            return setting
        try:
            return cls().save()
        except NotUniqueError:
            # 并发首次访问：另一个进程刚建好，用它的。
            return cls.objects(type="default").first()

    def to_api(self):
        return {
            "scan_enabled": self.scan_enabled,
            "run_enabled": self.run_enabled,
            "run_max_bytes": self.run_max_bytes,
            "run_max_objects": self.run_max_objects,
            "run_max_projects": self.run_max_projects,
            "updated_by": str(self.updated_by) if self.updated_by else None,
            "update_time": self.update_time.isoformat() if self.update_time else None,
            "last_scan_time": self.last_scan_time.isoformat()
            if self.last_scan_time
            else None,
            "last_scan_result": self.last_scan_result,
            "last_scan_queued": self.last_scan_queued,
        }


class StoragePoolLock(Document):
    """项目级排他锁（租约式）。

    为什么不用 ``Project.file_order_lock``：那是核心字段，语义是「文件排序」，
    借来当迁移锁会让两件不相干的事互相阻塞；而且核心不该为一个可选模块背字段。

    实现用「以 project_id 为主键插入 + 唯一索引冲突即抢占失败」，天然原子；
    ``expire_at`` 保证 worker 被 kill 后锁不会永久卡住。
    """

    project_id = ObjectIdField(primary_key=True, db_field="_id")
    transfer_id = ObjectIdField(db_field="t")
    owner = StringField(db_field="o", default="")
    expire_at = DateTimeField(db_field="ea")

    meta = {"collection": "storage_pool_lock"}

    @classmethod
    def acquire(cls, project_id, transfer_id=None, owner="", ttl_seconds=1800):
        now = datetime.datetime.utcnow()
        expire_at = now + datetime.timedelta(seconds=ttl_seconds)
        # 先尝试接管过期锁
        taken = cls.objects(pk=project_id, expire_at__lt=now).update_one(
            set__transfer_id=transfer_id, set__owner=owner, set__expire_at=expire_at
        )
        if taken:
            return True
        try:
            cls(
                project_id=project_id,
                transfer_id=transfer_id,
                owner=owner,
                expire_at=expire_at,
            ).save(force_insert=True)
            return True
        except NotUniqueError:
            return False

    @classmethod
    def release(cls, project_id, owner=None, transfer_id=None):
        """释放锁。传了 ``owner`` / ``transfer_id`` 就只删自己那份。

        早期版本无条件 ``delete()``：租约过期、被另一个 worker 接管之后，
        迟到的 ``finally`` 会把别人的锁删掉，两个 worker 于是同时进临界区。
        """
        query = cls.objects(pk=project_id)
        if owner is not None:
            query = query.filter(owner=owner)
        if transfer_id is not None:
            query = query.filter(transfer_id=transfer_id)
        try:
            query.delete()
        except Exception:  # pragma: no cover - 释放失败不该盖住真正的错误
            logger.exception("释放存储池锁失败：project=%s", project_id)


class StoragePoolStatsMixin:
    """统计口径集中在这里，避免 API 层散落一堆 count 查询。"""

    @staticmethod
    def overview():
        from app.models.project import Project

        sources = StoragePoolSource.objects.count()
        enabled_sources = StoragePoolSource.objects(enabled=True).count()
        migrated_projects = Project.objects(storage_source_id__ne=None).count()
        active = StorageTransfer.objects(status__in=TransferStatus.ACTIVE).count()
        return {
            "sources": sources,
            "enabled_sources": enabled_sources,
            "migrated_projects": migrated_projects,
            "active_transfers": active,
        }


#: 供外部只过滤需要的字段时使用。
__all__ = [
    "StoragePoolSource",
    "StorageTransfer",
    "StorageTransferObject",
    "StoragePoolLock",
    "StoragePoolSetting",
    "StoragePoolStatsMixin",
]
