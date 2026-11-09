"""Rebuild caches from primary records, never from directory rollups."""

from collections import defaultdict

from app.constants.file import FileType


def ref_id(value):
    return getattr(value, "id", value)


def rebuild_project_statistics(project, *, apply=True):
    from app.models.file import File, FileTargetCache, Source, Translation
    from app.models.output import Output
    from app.models.project import Project
    from app.models.target import Target

    project.reload()
    files = list(File._get_collection().find({"p": project.pk}))
    by_id = {row["_id"]: row for row in files}
    file_ids = list(by_id)
    targets = list(Target._get_collection().find({"t": project.pk}))
    target_ids = {row["_id"] for row in targets}
    sources = list(Source._get_collection().find({"f": {"$in": file_ids}}))
    by_source = {row["_id"]: row for row in sources}
    translated, checked = defaultdict(set), defaultdict(set)
    # Source reference and selection are different fields.
    source_key = Translation._fields["source"].db_field
    for row in Translation._get_collection().find(
        {source_key: {"$in": list(by_source)}}
    ):
        source = by_source[ref_id(row[source_key])]
        target = ref_id(row[Translation._fields["target"].db_field])
        if source.get("b", False) or target not in target_ids:
            continue
        key = (ref_id(source["f"]), target)
        translated[key].add(source["_id"])
        if row.get(Translation._fields["selected"].db_field, False):
            checked[key].add(source["_id"])

    counts = {key: dict(fo=0, fc=0, fs=0, sc=0, tsc=0, csc=0) for key in by_id}
    source_counts = defaultdict(int)
    for row in sources:
        if not row.get("b", False):
            source_counts[ref_id(row["f"])] += 1
    cache_counts = defaultdict(lambda: [0, 0])
    totals = dict(fo=0, fc=0, sc=0, tsc=0, csc=0, tc=len(targets))
    target_totals = defaultdict(lambda: [0, 0])
    for row in files:
        fid = row["_id"]
        if row["t"] == FileType.FOLDER:
            if row.get("ac", True):
                totals["fo"] += 1
                for ancestor in row.get("a", []):
                    if ref_id(ancestor) in counts:
                        counts[ref_id(ancestor)]["fo"] += 1
            continue
        own = counts[fid]
        own["fs"] = row.get("fs", 0)
        own["sc"] = source_counts[fid]
        for tid in target_ids:
            values = [len(translated[fid, tid]), len(checked[fid, tid])]
            cache_counts[fid, tid] = values
            own["tsc"] += values[0]
            own["csc"] += values[1]
            if row.get("ac", True):
                target_totals[tid][0] += values[0]
                target_totals[tid][1] += values[1]
                for ancestor in row.get("a", []):
                    if ref_id(ancestor) in by_id:
                        rollup = cache_counts[ref_id(ancestor), tid]
                        rollup[0] += values[0]
                        rollup[1] += values[1]
        if not row.get("ac", True):
            continue
        totals["fc"] += 1
        for field in ("sc", "tsc", "csc"):
            totals[field] += own[field]
        for ancestor in row.get("a", []):
            if ref_id(ancestor) not in counts:
                raise ValueError("文件引用了不存在的祖先目录")
            counts[ref_id(ancestor)]["fc"] += 1
            for field in ("fs", "sc", "tsc", "csc"):
                counts[ref_id(ancestor)][field] += own[field]

    stored = [row for row in files if row["t"] != FileType.FOLDER and row.get("sa")]
    totals["cover_bytes"] = sum(row.get("cover_bytes", 0) for row in stored)
    totals["resample_bytes"] = sum(row.get("resample_bytes", 0) for row in stored)
    totals["ob"] = sum(
        row.get("sb", 0)
        for row in Output._get_collection().find(
            {"p": project.pk, "file_name": {"$gt": ""}}
        )
    )
    original_bytes = sum(row.get("fs", 0) * 1024 for row in stored)
    total_bytes = (
        original_bytes + totals["cover_bytes"] + totals["resample_bytes"] + totals["ob"]
    )
    totals["fs"] = round(total_bytes / 1024)
    if apply:
        for fid, values in counts.items():
            File._get_collection().update_one({"_id": fid}, {"$set": values})
        for fid in by_id:
            for tid in target_ids:
                values = cache_counts[fid, tid]
                FileTargetCache._get_collection().update_one(
                    {"f": fid, "t": tid},
                    {
                        "$set": {"ts": values[0], "cs": values[1]},
                        "$setOnInsert": {"label_epoch": project.label_revision},
                    },
                    upsert=True,
                )
        for tid in target_ids:
            Target._get_collection().update_one(
                {"_id": tid},
                {"$set": {"tsc": target_totals[tid][0], "csc": target_totals[tid][1]}},
            )
        Project._get_collection().update_one({"_id": project.pk}, {"$set": totals})
        project.reload()
    return {
        **totals,
        "output_bytes": totals["ob"],
        "original_bytes": original_bytes,
        "total_bytes": total_bytes,
    }
