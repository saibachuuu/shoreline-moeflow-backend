"""
关于用户个人的API
"""

from flask import request

from app.core.responses import MoePagination
from app.core.views import MoeAPIView
from app.decorators.auth import token_required
from app.models.user import User
from app.validators import ChangeInfoSchema
from app.validators.auth import (
    ChangeEmailSchema,
    ChangePasswordSchema,
    LoginSchema,
    ResetPasswordSchema,
)
from app.validators.join_process import (
    SearchInvitationSchema,
    SearchRelatedApplicationSchema,
)
from app.core.api import QueryParser
from flask_babel import gettext
from app.models.project import Project
from app.models.application import Application
from app.services.project_member import ProjectMemberService
from app.services.identity_permission import CLEARED, COMPLETED, NORMAL
from app.services.user_alias import UserAliasService
from app.utils.search import normalize_search_text


class MeTokenAPI(MoeAPIView):
    def post(self):
        """
        @api {post} /v1/user/token 创建用户令牌
        @apiVersion 1.0.0
        @apiName get_token
        @apiGroup Me
        @apiUse APIHeader

        @apiParam {String}      email        邮箱
        @apiParam {String}      password     密码
        @apiParam {String}      captcha_info 验证码签名
        @apiParam {String}      captcha      验证码

        @apiParamExample {json} 请求示例
        {
            "email":"123@123.com",
            "password":"123123",
            "captcha_info":"dafkaldjfl2183u21903kljlkjds",
            "captcha":"989989"
        }

        @apiSuccess {String} token
            身份验证token,附加到 `Authorization` Header中,访问需要登录的API
        @apiSuccessExample {json} 返回示例
        {
            "token": "eyJhbGciOiJIUzI...IV3kUw2_MF2zyvfjAxc"
        }

        @apiUse ValidateError
        """
        data = self.get_json(LoginSchema())
        # 计算token
        user = User.by_email(data["email"])
        token = user.generate_token()
        return {"token": token}


class MeInfoAPI(MoeAPIView):
    @token_required
    def get(self):
        """
        @api {get} /v1/user/info 获取自己资料
        @apiVersion 1.0.0
        @apiName get_user_info
        @apiGroup Me
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiUse UserInfoModel
        @apiSuccessExample {json} 返回示例
        {
            "avatar": null,
            "id": "5911930d7e4b036e2df3a910",
            "name": "123123",
            "signature": "這個用戶還沒有簽名"
        }

        @apiUse NeedTokenError
        @apiUse BadTokenError
        """
        return self.current_user.to_api()

    @token_required
    def put(self):
        """
        @api {put} /v1/user/info 修改自己资料
        @apiVersion 1.0.0
        @apiName set_user_info
        @apiGroup Me
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiParam {String} name 昵称
        @apiParam {String} signature 签名
        @apiParam {String} locale 语言

        @apiUse UserInfoModel

        @apiUse ValidateError
        """
        data = self.get_json(
            ChangeInfoSchema(), context={"old_name": self.current_user.name}
        )
        aliases_provided = "aliases" in data
        aliases = data.pop("aliases", None)
        default_display_name_provided = "default_display_name" in data
        default_display_name = data.pop("default_display_name", None)
        self.current_user.name = data["name"]
        if default_display_name_provided:
            self.current_user.default_display_name = (
                default_display_name.strip()
                if isinstance(default_display_name, str)
                else ""
            )
        self.current_user.signature = data["signature"]
        self.current_user.locale = data["locale"]
        if aliases_provided:
            # UserAliasService performs the same normalization and audit as
            # PATCH /v1/me/aliases while saving the complete profile once.
            UserAliasService.replace(
                self.current_user,
                self.current_user,
                aliases,
                default_display_name=self.current_user.default_display_name
                if default_display_name_provided
                else None,
                request_id=request.headers.get("X-Request-ID")
                or request.headers.get("Idempotency-Key"),
            )
        else:
            self.current_user.save()
        self.current_user.reload()
        return {"message": gettext("修改成功"), "user": self.current_user.to_api()}


