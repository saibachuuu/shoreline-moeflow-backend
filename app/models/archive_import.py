from __future__ import annotations

import datetime
from mongoengine import (
    BooleanField,
    DateTimeField,
    Document,
    IntField,
    ReferenceField,
    StringField,
)
from typing import TYPE_CHECKING

from app.constants.archive_import import ArchiveImportStatus

if TYPE_CHECKING:
    from app.models.project import Project
    from app.models.user import User


class ArchiveImportTask(Document):
    """从画廊归档 URL 导入图片的项目任务（每个项目可有多条历史记录）。"""

    project = ReferenceField("Project", db_field="p", required=True)
    user = ReferenceField("User", db_field="u")  # 触发人
    gid = StringField(db_field="g", default="")
    token = StringField(db_field="t", default="")
    gallery_url = StringField(db_field="gu", default="")  # 原始画廊 URL（展示/排查用）
    status = IntField(db_field="s", default=ArchiveImportStatus.QUEUED)
    stage = StringField(db_field="st", default="")  # 供前端展示的当前阶段文案
    total_pages = IntField(db_field="n", default=0)
    completed_pages = IntField(db_field="c", default=0)
    error = StringField(db_field="e", default="")
    zip_url = StringField(db_field="zu", default="")
    dismissed = BooleanField(db_field="d", default=False)  # 用户已关闭该任务的提示(前端不再展示)
    create_time = DateTimeField(db_field="ct", default=datetime.datetime.utcnow)
    update_time = DateTimeField(db_field="ut", default=datetime.datetime.utcnow)

    @classmethod
    def create(
        cls,
        /,
        *,
        project: "Project",
        user: "User" | None = None,
        gid: str,
        token: str,
        gallery_url: str = "",
    ) -> "ArchiveImportTask":
        task = cls(
            project=project,
            user=user,
            gid=str(gid),
            token=str(token),
            gallery_url=gallery_url,
            status=ArchiveImportStatus.QUEUED,
        ).save()
        return task

    @classmethod
    def latest(cls, project: "Project") -> "ArchiveImportTask | None":
        # 按 -id 排序（ObjectId 单调递增），比 create_time 毫秒级时间戳更可靠
        return cls.objects(project=project).order_by("-id").first()

    def set_progress(
        self, /, *, status=None, stage=None, total=None, completed=None, error=None, zip_url=None
    ) -> None:
        """小写频繁写一次的进度/状态更新（不触发 save 的并发覆盖）。"""
        fields = {}
        if status is not None:
            fields["status"] = int(status)
        if stage is not None:
            fields["stage"] = stage
        if total is not None:
            fields["total_pages"] = max(0, int(total))
        if completed is not None:
            fields["completed_pages"] = max(0, int(completed))
        if error is not None:
            fields["error"] = error[:2000]
        if zip_url is not None:
            fields["zip_url"] = zip_url
        if fields:
            fields["update_time"] = datetime.datetime.utcnow()
            self.update(**fields)

    def claim(self) -> bool:
        """Atomically move a queued task to resolving exactly once."""

        changed = ArchiveImportTask.objects(
            id=self.id, status=ArchiveImportStatus.QUEUED
        ).update_one(
            set__status=ArchiveImportStatus.RESOLVING,
            set__stage="解析归档链接",
            set__update_time=datetime.datetime.utcnow(),
        )
        if changed:
            self.reload()
        return bool(changed)

    def to_api(self) -> dict:
        return {
            "id": str(self.id),
            "project_id": str(self.project.id),
            "gid": self.gid,
            "token": self.token,
            "gallery_url": self.gallery_url,
            "status": self.status,
            "status_name": ArchiveImportStatus.get_detail_by_value(
                self.status, "name", ""
            ),
            "stage": self.stage,
            "total_pages": self.total_pages,
            "completed_pages": self.completed_pages,
            "error": self.error,
            "dismissed": bool(getattr(self, "dismissed", False)),
            "create_time": self.create_time.isoformat(),
            "update_time": self.update_time.isoformat(),
        }

    def clear(self):
        self.delete()
