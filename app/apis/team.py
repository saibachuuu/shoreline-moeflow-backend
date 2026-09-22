from marshmallow import ValidationError
from app.exceptions.auth import UserNotExistError
from app.exceptions.project import ProjectNotExistError
from typing import List
from app.exceptions.team import OnlyAllowAdminCreateTeamError
from app.models.site_setting import SiteSetting
from app.models.user import User
from app.models.language import Language
from flask import json, request, current_app
from flask_babel import gettext
import uuid

from app.core.responses import MoePagination
from app.core.views import MoeAPIView
from app.decorators.auth import token_required
from app.decorators.url import fetch_model
from app.exceptions import NoPermissionError, RequestDataEmptyError
from app.models.project import Project
from app.models.team import Team, TeamPermission
from app.constants.project import ProjectStatus
from app.tasks.output_team_projects import output_team_projects
from app.validators.project import (
    CreateProjectSchema,
    ImportProjectSchema,
    TeamInsightProjectListSchema,
    TeamInsightUserListSchema,
)
from app.validators.team import CreateTeamSchema, EditTeamSchema
from app.models.project import ProjectRole, ProjectSet
from app.services.project_member import ProjectMemberService
from app.services.team_member import TeamMemberService
from app.services.identity_permission import IdentityPermissionService
from app.validators.project import ProjectSetsSchema
from app.utils.secrets import decrypt_secret, encrypt_secret
from app.utils.external_url import normalize_external_api_url as normalize_archive_api_url
from app.exceptions.base import ValidateError
from app.modules import notify_project_created


def getLanguageByCode(code):
    lang = Language.by_code(code)
    return lang.id


def _project_with_member_summary(project, user):
    """Serialize a single created/imported project with its member summary.

    List endpoints fill ``member_summary`` per page; single-project responses
    (create/import/lifecycle/detail) must do the same so the frontend member
    stats never render with an undefined summary.
    """
    data = project.to_api(user=user)
    data["member_summary"] = ProjectMemberService.member_summaries([project]).get(
        str(project.id), []
    )
    return data


def _apply_archive_api_keys(team, submitted):
    """合并前端提交的归档 API key 列表。

    提交项语义：
    - 含 id（可选带 key）→ 更新现有项（remark/enabled；带 key 则替换明文）
    - 不含 id 且含 key → 新增项
    - 现有项中未被提交引用的 id → 删除
    """
    existing = {
        str(item.get("id")): dict(item)
        for item in (team.archive_api_keys or [])
        if isinstance(item, dict) and item.get("id")
    }
    result = []
    seen_ids = set()
    for raw in submitted:
        if not isinstance(raw, dict):
            raise ValidateError(gettext("archive_api_keys 必须是对象数组"))
        item_id = str(raw.get("id") or "").strip()
        has_key = raw.get("key") is not None
        if item_id:
            seen_ids.add(item_id)
            base = existing.get(item_id)
            if base is None:
                raise ValidateError(gettext("archive_api_keys 包含不存在的 id"))
            base["remark"] = str(raw.get("remark", base.get("remark", "")) or "")[:200]
            base["enabled"] = raw.get("enabled", base.get("enabled", True)) is not False
            if has_key:
                new_key = str(raw["key"]).strip()
                if not new_key:
                    raise ValidateError(gettext("archive API key 不能为空"))
                base["key"] = encrypt_secret(new_key[:512])
            elif base.get("key"):
                # Re-save legacy plaintext values in encrypted form whenever a
                # team settings update touches the key list.
                try:
                    base["key"] = encrypt_secret(decrypt_secret(base["key"]))
                except ValueError as exc:
                    raise ValidateError(gettext(str(exc))) from exc
            result.append(base)
        elif has_key:
            new_key = str(raw["key"]).strip()
            if not new_key:
                raise ValidateError(gettext("archive API key 不能为空"))
            result.append(
                {
                    "id": str(uuid.uuid4()),
                    "key": encrypt_secret(new_key[:512]),
                    "remark": str(raw.get("remark") or "")[:200],
                    "enabled": raw.get("enabled", True) is not False,
                }
            )
        else:
            raise ValidateError(gettext("archive_api_keys 每项需要 id 或 key"))
    return result


