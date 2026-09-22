"""模块自有配置。

需求方确认：**各模块的配置用环境变量或单独的配置文件来控制**。
因此本文件不读、也不写核心的 `app/config.py`（那会违反 C1）。

优先级：环境变量 > `config.local.py` > 内置默认值。
`config.local.py` 必须 gitignore，用于本机开发放密钥。
"""

from __future__ import annotations

import os

# ---- 内置默认值 ----------------------------------------------------------

DEFAULTS = {
    # 官方端点
    "API_URL": "https://partner-api.ziteng.org/partner-api/v1/works/search",
    # 密钥。缺失时模块不注册（见 __init__._enabled）
    "API_KEY": "",
    # 是否连 withdrawn（已终止/撞车不做）一起查。
    # 默认 False —— 需求方确认只查已立项的。置 True 可恢复「撞车不做」信号。
    "INCLUDE_INACTIVE": False,
    # 关键词缓存有效期（秒）。默认 7 天。
    "CACHE_TTL": 7 * 24 * 3600,
    # 翻页上限。高频词命中可达 20 页，必须封顶以免占满 1 req/s 配额。
    "MAX_PAGES": 3,
    # 单请求超时（秒）
    "TIMEOUT": 20.0,
    # 两次请求之间的最小间隔（秒）。对方全局配额为 1 req/s，取 1.1s 留余量。
    "MIN_INTERVAL": 1.1,
    # 列表展示上限
    "MAX_RESULTS": 5,
    # 单次查询任务的总超时（秒）；超出按「查询失败」处理，不得当作「未查到」
    "TASK_TIMEOUT": 180.0,
}

ENV_PREFIX = "ZITENG_PARTNER_"

_BOOL_TRUE = {"1", "true", "yes", "on"}

_local_cache: dict | None = None


def _local() -> dict:
    """读取 config.local.py（若存在）。结果缓存，便于测试时清理。"""
    global _local_cache
    if _local_cache is None:
        try:
            from . import config_local  # type: ignore[attr-defined]

            _local_cache = {
                key: value
                for key, value in vars(config_local).items()
                if key.isupper()
            }
        except ImportError:
            _local_cache = {}
    return _local_cache


def reset_cache() -> None:
    """清空 config.local.py 的缓存。仅供测试使用。"""
    global _local_cache
    _local_cache = None


def _coerce(name: str, raw: str):
    """按默认值的类型解释环境变量字符串。"""
    default = DEFAULTS[name]
    if isinstance(default, bool):
        return raw.strip().lower() in _BOOL_TRUE
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw


def get(name: str):
    """按优先级取值：环境变量 > config.local.py > 默认值。"""
    if name not in DEFAULTS:
        raise KeyError(f"未知的配置项: {name}")

    env_value = os.environ.get(ENV_PREFIX + name)
    if env_value is not None and env_value != "":
        return _coerce(name, env_value)

    local_value = _local().get(name)
    if local_value is not None and local_value != "":
        return local_value

    return DEFAULTS[name]


# ---- 便捷访问器 ----------------------------------------------------------


def api_key() -> str:
    return str(get("API_KEY") or "").strip()


def api_url() -> str:
    return str(get("API_URL") or "").strip()


def include_inactive() -> bool:
    return bool(get("INCLUDE_INACTIVE"))


def cache_ttl() -> int:
    return int(get("CACHE_TTL"))


def max_pages() -> int:
    return int(get("MAX_PAGES"))


def timeout() -> float:
    return float(get("TIMEOUT"))


def min_interval() -> float:
    return float(get("MIN_INTERVAL"))


def max_results() -> int:
    return int(get("MAX_RESULTS"))


def task_timeout() -> float:
    return float(get("TASK_TIMEOUT"))