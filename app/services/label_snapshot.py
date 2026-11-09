"""Immutable, chunked label snapshots and journaled destructive operations."""

import datetime
import hashlib
from collections import defaultdict
from contextlib import contextmanager

from bson import BSON, Binary, ObjectId
from mongoengine.connection import get_db

from app.core.api import APIError
from app.services.label_lock import label_lock
from app.services.project_statistics import rebuild_project_statistics, ref_id

VERSION = 1
CHUNK_BYTES = 4 * 1024 * 1024


class LabelOperationError(APIError):
    status_code = 409
    message = "标签操作无法执行"


def collections():
    db = get_db()
    snapshots, chunks = db.project_label_snapshot, db.project_label_snapshot_chunk
    snapshots.create_index([("project", 1), ("created", -1), ("_id", -1)])
    chunks.create_index([("snapshot", 1), ("index", 1)], unique=True)
    return snapshots, chunks


def label_data(project):
    from app.models.file import File, Source, Translation, Tip
    from app.models.target import Target

    files = list(File._get_collection().find({"p": project.pk}).sort("_id", 1))
    sources = list(
        Source._get_collection()
        .find({"f": {"$in": [row["_id"] for row in files]}})
        .sort("_id", 1)
    )
    source_ids = [row["_id"] for row in sources]
    data = {
        "files": [
            {key: row.get(key) for key in ("_id", "sa", "md", "t", "v")}
            for row in files
        ],
        "targets": [
            {"_id": row["_id"], "l": row["l"]}
            for row in Target._get_collection().find({"t": project.pk}).sort("_id", 1)
        ],
        "sources": sources,
        "translations": list(
            Translation._get_collection()
            .find({"o": {"$in": source_ids}})
            .sort("_id", 1)
        ),
        "tips": list(
            Tip._get_collection().find({"o": {"$in": source_ids}}).sort("_id", 1)
        ),
    }
    for key in ("sources", "translations", "tips"):
        for row in data[key]:
            row.pop("label_epoch", None)  # concurrency fence, not label content
    return data


def data_digest(data):
    digest = hashlib.sha256()
    for kind, rows in data.items():
        digest.update(kind.encode())
        for row in rows:
            digest.update(BSON.encode(row))
    return digest.hexdigest()


def create_snapshot(project, actor, name="", *, reason="manual"):
    with label_lock(project.pk):
        project.reload()
        if project.label_recovery:
            raise LabelOperationError("项目需要先恢复未完成的操作")
        data = label_data(project)
        snapshots, chunks = collections()
        sid = ObjectId()
        now = datetime.datetime.utcnow()
        metadata = {
            "_id": sid,
            "project": project.pk,
            "name": name.strip()[:120] or now.strftime("快照 %Y-%m-%d %H:%M:%S UTC"),
            "actor": actor.pk,
            "actor_name": actor.name,
            "created": now,
            "version": VERSION,
            "reason": reason,
            "ready": False,
            "digest": data_digest(data),
            "counts": {key: len(rows) for key, rows in data.items()},
            "revision": project.label_revision,
        }
        snapshots.insert_one(metadata)
        index = 0
        try:
            for kind, rows in data.items():
                batch, size = [], 0
                for row in rows:
                    row_size = len(BSON.encode(row))
                    if batch and size + row_size > CHUNK_BYTES:
                        encoded = BSON.encode({"rows": batch})
                        chunks.insert_one(
                            {
                                "snapshot": sid,
                                "index": index,
                                "kind": kind,
                                "data": Binary(encoded),
                                "hash": hashlib.sha256(encoded).hexdigest(),
                            }
                        )
                        index += 1
                        batch, size = [], 0
                    batch.append(row)
                    size += row_size
                if batch:
                    encoded = BSON.encode({"rows": batch})
                    chunks.insert_one(
                        {
                            "snapshot": sid,
                            "index": index,
                            "kind": kind,
                            "data": Binary(encoded),
                            "hash": hashlib.sha256(encoded).hexdigest(),
                        }
                    )
                    index += 1
            snapshots.update_one(
                {"_id": sid}, {"$set": {"ready": True, "chunks": index}}
            )
        except Exception:
            chunks.delete_many({"snapshot": sid})
            snapshots.delete_one({"_id": sid})
            raise
        return sid


