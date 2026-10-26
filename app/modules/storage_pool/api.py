"""存储池的管理接口。

全部 ``@admin_required``：这是站长级操作，能改「项目文件存在哪」。

安全约定：
* 响应**永不包含** ``secret_access_key``，``access_key_id`` 只回前后各 4 位；
* 日志不打凭据；
* ``sub_path`` 在该来源已有项目迁入后禁止修改（前后端双拦）。
"""

import datetime
import logging

from flask import Blueprint
from flask_babel import gettext

from app.core.api import APIError
from app.core.responses import MoePagination
from app.core.views import MoeAPIView
from app.decorators.auth import admin_required
from app.models.project import Project

from .constants import TransferDirection, TransferObjectStatus, TransferStatus
from .models import (
    StoragePoolSetting,
    StoragePoolSource,
    StoragePoolStatsMixin,
    StorageTransfer,
    StorageTransferObject,
    normalize_sub_path,
)
from .tasks import storage_pool_scan
from . import transfer as transfer_service
from .validators import (
    MigrateSchema,
    SourceCreateSchema,
    SourceUpdateSchema,
    StoragePoolSettingSchema,
)

logger = logging.getLogger(__name__)


class SourceNotFoundError(APIError):
    status_code = 404
    code = 1
    message = gettext("存储来源不存在")


class SourceInUseError(APIError):
    status_code = 409
    code = 2
    message = gettext("该来源已有项目迁入，请先回迁再删除")


class SubPathLockedError(APIError):
    status_code = 409
    code = 3
    message = gettext(
        "该来源已有项目迁入，修改子路径会导致这些项目读不到文件。"
        "请先把项目回迁到默认存储，再修改"
    )


class SourceUnreachableError(APIError):
    status_code = 400
    code = 4
    message = gettext("存储来源连通性测试失败")


class TransferNotAllowedError(APIError):
    status_code = 409
    code = 5
    message = gettext("当前状态不允许该操作")


def _get_source(source_id):
    source = StoragePoolSource.objects(id=source_id).first()
    if source is None:
        raise SourceNotFoundError()
    return source


class SourceListAPI(MoeAPIView):
    @admin_required
    def get(self):
        """
        @api {get} /v1/admin/storage-pool/sources 存储来源列表
        @apiVersion 1.0.0
        @apiName getStoragePoolSourceListAPI
        @apiGroup StoragePool
        @apiUse APIHeader
        @apiUse TokenHeader
        """
        p = MoePagination()
        sources = StoragePoolSource.objects().order_by("-create_time")
        return p.set_objects(sources)

    @admin_required
    def post(self):
        """
        @api {post} /v1/admin/storage-pool/sources 新增存储来源
        @apiVersion 1.0.0
        @apiName postStoragePoolSourceAPI
        @apiGroup StoragePool
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiParam {String} name 名称
        @apiParam {String} endpoint 服务端点
        @apiParam {String} bucket 桶名
        @apiParam {String} [sub_path=storage/] 子路径（即该来源的 key 总前缀）
        @apiParam {String} access_key_id 访问密钥 ID
        @apiParam {String} secret_access_key 访问密钥
        """
        data = self.get_json(SourceCreateSchema())
        source = StoragePoolSource(**data)
        # 先测连通性再落库：存一个连不上的来源，只会在日后迁移时炸。
        try:
            transfer_service.test_connection(source)
        except Exception as error:
            logger.warning("存储来源连通性测试失败：%s", error)
            raise SourceUnreachableError(
                f"{gettext('存储来源连通性测试失败')}：{error}"
            )
        source.save()
        return source.to_api()


class SourceAPI(MoeAPIView):
    @admin_required
    def put(self, source_id):
        """
        @api {put} /v1/admin/storage-pool/sources/:id 更新存储来源
        @apiVersion 1.0.0
        @apiName putStoragePoolSourceAPI
        @apiGroup StoragePool
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiDescription 凭据字段留空表示不修改。`has_data` 为真时禁止改 `sub_path`。
        """
        source = _get_source(source_id)
        data = self.get_json(SourceUpdateSchema())

        if "sub_path" in data:
            new_sub_path = normalize_sub_path(data["sub_path"])
            if new_sub_path != source.normalized_sub_path() and source.has_data:
                raise SubPathLockedError()

        for key, value in data.items():
            if key in ("access_key_id", "secret_access_key") and not value:
                continue
            setattr(source, key, value)
        source.update_time = datetime.datetime.utcnow()
        source.save()
        return source.to_api()

    @admin_required
    def delete(self, source_id):
        """
        @api {delete} /v1/admin/storage-pool/sources/:id 删除存储来源
        @apiVersion 1.0.0
        @apiName deleteStoragePoolSourceAPI
        @apiGroup StoragePool
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiDescription 仍有项目指向该来源时拒绝删除（否则这些项目立刻读不到文件）。
        """
        source = _get_source(source_id)
        if Project.objects(storage_source_id=source.id).first() is not None:
            raise SourceInUseError()
        source.delete()
        return {"message": gettext("已删除")}


