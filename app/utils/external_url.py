"""外部 API 地址的校验工具（通用）。

**为什么在核心**：`normalize_external_api_url` 被核心的 `app/apis/team.py`
（团队设置里填写第三方档案 API 基址）与归档导入模块**同时**使用。
若把它留在模块里，核心就会反向 import 模块，破坏 C1。
它本身与「归档导入」无关——只做外部地址的安全校验（HTTPS、禁止内网/保留地址、
可选主机白名单），因此属于通用工具。

模块可以使用核心（核心是底座），核心**不得**使用模块。
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

from flask_babel import lazy_gettext

__all__ = ["normalize_external_api_url"]


def normalize_external_api_url(
    value: str,
    *,
    allowed_hosts: tuple[str, ...] | list[str] = (),
    require_allowlist: bool = False,
) -> str:
    """校验一个外部 API 基址，返回去掉结尾斜杠的形式。

    在把凭据发给对方之前调用，防止凭据被送到内网地址（SSRF）。

    :param allowed_hosts: 允许的主机白名单；支持 `*.example.com` 与 `example.com`
        （后者自动涵盖子域）。
    :param require_allowlist: 为真时，主机必须命中白名单，否则报错。
    """
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
    host_allowed = any(
        host == item or host.endswith("." + item) for item in normalized_hosts
    )
    if require_allowlist and not host_allowed:
        raise ValueError(lazy_gettext("团队归档 API 地址不在站点允许的主机白名单中"))
    return value.rstrip("/")