def load_snapshot(project, snapshot_id):
    snapshots, chunks = collections()
    try:
        sid = ObjectId(snapshot_id)
    except (ValueError, TypeError) as error:
        raise LabelOperationError("快照编号无效") from error
    meta = snapshots.find_one({"_id": sid, "project": project.pk, "ready": True})
    if not meta or meta["version"] != VERSION:
        raise LabelOperationError("快照不存在或版本不兼容")
    data = {key: [] for key in ("files", "targets", "sources", "translations", "tips")}
    parts = list(chunks.find({"snapshot": sid}).sort("index", 1))
    if len(parts) != meta["chunks"]:
        raise LabelOperationError("快照数据不完整")
    for index, part in enumerate(parts):
        encoded = bytes(part["data"])
        if (
            part["index"] != index
            or hashlib.sha256(encoded).hexdigest() != part["hash"]
        ):
            raise LabelOperationError("快照完整性校验失败")
        data[part["kind"]].extend(BSON(encoded).decode()["rows"])
    if data_digest(data) != meta["digest"]:
        raise LabelOperationError("快照完整性校验失败")
    return data


def validate_restore(project, data):
    current = label_data(project)
    files = {row["_id"]: row for row in current["files"]}
    targets = {row["_id"]: row for row in current["targets"]}
    for row in data["files"]:
        if files.get(row["_id"]) != row:
            raise LabelOperationError("快照关联的文件已删除或被替换，不能完整恢复")
    for row in data["targets"]:
        if targets.get(row["_id"]) != row:
            raise LabelOperationError("快照关联的目标语言已删除或改变")


def replace_labels(project, data):
    from app.models.file import Source, Translation, Tip

    current_ids = [row["_id"] for row in label_data(project)["sources"]]
    # Raw collection writes do not run MongoEngine CASCADE rules.
    Translation._get_collection().delete_many({"o": {"$in": current_ids}})
    Tip._get_collection().delete_many({"o": {"$in": current_ids}})
    Source._get_collection().delete_many({"_id": {"$in": current_ids}})
    for cls, key in ((Source, "sources"), (Translation, "translations"), (Tip, "tips")):
        if data[key]:
            cls._get_collection().insert_many(data[key], ordered=True)


@contextmanager
def protected_operation(project, actor, reason):
    from app.models.project import Project

    with label_lock(project.pk):
        sid = create_snapshot(project, actor, reason=reason)
        Project.objects(id=project.pk).update(set__label_recovery=sid)
        try:
            yield sid
            rebuild_project_statistics(project)
        except Exception:
            # Keep the journal marker if rollback itself fails. No further
            # writes are permitted until the protection snapshot is recovered.
            replace_labels(project, load_snapshot(project, sid))
            rebuild_project_statistics(project)
            advance_revision(project)
            raise
        else:
            advance_revision(project)


def advance_revision(project):
    """Stamp every owned row before publishing the new project generation."""
    from app.models.file import File, FileTargetCache, Source, Translation, Tip
    from app.models.target import Target
    from app.models.project import Project

    project.reload()
    epoch = project.label_revision + 1
    files = list(File.objects(project=project).scalar("id"))
    sources = [
        row["_id"]
        for row in Source._get_collection().find({"f": {"$in": files}}, {"_id": 1})
    ]
    for cls, condition in (
        (File, {"p": project.pk}),
        (Target, {"t": project.pk}),
        (FileTargetCache, {"f": {"$in": files}}),
        (Source, {"f": {"$in": files}}),
        (Translation, {"o": {"$in": sources}}),
        (Tip, {"o": {"$in": sources}}),
    ):
        cls._get_collection().update_many(condition, {"$set": {"label_epoch": epoch}})
    Project.objects(id=project.pk).update(
        unset__label_recovery=1,
        set__label_revision=epoch,
        set__edit_time=datetime.datetime.utcnow(),
    )
    project.reload()


def restore_snapshot(project, actor, snapshot_id):
    with label_lock(project.pk):
        data = load_snapshot(project, snapshot_id)
        validate_restore(project, data)
        with protected_operation(project, actor, "restore") as sid:
            replace_labels(project, data)
        return sid


def recover_operation(project):
    with label_lock(project.pk, recovery=True):
        project.reload()
        if not project.label_recovery:
            raise LabelOperationError("项目没有待恢复操作")
        data = load_snapshot(project, project.label_recovery)
        validate_restore(project, data)
        replace_labels(project, data)
        rebuild_project_statistics(project)
        advance_revision(project)


