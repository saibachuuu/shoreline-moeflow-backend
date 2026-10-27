"""迁移状态机。

顺序即安全性（PENDING → COPYING → VERIFIED → SWITCHED → DONE）：

1. **切指针在删源之前**：切换前读源、切换后读池，中间不存在「两边都读不到」
   的窗口；删源失败只浪费空间，不影响任何读取。
2. **切指针前必须重读 ``project.edit_time``**：搬的过程中用户动了文件，
   就置 ABORTED 让下一轮重来 —— 宁可多中止几轮，也不能在清单不全时切指针。
3. **切指针必须在项目级锁内完成**，锁内再枚举一次清单，确认没有新增对象。

每一步都做成幂等的：失败后 ``retry`` 从断点继续（逐对象状态在
``StorageTransferObject`` 里）。
"""

import datetime
import logging

from flask import current_app

from app import oss
from app.constants.file import FileType
from app.models.file import File
from app.models.output import Output
from app.models.project import Project

from .backend import PoolBackend, S3Transfer
from .constants import (
    CONTINUE_COUNTDOWN,
    MAX_STALLS,
    PROBE_OBJECT_NAME,
    StorageObjectKind,
    TransferDirection,
    TransferObjectStatus,
    TransferStatus,
)
from .models import (
    StoragePoolLock,
    StorageTransfer,
    StorageTransferObject,
    TransferBreakdown,
)

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# 清单
# ----------------------------------------------------------------------
def enumerate_object_kinds(project) -> dict:
    """列出该项目需要搬运的 ``{相对 key: kind}``。

    kind 取 ``StorageObjectKind`` 的三值之一。TEXT 的 key 也在 ``files/`` 下，
    仅靠前缀分不出原图和文本，所以在这里一次查清，后面建行、记账都用它。
    """
    from .constants import StorageObjectKind

    file_prefix = current_app.config["OSS_FILE_PREFIX"]
    output_prefix = current_app.config["OSS_OUTPUT_PREFIX"]

    kinds = {}
    # 原图与文本原文：都在 files/ 下，kind 不同（记账口径不同，见 _copy_objects）。
    # 文件夹没有 save_name，查出来是空串，跳过。
    text_names = set(
        File.objects(project=project, type=FileType.TEXT).scalar("save_name")
    )
    for save_name in File.objects(
        project=project, type__in=(FileType.IMAGE, FileType.TEXT)
    ).scalar("save_name"):
        if save_name:
            key = file_prefix + save_name
            kinds[key] = (
                StorageObjectKind.TEXT
                if save_name in text_names
                else StorageObjectKind.ORIGINAL
            )
    # 导出产物：outputs/<output.id>/<file_name>（含 txt 导出，一起搬）。
    for output in Output.objects(project=project, file_name__ne=""):
        kinds[output_prefix + str(output.id) + "/" + output.file_name] = (
            StorageObjectKind.OUTPUT
        )
    return kinds


def enumerate_objects(project) -> list[str]:
    """列出该项目需要搬运的相对 key。

    **按数据库清单枚举，不反查桶**：更准（桶里可能有别的项目/历史的垃圾），
    也不依赖桶的列举权限。

    搬三类：原图（IMAGE）、文本原文（TEXT，key 也在 ``files/`` 下）与导出
    产物（``outputs/<id>/<file_name>``，含 txt 导出）；派生图、头像都不搬。
    """
    return list(enumerate_object_kinds(project))


def _default_backend():
    return oss


def _backends_for(transfer):
    """返回 ``(src, dst)``。

    IN：默认存储 → 目标池源；OUT：当前池源 → 默认存储；
    MOVE：源池源 → 目标池源（直转，不经过默认存储）。
    """
    pool_of = PoolBackend
    if transfer.direction == TransferDirection.IN:
        return _default_backend(), pool_of(transfer.source)
    if transfer.direction == TransferDirection.OUT:
        return pool_of(transfer.source), _default_backend()
    # MOVE
    return pool_of(transfer.source), pool_of(transfer.target_source)


# ----------------------------------------------------------------------
# 建任务
# ----------------------------------------------------------------------
def prepare(project, source, direction=TransferDirection.IN, target_source=None):
    """建（或复用）一个迁移任务，并把清单落成逐对象行。

    幂等：同一项目已有未结束的任务时直接返回它，不重复建。
    复用键包含方向、来源与目标 —— 迁入、回迁、直转是三件不同的事，
    拿错任务会把数据往反方向搬。
    """
    query = StorageTransfer.objects(
        project=project,
        source=source,
        direction=direction,
        status__in=TransferStatus.ACTIVE,
    )
    # target_source 是后加字段：旧任务读出来是 None，正好和
    # 「IN/OUT 不分目标」的语义对上，直接按值比即可。
    query = query.filter(target_source=target_source)
    active = query.first()
    if active is not None:
        return active

    transfer = StorageTransfer(
        source=source,
        project=project,
        status=TransferStatus.PENDING,
        direction=direction,
        target_source=target_source,
        snapshot_edit_time=project.edit_time,
        breakdown=TransferBreakdown(),
    ).save()

    _build_object_rows(transfer, enumerate_object_kinds(project))
    transfer.update(
        set__total_objects=StorageTransferObject.objects(transfer=transfer).count(),
        # 估算值：项目占用（KB）× 1024。真正搬多少以 copied_bytes 为准，
        # 这个数只用于「本轮预算够不够开工」的判断和进度条分母。
        set__total_bytes=(project.file_size or 0) * 1024,
    )
    transfer.reload()
    return transfer


