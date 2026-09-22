"""模块 API。

自有蓝图，因此 `app/apis/urls.py` 一行都不用改（见 docs/optional-modules.md §3.4）。
URL 前缀刻意与核心 project 蓝图保持一致（`/v1/projects/...`），前端可统一处理。
"""

from __future__ import annotations

from flask import Blueprint
from flask_babel import gettext

from app.core.views import MoeAPIView
from app.decorators.auth import token_required
from app.decorators.url import fetch_model
from app.exceptions import NoPermissionError
from app.models.project import Project, ProjectPermission

from . import config
from .models import ZitengCheck
from .service import ensure_check
from .tasks import enqueue_check, reset_for_retry

blueprint = Blueprint("ziteng_partner", __name__)


class ZitengCheckAPI(MoeAPIView):
    """查询 / 重新触发某项目的查重。"""

    @token_required
    @fetch_model(Project)
    def get(self, project: Project):
        """
        @api {get} /v1/projects/<project_id>/ziteng-check
            查询项目的紫藤查重结果
        @apiVersion 1.0.0
        @apiName getZitengCheckAPI
        @apiGroup Project
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiSuccessExample {json} 返回示例
        {
            "check": {
                "verdict": "suspected",
                "suspects": [],
                "suspect_count": 0
            }
        }
        """
        if not self.current_user.can(project, ProjectPermission.ACCESS):
            raise NoPermissionError(gettext("您没有此项目的访问权限"))
        check = ZitengCheck.objects(project_id=str(project.id)).first()
        return {"check": check.to_api() if check is not None else None}

    @token_required
    @fetch_model(Project)
    def post(self, project: Project):
        """
        @api {post} /v1/projects/<project_id>/ziteng-check
            重新触发项目的紫藤查重
        @apiVersion 1.0.0
        @apiName postZitengCheckAPI
        @apiGroup Project
        @apiUse APIHeader
        @apiUse TokenHeader
        """
        if not self.current_user.can(project, ProjectPermission.ACCESS):
            raise NoPermissionError(gettext("您没有此项目的访问权限"))
        ensure_check(str(project.id), project.name)
        reset_for_retry(str(project.id))
        enqueue_check(str(project.id))
        check = ZitengCheck.objects(project_id=str(project.id)).first()
        return {"check": check.to_api() if check is not None else None}


class ZitengConfigAPI(MoeAPIView):
    """模块的只读状态，供前端判断是否显示入口。**不暴露密钥**。"""

    @token_required
    def get(self):
        """
        @api {get} /v1/ziteng-partner/config
            查询紫藤查重模块是否启用
        @apiVersion 1.0.0
        @apiName getZitengPartnerConfigAPI
        @apiGroup Site
        @apiUse APIHeader
        @apiUse TokenHeader
        """
        return {
            "enabled": bool(config.api_key()),
            "max_results": config.max_results(),
        }


blueprint.add_url_rule(
    "/v1/projects/<project_id>/ziteng-check",
    methods=["GET", "POST", "OPTIONS"],
    view_func=ZitengCheckAPI.as_view("ziteng_check"),
)
blueprint.add_url_rule(
    "/v1/ziteng-partner/config",
    methods=["GET", "OPTIONS"],
    view_func=ZitengConfigAPI.as_view("ziteng_partner_config"),
)