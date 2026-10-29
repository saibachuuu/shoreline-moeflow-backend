"""按当前存储模式回填历史缺失的容量字段（缩略图 / 重采样图 / 导出产物）。

背景：``File.cover_bytes`` / ``File.resample_bytes`` 与
``Output.storage_bytes`` 是后加的增量记账字段，历史数据从未写入过；m0009
只回填了 ``Project.file_size``。本脚本按**当前存储模式**逐个对象探测实际
大小并补账：

- 缩略图与重采样图**永远在默认存储**：``thumbnail.py`` 写死全局 ``oss``，
  项目迁入存储池后也不会变；
- 导出产物跟随项目当前后端（``storage_router.backend_for(project)``）；
- 写入时同步 ``Project.cover_bytes`` / ``resample_bytes`` / ``output_bytes``
  与 ``file_size``，口径与 ``File.apply_derived_bytes()``、
  ``_record_output_bytes()`` 一致。

只处理「字段为 0 或缺失」的记录，幂等、可断点重跑；默认 dry-run（只读）。

用法::

    # 只探测，不写库
    python -m app.scripts.backfill_storage_usage
    # 真写；先小批量试点，确认数字与桶账单/抽样一致再全量
    python -m app.scripts.backfill_storage_usage --apply --limit 1000
    # 只补导出产物
    python -m app.scripts.backfill_storage_usage --apply --only output
    # 只处理一个项目
    python -m app.scripts.backfill_storage_usage --apply --project-id <ObjectId>

HEAD 请求量 = 待补图片数 × 2 + 待补导出数。五万张图约十万次请求，先
``--limit`` 试点，避免一次性把对象存储的请求配额和账单打满。
"""

from __future__ import annotations

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from mongoengine.queryset.visitor import Q

logger = logging.getLogger("backfill_storage_usage")


# ----------------------------------------------------------------------
# 查询
# ----------------------------------------------------------------------
def _zero_or_missing(field: str) -> Q:
    """匹配「字段为 0」或「字段缺失」的文档。

    历史文档可能压根没有这个键（字段是后加的），只写 ``field=0`` 会漏掉。
    """
    return Q(**{field: 0}) | Q(**{f"{field}__exists": False})


def _pending_files(project_id=None, limit=None):
    from app.constants.file import FileType
    from app.models.file import File

    query = File.objects(type=FileType.IMAGE, save_name__ne="")
    if project_id is not None:
        query = query.filter(project=project_id)
    query = query.filter(
        _zero_or_missing("cover_bytes") | _zero_or_missing("resample_bytes")
    ).order_by("id")
    if limit:
        query = query.limit(limit)
    return list(query)


def _pending_outputs(project_id=None, limit=None):
    from app.models.output import Output

    query = Output.objects(file_name__gt="")
    if project_id is not None:
        query = query.filter(project=project_id)
    query = query.filter(_zero_or_missing("storage_bytes")).order_by("id")
    if limit:
        query = query.limit(limit)
    return list(query)


# ----------------------------------------------------------------------
# 探测
# ----------------------------------------------------------------------
def _derived_keys(file, config) -> list[str]:
    """缩略图的相对 key，与 ``thumbnail.py`` 的命名保持一字不差。"""
    stem = (file.save_name or "").rsplit(".", 1)[0]
    prefix = config["OSS_FILE_PREFIX"]
    return [
        f"{prefix}{config['OSS_PROCESS_COVER_NAME']}-{stem}.webp",
        f"{prefix}{config['OSS_PROCESS_RESAMPLE_NAME']}-{stem}.webp",
    ]


def _output_key(output, config) -> str:
    return f"{config['OSS_OUTPUT_PREFIX']}{output.id}/{output.file_name}"


def _head_size(backend, key):
    """返回 ``(size, error)``。对象不存在时 size 为 ``None``、error 为 ``None``。"""
    try:
        meta = backend.head_object(key)
    except Exception as error:  # noqa: BLE001 - 网络/权限问题：跳过，下次再试
        return None, error
    if meta is None:
        return None, None
    return int(meta.get("size") or 0), None


def _probe_derived(file, config):
    """返回 ``((cover_size, resample_size), error)``。

    两个对象都不存在时返回 ``(None, None)``，由调用方计入 missing。
    """
    from app import oss

    cover_key, resample_key = _derived_keys(file, config)
    cover_size, error = _head_size(oss, cover_key)
    if error is not None:
        return None, error
    resample_size, error = _head_size(oss, resample_key)
    if error is not None:
        return None, error
    if cover_size is None and resample_size is None:
        return None, None
    return (cover_size or 0, resample_size or 0), None


# ----------------------------------------------------------------------
# 写库
# ----------------------------------------------------------------------
def _apply_derived(file, cover_size: int, resample_size: int) -> tuple[int, int]:
    """写 ``File.cover_bytes`` / ``resample_bytes`` 并把差值汇总到项目。"""
    from app.models.file import File

    previous_cover = file.cover_bytes or 0
    previous_resample = file.resample_bytes or 0
    if cover_size == previous_cover and resample_size == previous_resample:
        return 0, 0
    File.objects(id=file.id).update(
        set__cover_bytes=cover_size,
        set__resample_bytes=resample_size,
    )
    file.apply_derived_bytes(
        cover_size - previous_cover,
        resample_size - previous_resample,
    )
    return cover_size - previous_cover, resample_size - previous_resample