class MeEmailAPI(MoeAPIView):
    @token_required
    def put(self):
        """
        @api {put} /v1/user/email 修改自己邮箱
        @apiVersion 1.0.0
        @apiName change_email
        @apiGroup Me
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiParam {String}      old_email_v_code   原邮箱验证码
        @apiParam {String}      new_email        新邮箱地址
        @apiParam {String}      new_email_v_code   新邮箱验证码
        @apiParamExample {json} 请求示例
        {
            "old_emailVCode":"A21KLk",
            "new_email":"123@123.com",
            "new_email_v_code":"kK12YI"
        }

        @apiUse 204

        @apiUse ValidateError
        """
        data = self.get_json(
            ChangeEmailSchema(), context={"old_email": self.current_user.email}
        )
        self.current_user.email = data["new_email"].lower()
        self.current_user.save()
        return {"message": gettext("修改成功"), "user": self.current_user.to_api()}


class MePasswordAPI(MoeAPIView):
    @token_required
    def put(self):
        """
        @api {put} /v1/user/password 修改自己密码
        @apiVersion 1.0.0
        @apiName change_password
        @apiGroup Me
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiParam {String}      old_password   原密码
        @apiParam {String}      new_password   新密码
        @apiParamExample {json} 请求示例
        {
            "old_password":"123123",
            "new_password":"321321"
        }

        @apiUse 204

        @apiUse ValidateError
        """
        data = self.get_json(
            ChangePasswordSchema(), context={"email": self.current_user.email}
        )
        self.current_user.password = data["new_password"]
        self.current_user.save()
        return {"message": gettext("修改成功，请重新登陆")}

    def delete(self):
        """
        @api {delete} /v1/user/password 重置用户密码
        @apiVersion 1.0.0
        @apiName reset_password
        @apiGroup Me
        @apiUse APIHeader

        @apiParam {String}      email   邮箱
        @apiParam {String}      v_code  验证码
        @apiParam {String}      password  新密码
        @apiParamExample {json} 请求示例
        {
            "email":"123@123.com",
            "v_code":"kJhu12",
            "password":"123123"
        }

        @apiUse 204

        @apiUse ValidateError
        """
        data = self.get_json(ResetPasswordSchema())
        user = User.by_email(data["email"])
        user.password = data["password"]
        user.save()
        # 计算token
        token = user.generate_token()
        return {"token": token}


class MeInvitationListAPI(MoeAPIView):
    @token_required
    def get(self):
        """
        @api {get} /v1/user/invitations?status=<status> 获取对自己的邀请
        @apiVersion 1.0.0
        @apiName get_user_invitation
        @apiGroup Me
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiParam {String} status 邀请状态，可选 "pending"/"deny"/"allow"

        @apiSuccess {Object[]} data 所有邀请
        @apiSuccess {String} data.id 邀请id
        @apiSuccess {Object} data.user 被邀请人信息
        @apiSuccess {String} data.user.id 被邀请人Id
        @apiSuccess {String} data.user.name 被邀请人用户名
        @apiSuccess {Object} data.operator 邀请人信息
        @apiSuccess {String} data.operator.id 邀请人用户Id
        @apiSuccess {String} data.operator.name 邀请人用户名
        @apiSuccess {String} data.create_time 邀请创建时间（Unix时间戳）
        @apiSuccessExample {json} 返回示例
        {
            "data": "待完成"
        }

        @apiUse NeedTokenError
        @apiUse BadTokenError
        """
        data = self.get_query({"status": [QueryParser.int]}, SearchInvitationSchema())
        p = MoePagination()
        objects = self.current_user.invitations(
            status=data["status"], skip=p.skip, limit=p.limit
        )
        return p.set_objects(objects)


