import datetime
import logging

from mongoengine import (
    BooleanField,
    IntField,
    ListField,
    ObjectIdField,
    DateTimeField,
    Document,
    NotUniqueError,
    StringField,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class PartnerSearchThrottle(Document):
    """
    站外撞车查询的速率限制记录。

    以「来源地址 + 可选自定义标识」作为文档主键，记录最近一次查询时间。
    站点在 `PartnerSearchSettings` 中配置的 `partner_search_rate_limit_seconds`
    秒数窗口内，同一主键只允许查询一次；窗口过后放行并刷新时间。
    """

    key = StringField(primary_key=True, db_field="_id", required=True)
    last_time = DateTimeField(db_field="lt", default=datetime.datetime.utcnow)

    @classmethod
    def check_allow(cls, key: str, rate_limit_seconds: int) -> bool:
        """
        检查当前是否被限流，并在放行时记录本次访问时间。

        :param key: 限流主键，如 "ip:<source_ip>" 或 "key:<client_key>"
        :param rate_limit_seconds: n 秒内仅允许一次；<=0 或 None 表示不限流
        :return: True 表示允许访问；False 表示仍在冷却窗口内
        """
        if rate_limit_seconds is None or rate_limit_seconds <= 0:
            return True
        now = datetime.datetime.utcnow()
        window_start = now - datetime.timedelta(seconds=rate_limit_seconds)
        # 若已有记录且已过期，则原子更新到 now 并放行
        updated = cls.objects(pk=key, last_time__lte=window_start).update_one(
            set__last_time=now
        )
        if updated:
            return True
        # 未命中条件：要么记录在窗口内（拒绝），要么记录不存在
        if cls.objects(pk=key).first() is None:
            # 记录不存在，尝试创建；并发下唯一的创建者放行，其余拒绝
            try:
                cls(key=key, last_time=now).save()
            except NotUniqueError:
                return False
            return True
        return False


class PartnerSearchSettings(Document):
    """Module-owned view of existing settings; no migration or reset required.

    Only these fields are updated. Core fields remain untouched, and removing
    this module leaves the core free to read the same settings document.
    """

    type = StringField(db_field="n", default="site")
    # == 站外撞车查询（partner search）==
    # 是否对外开放本站的项目撞车查询接口
    partner_search_enabled = BooleanField(db_field="pse", default=False)
    # 允许被站外查询索引的团队（仅这些团队下的项目会被检索）
    partner_search_team_ids = ListField(ObjectIdField(), db_field="pst", default=list)
    # 速率限制：同一来源地址在 n 秒内仅允许查询一次
    partner_search_rate_limit_seconds = IntField(db_field="psq", default=10)
    # 单次查询返回结果的条数上限
    partner_search_max_limit = IntField(db_field="psm", default=20)

    meta = {"collection": "site_setting", "strict": False, "auto_create_index": False}

    @classmethod
    def get(cls):
        return cls.objects(type="site").first() or cls(type="site")

    def to_api(self):
        return {
            "partner_search_enabled": self.partner_search_enabled,
            "partner_search_team_ids": [
                str(value) for value in self.partner_search_team_ids
            ],
            "partner_search_rate_limit_seconds": self.partner_search_rate_limit_seconds,
            "partner_search_max_limit": self.partner_search_max_limit,
        }
