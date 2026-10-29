"""存储池的定时任务。

两个任务：

* ``tasks.storage_pool_scan``   每日巡查，挑出够冷且无人使用的项目，排进队列。
* ``tasks.storage_pool_run``    真正搬家（也可由 API 手动触发）。

都走 default 队列（模块 ``ModuleSpec.queue`` 为 None）—— 搬家的耗时在
网络 IO，不占 CPU，塞进 output 队列反而会拖慢导出。

**开关**：beat 条目常驻（否则第一次打开还得重启 beat，就不是热切换了），
两个任务各自在开头读一次 ``StoragePoolSetting``。读不到就按「已停用」处理
（fail-closed）—— 迁移会搬数据并删源，看不清开关时宁可不做。
"""

import datetime
import logging

from app import celery
from app.constants.project import ProjectStatus
from app.models import connect_db
from app.models.presence import ProjectPresence
from app.models.project import Project

from .constants import TransferDirection, TransferStatus
from .models import StoragePoolSetting, StoragePoolSource, StorageTransfer

logger = logging.getLogger(__name__)

#: 一轮巡查最多处理多少个项目，处理完排队下一轮。
SCAN_BATCH_SIZE = 200

#: 设置里没填时的兜底预算（与 StoragePoolSetting 的默认值保持一致）。
DEFAULT_MAX_BYTES = 20 * 1024**3
DEFAULT_MAX_PROJECTS = 20


def _switch(name):
    """读开关，失败一律返回 ``None``（调用方按「关」处理）。

    不做进程内缓存：一天读一次文档的成本可以忽略，而缓存会制造
    「后台关了但任务还在跑」的错觉。
    """
    try:
        setting = StoragePoolSetting.get()
        return bool(getattr(setting, name, None))
    except Exception:
        logger.exception("读取存储池开关 %s 失败，按已停用处理", name)
        return None


@celery.task(name="tasks.storage_pool_scan")
def storage_pool_scan(batch_size: int = SCAN_BATCH_SIZE, force: bool = False):
    """每日巡查：给「够冷 + 没人用 + 没在迁」的项目排迁移任务。

    :param force: 手动触发时为 True —— **忽略开关**。这是「关掉自动、偶尔手动」
        的用法，不是漏洞。
    """
    from . import transfer as transfer_service

    config = celery.conf.app_config
    connect_db(config)

    if not force:
        enabled = _switch("scan_enabled")
        if enabled is False:
            logger.info("存储池自动巡查已停用，跳过")
            return "跳过：自动巡查已停用"
        if enabled is None:
            return "跳过：读取自动巡查开关失败（按已停用处理）"

    sources = list(StoragePoolSource.objects(enabled=True))
    if not sources:
        # 这也是一次真实执行（只是没活可干），记下来比让界面永远显示
        # 「尚未执行过巡查」更诚实。
        _record_scan_result("没有启用的存储来源", 0)
        return "跳过：没有启用的存储来源"

    setting = StoragePoolSetting.get()
    max_bytes = setting.run_max_bytes or DEFAULT_MAX_BYTES
    max_projects = setting.run_max_projects or DEFAULT_MAX_PROJECTS

    queued = 0
    # 预算是全局的：多来源时也不能每个来源各用一份，否则超发 N 倍。
    spent = 0
    started = 0
    for source in sources:
        if queued >= batch_size or started >= max_projects:
            break
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(
            days=source.inactive_days or 90
        )
        # 严格旧先新后：按 edit_time 升序，最冷的最先走。候选集最多 batch_size
        # 条，内存排序成本可忽略（不为此建索引）。
        candidates = (
            Project.objects(
                edit_time__lt=cutoff,
                storage_source_id=None,  # 还没迁的项目
                # 站长手动锁定的项目不参与自动挑选，只接受手动搬迁。
                storage_pool_locked__ne=True,
                # 已完结 / 正在清空的项目不参与自动搬迁：run() 里也有守卫，
                # 但在候选阶段就排除，才不会建出注定 ABORTED 的任务、白占
                # scan 的队头名额。
                status=ProjectStatus.WORKING,
                clear_in_progress__ne=True,
            )
            .order_by("edit_time", "id")
            .limit(batch_size)
        )
        for project in candidates:
            if queued >= batch_size or started >= max_projects:
                break
            # 实时重载项目最新属性，确保拿到并发写入的最新 edit_time 与锁定状态
            try:
                project.reload()
            except Exception:
                continue

            if project.edit_time and project.edit_time >= cutoff:
                logger.info(
                    "存储池巡查跳过项目（最新 edit_time >= cutoff 已不再是冷数据）：project=%s",
                    project.id,
                )
                continue

            if getattr(project, "storage_pool_locked", False):
                continue

            # 整项目取整：预算只决定「这轮开工哪几个项目」，不切分单个项目。
            # 项目占用取自 Project.file_size（KB，迁移 m0009 回填过）。
            estimate = (project.file_size or 0) * 1024
            if started >= 1 and spent + estimate > max_bytes:
                # 装不下这个整项目就留到下一轮；至少排一个，避免超大项目
                # 永远排在队尾、永远轮不到。
                break
            if _is_busy(project):
                continue
            if StorageTransfer.objects(
                project=project, status__in=TransferStatus.ACTIVE
            ).first():
                continue
            try:
                transfer = transfer_service.prepare(
                    project, source, TransferDirection.IN
                )
            except Exception:
                logger.exception("创建迁移任务失败：project=%s", project.id)
                continue
            storage_pool_run.delay(str(transfer.id))
            spent += estimate
            started += 1
            queued += 1

    _record_scan_result(f"已排队 {queued} 个迁移任务", queued)
    return f"已排队 {queued} 个迁移任务"


