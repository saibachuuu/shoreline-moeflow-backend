import datetime
import logging

from mongoengine import (
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
    站点在 `SiteSetting` 中配置的 `partner_search_rate_limit_seconds`
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
