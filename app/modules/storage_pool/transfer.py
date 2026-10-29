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
from app.constants.project import ProjectStatus
from app.models.file import File
from app.models.output import Output
from app.models.project import Project

from .backend import PoolBackend, S3Transfer
from .constants import (
    CONTINUE_COUNTDOWN,
    DELETE_BATCH_SIZE,
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

    搬三类：原图（IMAGE）、重采样图（resample）与导出产物（outputs/）。
    文本原文不搬（前端无展示，留在默认存储）；小封面、校对标记图、
    头像同样不搬。

    重采样图按 ``save_name`` 推导，**不检查对象是否存在**：检查要么列桶、
    要么 N 次 HEAD，成本高；不存在的对象由搬运阶段标记 ``SKIPPED``。
    """
    from .constants import StorageObjectKind

    file_prefix = current_app.config["OSS_FILE_PREFIX"]
    output_prefix = current_app.config["OSS_OUTPUT_PREFIX"]
    resample_prefix = f"{file_prefix}{current_app.config['OSS_PROCESS_RESAMPLE_NAME']}-"

    kinds = {}
    # 原图与重采样图：文件夹没有 save_name，查出来是空串，跳过。
    for save_name in File.objects(project=project, type=FileType.IMAGE).scalar(
        "save_name"
    ):
        if not save_name:
            continue
        kinds[file_prefix + save_name] = StorageObjectKind.ORIGINAL
        stem = save_name.rsplit(".", 1)[0]
        kinds[f"{resample_prefix}{stem}.webp"] = StorageObjectKind.RESAMPLE

    # 导出产物：outputs/<output.id>/<file_name>（含 txt 导出，一起搬）。
    # 用 ``$gt: ""`` 而不是 ``$ne: ""``：后者会把「字段缺失」的老文档
    # 也算进来，枚举出 ``outputs/<id>/`` 这种空文件名的 key，搬它必然 404，
    # 于是一个历史文档就能把整趟迁移判成失败。
    for output in Output.objects(project=project, file_name__gt=""):
        kinds[output_prefix + str(output.id) + "/" + output.file_name] = (
            StorageObjectKind.OUTPUT
        )
    return kinds


def enumerate_objects(project) -> list[str]:
    """列出该项目需要搬运的相对 key。

    **按数据库清单枚举，不反查桶**：更准（桶里可能有别的项目/历史的垃圾），
    也不依赖桶的列举权限。

    搬三类：原图（IMAGE）、重采样图（resample）与导出产物
    （``outputs/<id>/<file_name>``，含 txt 导出）；文本、小封面、校对标记图、
    头像都不搬。
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
    """按 ``{key: kind}`` 落逐对象行。建任务与 ABORTED 重试共用。

    一次 ``insert`` 而不是逐条 ``save``：几千个对象时逐条往返能把建任务
    这一步本身拖成好几秒，而它就跑在请求线程里。
    """
    if not kinds:
        return
    StorageTransferObject.objects.insert(
        [
            StorageTransferObject(
                transfer=transfer,
                src_key=key,
                dst_key=key,
                kind=kind,
                status=TransferObjectStatus.PENDING,
            )
            for key, kind in kinds.items()
        ],
        load_bulk=False,
    )


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

    if project.status != ProjectStatus.WORKING or project.clear_in_progress:
        transfer.update(
            set__status=TransferStatus.ABORTED,
            set__last_error="项目已完结或正在清空，迁移已中止",
            set__finish_time=datetime.datetime.utcnow(),
        )
        return "中止：项目已完结或正在清空"

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
        switched, error, retryable = _switch_pointer(transfer, project, source)
        if not switched:
            if retryable:
                # 锁被别的任务占着：这是排期问题，不是数据问题。判 ABORTED
                # 会把任务挂在那儿等人来点重试，自动巡查等于白跑一趟。
                _continue_later(transfer.id)
                return f"稍后重试：{error}"
            transfer.update(
                set__status=TransferStatus.ABORTED,
                set__last_error=error,
                set__finish_time=datetime.datetime.utcnow(),
            )
            return f"中止：{error}"
        transfer.reload()

    if transfer.status == TransferStatus.SWITCHED:
        _delete_source_objects(transfer, src)
        # 源端没删干净就不能标 DONE：DONE 之后没有任何流程会再回来删它，
        # 那些对象会永久占据源桶，扩容等于白做。留在 SWITCHED 反复重试，
        # 连续失败到 MAX_STALLS 才放弃，并把残留数量写进 last_error 留痕。
        remaining = StorageTransferObject.objects(
            transfer=transfer, status=TransferObjectStatus.VERIFIED
        ).count()
        if remaining:
            stalls = (transfer.stall_count or 0) + 1
            if stalls >= MAX_STALLS:
                transfer.update(
                    set__status=TransferStatus.DONE,
                    set__finish_time=datetime.datetime.utcnow(),
                    set__last_error=f"源端仍有 {remaining} 个对象未删除，需人工清理",
                )
                _refresh_source_has_data(source)
                return f"完成，但源端残留 {remaining} 个对象"
            transfer.update(set__stall_count=stalls)
            _continue_later(transfer.id)
            return f"进行中：源端还剩 {remaining} 个对象待删除，已安排下一轮"
        transfer.update(
            set__status=TransferStatus.DONE,
            set__finish_time=datetime.datetime.utcnow(),
            set__stall_count=0,
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


def _recompute_counters(transfer):
    """按逐对象行的最终状态重算计数（幂等）。

    早期版本在搬运循环里逐对象 ``$inc``：校验失败重试时同一个对象会被
    加两次，进度条能超 100%、``breakdown`` 也跟着虚高。改成重算之后，
    重试、并发推进、ABORTED 重建清单都收敛到同一个数。
    """
    output_prefix = current_app.config["OSS_OUTPUT_PREFIX"]
    copied_objects = 0
    copied_bytes = 0
    origin_bytes = 0
    output_bytes = 0
    rows = StorageTransferObject.objects(
        transfer=transfer,
        status__in=[
            TransferObjectStatus.COPIED,
            TransferObjectStatus.VERIFIED,
            TransferObjectStatus.SOURCE_DELETED,
        ],
    )
    for row in rows:
        size = row.size or 0
        copied_objects += 1
        copied_bytes += size
        # 文本原文的 key 也在 files/ 下，仅靠前缀分不出；建任务时写死的
        # kind 说了算，旧任务没有 kind 才按前缀回退。RESAMPLE 与原图一起
        # 计入 origin_bytes；TEXT 是旧任务遗留，仍按 output_bytes 处理。
        kind = row.kind or (
            StorageObjectKind.OUTPUT
            if row.src_key.startswith(output_prefix)
            else StorageObjectKind.ORIGINAL
        )
        if kind in (StorageObjectKind.OUTPUT, StorageObjectKind.TEXT):
            output_bytes += size
        else:
            origin_bytes += size
    StorageTransfer.objects(id=transfer.id).update_one(
        set__copied_objects=copied_objects,
        set__copied_bytes=copied_bytes,
        set__breakdown=TransferBreakdown(
            origin_bytes=origin_bytes,
            output_bytes=output_bytes,
            object_count=copied_objects,
        ),
    )


def _is_not_found(error) -> bool:
    """判断异常是不是「源对象不存在」。

    派生图（resample）可能还没生成，这不是迁移失败；记成 SKIPPED 继续走。
    覆盖三种后端：oss2（阿里云，以及 PoolBackend 转译后的 404）、
    boto3（默认存储的 S3 分支）、本地文件系统。
    """
    from botocore.exceptions import ClientError
    from oss2.exceptions import NoSuchKey

    if isinstance(error, (NoSuchKey, FileNotFoundError)):
        return True
    if isinstance(error, ClientError):
        code = str(error.response.get("Error", {}).get("Code", ""))
        return code in ("404", "NoSuchKey", "NotFound")
    return False


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

    moved = 0
    for row in pending_qs.limit(limit):
        try:
            size, etag = S3Transfer(src, dst).transfer(row.src_key, size=row.size)
        except Exception as error:
            if _is_not_found(error):
                # 派生图还没生成：跳过，不删源、不算失败。
                StorageTransferObject.objects(
                    id=row.id, status=TransferObjectStatus.PENDING
                ).update_one(
                    set__status=TransferObjectStatus.SKIPPED,
                    set__last_error="源对象不存在，已跳过",
                    inc__attempts=1,
                )
                continue
            logger.exception("对象搬运失败：%s", row.src_key)
            StorageTransferObject.objects(
                id=row.id, status=TransferObjectStatus.PENDING
            ).update_one(
                set__status=TransferObjectStatus.FAILED,
                set__last_error=str(error)[:500],
                inc__attempts=1,
            )
            continue
        # 原子推进：并发 worker 或重放的续跑里，只有一个能把这一行从
        # PENDING 拿走，``moved`` 因此不会被算两遍。
        claimed = StorageTransferObject.objects(
            id=row.id, status=TransferObjectStatus.PENDING
        ).update_one(
            set__status=TransferObjectStatus.COPIED,
            set__size=size,
            set__etag=etag or "",
            set__last_error="",
        )
        if claimed:
            moved += 1

    _recompute_counters(transfer)

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
    """★ 切指针。锁内二次确认清单，再改 ``Project.storage_source_id``。

    返回 ``(是否切换成功, 原因, 可否直接重试)``。第三项用来区分两件事：
    「清单过期」要重建（走 ABORTED + 重试按钮），「锁被别人占着」只是排期
    问题，安排下一轮即可，不该把任务判死等人来点。
    """
    project.reload()
    snapshot = transfer.snapshot_edit_time
    if snapshot and project.edit_time and project.edit_time != snapshot:
        return False, "搬运期间项目文件有变更，已中止（可重试）", False

    # owner 必须能区分「谁持有的锁」：所有人都写死 "transfer" 时，迟到的
    # 释放分不清自己那份，会删掉别人接管的租约。
    owner = f"transfer:{transfer.id}"
    if not StoragePoolLock.acquire(project.id, transfer_id=transfer.id, owner=owner):
        return False, "项目正被另一个迁移任务锁定，稍后重试", True

    try:
        project.reload()
        if project.status != ProjectStatus.WORKING or project.clear_in_progress:
            return False, "项目已完结或正在清空，迁移已中止", False
        current_keys = set(enumerate_objects(project))
        recorded = set(
            StorageTransferObject.objects(transfer=transfer).scalar("src_key")
        )
        missing = current_keys - recorded
        if missing:
            return False, f"清单不一致：新增 {len(missing)} 个对象未搬运，已中止", False
        if snapshot and project.edit_time and project.edit_time != snapshot:
            return False, "搬运期间项目文件有变更，已中止（可重试）", False

        target_id = None if transfer.direction == TransferDirection.OUT else source.id
        if transfer.direction == TransferDirection.MOVE:
            # 直转的目标是另一池源，不在 source 上。
            target_id = transfer.target_source.id if transfer.target_source else None
        # 方向守卫：两个反向任务并发时，后切的那个必须停下来，
        # 否则 IN/OUT 互相覆盖，数据在两桶之间来回倒。
        project.reload()
        current_id = getattr(project, "storage_source_id", None)
        if transfer.direction == TransferDirection.IN:
            if current_id is not None and str(current_id) == str(target_id):
                transfer.update(set__status=TransferStatus.SWITCHED)
                return True, "", False
            if current_id is not None:
                return False, "项目已被其他任务迁移，已中止", False
        elif transfer.direction == TransferDirection.MOVE:
            if current_id is not None and str(current_id) == str(target_id):
                transfer.update(set__status=TransferStatus.SWITCHED)
                return True, "", False
            # 直转要求项目还在源上：中途被回迁/转走就停下，
            # 否则会把一份旧桶的数据切成新指针。
            if current_id is None or str(current_id) != str(source.id):
                return False, "项目已被其他任务迁移，已中止", False
        else:
            if current_id is None:
                transfer.update(set__status=TransferStatus.SWITCHED)
                return True, "", False
            if str(current_id) != str(source.id):
                return False, "项目已被其他任务迁移，已中止", False
        Project.objects(id=project.id).update(
            set__storage_source_id=target_id,
            set__storage_source_switched_time=datetime.datetime.utcnow(),
        )
        transfer.update(set__status=TransferStatus.SWITCHED)
        return True, "", False
    finally:
        StoragePoolLock.release(project.id, owner=owner)


def _delete_source_objects(transfer, src):
    """删源端对象。只删「已校验」的行 —— 没校验过的宁可留着。

    按目录、每 1000 个一批发 ``delete_objects``：逐条删时 2000 个对象就是
    2000 次往返，worker 还没跑完就先超时，源端对象一直留着，扩容等于没扩。
    """
    rows = list(
        StorageTransferObject.objects(
            transfer=transfer, status=TransferObjectStatus.VERIFIED
        )
    )
    groups = {}
    for row in rows:
        path, _, name = row.src_key.rpartition("/")
        groups.setdefault(path + "/" if path else "", []).append((name, row.id))

    for path, items in groups.items():
        for start in range(0, len(items), DELETE_BATCH_SIZE):
            batch = items[start : start + DELETE_BATCH_SIZE]
            try:
                src.delete(path, [name for name, _ in batch])
            except Exception:
                # 删源失败不影响正确性（对象只是多占一份空间），行保持
                # VERIFIED，下一轮还能再试。
                logger.warning(
                    "批量删除源端对象失败：%s（%d 个）", path, len(batch), exc_info=True
                )
                continue
            StorageTransferObject.objects(
                id__in=[row_id for _, row_id in batch]
            ).update(set__status=TransferObjectStatus.SOURCE_DELETED)


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