class SourceTestAPI(MoeAPIView):
    @admin_required
    def post(self, source_id):
        """
        @api {post} /v1/admin/storage-pool/sources/:id/test 连通性测试
        @apiVersion 1.0.0
        @apiName postStoragePoolSourceTestAPI
        @apiGroup StoragePool
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiDescription head_bucket + 写读删探针，探针对象用后即删。
        """
        source = _get_source(source_id)
        try:
            transfer_service.test_connection(source)
        except Exception as error:
            logger.warning("存储来源连通性测试失败：%s", error)
            return {"ok": False, "message": str(error)}
        return {"ok": True, "message": gettext("连通性正常")}


class TransferListAPI(MoeAPIView):
    @admin_required
    def get(self):
        """
        @api {get} /v1/admin/storage-pool/transfers 迁移记录
        @apiVersion 1.0.0
        @apiName getStoragePoolTransferListAPI
        @apiGroup StoragePool
        @apiUse APIHeader
        @apiUse TokenHeader
        """
        p = MoePagination()
        transfers = StorageTransfer.objects().order_by("-create_time")
        return p.set_objects(transfers)


class ProjectMigrateAPI(MoeAPIView):
    @admin_required
    def post(self, project_id):
        """
        @api {post} /v1/admin/storage-pool/projects/:id/migrate 迁入存储池
        @apiVersion 1.0.0
        @apiName postStoragePoolMigrateAPI
        @apiGroup StoragePool
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiParam {String} source_id 目标存储来源
        """
        data = self.get_json(MigrateSchema())
        project = Project.objects(id=project_id).first()
        if project is None:
            raise TransferNotAllowedError(gettext("项目不存在"))
        if project.storage_source_id:
            raise TransferNotAllowedError(gettext("该项目已在存储池中"))
        source = _get_source(data["source_id"])
        transfer = transfer_service.prepare(project, source, TransferDirection.IN)
        _dispatch(transfer)
        return transfer.to_api()


class ProjectRestoreAPI(MoeAPIView):
    @admin_required
    def post(self, project_id):
        """
        @api {post} /v1/admin/storage-pool/projects/:id/restore 回迁到默认存储
        @apiVersion 1.0.0
        @apiName postStoragePoolRestoreAPI
        @apiGroup StoragePool
        @apiUse APIHeader
        @apiUse TokenHeader
        """
        project = Project.objects(id=project_id).first()
        if project is None:
            raise TransferNotAllowedError(gettext("项目不存在"))
        if not project.storage_source_id:
            raise TransferNotAllowedError(gettext("该项目不在存储池中"))
        source = StoragePoolSource.objects(id=project.storage_source_id).first()
        if source is None:
            raise SourceNotFoundError()
        transfer = transfer_service.prepare(project, source, TransferDirection.OUT)
        _dispatch(transfer)
        return transfer.to_api()


class TransferRetryAPI(MoeAPIView):
    @admin_required
    def post(self, transfer_id):
        """
        @api {post} /v1/admin/storage-pool/transfers/:id/retry 重试迁移
        @apiVersion 1.0.0
        @apiName postStoragePoolTransferRetryAPI
        @apiGroup StoragePool
        @apiUse APIHeader
        @apiUse TokenHeader
        """
        transfer = StorageTransfer.objects(id=transfer_id).first()
        if transfer is None:
            raise TransferNotAllowedError(gettext("迁移任务不存在"))
        if transfer.status not in TransferStatus.RETRYABLE:
            raise TransferNotAllowedError()
        # 失败对象回到待搬运，其余状态保留（已校验的不必重搬）。
        StorageTransferObject.objects(
            transfer=transfer, status=TransferObjectStatus.FAILED
        ).update(set__status=TransferObjectStatus.PENDING)
        transfer.update(
            set__status=TransferStatus.PENDING,
            set__last_error="",
            set__finish_time=None,
        )
        _dispatch(transfer)
        return transfer.to_api()


class StatsAPI(MoeAPIView):
    @admin_required
    def get(self):
        """
        @api {get} /v1/admin/storage-pool/stats 统计
        @apiVersion 1.0.0
        @apiName getStoragePoolStatsAPI
        @apiGroup StoragePool
        @apiUse APIHeader
        @apiUse TokenHeader
        """
        return StoragePoolStatsMixin.overview()