class TeamListAPI(MoeAPIView):
    @token_required
    def get(self):
        """
        @api {get} /v1/teams?word=<word> 获取团队列表
        @apiVersion 1.0.0
        @apiName get_team_list
        @apiGroup Team
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiParam word 搜索关键词

        @apiSuccessExample {json} 返回示例
        {

        }
        """
        # 暂时不开放此接口，只能通过 ID 加入申请某个团队
        if not current_app.config["TESTING"]:
            return
        query = self.get_query()
        # word 不可为空
        if "word" not in query or query["word"] == "":
            raise RequestDataEmptyError
        p = MoePagination()
        objects = (
            Team.objects(name__icontains=query["word"]).skip(p.skip).limit(p.limit)
        )
        # 检测自己是否加入了这个团队，并返回 joined 值
        data = []
        my_teams = self.current_user.teams()
        for o in objects:
            item = o.to_api()
            if o in my_teams:
                item["joined"] = True
            else:
                item["joined"] = False
            data.append(item)
        p.set_data(data=data, count=objects.count())
        return p

    @token_required
    def post(self):
        """
        @api {post} /v1/teams 新建团队
        @apiVersion 1.0.0
        @apiName post_team_list
        @apiGroup Team
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiParam {String} name 团队名
        @apiParam {String} intro 团队介绍
        @apiParam {String} allow_apply_type 允许谁申请加入（通过/types接口获取）
        @apiParam {String} application_check_type 如何处理加入申请（通过/types接口获取）
        @apiParam {String} default_role 加入后默认角色（通过/types接口获取）
        @apiParamExample {json} 请求示例
        {
            "name":"123123"
        }

        @apiSuccess {String} msg 提示消息
        @apiSuccessExample {json} 返回示例
        {
            "message": "创建成功",
            "team": {}
        }

        @apiUse ValidateError
        """
        if (
            SiteSetting.get().only_allow_admin_create_team
            and not self.current_user.admin
        ):
            raise OnlyAllowAdminCreateTeamError
        # 处理请求数据
        data = self.get_json(CreateTeamSchema())
        # 创建团队
        team = Team.create(
            data["name"],
            creator=self.current_user,
            default_role=data["default_role"],
            allow_apply_type=data["allow_apply_type"],
            application_check_type=data["application_check_type"],
            intro=data["intro"],
        )
        return {
            "message": gettext("创建成功"),
            "team": team.to_api(user=self.current_user),
        }


