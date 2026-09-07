import ipaddress
from urllib.parse import urlparse

from flask_babel import lazy_gettext
from marshmallow import fields, validates_schema
from marshmallow.exceptions import ValidationError

from app.validators.custom_schema import DefaultSchema


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


def normalize_archive_api_url(
    value: str,
    *,
    allowed_hosts: tuple[str, ...] | list[str] = (),
    require_allowlist: bool = False,
) -> str:
    """Validate a provider base URL before the worker sends credentials to it."""

    value = str(value or "").strip()
    parsed = urlparse(value)
    if parsed.scheme != "https":
        raise ValueError(lazy_gettext("归档 API 地址必须使用 HTTPS"))
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(lazy_gettext("归档 API 地址无效"))
    if parsed.fragment or parsed.query:
        raise ValueError(lazy_gettext("归档 API 地址不能包含 query 或 fragment"))

    host = parsed.hostname.rstrip(".").lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if host in {"localhost", "localhost.localdomain"} or host.endswith(
            (".local", ".internal")
        ):
            raise ValueError(lazy_gettext("归档 API 地址不能指向本地或内部主机"))
    else:
        if not address.is_global:
            raise ValueError(lazy_gettext("归档 API 地址不能指向私有或保留 IP"))

    normalized_hosts = tuple(
        str(item).strip().lower().lstrip("*.").rstrip(".")
        for item in allowed_hosts
        if str(item).strip()
    )
    host_allowed = any(host == item or host.endswith("." + item) for item in normalized_hosts)
    if require_allowlist and not host_allowed:
        raise ValueError(lazy_gettext("团队归档 API 地址不在站点允许的主机白名单中"))
    return value.rstrip("/")