class UsageAPI(MoeAPIView):
    @admin_required
    def get(self):
        """
        @api {get} /v1/admin/storage-pool/usage 全站用量
        @apiVersion 1.0.0
        @apiName getStoragePoolUsageAPI
        @apiGroup StoragePool
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiDescription 全部基于数据库里的项目占用求和，**不列举桶**：一次 LIST
        在大桶上要几十秒且没有分页意义。因此这里的数字不含「未归属对象」
        （桶里没有项目指向的孤儿与历史残留），需要和桶账单对照。
        """

        aggregate = Project.objects.aggregate(
            [
                {
                    "$group": {
                        "_id": None,
                        "projects": {"$sum": 1},
                        "file_size_kb": {"$sum": "$fs"},
                        "derived_bytes": {"$sum": "$dvb"},
                        "output_bytes": {"$sum": "$ob"},
                    }
                }
            ]
        )
        overall = next(iter(aggregate), None) or {}

        total_bytes = int(overall.get("file_size_kb") or 0) * 1024
        # 待办 = 还没迁出默认存储、又不是空项目的那些。
        pending_row = (
            next(
                iter(
                    Project.objects(storage_source_id=None).aggregate(
                        [
                            {
                                "$group": {
                                    "_id": None,
                                    "projects": {
                                        "$sum": {"$cond": [{"$gt": ["$fs", 0]}, 1, 0]}
                                    },
                                    "file_size_kb": {
                                        "$sum": {
                                            "$cond": [{"$gt": ["$fs", 0]}, "$fs", 0]
                                        }
                                    },
                                }
                            }
                        ]
                    )
                ),
                None,
            )
            or {}
        )
        pending = {
            "projects": int(pending_row.get("projects") or 0),
            "bytes": int(pending_row.get("file_size_kb") or 0) * 1024,
        }

        by_source = []
        source_names = {
            str(source.id): source.name for source in StoragePoolSource.objects()
        }
        rows = Project.objects.aggregate(
            [
                {
                    "$group": {
                        "_id": "$csid",
                        "projects": {"$sum": 1},
                        "file_size_kb": {"$sum": "$fs"},
                    }
                }
            ]
        )
        for row in rows:
            source_id = row.get("_id")
            key = str(source_id) if source_id else None
            by_source.append(
                {
                    "source_id": key,
                    "name": source_names.get(key, ""),
                    "projects": int(row.get("projects") or 0),
                    "bytes": int(row.get("file_size_kb") or 0) * 1024,
                }
            )
        by_source.sort(key=lambda item: item["bytes"], reverse=True)

        top = [
            {
                "id": str(project.id),
                "name": project.name,
                "bytes": int(project.file_size or 0) * 1024,
            }
            for project in Project.objects(file_size__gt=0)
            .order_by("-file_size")
            .limit(10)
            .only("name", "file_size")
        ]

        return {
            "total": {
                "projects": int(overall.get("projects") or 0),
                "bytes": total_bytes,
                "derived_bytes": int(overall.get("derived_bytes") or 0),
                "output_bytes": int(overall.get("output_bytes") or 0),
            },
            "pending": pending,
            "migrated_projects": Project.objects(storage_source_id__ne=None).count(),
            "by_source": by_source,
            "top_projects": top,
        }


class SettingsAPI(MoeAPIView):
    @admin_required
    def get(self):
        """
        @api {get} /v1/admin/storage-pool/settings 读取运行开关
        @apiVersion 1.0.0
        @apiName getStoragePoolSettingsAPI
        @apiGroup StoragePool
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiDescription `scan_enabled` 是自动巡查总闸（默认关闭）；`run_enabled`
        是紧急停止（关掉后已排队任务停在待执行）。`last_scan_*` 只在真的执行过
        巡查时更新，被开关跳过时不覆盖。
        """
        setting = StoragePoolSetting.get()
        data = setting.to_api()
        data["next_run_at"] = _next_scan_time()
        return data

    @admin_required
    def put(self):
        """
        @api {put} /v1/admin/storage-pool/settings 修改运行开关
        @apiVersion 1.0.0
        @apiName putStoragePoolSettingsAPI
        @apiGroup StoragePool
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiParam {Boolean} [scan_enabled] 是否允许自动巡查挑选新项目
        @apiParam {Boolean} [run_enabled] 是否允许 worker 领取新批次
        """
        data = self.get_json(StoragePoolSettingSchema())
        setting = StoragePoolSetting.get()

        changes = {}
        for field in (
            "scan_enabled",
            "run_enabled",
            "run_max_bytes",
            "run_max_objects",
            "run_max_projects",
        ):
            if field in data and getattr(setting, field) != data[field]:
                changes[field] = data[field]

        if changes:
            setting.update(
                set__update_time=datetime.datetime.utcnow(),
                set__updated_by=self.current_user.id,
                **{f"set__{key}": value for key, value in changes.items()},
            )
            setting.reload()
            # 审计：开关变更必须能在日志里查到是谁、什么时候、改成了什么。
            logger.info(
                "存储池开关变更：%s by user<%s>",
                ", ".join(f"{key}={value}" for key, value in changes.items()),
                self.current_user.id,
            )

        result = setting.to_api()
        result["next_run_at"] = _next_scan_time()
        return result