def _build_object_rows(transfer, kinds) -> None:
    """按 ``{key: kind}`` 落逐对象行。建任务与 ABORTED 重试共用。"""
    for key, kind in kinds.items():
        StorageTransferObject(
            transfer=transfer,
            src_key=key,
            dst_key=key,
            kind=kind,
            status=TransferObjectStatus.PENDING,
        ).save()


# ----------------------------------------------------------------------
# 执行
# ----------------------------------------------------------------------
def run(transfer_id):
    """执行任务到「下一个稳定状态」。可反复调用（重试即续跑）。"""
    transfer = StorageTransfer.objects(id=transfer_id).first()
    if transfer is None:
        return "跳过：迁移任务不存在"
    if transfer.status in (TransferStatus.DONE,):
        return "跳过：已完成"

    # PENDING → COPYING（原子，防止并发重复领取）
    claimed = StorageTransfer.objects(
        id=transfer.id,
        status__in=[TransferStatus.PENDING] + list(TransferStatus.RETRYABLE),
    ).update_one(
        set__status=TransferStatus.COPYING,
        set__start_time=transfer.start_time or datetime.datetime.utcnow(),
        set__last_error="",
    )
    if not claimed:
        transfer.reload()
        if transfer.status not in (
            TransferStatus.COPYING,
            TransferStatus.VERIFIED,
            TransferStatus.SWITCHED,
        ):
            return f"跳过：状态 {transfer.status} 不允许执行"
    transfer.reload()

    source = transfer.source
    project = transfer.project
    if source is None or project is None:
        _fail(transfer, "来源或项目已不存在")
        return "失败：来源或项目已不存在"

    src, dst = _backends_for(transfer)

    if transfer.status == TransferStatus.COPYING:
        moved, pending, failed = _copy_objects(transfer, src, dst, source)
        if failed:
            _fail(transfer, f"{failed} 个对象搬运失败，可重试")
            return f"失败：{failed} 个对象搬运失败"
        if pending:
            # 项目级原子性要求：已开工的项目必须由自己跑到 DONE，不能等下次
            # 巡查（那可能是一天后，项目一直卡在 COPYING、读的还是旧桶）。
            if moved:
                transfer.update(set__stall_count=0)
            else:
                stalls = (transfer.stall_count or 0) + 1
                if stalls >= MAX_STALLS:
                    _fail(
                        transfer,
                        f"连续 {stalls} 轮没有任何进展（还剩 {pending} 个对象），"
                        "已停止自动重试，请检查来源连通性后手动重新触发",
                    )
                    return f"失败：连续 {stalls} 轮零进展"
                transfer.update(set__stall_count=stalls)
            _continue_later(transfer.id)
            return f"进行中：还剩 {pending} 个对象，已安排下一轮"
        transfer.update(
            set__status=TransferStatus.VERIFIED,
            set__copied_objects=StorageTransferObject.objects(
                transfer=transfer,
                status__in=[
                    TransferObjectStatus.COPIED,
                    TransferObjectStatus.VERIFIED,
                    TransferObjectStatus.SOURCE_DELETED,
                ],
            ).count(),
        )
        transfer.reload()

    if transfer.status == TransferStatus.VERIFIED:
        ok, error = _verify_objects(transfer, dst)
        if not ok:
            _fail(transfer, error)
            return f"失败：{error}"
        switched, error = _switch_pointer(transfer, project, source)
        if not switched:
            transfer.update(
                set__status=TransferStatus.ABORTED,
                set__last_error=error,
                set__finish_time=datetime.datetime.utcnow(),
            )
            return f"中止：{error}"
        transfer.reload()

    if transfer.status == TransferStatus.SWITCHED:
        _delete_source_objects(transfer, src)
        transfer.update(
            set__status=TransferStatus.DONE,
            set__finish_time=datetime.datetime.utcnow(),
        )
        _refresh_source_has_data(source)
        # 直转搬空了源、填满了目标：两边的 has_data 都要重算，
        # 否则源删空了还锁着 sub_path，目标进了项目还显示无数据。
        if (
            transfer.direction == TransferDirection.MOVE
            and transfer.target_source is not None
        ):
            _refresh_source_has_data(transfer.target_source)
        return "成功：迁移完成"

    return f"未知状态：{transfer.status}"


