from flask_babel import gettext

from app.core.views import MoeAPIView
from app.decorators.auth import token_required
from app.decorators.url import fetch_model
from app.constants.archive_import import ArchiveImportStatus
from app.constants.project import ProjectStatus
from app.exceptions import NoPermissionError
from app.exceptions.project import ProjectFinishedError
from app.models.archive_import import ArchiveImportTask
from app.models.project import Project, ProjectPermission
from app.tasks.archive_import import import_archive_from_gallery
from app.validators.archive_import import ArchiveImportSchema
from app.exceptions.base import ValidateError


class ArchiveImportAPI(MoeAPIView):
    """触发 / 重试 画廊归档导入任务"""

    @token_required
    @fetch_model(Project)
    def post(self, project: Project):
        """
        @api {post} /v1/projects/<project_id>/import-from-archive
            触发从画廊归档导入图片
        @apiVersion 1.0.0
        @apiName postArchiveImportAPI
        @apiGroup Project
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiParam {String} gid 画廊 gid
        @apiParam {String} token 画廊 token
        @apiParam {String} [gallery_url] 原始画廊 URL（展示用）

        @apiSuccessExample {json} 返回示例
        {
            "message": "归档导入任务已加入队列",
            "task": {}
        }
        """
        if project.status != ProjectStatus.WORKING:
            raise ProjectFinishedError
        if not self.current_user.can(project, ProjectPermission.ADD_FILE):
            raise NoPermissionError(gettext("您没有此项目的上传文件权限"))
        data = self.get_json(ArchiveImportSchema())
        # 单任务去重：进行中/刚完成的任务存在时不允许重复触发
        existing = ArchiveImportTask.objects(
            project=project, status__in=list(ArchiveImportStatus.RUNNING)
        ).first()
        if existing is not None:
            raise ValidateError(gettext("该项目已有归档导入任务进行中"))
        task = ArchiveImportTask.create(
            project=project,
            user=self.current_user,
            gid=data["gid"],
            token=data["token"],
            gallery_url=data.get("gallery_url") or "",
        )
        import_archive_from_gallery(
            str(project.id),
            data["gid"],
            data["token"],
            task_id=str(task.id),
        )
        return {
            "message": gettext("归档导入任务已加入队列"),
            "task": task.to_api(),
        }


class ArchiveImportTaskAPI(MoeAPIView):
    """查询项目的归档导入任务状态（前端轮询）"""

    @token_required
    @fetch_model(Project)
    def get(self, project: Project):
        """
        @api {get} /v1/projects/<project_id>/import-task 获取归档导入任务状态
        @apiVersion 1.0.0
        @apiName getArchiveImportTaskAPI
        @apiGroup Project
        @apiUse APIHeader
        @apiUse TokenHeader
        """
        if not self.current_user.can(project, ProjectPermission.ACCESS):
            raise NoPermissionError(gettext("您没有此项目的访问权限"))
        task = ArchiveImportTask.latest(project)
        return {"task": task.to_api() if task is not None else None}

    @token_required
    @fetch_model(Project)
    def post(self, project: Project):
        """
        @api {post} /v1/projects/<project_id>/import-task/dismiss
            关闭项目的归档导入提示(永久,存库)
        @apiVersion 1.0.0
        @apiName postArchiveImportTaskDismissAPI
        @apiGroup Project
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiSuccessExample {json} 返回示例
        {
            "message": "归档导入提示已关闭",
            "dismissed": true
        }
        """
        if not self.current_user.can(project, ProjectPermission.ACCESS):
            raise NoPermissionError(gettext("您没有此项目的访问权限"))
        # 一次性关闭该项目所有非运行中的历史任务提示,
        # 避免「关掉最新一条又冒出更早一条」;运行中的任务不受影响。
        ArchiveImportTask.objects(
            project=project, status__nin=list(ArchiveImportStatus.RUNNING)
        ).update(set__dismissed=True)
        return {
            "message": gettext("归档导入提示已关闭"),
            "dismissed": True,
        }