class TeamAPI(MoeAPIView):
    @token_required
    @fetch_model(Team)
    def get(self, team):
        """
        @api {get} /v1/teams/<team_id> 获取某个团队的信息
        @apiVersion 1.0.0
        @apiName get_team
        @apiGroup Team
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiSuccessExample {json} 返回示例
        {

        }
        """
        if not self.current_user.can(team, TeamPermission.ACCESS):
            raise NoPermissionError
        return team.to_api(user=self.current_user)

    @token_required
    @fetch_model(Team)
    def put(self, team):
        """
        @api {put} /v1/teams/<team_id> 修改团队
        @apiVersion 1.0.0
        @apiName put_team
        @apiGroup Team
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiParam {String} team_id 团队id
        @apiParamExample {json} 请求示例
        {
            "name":"123123"
        }

        @apiSuccess {String} msg 提示消息
        @apiSuccessExample {json} 返回示例
        {
            "message": "创建成功"
        }

        @apiUse ValidateError
        """
        # 检查是否有访问权限
        if not self.current_user.can(team, TeamPermission.CHANGE):
            raise NoPermissionError
        data = self.get_json(EditTeamSchema(), context={"team": team})
        # 工作人员资格校验模式只允许团队创建者修改，管理员不能绕过
        # 资格体系或让非成员任意进入职位。
        if (
            "worker_qualification_mode" in data
            and not IdentityPermissionService.is_team_creator(self.current_user, team)
        ):
            raise NoPermissionError(
                gettext("只有团队创建者可以修改工作人员资格校验设置")
            )
        if "archive_api_keys" in data:
            data["archive_api_keys"] = _apply_archive_api_keys(
                team, data["archive_api_keys"]
            )
        if "archive_api_url" in data:
            raw_api_url = (data["archive_api_url"] or "").strip()
            if raw_api_url:
                try:
                    data["archive_api_url"] = normalize_archive_api_url(
                        raw_api_url,
                        allowed_hosts=current_app.config.get(
                            "ARCHIVE_PROVIDER_API_ALLOWED_HOSTS", ()
                        ),
                        require_allowlist=True,
                    )
                except ValueError as exc:
                    raise ValidateError(gettext(str(exc))) from exc
            else:
                data["archive_api_url"] = ""
        if data:
            team.update(**data)
            team.reload()
            return {
                "message": gettext("修改成功"),
                "team": team.to_api(user=self.current_user),
            }
        else:
            raise RequestDataEmptyError

    @token_required
    @fetch_model(Team)
    def delete(self, team):
        """
        @api {delete} /v1/teams/<team_id> 解散团队
        @apiVersion 1.0.0
        @apiName delete_team
        @apiGroup Team
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiSuccess {String} msg 提示消息
        @apiSuccessExample {json} 返回示例
        {
            "message": "解散成功"
        }

        @apiUse ValidateError
        """
        # 检查是否有访问权限
        if not self.current_user.can(team, TeamPermission.DELETE):
            raise NoPermissionError
        # 检查是否有未完结的项目
        if team.projects(status=ProjectStatus.WORKING).count() > 0:
            raise NoPermissionError(
                gettext("此团队含有未完结的项目，不能解散（请先完结或者转移项目）")
            )
        team.clear()
        return {"message": gettext("解散成功")}