def _apply_output(output, size: int) -> int:
    """写 ``Output.storage_bytes`` 并把差值汇总到项目；返回实际增量。"""
    from app.models.output import Output
    from app.models.project import Project

    previous = output.storage_bytes or 0
    if size == previous:
        return 0
    delta = size - previous
    Output.objects(id=output.id).update(set__storage_bytes=size)
    Project.objects(id=output.project.id).update(
        inc__output_bytes=delta,
        inc__file_size=round(delta / 1024),
    )
    return delta


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def _empty_stats() -> dict:
    return {
        "scanned": 0,
        "found": 0,
        "missing": 0,
        "errors": 0,
        "bytes": 0,
        "written": 0,
    }


def _iter_probes(tasks, workers):
    """并发探测，``tasks`` 为 ``(row, callable)``；结果按完成顺序 yield。"""
    if workers <= 1:
        for row, probe in tasks:
            size, error = probe()
            yield row, size, error
        return
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(probe): row for row, probe in tasks}
        for future in as_completed(futures):
            row = futures[future]
            try:
                yield row, *future.result()
            except Exception as error:  # noqa: BLE001 - probe 自己已兜住
                yield row, None, error


def _run_derived(files, config, args, stats):
    logger.info("待补缩略图账：%d 个文件", len(files))

    def task_for(file):
        return file, lambda: _probe_derived(file, config)

    for file, result, error in _iter_probes(
        (task_for(file) for file in files), args.workers
    ):
        stats["scanned"] += 1
        if error is not None:
            stats["errors"] += 1
            logger.warning("探测派生图失败：file=%s %s", file.id, error)
            continue
        if result is None:
            stats["missing"] += 1
            continue
        cover_size, resample_size = result
        stats["found"] += 1
        if args.dry_run:
            logger.info(
                "[dry-run] file=%s cover=%d resample=%d",
                file.id,
                cover_size,
                resample_size,
            )
            stats["bytes"] += cover_size + resample_size
            continue
        cover_delta, resample_delta = _apply_derived(file, cover_size, resample_size)
        if cover_delta or resample_delta:
            stats["written"] += 1
            stats["bytes"] += cover_delta + resample_delta
            logger.info(
                "回填派生图：file=%s cover=%d resample=%d "
                "project_delta_cover=%d project_delta_resample=%d",
                file.id,
                cover_size,
                resample_size,
                cover_delta,
                resample_delta,
            )


def _run_output(outputs, config, args, stats):
    from app import oss
    from app.services import storage_router

    logger.info("待补导出产物账：%d 个导出", len(outputs))

    # 同一来源共享后端；backend_for 没有请求上下文缓存，逐个建 boto3
    # client 会把脚本拖成 N 次连接初始化。
    backends: dict[str, object] = {}

    def backend_for(output):
        key = str(getattr(output.project, "storage_source_id", None) or "")
        if key not in backends:
            backends[key] = storage_router.backend_for(output.project) or oss
        return backends[key]

    def task_for(output):
        backend = backend_for(output)
        return output, lambda: _head_size(backend, _output_key(output, config))

    for output, size, error in _iter_probes(
        (task_for(output) for output in outputs), args.workers
    ):
        stats["scanned"] += 1
        if error is not None:
            stats["errors"] += 1
            logger.warning("探测导出产物失败：output=%s %s", output.id, error)
            continue
        if size is None:
            stats["missing"] += 1
            continue
        stats["found"] += 1
        if args.dry_run:
            logger.info("[dry-run] output=%s bytes=%d", output.id, size)
            stats["bytes"] += size
            continue
        delta = _apply_output(output, size)
        if delta:
            stats["written"] += 1
            stats["bytes"] += delta
            logger.info(
                "回填导出产物：output=%s bytes=%d project_delta=%d",
                output.id,
                size,
                delta,
            )


def _log_summary(prefix, stats):
    logger.info(
        "%s scanned=%d found=%d missing=%d errors=%d written=%d bytes=%d",
        prefix,
        stats["scanned"],
        stats["found"],
        stats["missing"],
        stats["errors"],
        stats["written"],
        stats["bytes"],
    )


def _configure_logging(verbose: bool):
    """脚本日志独立输出：容器里 LOG_LEVEL 未设置时 root 可能没有 handler。"""
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="按当前存储模式回填历史缺失的容量字段（缩略图 / 导出产物）",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正写库；不传则只探测（dry-run）",
    )
    parser.add_argument(
        "--only",
        choices=("all", "derived", "output"),
        default="all",
        help="只处理某一类（默认 all）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="每一类最多处理多少条（0 = 不限，试点用）",
    )
    parser.add_argument(
        "--project-id",
        default="",
        help="只处理指定项目（ObjectId）",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="HEAD 并发数（默认 8；设 1 可串行排障）",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="打印每条探测结果",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.verbose)
    args.dry_run = not args.apply
    if args.dry_run:
        logger.info("dry-run：只探测，不写库（加 --apply 才真正回填）")

    project_id = None
    if args.project_id:
        from bson import ObjectId
        from bson.errors import InvalidId

        try:
            project_id = ObjectId(args.project_id)
        except InvalidId:
            logger.error("--project-id 不是合法的 ObjectId：%s", args.project_id)
            return 2

    from app import create_app

    app = create_app()
    with app.app_context():
        config = app.config
        if args.only in ("all", "derived"):
            stats = _empty_stats()
            _run_derived(
                _pending_files(project_id=project_id, limit=args.limit or None),
                config,
                args,
                stats,
            )
            _log_summary("derived", stats)
        if args.only in ("all", "output"):
            stats = _empty_stats()
            _run_output(
                _pending_outputs(project_id=project_id, limit=args.limit or None),
                config,
                args,
                stats,
            )
            _log_summary("output", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
