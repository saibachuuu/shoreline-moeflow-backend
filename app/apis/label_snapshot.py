from flask import request

from app.core.responses import MoePagination
from app.core.views import MoeAPIView
from app.decorators.auth import token_required
from app.decorators.url import fetch_model
from app.exceptions import NoPermissionError
from app.models.project import Project, ProjectPermission
from app.services.identity_permission import IdentityPermissionService
from app.services import label_snapshot as service


def capabilities(user, project):
    access = user.can(project, ProjectPermission.ACCESS)
    manager = access and IdentityPermissionService.is_project_manager(user, project)
    proofread = access and user.can(project, ProjectPermission.PROOFREAD_TRA)
    return {
        "create": access
        and (manager or proofread or user.can(project, ProjectPermission.OUTPUT_TRA)),
        "restore": bool(manager),
        "clear_proofread": bool(manager or proofread),
        "merge": bool(
            manager
            or (
                proofread
                and user.can(project, ProjectPermission.CHECK_TRA)
                and user.can(project, ProjectPermission.ADD_TRA)
            )
        ),
    }


def require(user, project, action):
    if not capabilities(user, project)[action]:
        raise NoPermissionError()


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise service.LabelOperationError("请求数据无效")
    return data


class ProjectSnapshotListAPI(MoeAPIView):
    @token_required
    @fetch_model(Project)
    def get(self, project):
        require(self.current_user, project, "create")
        snapshots, _ = service.collections()
        pagination = MoePagination(default_limit=20, max_limit=100)
        query = {"project": project.pk, "ready": True}
        rows = (
            snapshots.find(query)
            .sort([("created", -1), ("_id", -1)])
            .skip(pagination.skip)
            .limit(pagination.limit)
        )
        return pagination.set_data(
            [
                {
                    "id": str(row["_id"]),
                    "name": row["name"],
                    "created": row["created"].isoformat(),
                    "actor_name": row["actor_name"],
                    "reason": row["reason"],
                    "counts": row["counts"],
                }
                for row in rows
            ],
            snapshots.count_documents(query),
        )

    @token_required
    @fetch_model(Project)
    def post(self, project):
        require(self.current_user, project, "create")
        data = body()
        name = data.get("name", "")
        if not isinstance(name, str) or len(name) > 120:
            raise service.LabelOperationError("快照名称最多 120 个字符")
        return {
            "snapshot_id": str(
                service.create_snapshot(project, self.current_user, name)
            )
        }


class ProjectLabelStateAPI(MoeAPIView):
    @token_required
    @fetch_model(Project)
    def get(self, project):
        if not self.current_user.can(project, ProjectPermission.ACCESS):
            raise NoPermissionError()
        data = service.label_data(project)
        return {
            "capabilities": capabilities(self.current_user, project),
            "has_source_content": any(
                row.get("c", "") != "" for row in data["sources"]
            ),
            "counts": {key: len(rows) for key, rows in data.items()},
            "recovery_required": bool(project.label_recovery),
        }


class ProjectSnapshotRestoreAPI(MoeAPIView):
    @token_required
    @fetch_model(Project)
    def post(self, project, snapshot_id):
        require(self.current_user, project, "restore")
        return {
            "snapshot_id": str(
                service.restore_snapshot(project, self.current_user, snapshot_id)
            )
        }


class ProjectLabelClearAPI(MoeAPIView):
    @token_required
    @fetch_model(Project)
    def post(self, project):
        data = body()
        field = data.get("field")
        require(
            self.current_user,
            project,
            "clear_proofread" if field == "proofread" else "restore",
        )
        for key in ("delete_labels", "clear_tips"):
            if key in data and not isinstance(data[key], bool):
                raise service.LabelOperationError("清空选项必须为布尔值")
        return {
            "snapshot_id": str(
                service.clear_labels(
                    project,
                    self.current_user,
                    field,
                    data.get("delete_labels", False),
                    data.get("clear_tips", False),
                )
            )
        }


class ProjectLabelMergeAPI(MoeAPIView):
    @token_required
    @fetch_model(Project)
    def get(self, project):
        require(self.current_user, project, "merge")
        target = project.target_by_id(request.args.get("target_id"))
        return service.merge_preview(project, target)

    @token_required
    @fetch_model(Project)
    def post(self, project):
        require(self.current_user, project, "merge")
        data = body()
        target = project.target_by_id(data.get("target_id"))
        versions, selections = data.get("versions", {}), data.get("selections", {})
        if not isinstance(versions, dict) or not isinstance(selections, dict):
            raise service.LabelOperationError("冲突选择数据无效")
        return {
            "snapshot_id": str(
                service.merge_labels(
                    project,
                    target,
                    self.current_user,
                    data.get("token"),
                    versions,
                    selections,
                )
            )
        }


class ProjectLabelRecoverAPI(MoeAPIView):
    @token_required
    @fetch_model(Project)
    def post(self, project):
        require(self.current_user, project, "restore")
        service.recover_operation(project)
        return {"recovered": True}