class TeamProjectListAPI(MoeAPIView):
    @token_required
    @fetch_model(Team)
    def get(self, team):
        """
        # noqa: E501
        @api {get} /v1/teams/<team_id>/projects?project_sets=<project_sets>&word=<word>&mode=<mode>&role=<role>&worker_name=<worker_name> 获取团队的所有项目
        @apiVersion 1.0.0
        @apiName get_team_project
        @apiGroup Team
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiParam {String} team_id 团队id

        @apiParam {Number} [status] 项目状态，可以传递多个，不传则为所有的，支持以下参数
            - 0  # 进行中的项目
            - 1  # 完成了的项目
            - 2  # 计划完成
            - 3  # 计划删除
        @apiParam {String} [project_set] 所在项目集id
        @apiParam {String[]} [project_sets] 搜索的项目集id，可传递多个
        @apiParam {String} [word] 模糊查询的名称
        @apiParam {String} [mode] 搜索模式: search-project-name / search-worker
        @apiParam {String} [role] 限定职位(英文key): provider/scan/scan_retoucher/translator/proofreader/picture_editor
        @apiParam {String} [worker_name] 人员名称

        @apiSuccessExample {json} 返回示例
        {

        }
        """
        mode = request.args.get("mode")
        if mode is None:
            mode = (
                "search-worker"
                if request.args.get("tag") or request.args.get("worker_name")
                else "search-project-name"
            )
        if mode not in ("search-project-name", "search-worker"):
            from app.exceptions.identity import InvalidIdentityRequestError

            raise InvalidIdentityRequestError("invalid project search mode")
        raw_status = request.args.getlist("status")
        status_tokens = []
        for value in raw_status:
            status_tokens.extend(value.split(","))
        status = [token.strip() for token in status_tokens if token.strip()]
        project_sets = request.args.getlist("project_sets")
        expanded_project_sets = [
            part.strip()
            for value in project_sets
            for part in value.split(",")
            if part.strip()
        ]
        single_project_set = request.args.get("project_set")
        if single_project_set:
            expanded_project_sets.append(single_project_set)
        unique_project_set_ids = list(dict.fromkeys(expanded_project_sets))
        if expanded_project_sets:
            from bson import ObjectId

            valid_ids = []
            for value in unique_project_set_ids:
                try:
                    valid_ids.append(ObjectId(value))
                except (TypeError, ValueError):
                    continue
            project_sets = (
                list(ProjectSet.objects(id__in=valid_ids, team=team))
                if len(valid_ids) == len(unique_project_set_ids)
                else []
            )
            if len(project_sets) != len(unique_project_set_ids):
                from app.exceptions import ProjectSetNotExistError

                raise ProjectSetNotExistError
        else:
            project_sets = None

        projects = ProjectMemberService.search_team_projects(
            team,
            self.current_user,
            mode=mode,
            word=request.args.get("word"),
            worker_name=request.args.get("worker_name"),
            tag=request.args.get("tag"),
            status=status or None,
            project_set=None,
            project_sets=project_sets,
        )
        p = MoePagination()
        paged_projects = list(projects[p.skip : p.skip + p.limit].select_related())
        data = Project.batch_to_list_api(
            paged_projects,
            self.current_user,
            team=team,
        )
        member_summaries = ProjectMemberService.member_summaries(
            paged_projects, compact=True
        )
        for item in data:
            item["member_summary"] = member_summaries.get(item["id"], [])
        return p.set_data(data=data, count=projects.count())

    @token_required
    @fetch_model(Team)
    def post(self, team):
        """
        @api {post} /v1/teams/<team_id>/projects 新建项目
        @apiVersion 1.0.0
        @apiName add_project
        @apiGroup Project
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiParam {String} name 项目名
        @apiParamExample {json} 请求示例
        {
            "name":"123123",
            "intro": "",
            "allow_apply_type": 2,
            "application_check_type": 1,
            "default_role": "Object ID",
            "project_set": "Object ID"
        }

        @apiSuccess {String} msg 提示消息
        @apiSuccessExample {json} 返回示例
        {
            "message": "创建成功",
            "project": {}
        }

        @apiUse ValidateError
        """
        # 检查用户权限
        if not self.current_user.can(team, TeamPermission.CREATE_PROJECT):
            raise NoPermissionError(gettext("您没有权限在这个团队创建项目"))
        # 处理请求数据
        data = self.get_json(CreateProjectSchema(), context={"team": team})
        # 创建项目
        project = Project.create(
            name=data["name"],
            team=team,
            project_set=data["project_set"],
            creator=self.current_user,
            default_role=data["default_role"],
            allow_apply_type=data["allow_apply_type"],
            application_check_type=data["application_check_type"],
            intro=data["intro"],
            source_language=data["source_language"],
            target_languages=data["target_languages"],
            labelplus_txt=data["labelplus_txt"],
        )
        # 通知可选模块（核心不认识任何具体模块）
        notify_project_created(project)
        return {
            "message": gettext("创建成功"),
            "project": _project_with_member_summary(project, self.current_user),
        }


class TeamProjectOutputListAPI(MoeAPIView):
    @token_required
    @fetch_model(Team)
    def post(self, team: Team):
        if not self.current_user.can(team, TeamPermission.AUTO_BECOME_PROJECT_ADMIN):
            raise NoPermissionError
        output_team_projects(str(team.id), str(self.current_user.id))
        return {"message": gettext("创建导出任务成功")}


