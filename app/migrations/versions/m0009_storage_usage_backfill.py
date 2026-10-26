"""Backfill project storage usage from the per-file cache.

项目占用从这一版起用于「存储池预算」与「体量徽标」，而历史数据里近半数
项目压根没被累加过（离线导入直接写库，绕过了 ``upload_real_file`` 的累加）。
本迁移做两件事：

1. 把历史上可能存在的旧键 ``output.fn`` 搬成 ``file_name``（幂等兜底）。
   实际上 ``df_field`` 从来不是合法参数，落库键一直是 ``file_name``，
   所以这一步通常是空操作 —— 留着是为了防止某个早期分支写过 ``fn``。
2. 按 ``file`` 集合聚合出每个项目真实占用，回填到 ``project.fs``。
   只补「为 0 或缺失」的项目，已有值一个不碰（抽查实测与求和完全相等）。

派生图与导出产物的历史字节数**补不回来**（当时没记录），接受系统性偏低；
按站长定的原则，这类一次性数据修正走迁移，不做定时校准任务。
"""

from app.migrations.runner import IrreversibleMigration

VERSION = "0009"
NAME = "storage_usage_backfill"
DESCRIPTION = "Backfill Project.file_size and normalise legacy output filename key"
LEVEL = "AUTO"


def _project_totals(db):
    """项目 -> 文件占用合计（KB），只统计非零文件。"""
    pipeline = [
        {"$match": {"fs": {"$gt": 0}}},
        {"$group": {"_id": "$p", "total": {"$sum": "$fs"}}},
    ]
    return db.file.aggregate(pipeline)


def up(db):
    # 1. 旧键兜底。目标键已存在时只清掉多余的 fn，绝不覆盖真实文件名。
    # 逐条处理而不是 ``$rename``：目标键已存在时各版本 MongoDB 的行为不一致。
    for legacy in db.output.find({"fn": {"$exists": True}}):
        if "file_name" in legacy:
            db.output.update_one({"_id": legacy["_id"]}, {"$unset": {"fn": ""}})
            continue
        db.output.update_one(
            {"_id": legacy["_id"]},
            {"$set": {"file_name": legacy["fn"]}, "$unset": {"fn": ""}},
        )

    # 2. 回填项目占用。只动 0 / 缺失的项目，已有值保持原样。
    # 逐条 update_one 而不是 bulk_write：迁移只跑一次，1825 个项目耗时以
    # 百毫秒计，换来的是与测试环境（mongomock）一致的批量语义。
    for row in _project_totals(db):
        project_id = row.get("_id")
        total = int(row.get("total") or 0)
        if not project_id or total <= 0:
            continue
        db.project.update_one(
            {
                "_id": project_id,
                "$or": [{"fs": {"$exists": False}}, {"fs": 0}, {"fs": None}],
            },
            {"$set": {"fs": total}},
        )


def verify(db):
    # 旧键必须清干净
    if db.output.count_documents({"fn": {"$exists": True}}):
        return False

    # 「有文件但项目占用仍为 0」的项目必须为 0 个。
    totals = {
        row["_id"]: int(row.get("total") or 0)
        for row in _project_totals(db)
        if row.get("_id") and int(row.get("total") or 0) > 0
    }
    if not totals:
        return True
    stale = db.project.count_documents(
        {
            "_id": {"$in": list(totals.keys())},
            "$or": [{"fs": {"$exists": False}}, {"fs": 0}, {"fs": None}],
        }
    )
    return stale == 0


def down(db):
    raise IrreversibleMigration(
        "Backfilled sizes are measured values; reverting them to 0 would lose data"
    )
