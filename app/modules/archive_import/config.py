"""归档导入模块的配置。

从核心 `app/config.py` 平移而来（见 docs/optional-modules.md §5）。
核心因此不再出现任何归档专属配置项（C1）。

**逐项说明哪些留下**：`ARCHIVE_API_KEY_ENCRYPTION_KEY` **没有**迁进来——
它被核心的 `app/utils/secrets.py` 使用，而核心 `Team` 的密钥加解密依赖它，
属于共享基础设施而非归档专属配置。

**注入方式**：`init(app)` 时把本模块的默认值写入 `app.config`。
- `celery.conf.app_config` 与 `app.config` 是**同一个 dict**，因此 worker 端
  通过 `celery.conf.app_config` 也能读到，任务代码无需改动读取方式。
- 只写入**尚未存在**的键，因此测试里 `self.app.config["ARCHIVE_..."] = ...`
  的既有写法依旧生效（测试在 app 创建之后才设置，本就后写覆盖）。

⚠️ **环境变量名与单位必须与平移前完全一致**，否则线上已设置的值会静默失效：
平移前核心里的写法是「环境变量名 ≠ 属性名」，且部分环境变量以 **MB** 为单位、
属性却是字节。这里照原样保留，不做"顺手统一"。
"""

from __future__ import annotations

import os

_MB = 1024 * 1024


def _raw(env_name: str, default: str = "") -> str:
    value = os.environ.get(env_name)
    if value is None or str(value).strip() == "":
        return default
    return str(value)


def _int(env_name: str, default: int) -> int:
    """读整数环境变量；非法值回落到默认值，避免笔误让模块启动失败。"""
    try:
        return int(_raw(env_name, str(default)).strip())
    except (TypeError, ValueError):
        return default


def provider_api_url() -> str:
    """第三方档案 API 的站点级默认基址（团队可覆盖）。"""
    return _raw("ARCHIVE_PROVIDER_API_URL").strip()


def provider_api_allowed_hosts() -> tuple[str, ...]:
    """团队自定义基址必须命中的主机白名单。"""
    raw = _raw("ARCHIVE_PROVIDER_API_ALLOWED_HOSTS")
    return tuple(
        host.strip().lower().rstrip(".") for host in raw.split(",") if host.strip()
    )


def max_zip_bytes() -> int:
    """归档 zip 的压缩体积上限（字节）。环境变量以 MB 计。"""
    return _int("ARCHIVE_MAX_ZIP_BYTES", 500) * _MB


def max_zip_uncompressed_bytes() -> int:
    """解压后总字节上限（防 zip 炸弹）。环境变量名是 `..._MB`。"""
    return _int("ARCHIVE_MAX_ZIP_UNCOMPRESSED_MB", 2048) * _MB


def max_zip_entries() -> int:
    """归档内图片条目数量上限。"""
    return _int("ARCHIVE_MAX_ZIP_ENTRIES", 5000)


def max_entry_bytes() -> int:
    """单张图片解压大小上限（字节）。环境变量以 MB 计。"""
    return _int("ARCHIVE_MAX_ENTRY_BYTES", 128) * _MB


def default_config() -> dict:
    """本模块贡献给 `app.config` 的默认值。

    键名与平移前核心 `app/config.py` 里的**属性名**完全一致，
    因此任务里 `config.get("ARCHIVE_MAX_ZIP_BYTES")` 这类读法一句都不用改。
    """
    return {
        "ARCHIVE_PROVIDER_API_URL": provider_api_url(),
        "ARCHIVE_PROVIDER_API_ALLOWED_HOSTS": provider_api_allowed_hosts(),
        "ARCHIVE_MAX_ZIP_BYTES": max_zip_bytes(),
        "ARCHIVE_MAX_ZIP_UNCOMPRESSED_BYTES": max_zip_uncompressed_bytes(),
        "ARCHIVE_MAX_ZIP_ENTRIES": max_zip_entries(),
        "ARCHIVE_MAX_ENTRY_BYTES": max_entry_bytes(),
    }


def apply_to_app(app) -> None:
    """把默认值注入 `app.config`，不覆盖已存在的键。

    「不覆盖」是为了让测试与显式配置优先——测试在 app 创建后写
    `self.app.config[...]`，而这里发生在 app 创建过程中。
    """
    for key, value in default_config().items():
        if key not in app.config:
            app.config[key] = value