class TeamProjectImportAPI(MoeAPIView):
    """
    Create new Project in current ProjectSet/Team, from exported Project JSON
    """

    @token_required
    @fetch_model(Team)
    @fetch_model(ProjectSet)
    def post(self, team, project_set):
        # 检查用户权限
        if not self.current_user.can(team, TeamPermission.CREATE_PROJECT):
            raise NoPermissionError(gettext("您没有权限在这个团队创建项目"))
        project_json_file = request.files["project"]
        project_json_data = json.load(project_json_file)
        project_json_data["project_set"] = str(project_set.id)
        project_json_data["default_role"] = str(
            ProjectRole.by_system_code(project_json_data["default_role"]).id
        )
        labelplus_file = request.files["labelplus"]
        labelplus_txt = labelplus_file.read().decode("utf-8")

        # 处理请求数据
        schema = ImportProjectSchema()
        schema.context = {"team": team}
        try:
            data = schema.load(project_json_data)
        except ValidationError as e:
            # 合并多个验证器对于同一字段的相同错误
            for key in e.messages.keys():
                e.messages[key] = list(set(e.messages[key]))
            raise ValidateError(e.messages, replace=True)
        # 创建项目
        project = Project.create(
            name=data["name"],
            team=team,
            project_set=data["project_set"],
            creator=self.current_user,
            default_role=data["default_role"],
            allow_apply_type=data["allow_apply_type"],
            application_check_type=data["application_check_type"],
            intro=data["intro"],
            source_language=data["source_language"],
            target_languages=[data["output_language"]],
            labelplus_txt=labelplus_txt,
        )
        # 通知可选模块（核心不认识任何具体模块）
        notify_project_created(project)
        return {
            "message": gettext("创建成功"),
            "project": _project_with_member_summary(project, self.current_user),
        }


class TeamProjectSetListAPI(MoeAPIView):
    @token_required
    @fetch_model(Team)
    def get(self, team):
        """
        @api {get} /v1/teams/<team_id>/project-sets?word=<word> 获取团队的所有项目集
        @apiVersion 1.0.0
        @apiName get_team_project_sets
        @apiGroup Team
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiParam {String} team_id 团队id
        @apiParam {String} [word] 模糊查询的名称

        @apiSuccessExample {json} 返回示例
        {

        }
        """
        # 检查是否有访问团队权限
        if not self.current_user.can(team, TeamPermission.ACCESS):
            raise NoPermissionError
        # 获取查询参数
        word = request.args.get("word")
        # 分页
        p = MoePagination()
        objects = team.project_sets(skip=p.skip, limit=p.limit, word=word)
        return p.set_objects(objects, func="to_list_api")

    @token_required
    @fetch_model(Team)
    def post(self, team):
        """
        @api {post} /v1/teams/<team_id>/project-sets 创建项目集
        @apiVersion 1.0.0
        @apiName add_team_project_set
        @apiGroup ProjectSet
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiParam {String} team_id 团队id
        @apiParamExample {json} 请求示例
        {
           "name": "name"
        }

        @apiSuccessExample {json} 返回示例
        {
            "message": "创建成功",
            "project_set": {}
        }
        """
        # 检查是否有创建项目权限
        if not self.current_user.can(team, TeamPermission.CREATE_PROJECT_SET):
            raise NoPermissionError(gettext("您没有权限在这个团队创建项目集"))
        # 获取data
        data = self.get_json(ProjectSetsSchema())
        project_set = ProjectSet.create(name=data["name"], team=team)
        return {"message": gettext("创建成功"), "project_set": project_set.to_api()}


def _insight_project_data(project: Project) -> dict:
    data = project.to_api(with_team=False)
    return {
        "group_type": data["group_type"],
        "id": data["id"],
        "name": data["name"],
        "project_set": data["project_set"],
    }


def get_insight_user_projects_data(
    user: User, team_projects: List[Project], /, *, skip=0, limit=5
):
    from app.models.project_member import ProjectMember

    relations = (
        ProjectMember.objects(user=user, project__in=team_projects, status="active")
        .skip(skip)
        .limit(limit)
    )
    user_projects_data = {
        "projects": [],
        "count": relations.count(),
    }
    for relation in relations:
        project_data = _insight_project_data(relation.project)
        project_data["member_summary"] = [relation.to_api(include_permissions=False)]
        user_projects_data["projects"].append(project_data)
    return user_projects_data


