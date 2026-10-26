"""储存相关的常量与「能力位」。

历史包袱说明
------------
`OSS_BUCKET_STYLE` 原本只有两个取值，但语义是反的：

* ``S3``  → 实际指**阿里云 OSS**（用 oss2 SDK，虚拟主机风格寻址）
* ``R2``  → 实际指**标准 S3 兼容协议**（用 boto3，路径风格寻址）

本模块把这两个概念拆干净：真正不同的实现只有两个，``BucketStyle.ALI`` 与
``BucketStyle.S3``；其余写法（``R2`` / ``MINIO`` / ``GARAGE`` / ``AWS`` /
``ALIYUN`` …）都只是别名，由 :func:`parse_bucket_style` 归一到这两者之一。

因为 ``S3`` 归一后等价于今天的 ``R2`` 分支，**存量 R2 部署行为零变化**，
只是名字终于诚实了；而存量把 ``S3`` 当阿里云用的部署，必须显式改成
``ALI``，否则会被当成标准 S3 处理（见 :func:`parse_bucket_style` 的告警）。
"""

import logging
from typing import NamedTuple, Optional

from app.constants.base import StrType

logger = logging.getLogger(__name__)


class StorageType(StrType):
    OSS = "OSS"
    LOCAL_STORAGE = "LOCAL_STORAGE"


class BucketStyle(StrType):
    """对象存储的协议实现风格（归一后的最终形态）。"""

    #: 标准 S3 兼容协议：AWS S3 / Cloudflare R2 / MinIO / Garage / 自建网关
    S3 = "S3"
    #: 阿里云 OSS 原生 SDK：独有的服务端图片处理与 CDN URL 鉴权
    ALI = "ALI"


#: 别名 → 归一值。键必须是大写。
_BUCKET_STYLE_ALIASES = {
    BucketStyle.S3: BucketStyle.S3,
    "R2": BucketStyle.S3,
    "AWS": BucketStyle.S3,
    "MINIO": BucketStyle.S3,
    "GARAGE": BucketStyle.S3,
    "S3_COMPATIBLE": BucketStyle.S3,
    BucketStyle.ALI: BucketStyle.ALI,
    "ALIYUN": BucketStyle.ALI,
    "ALIYUN_OSS": BucketStyle.ALI,
    "OSS": BucketStyle.ALI,
}

#: 未显式配置时的默认风格。
DEFAULT_BUCKET_STYLE = BucketStyle.S3

#: 用于识别「被误判为标准 S3 的阿里云 endpoint」。
_ALIYUN_ENDPOINT_MARKER = "aliyuncs.com"


class StorageCapabilities(NamedTuple):
    """一份存储实现的「能力位」。

    调用方不该再问「你是不是 R2」，而该问「你支持不支持某能力」——
    这样接第三种实现（比如以后的腾讯云 COS）时不用改业务代码。
    """

    #: 支持服务端图片处理（``x-oss-process`` 之类），即缩略图不用我们自己生成。
    server_side_image_processing: bool
    #: 支持厂商私有的 CDN URL 鉴权（阿里云鉴权方式 C）。
    cdn_url_auth: bool

    @property
    def local_thumbnail_generation(self) -> bool:
        """是否需要本程序自行生成并上传缩略图。"""
        return not self.server_side_image_processing


_CAPABILITIES = {
    BucketStyle.S3: StorageCapabilities(
        server_side_image_processing=False,
        cdn_url_auth=False,
    ),
    BucketStyle.ALI: StorageCapabilities(
        server_side_image_processing=True,
        cdn_url_auth=True,
    ),
}


class BucketStyleResult(NamedTuple):
    """:func:`parse_bucket_style` 的返回值。"""

    #: 归一后的风格
    style: str
    #: 原始输入，便于日志里对照
    raw: Optional[str]
    #: ``None`` / ``aliyun_endpoint_as_s3`` / ``unknown``
    issue: Optional[str]

    @property
    def capabilities(self) -> StorageCapabilities:
        return capabilities_for(self.style)


