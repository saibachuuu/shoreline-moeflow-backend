"""Directory-local manual image order and export filename mapping."""

import datetime
import hashlib
import json

from flask_babel import gettext
from mongoengine import Q
from pymongo import UpdateOne

from app.constants.file import FileType
from app.constants.project import ProjectStatus
from app.exceptions import NoPermissionError
from app.exceptions.base import ValidateError
from app.models.file import File
from app.services.identity_permission import IdentityPermissionService


def can_order_files(user, project, snapshot=None):
    if user is None or project.status != ProjectStatus.WORKING:
        return False
    if snapshot is None:
        snapshot = IdentityPermissionService.project_snapshot(user, project)
    # Built-in identity provenance, NOT generic CHANGE/MANAGE_MEMBERS rights.
    # Reusing the snapshot keeps project-list serialization query-bounded and
    # automatically excludes invited/removed project identities.
    allowed_sources = {
        "team_inherited:creator",
        "team_inherited:admin",
        "project_tag:creator",
        "project_tag:admin",
    }
    return snapshot.has("project:ACCESS") and any(
        allowed_sources.intersection(sources)
        for sources in snapshot.permission_sources.values()
    )


def order_snapshot(project):
    # The current project screen displays the root directory only.
    files = list(project.files(parent=None, type_only=FileType.IMAGE))
    version = hashlib.sha256(
        json.dumps(
            [[str(f.id), f.name, f.manual_order] for f in files], ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()
    return files, version


def save_order(project, user, file_ids, version, *, reset_to_default=False):
    from app.models.project import Project

    if not can_order_files(user, project):
        raise NoPermissionError
    now = datetime.datetime.utcnow()
    # Serialize reorder requests across processes; expired leases recover crashes.
    locked = Project.objects(
        Q(file_order_lock=None) | Q(file_order_lock__lt=now),
        id=project.id,
    ).modify(set__file_order_lock=now + datetime.timedelta(minutes=5), new=True)
    if locked is None:
        raise ValidateError(gettext("文件顺序正在保存，请稍后重试"))
    try:
        files, current_version = order_snapshot(project)
        if current_version != version:
            raise ValidateError(gettext("文件列表或顺序已变化，请取消编辑后重新加载"))
        if len(file_ids) != len(set(file_ids)) or set(file_ids) != {
            str(f.id) for f in files
        }:
            raise ValidateError(gettext("必须提供当前目录全部图片，且不可重复"))
        if reset_to_default:
            # Clear persistence, not just sort current names into new manual ranks.
            # Version/set validation and the same admin guard apply to resets.
            File.objects(
                project=project,
                parent=None,
                type=FileType.IMAGE,
                activated=True,
                id__in=[file.id for file in files],
            ).update(unset__manual_order=1)
            return
        # Merely entering edit mode and saving must not enable manual ordering.
        if file_ids == [str(f.id) for f in files]:
            return
        by_id = {str(f.id): f for f in files}
        operations = [
            UpdateOne(
                {"_id": by_id[file_id].id, "p": project.id, "ac": True},
                {"$set": {"mo": index}},
            )
            for index, file_id in enumerate(file_ids, 1)
        ]
        if operations:
            File._get_collection().bulk_write(operations, ordered=True)
    finally:
        Project.objects(
            id=project.id, file_order_lock=locked.file_order_lock
        ).update_one(
            unset__file_order_lock=1,
        )


def export_file_names(files):
    """Use one mapping for pictures and LabelPlus; never rename stored files.

    Once any selected image has manual order, number the entire exported set,
    including legacy/unordered directories, so no bare name disrupts sorting.
    """
    if not any(file.manual_order is not None for file in files):
        return {}
    width = max(4, len(str(len(files))))
    return {
        str(file.id): f"{index:0{width}d}_{file.name}"
        for index, file in enumerate(files, 1)
    }


def image_neighbors(file):
    """Indexed neighbors, with the same order as the directory list."""
    images = File.objects(
        project=file.project, parent=file.parent, type=FileType.IMAGE, activated=True
    )
    if not images.filter(manual_order__ne=None).only("id").first():
        # Preserve the old name-only behavior in untouched directories.
        return (
            images.filter(sort_name__lt=file.sort_name).order_by("-sort_name").first(),
            images.filter(sort_name__gt=file.sort_name).order_by("sort_name").first(),
        )
    same_rank = Q(manual_order=file.manual_order)
    before = same_rank & (
        Q(sort_name__lt=file.sort_name) | Q(sort_name=file.sort_name, id__lt=file.id)
    )
    after = same_rank & (
        Q(sort_name__gt=file.sort_name) | Q(sort_name=file.sort_name, id__gt=file.id)
    )
    if file.manual_order is None:
        after |= Q(manual_order__ne=None)
    else:
        before |= Q(manual_order=None) | Q(manual_order__lt=file.manual_order)
        after |= Q(manual_order__gt=file.manual_order)
    return (
        images.filter(before).order_by("-manual_order", "-sort_name", "-id").first(),
        images.filter(after).order_by("manual_order", "sort_name", "id").first(),
    )