def _copy_objects(transfer, src, dst, source):
    """逐对象搬运。返回 ``(本轮搬了多少, 还剩多少, 失败多少)``。

    单次调用上限取源与目标配置的较小值：一个项目几千张图，拆成多次
    才不会让一次任务跑几十分钟被 worker 超时杀掉。**这是安全阀，不是预算**
    —— 预算只在 scan 的项目粒度上做。
    """
    limits = [source.max_objects_per_run or 2000]
    if (
        transfer.direction == TransferDirection.MOVE
        and transfer.target_source is not None
    ):
        limits.append(transfer.target_source.max_objects_per_run or 2000)
    limit = min(limits)
    pending_qs = StorageTransferObject.objects(
        transfer=transfer, status=TransferObjectStatus.PENDING
    ).order_by("id")

    output_prefix = current_app.config["OSS_OUTPUT_PREFIX"]
    # 旧任务可能没有 breakdown（字段是后加的），补一个空壳再累加，
    # 否则 $inc 落在 null 上会直接报错。
    if transfer.breakdown is None:
        StorageTransfer.objects(id=transfer.id).update(
            set__breakdown=TransferBreakdown()
        )

    moved = 0
    for row in pending_qs.limit(limit):
        try:
            size, etag = S3Transfer(src, dst).transfer(row.src_key, size=row.size)
            moved += 1
            row.update(
                set__status=TransferObjectStatus.COPIED,
                set__size=size,
                set__etag=etag or "",
                set__last_error="",
            )
            counters = {
                "inc__copied_objects": 1,
                "inc__copied_bytes": size,
                "inc__breakdown__object_count": 1,
            }
            # 文本原文的 key 也在 files/ 下，仅靠前缀分不出；
            # 建任务时写死的 kind 说了算，旧任务没有 kind 才按前缀回退。
            kind = row.kind or (
                StorageObjectKind.OUTPUT
                if row.src_key.startswith(output_prefix)
                else StorageObjectKind.ORIGINAL
            )
            if kind in (StorageObjectKind.OUTPUT, StorageObjectKind.TEXT):
                counters["inc__breakdown__output_bytes"] = size
            else:
                counters["inc__breakdown__origin_bytes"] = size
            StorageTransfer.objects(id=transfer.id).update_one(**counters)
        except Exception as error:
            logger.exception("对象搬运失败：%s", row.src_key)
            row.update(
                set__status=TransferObjectStatus.FAILED,
                set__last_error=str(error)[:500],
                inc__attempts=1,
            )

    pending = StorageTransferObject.objects(
        transfer=transfer, status=TransferObjectStatus.PENDING
    ).count()
    failed = StorageTransferObject.objects(
        transfer=transfer, status=TransferObjectStatus.FAILED
    ).count()
    return moved, pending, failed


def _continue_later(transfer_id):
    """安排下一轮执行（自动续跑）。

    用 ``send_task`` 按名字发：``tasks.py`` 导入了本模块，反向 import 会成环。
    发不出去只记日志 —— 项目会停在 COPYING，可手动重新触发，不会脏数据。
    """
    try:
        from app import celery

        celery.send_task(
            "tasks.storage_pool_run",
            args=[str(transfer_id)],
            countdown=CONTINUE_COUNTDOWN,
        )
    except Exception:  # pragma: no cover - broker 故障
        logger.exception("安排下一轮迁移失败：transfer=%s", transfer_id)


def _verify_objects(transfer, dst):
    """逐个 ``head_object`` 比对大小；有 etag 也比。"""
    rows = StorageTransferObject.objects(
        transfer=transfer, status=TransferObjectStatus.COPIED
    )
    for row in rows:
        meta = dst.head_object(row.dst_key)
        if meta is None:
            row.update(
                set__status=TransferObjectStatus.FAILED, set__last_error="目的端不存在"
            )
            continue
        if row.size and meta["size"] != row.size:
            row.update(
                set__status=TransferObjectStatus.FAILED,
                set__last_error=f"大小不一致：期望 {row.size} 实际 {meta['size']}",
            )
            continue
        if row.etag and meta.get("etag") and row.etag != meta["etag"]:
            row.update(
                set__status=TransferObjectStatus.FAILED,
                set__last_error=f"etag 不一致：期望 {row.etag} 实际 {meta['etag']}",
            )
            continue
        row.update(set__status=TransferObjectStatus.VERIFIED)

    failed = StorageTransferObject.objects(
        transfer=transfer, status=TransferObjectStatus.FAILED
    ).first()
    if failed is not None:
        return False, f"{failed.src_key} 校验失败：{failed.last_error}"
    return True, ""


