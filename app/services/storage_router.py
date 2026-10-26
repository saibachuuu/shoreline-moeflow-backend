"""按项目选储存后端（核心，保持通用）。

核心只需要知道一件事：**这个项目该往哪个存储读写**。
这里刻意不出现任何具体模块名、来源类型或凭据概念 —— 那属于可选模块。

工作方式与 ``app/services/notifications.py`` 的发布者同构：
核心只提供一个空注册表，模块在 ``init()`` 时挂解析器；

    storage_router.register(resolve)      # 模块侧
    backend = storage_router.backend_for(project) or oss   # 调用方

``resolve(source_id)`` 应返回一个与 :class:`app.services.oss.OSS` 同签名的后端
（鸭子类型：``sign_url`` / ``download`` / ``upload`` / ``delete`` / ``is_exist`` /
``list_objects`` / ``head_object`` / ``copy_object``），或 ``None`` 表示「不认识这个 id」。

**注册表为空时，``backend_for()`` 永远返回 ``None``** —— 因此这套路由接进来
之后（模块尚未安装/未启用时），行为与改造前完全一致：一切走全局 ``oss``。
"""

import logging

from bson import ObjectId

logger = logging.getLogger(__name__)

#: 解析器列表。每个元素是 ``Callable[[ObjectId], backend | None]``。
_resolvers = []

#: 请求级缓存用的 ``flask.g`` 属性名。
_REQUEST_CACHE_ATTR = "_storage_router_backend_cache"


def register(resolver):
    """注册一个解析器。重复注册会被忽略，避免模块热重载后链式重复调用。"""
    if resolver in _resolvers:
        return resolver
    _resolvers.append(resolver)
    logger.debug("储存解析器已注册：%s", getattr(resolver, "__qualname__", resolver))
    return resolver


def unregister(resolver):
    """反注册（仅测试与模块卸载用）。"""
    if resolver in _resolvers:
        _resolvers.remove(resolver)


def clear():
    """清空注册表（仅测试用）。"""
    _resolvers.clear()


def has_resolvers() -> bool:
    return bool(_resolvers)


def _ref_id(ref):
    """从 DBRef / Document / 裸 id 里取出 ObjectId，不触发解引用。"""
    if ref is None:
        return None
    if isinstance(ref, ObjectId):
        return ref
    # bson.DBRef 与 Document 都有 .id
    return getattr(ref, "id", None)


def _request_cache():
    """当前请求的解析缓存；不在请求上下文里则返回 ``None``（不缓存）。

    列表接口里同一页文件通常属于同一个项目，缓存让「每行解析一次」
    退化为「每页解析一次」，这是避免 N+1 的关键。
    """
    try:
        from flask import g, has_request_context

        if not has_request_context():
            return None
        cache = getattr(g, _REQUEST_CACHE_ATTR, None)
        if cache is None:
            cache = {}
            setattr(g, _REQUEST_CACHE_ATTR, cache)
        return cache
    except Exception:  # pragma: no cover - flask 异常上下文里也允许继续
        return None


def _cache_key(project_id):
    """缓存键统一成 str：同一项目可能一次以 ObjectId、一次以 str 传进来。"""
    return str(project_id)


def backend_for(project):
    """返回该项目应使用的储存后端；``None`` 表示「用默认存储」。

    注意：这里的 ``None`` 是**正常返回值**，不是错误。调用方必须写成
    ``backend_for(project) or oss``。
    """
    if project is None or not _resolvers:
        return None
    raw_id = _ref_id(getattr(project, "id", None))
    cache_key = _cache_key(raw_id) if raw_id is not None else None
    cache = _request_cache()
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    backend = _resolve_source(getattr(project, "storage_source_id", None))
    if cache is not None and cache_key is not None:
        cache[cache_key] = backend
    return backend


def backend_for_id(project_id):
    """按项目 id 解析（文件序列化与任务里通常只有 id）。

    读不到项目、没有注册器、或无人认领该来源时都返回 ``None``（走默认存储），
    不抛异常。
    """
    if not _resolvers or not project_id:
        return None
    project_id = _ref_id(project_id) or project_id
    cache_key = _cache_key(project_id)

    cache = _request_cache()
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    from app.models.project import Project

    project = Project.objects(id=project_id).only("storage_source_id").first()
    backend = (
        _resolve_source(getattr(project, "storage_source_id", None))
        if project is not None
        else None
    )
    if cache is not None:
        cache[cache_key] = backend
    return backend


def _resolve_source(source_id):
    """把来源 id 交给注册器，第一个认领的赢。"""
    if not source_id:
        return None
    for resolver in list(_resolvers):
        try:
            backend = resolver(source_id)
        except Exception:
            # 某个模块的解析器炸了不该让整个读路径挂掉：退化为默认存储，
            # 至少文件还能读（可能读到旧位置，但比 500 好）。
            logger.exception(
                "储存解析器失败，已退化为默认存储：source_id=%s", source_id
            )
            continue
        if backend is not None:
            return backend
    logger.warning("储存来源无人认领，已退化为默认存储：source_id=%s", source_id)
    return None
