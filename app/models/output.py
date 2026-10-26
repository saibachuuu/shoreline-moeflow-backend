import os
from app.utils import default
from mongoengine.base.fields import ObjectIdField
from mongoengine.fields import ListField
from app.constants.output import OutputStatus, OutputTypes
from app.exceptions.output import OutputNotExistError
import datetime
from flask import current_app

from app.utils.logging import logger
from mongoengine import (
    DateTimeField,
    Document,
    IntField,
    LongField,
    ReferenceField,
    StringField,
)
from typing import List, TYPE_CHECKING
import oss2
from app import oss

if TYPE_CHECKING:
    from app.models.project import Project
    from app.models.target import Target
    from app.models.user import User


class Output(Document):
    """项目的导出"""

    project = ReferenceField("Project", db_field="p", required=True)
    target = ReferenceField("Target", db_field="t", required=True)
    user = ReferenceField("User", db_field="u")  # 操作人
    status = IntField(db_field="s", default=OutputStatus.QUEUING)
    type = IntField(db_field="ty", default=OutputTypes.ALL)
    # 注意这里不写 db_field：历史代码误写成 ``df_field="fn"``（不是合法参数，
    # 被 mongoengine 静默忽略），所以落库键一直就是 ``file_name``。补一个
    # ``db_field="fn"`` 反而会让存量文档全部读不到。
    file_name = StringField(default="")
    # 导出产物的实际字节数（zip 或纯文本 txt），精确值。
    storage_bytes = LongField(db_field="sb", default=0)
    create_time = DateTimeField(db_field="ct", default=datetime.datetime.utcnow)
    file_ids_include = ListField(ObjectIdField(), default=list)
    file_ids_exclude = ListField(ObjectIdField(), default=list)

    @classmethod
    def create(
        cls,
        /,
        *,
        project: "Project",
        target: "Target",
        user: "User",
        type: int,
        file_ids_include: List[str] = None,
        file_ids_exclude: List[str] = None,
    ) -> "Output":
        output = cls(
            project=project,
            target=target,
            user=user,
            type=type,
            file_ids_include=file_ids_include,
            file_ids_exclude=file_ids_exclude,
        ).save()
        return output

    @staticmethod
    def _storage_backend_for(output):
        """导出产物所在的后端：跟着项目走。

        注册表为空时直接返回全局 ``oss``，不碰 ``output.project``。
        """
        from app.services import storage_router

        if not storage_router.has_resolvers():
            return oss
        return storage_router.backend_for(output.project) or oss

    @classmethod
    def _delete_objects(cls, outputs):
        """按后端分组删除产物对象。

        产物跟着项目走，所以一批产物可能分散在多个后端（默认存储 + 若干池来源），
        不能再用一次 batch 调用。分组键用项目上的来源 id，而不是后端对象本身
        （每次解析都可能返回新实例，按身份分组会把同源拆成多组）。
        """
        prefix = current_app.config["OSS_OUTPUT_PREFIX"]
        groups = {}
        for output in outputs:
            backend = cls._storage_backend_for(output)
            group_key = str(getattr(output.project, "storage_source_id", "") or "")
            groups.setdefault(group_key, [backend, []])[1].append(output)
        for backend, group in groups.values():
            backend.delete(
                prefix,
                [str(output.id) + "/" + output.file_name for output in group],
            )
            rmdir = getattr(backend, "rmdir", None)
            if rmdir is not None:
                rmdir([os.path.join(prefix, str(output.id)) for output in group])
            # 对象真的删掉了才回退项目账，避免删除失败却把账扣了。
            cls._release_project_bytes(group)

    @classmethod
    def _release_project_bytes(cls, outputs):
        """产物删除后回退项目账（``output_bytes`` + ``file_size``）。

        幂等靠把 ``storage_bytes`` 归零：同一批产物被重复清理时第二次不会再扣。
        删失败的分组不会走到这里，宁可账面偏高也不要把没删掉的空间算成已释放。
        """
        from app.models.project import Project

        totals = {}
        touched = []
        for output in outputs:
            size = output.storage_bytes or 0
            if not size:
                continue
            project = getattr(output, "project", None)
            if project is None:
                continue
            totals[project.id] = totals.get(project.id, 0) + size
            touched.append(output.id)
            # 内存里的实例也归零，重复清理同一批对象时不会再扣第二次。
            output.storage_bytes = 0
        if not totals:
            return
        for project_id, size in totals.items():
            Project.objects(id=project_id).update(
                inc__output_bytes=-size,
                inc__file_size=-round(size / 1024),
            )
        cls.objects(id__in=touched).update(set__storage_bytes=0)

    @classmethod
    def delete_real_files(cls, outputs):
        try:
            cls._delete_objects(outputs)
        except oss2.exceptions.NoSuchKey as e:
            logger.error(e)
        except Exception as e:
            logger.error(e)

    @classmethod
    def delete_real_files_strict(cls, outputs):
        """Delete output objects and propagate storage failures.

        The legacy helper intentionally treats missing or unavailable storage
        as a best-effort cleanup.  Lifecycle clearing needs the opposite
        contract so it can leave the project retryable instead of reporting a
        successful destructive operation while objects remain.
        """

        outputs = list(outputs)
        if not outputs:
            return
        cls._delete_objects(outputs)

    def delete_real_file(self):
        try:
            self._delete_objects([self])
        except oss2.exceptions.NoSuchKey as e:
            logger.error(e)
        except Exception as e:
            logger.error(e)

    def clear(self):
        self.delete()

    @classmethod
    def by_id(cls, id):
        file = cls.objects(id=id).first()
        if file is None:
            raise OutputNotExistError
        return file

    def to_api(self):
        data = {
            "id": str(self.id),
            "project": self.project.to_api(),
            "target": self.target.to_api(),
            "user": default(self.user, None, "to_api"),
            "type": self.type,
            "status": self.status,
            # 产物字节数（精确）。0 表示历史数据未记录或还没导出完成。
            "storage_bytes": self.storage_bytes,
            "status_details": OutputStatus.to_api(),
            "file_ids_include": [str(id) for id in self.file_ids_include],
            "file_ids_exclude": [str(id) for id in self.file_ids_exclude],
            "create_time": self.create_time.isoformat(),
        }
        if self.status == OutputStatus.SUCCEEDED:
            # 导出产物跟着项目走：项目在存储池里时产物也在那儿。
            from app.services import storage_router

            backend = storage_router.backend_for(self.project) or oss
            data["link"] = backend.sign_url(
                current_app.config["OSS_OUTPUT_PREFIX"] + str(self.id) + "/",
                self.file_name,
                download=True,
            )
        return data