def _record_scan_result(result, queued):
    """把「上次真的跑了」写进设置文档，供后台卡片展示。

    注意只在**真的执行了巡查**时调用：被开关跳过时不写，否则会把上一次
    的真实结果冲成「跳过」。
    """
    try:
        StoragePoolSetting.objects(type="default").update_one(
            set__last_scan_time=datetime.datetime.utcnow(),
            set__last_scan_result=str(result)[:500],
            set__last_scan_queued=int(queued or 0),
        )
    except Exception:  # pragma: no cover - 记录失败不影响巡查本身
        logger.exception("写入巡查结果失败")


def _is_busy(project) -> bool:
    """有活跃在线状态或近期有编辑动作的项目不要动 —— 用户可能正在上面干活。

    双重保险：
    1. 在线心跳保护：当前有效心跳（expire_at > now）或最近 5 分钟内有心跳上报记录
       （容忍心跳短暂失联/网络抖动，避免临界时点刚好踩中巡查）；
    2. 近期编辑保护：项目在最近 15 分钟内有任何修改记录（edit_time > now - 15min），
       即使尚未触发心跳或刚保存完信息，也坚决不纳入冷数据搬迁。
    """
    now = datetime.datetime.utcnow()
    # 保险 1：在线活跃状态（含 5 分钟安全缓冲）
    presence_cutoff = now - datetime.timedelta(minutes=5)
    has_presence = (
        ProjectPresence.objects(
            project=project,
            expire_at__gt=now,
        ).first()
        is not None
        or ProjectPresence.objects(
            project=project,
            last_heartbeat__gt=presence_cutoff,
        ).first()
        is not None
    )
    if has_presence:
        logger.info("存储池巡查跳过活跃项目（检测到在线成员）：project=%s", project.id)
        return True

    # 保险 2：项目近期编辑保护（最近 15 分钟内有编辑动作）
    recent_edit_cutoff = now - datetime.timedelta(minutes=15)
    edit_time = getattr(project, "edit_time", None)
    if edit_time and edit_time > recent_edit_cutoff:
        logger.info(
            "存储池巡查跳过活跃项目（近期有编辑动作 edit_time=%s）：project=%s",
            edit_time,
            project.id,
        )
        return True

    return False


@celery.task(name="tasks.storage_pool_run")
def storage_pool_run(transfer_id):
    """执行一趟迁移（可反复调用，重试即续跑）。

    任务体由 ``FlaskTask`` 包在 app context 里，``current_app.config`` 可直接用。
    """
    from app import oss
    from . import transfer as transfer_service

    config = celery.conf.app_config
    connect_db(config)

    # 第二道闸：站长的「紧急停止」。关掉时不领取新批次，任务停在 PENDING，
    # 恢复后继续（断点续跑靠 storage_transfer_object 里的逐对象状态）。
    # 读不到开关按 False 处理（fail-closed），宁可让任务多等一轮。
    if not _switch("run_enabled"):
        return "暂停：自动迁移已停止（任务保留在待执行，恢复后继续）"

    # worker 启动时已经 init 过；这里再 init 一次，保证换了配置的进程也拿到新桶。
    oss.init(config)
    return transfer_service.run(transfer_id)