class ScanAPI(MoeAPIView):
    @admin_required
    def post(self):
        """
        @api {post} /v1/admin/storage-pool/scan 手动巡查一次
        @apiVersion 1.0.0
        @apiName postStoragePoolScanAPI
        @apiGroup StoragePool
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiDescription **不受 `scan_enabled` 限制**：这就是「关掉自动、偶尔手动」
        的用法。异步入队，不同步执行。
        """
        if not StoragePoolSource.objects(enabled=True).first():
            return {"message": gettext("没有启用的存储来源，无需巡查")}

        try:
            storage_pool_scan.delay(force=True)
        except Exception:
            logger.exception("巡查任务入队失败，改为同步执行")
            storage_pool_scan(force=True)
        return {"message": gettext("巡查任务已排队")}


def _next_scan_time():
    """下一次 03:00（按 celery 配置的时区）。算不出来返回 ``None``。

    只是个展示值：真实的触发由 beat 负责，这里不参与调度。
    """
    try:
        from zoneinfo import ZoneInfo

        from app import celery

        tz_name = celery.conf.timezone
        tz = ZoneInfo(str(tz_name)) if tz_name else datetime.timezone.utc
        now = datetime.datetime.now(tz)
        target = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        return target.isoformat()
    except Exception:  # pragma: no cover - 时区数据缺失时不影响接口
        logger.warning("计算下次巡查时间失败", exc_info=True)
        return None


def _dispatch(transfer):
    """入队执行。broker 不可用时退化为同步执行，至少别让按钮点了没反应。"""
    from .tasks import storage_pool_run

    try:
        storage_pool_run.delay(str(transfer.id))
    except Exception:
        logger.exception("迁移任务入队失败，改为同步执行：%s", transfer.id)
        storage_pool_run(str(transfer.id))


blueprint = Blueprint("storage_pool", __name__, url_prefix="/v1/admin/storage-pool")
blueprint.add_url_rule(
    "/sources",
    methods=["GET", "POST", "OPTIONS"],
    view_func=SourceListAPI.as_view("source_list"),
)
blueprint.add_url_rule(
    "/sources/<source_id>",
    methods=["PUT", "DELETE", "OPTIONS"],
    view_func=SourceAPI.as_view("source"),
)
blueprint.add_url_rule(
    "/sources/<source_id>/test",
    methods=["POST", "OPTIONS"],
    view_func=SourceTestAPI.as_view("source_test"),
)
blueprint.add_url_rule(
    "/transfers",
    methods=["GET", "OPTIONS"],
    view_func=TransferListAPI.as_view("transfer_list"),
)
blueprint.add_url_rule(
    "/transfers/<transfer_id>/retry",
    methods=["POST", "OPTIONS"],
    view_func=TransferRetryAPI.as_view("transfer_retry"),
)
blueprint.add_url_rule(
    "/projects/<project_id>/migrate",
    methods=["POST", "OPTIONS"],
    view_func=ProjectMigrateAPI.as_view("project_migrate"),
)
blueprint.add_url_rule(
    "/projects/<project_id>/restore",
    methods=["POST", "OPTIONS"],
    view_func=ProjectRestoreAPI.as_view("project_restore"),
)
blueprint.add_url_rule(
    "/stats",
    methods=["GET", "OPTIONS"],
    view_func=StatsAPI.as_view("stats"),
)
blueprint.add_url_rule(
    "/usage",
    methods=["GET", "OPTIONS"],
    view_func=UsageAPI.as_view("usage"),
)
blueprint.add_url_rule(
    "/settings",
    methods=["GET", "PUT", "OPTIONS"],
    view_func=SettingsAPI.as_view("settings"),
)
blueprint.add_url_rule(
    "/scan",
    methods=["POST", "OPTIONS"],
    view_func=ScanAPI.as_view("scan"),
)