def capabilities_for(style: str) -> StorageCapabilities:
    """按归一后的风格取能力位；未知值按标准 S3 处理（保守：不依赖厂商特性）。"""
    return _CAPABILITIES.get(style, _CAPABILITIES[DEFAULT_BUCKET_STYLE])


def storage_capabilities(config) -> StorageCapabilities:
    """按一份配置（``current_app.config`` 或 celery 的 ``app_config``）取能力位。

    纯函数，不写日志，可以放在每次渲染都会经过的路径上。
    """
    return parse_bucket_style(
        config.get("OSS_BUCKET_STYLE"), config.get("OSS_ENDPOINT")
    ).capabilities


def requires_local_thumbnail(config) -> bool:
    """是否需要本程序自行生成缩略图。

    本地存储必然要；标准 S3（R2 / MinIO / Garage / AWS）没有服务端图片处理，
    也要；只有阿里云 OSS 走 ``x-oss-process``，不需要。

    这个判断原先散落在 4 个文件里，一律写成
    ``STORAGE_TYPE == LOCAL_STORAGE or OSS_BUCKET_STYLE == "R2"``。
    """
    if config.get("STORAGE_TYPE") != StorageType.OSS:
        return config.get("STORAGE_TYPE") == StorageType.LOCAL_STORAGE
    return storage_capabilities(config).local_thumbnail_generation


def parse_bucket_style(
    raw: Optional[str], endpoint: Optional[str] = None
) -> BucketStyleResult:
    """把 ``OSS_BUCKET_STYLE`` 的原始值归一到 :class:`BucketStyle`。

    纯函数、无副作用（不写日志），因此可以放在热路径里反复调用；
    需要告警时用 :func:`report_bucket_style_issues`。

    :param raw: ``OSS_BUCKET_STYLE`` 原始值，可为空
    :param endpoint: ``OSS_ENDPOINT``，仅用于识别误配
    """
    token = str(raw or "").strip().upper()
    aliyun_endpoint = _ALIYUN_ENDPOINT_MARKER in str(endpoint or "").lower()

    if not token:
        # 未配置 = 走默认。若 endpoint 是阿里云，说明这是「老配置没写这一项」，
        # 属于危险情形：之前被当成阿里云用，现在会被当成标准 S3。
        return BucketStyleResult(
            style=DEFAULT_BUCKET_STYLE,
            raw=raw,
            issue="aliyun_endpoint_as_s3" if aliyun_endpoint else None,
        )

    style = _BUCKET_STYLE_ALIASES.get(token)
    if style is None:
        return BucketStyleResult(style=DEFAULT_BUCKET_STYLE, raw=raw, issue="unknown")

    # 显式配成标准 S3、endpoint 却是阿里云 —— 签名方式与 URL 结构会全错。
    issue = (
        "aliyun_endpoint_as_s3" if style == BucketStyle.S3 and aliyun_endpoint else None
    )
    return BucketStyleResult(style=style, raw=raw, issue=issue)


def report_bucket_style_issues(result: BucketStyleResult, log=None) -> None:
    """把归一过程中发现的问题写进日志。每个进程调用一次即可（``OSS.init``）。"""
    log = log or logger
    if result.issue == "aliyun_endpoint_as_s3":
        log.error(
            "储存配置危险：OSS_ENDPOINT 指向阿里云（aliyuncs.com），但 OSS_BUCKET_STYLE=%r "
            "被解析为标准 S3（协议不同：签名方式、URL 结构、服务端图片处理都不兼容）。"
            "阿里云 OSS 请显式设置 OSS_BUCKET_STYLE=ALI，并重启服务。",
            result.raw if result.raw not in (None, "") else "(未设置)",
        )
    elif result.issue == "unknown":
        log.warning(
            "OSS_BUCKET_STYLE=%r 不是已知取值，已按标准 S3 处理。"
            "可用取值：S3 / ALI（别名 R2、MINIO、GARAGE、AWS、ALIYUN 也接受）。",
            result.raw,
        )
