import logging
from mongoengine import (
    Document,
    ListField,
    BooleanField,
    IntField,
    StringField,
    ObjectIdField,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class SiteSetting(Document):
    """
    This document only have one document, of which the type is 'site'.
    """

    type = StringField(db_field="n", required=True, unique=True)
    enable_whitelist = BooleanField(db_field="ew", default=True)
    whitelist_emails = ListField(StringField(), db_field="we", default=list)
    only_allow_admin_create_team = BooleanField(db_field="oacg", default=True)
    auto_join_team_ids = ListField(ObjectIdField(), db_field="ajt", default=list)
    homepage_html = StringField(db_field="h", default="")
    homepage_css = StringField(db_field="hc", default="")
    custom_site_title = StringField(db_field="st", default="")
    homepage_welcome = StringField(db_field="hw", default="")
    homepage_image_url = StringField(db_field="hi", default="")

    # == 站外撞车查询（partner search）==
    # 是否对外开放本站的项目撞车查询接口
    partner_search_enabled = BooleanField(db_field="pse", default=False)
    # 允许被站外查询索引的团队（仅这些团队下的项目会被检索）
    partner_search_team_ids = ListField(ObjectIdField(), db_field="pst", default=list)
    # 速率限制：同一来源地址在 n 秒内仅允许查询一次
    partner_search_rate_limit_seconds = IntField(db_field="psq", default=10)
    # 单次查询返回结果的条数上限
    partner_search_max_limit = IntField(db_field="psm", default=20)

    meta = {
        "indexes": [
            "type",
        ]
    }

    @classmethod
    def init_site_setting(cls):
        if cls.objects(type="site").count() > 0:
            logger.debug("已有站点设置，跳过初始化")
        else:
            logger.debug("初始化站点设置")
            cls(type="site").save()

    @classmethod
    def get(cls) -> "SiteSetting":
        return cls.objects(type="site").first()

    def to_api(self):
        return {
            "enable_whitelist": self.enable_whitelist,
            "whitelist_emails": self.whitelist_emails,
            "only_allow_admin_create_team": self.only_allow_admin_create_team,
            "auto_join_team_ids": [str(id) for id in self.auto_join_team_ids],
            "homepage_html": self.homepage_html,
            "homepage_css": self.homepage_css,
            "custom_site_title": self.custom_site_title,
            "homepage_welcome": self.homepage_welcome,
            "homepage_image_url": self.homepage_image_url,
            "partner_search_enabled": self.partner_search_enabled,
            "partner_search_team_ids": [str(id) for id in self.partner_search_team_ids],
            "partner_search_rate_limit_seconds": self.partner_search_rate_limit_seconds,
            "partner_search_max_limit": self.partner_search_max_limit,
        }