def clear_labels(project, actor, field, delete_labels=False, clear_tips=False):
    from app.models.file import File, Source, Translation, Tip

    if field not in ("source", "translation", "proofread"):
        raise LabelOperationError("请选择有效栏位")
    if delete_labels and field == "proofread":
        raise LabelOperationError("清空校对不能删除标签")
    with label_lock(project.pk):
        data = label_data(project)
        if (
            delete_labels
            and field == "translation"
            and any(row.get("c", "") != "" for row in data["sources"])
        ):
            raise LabelOperationError("原文字段有内容，不能完全删除标签")
        source_ids = [row["_id"] for row in data["sources"]]
        condition = {"o": {"$in": source_ids}}
        with protected_operation(project, actor, "clear") as sid:
            if field in ("source", "translation"):
                Translation._get_collection().delete_many(condition)
            else:
                Translation._get_collection().update_many(
                    condition, {"$set": {"p": ""}, "$unset": {"pr": ""}}
                )
                # Selected state is intentionally preserved, including empty
                # translation placeholders selected before clearing proofread.
            if delete_labels or clear_tips:
                Tip._get_collection().delete_many(condition)
            if delete_labels:
                Source._get_collection().delete_many({"_id": {"$in": source_ids}})
            elif field == "source":
                text_files = list(File.objects(project=project, type=3).scalar("id"))
                Source._get_collection().update_many(
                    {"_id": {"$in": source_ids}},
                    {"$set": {"c": "", "pt": [], "e": datetime.datetime.utcnow()}},
                )
                Source._get_collection().update_many(
                    {"f": {"$in": text_files}}, {"$set": {"b": True}}
                )
        return sid


def merge_preview(project, target):
    from app.models.user import User

    data = label_data(project)
    rows = [row for row in data["translations"] if ref_id(row["t"]) == target.pk]
    groups = defaultdict(list)
    users = {}
    for row in rows:
        if row.get("p", "") and row["p"] != row.get("c", ""):
            uid = ref_id(row.get("pr"))
            if uid is None or not User.objects(id=uid).first():
                raise LabelOperationError("校对者账号缺失，不能合并")
            if uid not in users:
                users[uid] = User.objects(id=uid).first().name
            groups[ref_id(row["o"]), uid].append(row)
    labels = {row["_id"]: row for row in data["sources"]}
    candidates = []
    for (source, user), revisions in groups.items():
        unique = {}
        for row in revisions:
            unique.setdefault(row["p"], row)
        candidates.append(
            {
                "key": f"{source}:{user}",
                "source_id": str(source),
                "rank": labels[source]["r"],
                "source_content": labels[source].get("c", ""),
                "user_id": str(user),
                "user_name": users[user],
                "versions": [
                    {"id": str(row["_id"]), "content": row["p"]}
                    for row in unique.values()
                ],
            }
        )
    by_source = defaultdict(list)
    for candidate in candidates:
        by_source[candidate["source_id"]].append(candidate["user_id"])
    return {
        "token": data_digest(data),
        "candidates": candidates,
        "selection_conflicts": [
            {"source_id": source, "users": users}
            for source, users in by_source.items()
            if len(users) > 1
        ],
        "proofread_count": sum(bool(row.get("p")) for row in rows),
        "has_source_content": any(row.get("c", "") != "" for row in data["sources"]),
    }


def merge_labels(project, target, actor, token, versions, selections):
    from app.models.file import Translation

    with label_lock(project.pk):
        preview = merge_preview(project, target)
        if token != preview["token"]:
            raise LabelOperationError("预览后数据已改变，请重新确认冲突")
        chosen = []
        source_users = defaultdict(list)
        for candidate in preview["candidates"]:
            options = candidate["versions"]
            selected = versions.get(
                candidate["key"], options[0]["id"] if len(options) == 1 else None
            )
            option = next((row for row in options if row["id"] == selected), None)
            if option is None:
                raise LabelOperationError("请解决所有校对内容冲突")
            chosen.append((candidate, option))
            source_users[candidate["source_id"]].append(candidate["user_id"])
        winners = {}
        for source, users in source_users.items():
            winner = selections.get(source, users[0] if len(users) == 1 else None)
            if winner not in users:
                raise LabelOperationError("请选择每个标签最终选定的翻译")
            winners[source] = winner
        source_ids = [row["_id"] for row in label_data(project)["sources"]]
        with protected_operation(project, actor, "merge") as sid:
            now = datetime.datetime.utcnow()
            for candidate, option in chosen:
                Translation._get_collection().update_one(
                    {
                        "o": ObjectId(candidate["source_id"]),
                        "t": target.pk,
                        "u": ObjectId(candidate["user_id"]),
                    },
                    {
                        "$set": {"c": option["content"], "mt": False, "e": now},
                        "$setOnInsert": {"ct": now, "s": False, "p": ""},
                    },
                    upsert=True,
                )
            for source, winner in winners.items():
                Translation._get_collection().update_many(
                    {"o": ObjectId(source), "t": target.pk},
                    {"$set": {"s": False}, "$unset": {"sr": ""}},
                )
                Translation._get_collection().update_one(
                    {"o": ObjectId(source), "t": target.pk, "u": ObjectId(winner)},
                    {"$set": {"s": True, "sr": actor.pk}},
                )
            condition = {"o": {"$in": source_ids}, "t": target.pk}
            Translation._get_collection().update_many(
                condition, {"$set": {"p": ""}, "$unset": {"pr": ""}}
            )
            Translation._get_collection().delete_many(
                {**condition, "c": "", "s": {"$ne": True}}
            )
        return sid
