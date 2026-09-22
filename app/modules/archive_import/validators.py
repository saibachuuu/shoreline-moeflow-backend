from flask_babel import lazy_gettext
from marshmallow import fields, validates_schema
from marshmallow.exceptions import ValidationError

from app.validators.custom_schema import DefaultSchema

# 外部地址校验已上移到通用工具（核心的 team.py 也要用它，
# 留在模块里会让核心反向依赖模块）。这里保留旧名以兼容既有调用点。
from app.utils.external_url import normalize_external_api_url

__all__ = ["ArchiveImportSchema", "normalize_archive_api_url"]


def normalize_archive_api_url(
    value: str,
    *,
    allowed_hosts: tuple[str, ...] | list[str] = (),
    require_allowlist: bool = False,
) -> str:
    """归档模块内的旧名，转调通用实现。"""
    return normalize_external_api_url(
        value, allowed_hosts=allowed_hosts, require_allowlist=require_allowlist
    )


class ArchiveImportSchema(DefaultSchema):
    """触发画廊归档导入的参数"""

    gid = fields.Str(required=True, validate=[lambda v: v.strip().isdigit()])
    token = fields.Str(required=True, validate=[lambda v: bool(v.strip())])
    gallery_url = fields.Str(required=False, allow_none=True)

    @validates_schema
    def verify_not_empty(self, data, **kwargs):
        if "gid" in data and not data["gid"].strip():
            raise ValidationError(lazy_gettext("gid 不能为空"), "gid")
        if "token" in data and not data["token"].strip():
            raise ValidationError(lazy_gettext("token 不能为空"), "token")
        if data.get("gallery_url") is not None and not data["gallery_url"].strip():
            data["gallery_url"] = ""