class TeamInsightUserListAPI(MoeAPIView):
    @token_required
    @fetch_model(Team)
    def get(self, team: Team):
        if not self.current_user.can(team, TeamPermission.INSIGHT):
            raise NoPermissionError(gettext("您没有权限查看本团队项目统计"))
        query = self.get_query(None, TeamInsightUserListSchema())
        p = MoePagination(max_limit=10)
        team_members = TeamMemberService.list_members(
            team, self.current_user, status="active", word=query["word"]
        )
        users = [member.user for member in team_members]
        team_projects = team.projects(status=ProjectStatus.WORKING).no_dereference()
        data = []
        for user in users[p.skip : p.skip + p.limit]:
            user_projects_data = get_insight_user_projects_data(user, team_projects)
            data.append({**user_projects_data, "user": user.to_api()})
        return p.set_data(data=data, count=len(users))


class TeamInsightUserProjectListAPI(MoeAPIView):
    @token_required
    @fetch_model(User)
    @fetch_model(Team)
    def get(self, team: Team, user: User):
        if not self.current_user.can(team, TeamPermission.INSIGHT):
            raise NoPermissionError(gettext("您没有权限查看本团队项目统计"))
        if TeamMemberService.for_user(team, user, active_only=True) is None:
            raise UserNotExistError
        p = MoePagination()
        team_projects = team.projects(status=ProjectStatus.WORKING).no_dereference()
        user_projects_data = get_insight_user_projects_data(
            user, team_projects, skip=p.skip, limit=p.limit
        )
        return p.set_data(
            data=user_projects_data["projects"], count=user_projects_data["count"]
        )


def get_insight_project_users_data(project: Project, /, *, skip=0, limit=5):
    from app.models.project_member import ProjectMember

    relations = (
        ProjectMember.objects(project=project, status="active").skip(skip).limit(limit)
    )
    project_users_data = {
        "users": [],
        "count": relations.count(),
    }
    for relation in relations:
        member_data = relation.to_api(include_permissions=False)
        if relation.user is not None:
            member_data["name"] = relation.user.name
        else:
            # External members carry no ``user`` document; expose their
            # display name under the same key so consumers always find it.
            member_data["name"] = relation.display_name
        project_users_data["users"].append(member_data)
    return project_users_data


class TeamInsightProjectListAPI(MoeAPIView):
    @token_required
    @fetch_model(Team)
    def get(self, team: Team):
        if not self.current_user.can(team, TeamPermission.INSIGHT):
            raise NoPermissionError(gettext("您没有权限查看本团队项目统计"))
        query = self.get_query(None, TeamInsightProjectListSchema())
        p = MoePagination(max_limit=10)
        projects = team.projects(
            skip=p.skip, limit=p.limit, status=ProjectStatus.WORKING, word=query["word"]
        )
        data = []
        for project in projects:
            project_users_data = get_insight_project_users_data(project)
            project_data = {
                **project_users_data,
                "project": _insight_project_data(project),
            }
            if self.current_user.can(team, TeamPermission.AUTO_BECOME_PROJECT_ADMIN):
                project_data["outputs"] = [
                    output.to_api() for output in project.outputs()
                ]
            data.append(project_data)
        return p.set_data(data=data, count=projects.count())


class TeamInsightProjectUserListAPI(MoeAPIView):
    @token_required
    @fetch_model(Project)
    @fetch_model(Team)
    def get(self, team: Team, project: Project):
        if not self.current_user.can(team, TeamPermission.INSIGHT):
            raise NoPermissionError(gettext("您没有权限查看本团队项目统计"))
        if project.team != team:
            raise ProjectNotExistError
        p = MoePagination()
        project_users_data = get_insight_project_users_data(
            project, skip=p.skip, limit=p.limit
        )
        return p.set_data(
            data=project_users_data["users"], count=project_users_data["count"]
        )
