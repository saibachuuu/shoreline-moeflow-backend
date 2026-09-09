"""
站外撞车查询接口。

供站外其他汉化组查询本站（被管理员指定团队）在做哪些项目，以规避撞车。
接口本身无需登录鉴权，但受站点设置中的速率限制与单次结果上限约束。
注意：已经完结或已清空的项目不纳入搜索范围，只检索正常进行中的项目。
"""

import logging

from bson import ObjectId
from flask import request

from app.constants.project import ProjectStatus
from app.core.api import APIError
from app.core.views import MoeAPIView
from app.models.partner_search import PartnerSearchThrottle
from app.models.project import Project
from app.models.site_setting import SiteSetting
from app.utils.search import normalize_search_text

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class PartnerSearchDisabledError(APIError):
    """外部查询功能未开启。"""

    status_code = 403
    code = 1
    message = "partner search is disabled"


class PartnerSearchRateLimitedError(APIError):
    """被速率限制拦截。"""

    status_code = 429
    code = 2
    message = "rate limited, please retry later"


class PartnerSearchEntryAPI(MoeAPIView):
    """
    @apiDefine PartnerSearchEntry
    @apiParam {String} keyword 查询关键词（作品名/项目名，模糊匹配）
    @apiParam {Number} [limit] 单次返回条数上限，不超过站点设定的上限
    @apiParam {Number} [page] 页码，从 1 开始
    """

    def post(self):
        """
        @api {post} /v1/partner-search-query-entry 站外撞车查询
        @apiVersion 1.0.0
        @apiName partnerSearchEntry
        @apiGroup PartnerSearch
        @apiUse APIHeader

        @apiParamExample {json} 请求示例
        {
            "keyword": "某某",
            "limit": 20,
            "page": 1
        }

        @apiSuccess {Number} total 匹配的项目总数
        @apiSuccess {Number} page 当前页码
        @apiSuccess {Number} limit 本次实际返回条数
        @apiSuccess {Object[]} projects 匹配的项目列表
        @apiSuccess {String} projects.id 项目 ID
        @apiSuccess {String} projects.name 项目名
        @apiSuccess {String} projects.source_name 作品原名
        @apiSuccess {String} projects.target_name 作品译名
        @apiSuccess {Number} projects.status 项目状态
        @apiSuccess {Object} projects.team_id 所属团队 ID
        @apiSuccess {String} projects.team_name 所属团队名
        """
        site_setting = SiteSetting.get()
        if not site_setting.partner_search_enabled:
            raise PartnerSearchDisabledError()

        # 解析请求体（容错 JSON 与非 JSON）
        json_data = request.get_json(silent=True)
        if json_data is None:
            json_data = {}
        raw_keyword = (json_data.get("keyword") or "").strip()
        raw_limit = json_data.get("limit")
        raw_page = json_data.get("page", 1)

        try:
            if raw_limit is None or int(raw_limit) <= 0:
                limit = site_setting.partner_search_max_limit
            else:
                limit = min(int(raw_limit), site_setting.partner_search_max_limit)
        except (TypeError, ValueError):
            limit = site_setting.partner_search_max_limit
        limit = max(1, limit)

        try:
            page = max(1, int(raw_page or 1))
        except (TypeError, ValueError):
            page = 1

        team_ids = site_setting.partner_search_team_ids or []
        valid_team_ids = []
        for value in team_ids:
            try:
                valid_team_ids.append(ObjectId(value))
            except (TypeError, ValueError):
                continue

        # 速率限制：key = ip + 自定义标识
        source_ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        if not source_ip:
            source_ip = request.remote_addr or "unknown"
        client_key = json_data.get("client_key") or ""
        throttle_key = "ip:{}".format(source_ip)
        if client_key:
            throttle_key += ":key:{}".format(client_key)
        if not PartnerSearchThrottle.check_allow(
            throttle_key, site_setting.partner_search_rate_limit_seconds
        ):
            raise PartnerSearchRateLimitedError()

        # 关键词为空或未配置可搜索团队时直接返回空列表
        if not raw_keyword or not valid_team_ids:
            return {
                "total": 0,
                "page": page,
                "limit": limit,
                "projects": [],
            }
        normalized = normalize_search_text(raw_keyword)

        # 仅检索正常进行中的项目（排除已完结 COMPLETED 与已清空 CLEARED 项目）
        base_queryset = Project.objects(
            team__in=valid_team_ids,
            status=ProjectStatus.WORKING,
            name_search__icontains=normalized,
        )
        total = base_queryset.count()
        skip = (page - 1) * limit
        projects = list(
            base_queryset.order_by("-edit_time", "-id").skip(skip).limit(limit)
        )
        team_cache = {}
        for project in projects:
            team_cache.setdefault(str(project.team.pk), project.team)

        data = []
        for project in projects:
            team = team_cache.get(str(project.team.pk))
            data.append(
                {
                    "id": str(project.id),
                    "name": project.name,
                    "source_name": project.source_name,
                    "target_name": project.target_name,
                    "status": project.status,
                    "intro": project.intro,
                    "team_id": str(team.id) if team else None,
                    "team_name": team.name if team else None,
                }
            )
        return {
            "total": total,
            "page": page,
            "limit": limit,
            "projects": data,
        }