def _switch_pointer(transfer, project, source):
    """★ 切指针。锁内二次确认清单，再改 ``Project.storage_source_id``。"""
    project.reload()
    snapshot = transfer.snapshot_edit_time
    if snapshot and project.edit_time and project.edit_time != snapshot:
        return False, "搬运期间项目文件有变更，已中止（可重试）"

    if not StoragePoolLock.acquire(
        project.id, transfer_id=transfer.id, owner="transfer"
    ):
        return False, "项目正被另一个迁移任务锁定，稍后重试"

    try:
        project.reload()
        current_keys = set(enumerate_objects(project))
        recorded = set(
            StorageTransferObject.objects(transfer=transfer).scalar("src_key")
        )
        missing = current_keys - recorded
        if missing:
            return False, f"清单不一致：新增 {len(missing)} 个对象未搬运，已中止"
        if snapshot and project.edit_time and project.edit_time != snapshot:
            return False, "搬运期间项目文件有变更，已中止（可重试）"

        target_id = None if transfer.direction == TransferDirection.OUT else source.id
        if transfer.direction == TransferDirection.MOVE:
            # 直转的目标是另一池源，不在 source 上。
            target_id = (
                transfer.target_source.id if transfer.target_source else None
            )
        # 方向守卫：两个反向任务并发时，后切的那个必须停下来，
        # 否则 IN/OUT 互相覆盖，数据在两桶之间来回倒。
        project.reload()
        current_id = getattr(project, "storage_source_id", None)
        if transfer.direction == TransferDirection.IN:
            if current_id is not None and str(current_id) == str(target_id):
                transfer.update(set__status=TransferStatus.SWITCHED)
                return True, ""
            if current_id is not None:
                return False, "项目已被其他任务迁移，已中止"
        elif transfer.direction == TransferDirection.MOVE:
            if current_id is not None and str(current_id) == str(target_id):
                transfer.update(set__status=TransferStatus.SWITCHED)
                return True, ""
            # 直转要求项目还在源上：中途被回迁/转走就停下，
            # 否则会把一份旧桶的数据切成新指针。
            if current_id is None or str(current_id) != str(source.id):
                return False, "项目已被其他任务迁移，已中止"
        else:
            if current_id is None:
                transfer.update(set__status=TransferStatus.SWITCHED)
                return True, ""
            if str(current_id) != str(source.id):
                return False, "项目已被其他任务迁移，已中止"
        Project.objects(id=project.id).update(
            set__storage_source_id=target_id,
            set__storage_source_switched_time=datetime.datetime.utcnow(),
        )
        transfer.update(set__status=TransferStatus.SWITCHED)
        return True, ""
    finally:
        StoragePoolLock.release(project.id)


def _delete_source_objects(transfer, src):
    """删源端对象。只删「已校验」的行 —— 没校验过的宁可留着。"""
    rows = StorageTransferObject.objects(
        transfer=transfer, status=TransferObjectStatus.VERIFIED
    )
    for row in rows:
        try:
            path, filename = (
                row.src_key.rsplit("/", 1) if "/" in row.src_key else ("", row.src_key)
            )
            src.delete(path + "/" if path else "", filename)
            row.update(set__status=TransferObjectStatus.SOURCE_DELETED)
        except Exception:
            # 删源失败不影响正确性（对象只是多占一份空间），记日志继续。
            logger.warning("删除源端对象失败：%s", row.src_key, exc_info=True)


def _refresh_source_has_data(source):
    """来源上只要还有项目指向它，就标记 has_data（此后禁改 sub_path）。"""
    still_used = Project.objects(storage_source_id=source.id).count()
    source.update(
        set__has_data=bool(still_used), set__update_time=datetime.datetime.utcnow()
    )


def _fail(transfer, message):
    transfer.update(
        set__status=TransferStatus.FAILED,
        set__last_error=message[:500],
        set__finish_time=datetime.datetime.utcnow(),
    )


# ----------------------------------------------------------------------
# 连通性测试
# ----------------------------------------------------------------------
def test_connection(source):
    """head_bucket + 写读删探针，全程用该来源自己的 sub_path。

    探针对象用后即删，不会在桶里留下垃圾。
    """
    backend = PoolBackend(source)
    backend.head_bucket()
    path = current_app.config["OSS_FILE_PREFIX"]
    probe = PROBE_OBJECT_NAME
    payload = b"moeflow-storage-pool-probe"
    backend.upload(path, probe, payload)
    try:
        meta = backend.head_object(path + probe)
        if meta is None or meta["size"] != len(payload):
            raise RuntimeError("探针对象写入后读不到或大小不对")
    finally:
        try:
            backend.delete(path, probe)
        except Exception:  # pragma: no cover
            logger.warning("探针对象清理失败：%s%s", path, probe)
    return True