class MeRelatedApplicationListAPI(MoeAPIView):
    @token_required
    def get(self):
        """
        @api {get} /v1/user/related-applications?status=<status> 获取自己可以管理的申请
        @apiVersion 1.0.0
        @apiName getMeRelatedApplicationList
        @apiGroup Me
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiParam {String} status 邀请状态，可选 "pending"/"deny"/"allow"

        @apiSuccess {Object[]} data 所有邀请
        @apiSuccess {String} data.id 邀请id
        @apiSuccess {Object} data.user 被邀请人信息
        @apiSuccess {String} data.user.id 被邀请人Id
        @apiSuccess {String} data.user.name 被邀请人用户名
        @apiSuccess {Object} data.operator 邀请人信息
        @apiSuccess {String} data.operator.id 邀请人用户Id
        @apiSuccess {String} data.operator.name 邀请人用户名
        @apiSuccess {String} data.create_time 邀请创建时间（Unix时间戳）
        @apiSuccessExample {json} 返回示例
        {
            "data": "待完成"
        }

        @apiUse NeedTokenError
        @apiUse BadTokenError
        """
        data = self.get_query(
            {"status": [QueryParser.int]}, SearchRelatedApplicationSchema()
        )
        p = MoePagination()
        objects = Application.get(
            status=data["status"],
            skip=p.skip,
            limit=p.limit,
            related_user_id=self.current_user.id,
        )
        return p.set_objects(objects, func_kwargs={"user": self.current_user})


class MeTeamListAPI(MoeAPIView):
    @token_required
    def get(self):
        """
        @api {get} /v1/user/teams 获取自己的所有团队
        @apiVersion 1.0.0
        @apiName get_user_team
        @apiGroup Me
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiSuccessExample {json} 返回示例
        {

        }
        """

        # 获取查询参数
        word = request.args.get("word")
        p = MoePagination()
        teams = self.current_user.teams(skip=p.skip, limit=p.limit, word=word)
        # The dashboard only needs identity permissions and basic navigation
        # data. Settings, archive keys and OCR quota belong to the detail API.
        data = [team.to_list_api(user=self.current_user) for team in teams]
        return p.set_data(data, count=teams.count())


class MeProjectListAPI(MoeAPIView):
    @token_required
    def get(self):
        """
        # noqa: E501
        @api {get} /v1/user/projects?word=<word> 获取用户的所有项目
        @apiVersion 1.0.0
        @apiName get_team_project
        @apiGroup Me
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiParam {Number} [status] 项目状态，可以传递多个，不传则为所有的，支持以下参数
            - 0  # 进行中的项目
            - 1  # 完成了的项目
            - 2  # 计划完成
            - 3  # 计划删除
        @apiParam {String} [word] 模糊查询的名称

        @apiSuccessExample {json} 返回示例
        {

        }
        """
        raw_status = request.args.getlist("status")
        status_tokens = [
            token.strip()
            for value in raw_status
            for token in value.split(",")
            if token.strip()
        ]
        status_names = {
            "NORMAL": {NORMAL},
            "CLEARED": {CLEARED},
            # The UI's completed bucket contains both terminal states.
            "COMPLETED": {COMPLETED, CLEARED},
        }
        requested_statuses = set()
        for token in status_tokens:
            if token not in status_names:
                from app.exceptions.identity import InvalidIdentityRequestError

                raise InvalidIdentityRequestError("invalid project status")
            requested_statuses.update(status_names[token])

        p = MoePagination()
        from app.models.project_member import ProjectMember

        # Resolve the user's project ids first, then let Mongo filter, count
        # and paginate Project documents.  The previous implementation loaded
        # every active membership and every referenced project into Python.
        project_ids = [
            item["project"]
            for item in ProjectMember._get_collection().find(
                {"user": self.current_user.pk, "status": "active"},
                {"project": 1},
            )
        ]
        projects = Project.objects(id__in=project_ids)
        if requested_statuses:
            projects = projects.filter(status__in=list(requested_statuses))
        word = request.args.get("word")
        if word:
            projects = projects.filter(
                name_search__icontains=normalize_search_text(word)
            )
        projects = projects.order_by("-edit_time")
        project_count = projects.count()
        paged_projects = list(projects.skip(p.skip).limit(p.limit).select_related())
        data = Project.batch_to_list_api(
            paged_projects,
            self.current_user,
        )
        member_summaries = ProjectMemberService.member_summaries(
            paged_projects, compact=True
        )
        for item in data:
            # Keep the integer status used by the card; detail-only aliases are
            # intentionally not generated on this hot path.
            item["member_summary"] = member_summaries.get(item["id"], [])
        return p.set_data(data=data, count=project_count)
