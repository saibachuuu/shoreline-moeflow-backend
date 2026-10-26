"""默认存储的只读概览接口。

排障时最常问的两件事是「现在到底往哪个桶写」和「key 的总前缀是什么」，
而这两项只存在于服务器的环境变量里，界面上看不见。本接口把它们读出来，
**永不下发任何凭据**（AK/SK、带账号 ID 的完整 endpoint 一律不出门）。

路由：``GET /v1/admin/storage/default``
"""

from urllib.parse import urlparse

from flask import current_app

from app.constants.storage import (
    BucketStyle,
    StorageType,
    parse_bucket_style,
    requires_local_thumbnail,
    storage_capabilities,
)
from app.core.views import MoeAPIView
from app.decorators.auth import admin_required


def _endpoint_host(endpoint: str) -> str:
    """只返回 endpoint 的 host。

    endpoint 里可能带路径、端口之外的东西（某些服务商把账号 ID 塞在 host 前面），
    但 host 本身是站长自己配的、且不含密钥，可以给。
    """
    if not endpoint:
        return ""
    parsed = urlparse(str(endpoint))
    return parsed.hostname or ""


def _warning_text(issue, raw_style) -> str:
    if issue == "aliyun_endpoint_as_s3":
        return (
            "配置危险：endpoint 指向阿里云，但储存风格被解析为标准 S3。"
            "阿里云请显式设置 OSS_BUCKET_STYLE=ALI，否则签名方式与图片处理都会失效。"
        )
    if issue == "unknown":
        return (
            f"OSS_BUCKET_STYLE={raw_style!r} 不是已知取值，已按标准 S3 处理。"
            "可用取值：S3 / ALI。"
        )
    return ""


class AdminStorageDefaultAPI(MoeAPIView):
    @admin_required
    def get(self):
        """
        @api {get} /v1/admin/storage/default 获取默认存储概览
        @apiVersion 1.0.0
        @apiName getAdminStorageDefaultAPI
        @apiGroup Admin
        @apiUse APIHeader
        @apiUse TokenHeader

        @apiSuccessExample {json} 返回示例
        {
            "storage_type": "OSS",
            "bucket_style": "S3",
            "bucket_style_raw": "R2",
            "endpoint_host": "xxxx.r2.cloudflarestorage.com",
            "bucket": "pictshare",
            "key_prefix": "storage/",
            "via_cdn": true,
            "cdn_domain": "https://cdn.example.com/",
            "prefixes": {
                "files": "files/",
                "outputs": "outputs/",
                "user_avatars": "user-avatars/",
                "team_avatars": "team-avatars/"
            },
            "capabilities": {
                "server_side_image_processing": false,
                "cdn_url_auth": false,
                "local_thumbnail_generation": true
            },
            "warning": ""
        }
        """
        config = current_app.config
        storage_type = config["STORAGE_TYPE"]
        parsed = parse_bucket_style(
            config.get("OSS_BUCKET_STYLE"), config.get("OSS_ENDPOINT")
        )
        capabilities = storage_capabilities(config)

        is_oss = storage_type == StorageType.OSS
        # 总前缀只在标准 S3（boto3）分支生效；阿里云分支历史行为无前缀。
        key_prefix = ""
        if is_oss and parsed.style == BucketStyle.S3:
            key_prefix = config.get("OSS_KEY_PREFIX", "storage/") or ""

        return {
            "storage_type": storage_type,
            "bucket_style": parsed.style if is_oss else None,
            "bucket_style_raw": parsed.raw or "",
            "endpoint_host": _endpoint_host(config.get("OSS_ENDPOINT", ""))
            if is_oss
            else "",
            "bucket": config.get("OSS_BUCKET_NAME", "") if is_oss else "",
            "key_prefix": key_prefix,
            "via_cdn": bool(config.get("OSS_VIA_CDN")) if is_oss else False,
            "cdn_domain": config.get("STORAGE_DOMAIN", ""),
            "prefixes": {
                "files": config["OSS_FILE_PREFIX"],
                "outputs": config["OSS_OUTPUT_PREFIX"],
                "user_avatars": config["OSS_USER_AVATAR_PREFIX"],
                "team_avatars": config["OSS_TEAM_AVATAR_PREFIX"],
            },
            "capabilities": {
                "server_side_image_processing": capabilities.server_side_image_processing,
                "cdn_url_auth": capabilities.cdn_url_auth,
            },
            # 缩略图到底谁生成：本地存储和标准 S3 都是我们自己生成，
            # 只有阿里云是服务端处理。前端用它决定要不要显示「重建缩略图」。
            "requires_local_thumbnail": requires_local_thumbnail(config),
            # 让误配直接长在管理员眼前，不必翻容器日志。
            "warning": _warning_text(parsed.issue, parsed.raw),
        }
