"""从第三方档案 API 导入画廊归档 zip 到项目。

流程：多 key 按序解析归档直链（NEW_API 契约）→ 流式下载 zip → CRC 校验 →
解包仅图片 → 逐个 ``project.upload``（复用现有 OSS/缩略图/权限体系）。
任务状态写入 ``ArchiveImportTask`` 供前端轮询。
"""

from __future__ import annotations

import io
import os
import re
import shutil
import tempfile
import zipfile
import zlib
from urllib.parse import urljoin, urlparse

import requests
from celery.utils.log import get_task_logger

from app import TMP_PATH, celery
from app.constants.archive_import import ArchiveImportStatus
from app.models import connect_db
from app.tasks import SyncResult, _FORCE_SYNC_TASK
from app.utils.secrets import decrypt_secret
from app.validators.archive_import import normalize_archive_api_url

logger = get_task_logger(__name__)

# 移植自 eh-tools archive_resolvers.py：hath.network H@H 归档直链形态
ARCHIVE_URL_PATTERN = re.compile(
    r"https://(?P<host>[A-Za-z0-9-]+)\.hath\.network/archive/"
    r"[^/\s\"']+/(?P<key_a>[A-Za-z0-9]+)/(?P<key_b>[A-Za-z0-9]+)/"
)

IMAGE_SUFFIXES = {"jpg", "jpeg", "png", "bmp", "gif", "webp"}


def canonicalize_archive_url(raw_url: str, gid: str) -> str | None:
    """把 H@H archive URL 规范化为 aria2-safe 形态（与 eh-tools 一致）。"""
    match = ARCHIVE_URL_PATTERN.search(raw_url)
    if not match:
        return None
    return (
        f"https://{match.group('host')}.hath.network/archive/{gid}/"
        f"{match.group('key_a')}/{match.group('key_b')}/2?start=1"
    )


def resolve_archive_url(api_url: str, gid: str, token: str, api_key: str) -> str | None:
    """NEW_API 契约：POST {api_url}/api/v1/parse，Bearer key。

    成功返回规范化的 hath.network 直链；任何非 200 / 含 error / 解析失败返回 None。
    """
    try:
        api_url = normalize_archive_api_url(api_url)
    except ValueError:
        return None
    endpoint = urljoin(f"{api_url.rstrip('/')}/", "api/v1/parse")
    try:
        response = requests.post(
            endpoint,
            json={
                "gallery_id": str(gid),
                "gallery_key": str(token),
                "force": False,
            },
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Client": "",
            },
            timeout=(5, 20),
            allow_redirects=False,
        )
    except requests.RequestException:
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict) or "error" in payload:
        return None
    raw_url = payload.get("archive_url")
    if not isinstance(raw_url, str):
        return None
    return canonicalize_archive_url(raw_url, str(gid))


def _download_zip(zip_url: str, max_bytes: int) -> str:
    """流式下载 zip 到 TMP_PATH 下临时目录，返回 zip 文件路径。

    大小超过 ``max_bytes`` 抛 ``ValueError``；仅允许 https。
    """
    parsed = urlparse(zip_url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not re.fullmatch(r"[a-z0-9-]+\.hath\.network", host)
        or parsed.username
        or parsed.password
    ):
        raise ValueError("归档直链必须使用 https")
    # 容器/部署环境可能没有预建临时目录：确保存在再 mkdtemp（防止毛糙崩溃卡死任务）
    os.makedirs(TMP_PATH, exist_ok=True)
    work_dir = tempfile.mkdtemp(prefix="moeflow-archive-", dir=TMP_PATH)
    zip_path = os.path.join(work_dir, "archive.zip")
    try:
        response = requests.get(
            zip_url,
            stream=True,
            timeout=(5, 60),
            allow_redirects=False,
        )
        response.raise_for_status()
        final_url = urlparse(response.url)
        final_host = (final_url.hostname or "").lower().rstrip(".")
        if final_url.scheme != "https" or not re.fullmatch(
            r"[a-z0-9-]+\.hath\.network", final_host
        ):
            raise ValueError("归档下载重定向地址必须是 hath.network HTTPS 地址")
        downloaded = 0
        with open(zip_path, "wb") as out:
            for chunk in response.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                downloaded += len(chunk)
                if downloaded > max_bytes:
                    raise ValueError(f"归档超过大小上限 {max_bytes} bytes")
                out.write(chunk)
        return zip_path
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise


def _stream_test_zip(
    zip_path: str,
    *,
    max_entries: int | None = None,
    max_uncompressed_bytes: int | None = None,
    max_entry_bytes: int | None = None,
) -> int:
    """逐条目流式读取并 CRC 校验，返回解压后总字节数。

    移植自 eh-tools archiver._stream_test_zip：能证明 zip 结构完整、可正常解压。
    """
    try:
        with zipfile.ZipFile(zip_path) as archive:
            infos = archive.infolist()
            if max_entries is not None and len(infos) > max_entries:
                raise ValueError(f"归档条目数量超过上限 {max_entries}")
            content_bytes = 0
            for entry in infos:
                if entry.is_dir():
                    continue
                entry_size = int(entry.file_size)
                if max_entry_bytes is not None and entry_size > max_entry_bytes:
                    raise ValueError(f"归档单条目大小超过上限 {max_entry_bytes} bytes")
                content_bytes += entry_size
                if (
                    max_uncompressed_bytes is not None
                    and content_bytes > max_uncompressed_bytes
                ):
                    raise ValueError(
                        f"归档解压后大小超过上限 {max_uncompressed_bytes} bytes"
                    )
                actual_bytes = 0
                with archive.open(entry) as member:
                    while chunk := member.read(1024 * 1024):
                        actual_bytes += len(chunk)
                        if (
                            max_entry_bytes is not None
                            and actual_bytes > max_entry_bytes
                        ):
                            raise ValueError(
                                f"归档单条目大小超过上限 {max_entry_bytes} bytes"
                            )
        return content_bytes
    except ValueError:
        raise
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        zlib.error,
        EOFError,
        RuntimeError,
        ValueError,
        OSError,
    ):
        raise ValueError("归档不是有效的 zip 或已损坏")


def _zip_image_entries(zip_path: str, max_entries: int):
    """列出 zip 内待导入的图片条目，返回 [(ZipInfo, 规范化文件名), ...]。

    - 剥掉 ``images/`` 前缀（现有导入约定）
    - 嵌套目录取 basename（防压缩包内子目录/路径注入）
    - 跳过隐藏文件、非图片、目录项
    """
    entries = []
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = info.filename
            # zip 炸弹保护：单条目解压大小上限由调用方按配置检查
            if name.startswith("images/"):
                name = name[len("images/") :]
            name = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            name = name.strip()
            if not name or name.startswith("."):
                continue
            suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if suffix not in IMAGE_SUFFIXES:
                continue
            entries.append((info, name))
        if len(entries) > max_entries:
            raise ValueError(f"归档图片数量超过上限 {max_entries}")
    return entries


@celery.task(name="tasks.archive_import_task")
def archive_import_task(project_id, gid, token, task_id=None):
    """任务主体：解析直链 → 下载 → 校验 → 导入。"""
    from app.models.archive_import import ArchiveImportTask
    from app.models.file import Filename
    from app.models.project import Project

    connect_db(celery.conf.app_config)
    try:
        from app import oss

        oss.init(celery.conf.app_config)
    except Exception as exc:  # 本地存储等场景可无 OSS
        logger.warning("oss.init failed (continue): %s", exc)

    # New dispatches carry the exact task id.  The field-matching fallback is
    # kept for old queued messages and tests, but never chooses an unrelated
    # newer task for the same project.
    if task_id:
        task = ArchiveImportTask.objects(id=task_id, project=project_id).first()
    else:
        task = (
            ArchiveImportTask.objects(
                project=project_id, gid=str(gid), token=str(token)
            )
            .order_by("-id")
            .first()
        )
    if task is None:
        return f"跳过：导入任务不存在 Project<{project_id}>"
    if task.status != ArchiveImportStatus.QUEUED:
        return f"跳过：任务不在排队状态 Task<{task.id}>"
    if not task.claim():
        return f"跳过：任务已被其他 worker 领取 Task<{task.id}>"
    # The persisted task is the source of truth once it has been correlated by
    # id.  This prevents a stale Celery payload from changing the gallery being
    # imported under a valid task record.
    gid = task.gid
    token = task.token
    project = Project.objects(id=project_id).first()
    if project is None:
        task.set_progress(
            status=ArchiveImportStatus.FAILED, stage="项目不存在", error="项目不存在"
        )
        return f"失败：项目不存在 {project_id}"

    config = celery.conf.app_config
    # 团队可配置自己的档案 API 基址（优先级高于全局）；留空则使用系统默认。
    team = project.team
    team_api_url = str(team.archive_api_url if team is not None else "").strip()
    configured_api_url = (
        team_api_url or str(config.get("ARCHIVE_PROVIDER_API_URL", "")).strip()
    )
    if not configured_api_url:
        task.set_progress(
            status=ArchiveImportStatus.FAILED,
            stage="未配置档案 API",
            error="系统未配置档案 API 地址",
        )
        return f"失败：未配置 ARCHIVE_PROVIDER_API_URL Project<{project_id}>"
    try:
        api_url = normalize_archive_api_url(
            configured_api_url,
            allowed_hosts=config.get("ARCHIVE_PROVIDER_API_ALLOWED_HOSTS", ()),
            require_allowlist=bool(team_api_url),
        )
    except ValueError as exc:
        task.set_progress(
            status=ArchiveImportStatus.FAILED,
            stage="档案 API 地址无效",
            error=str(exc),
        )
        return f"失败：档案 API 地址无效 {exc}"
    max_zip_bytes = int(config.get("ARCHIVE_MAX_ZIP_BYTES", 500 * 1024 * 1024))
    max_entries = int(config.get("ARCHIVE_MAX_ZIP_ENTRIES", 5000))
    max_entry_bytes = int(config.get("ARCHIVE_MAX_ENTRY_BYTES", 128 * 1024 * 1024))
    max_uncompressed_bytes = int(
        config.get(
            "ARCHIVE_MAX_ZIP_UNCOMPRESSED_BYTES",
            2048 * 1024 * 1024,
        )
    )

    keys = []
    for item in (team.archive_api_keys if team is not None else []) or []:
        if not isinstance(item, dict) or item.get("enabled", True) is False:
            continue
        try:
            key = decrypt_secret(item.get("key", "")).strip()
        except ValueError:
            logger.warning("Skipping an archive API key that cannot be decrypted")
            continue
        if key:
            keys.append(key)
    if not keys:
        task.set_progress(
            status=ArchiveImportStatus.FAILED,
            stage="未配置档案 API key",
            error="团队未配置档案 API key，请联系管理员",
        )
        return f"失败：团队未配置 archive_api_keys Project<{project_id}>"

    task.set_progress(error="")
    zip_url = None
    for key in keys:
        zip_url = resolve_archive_url(api_url, gid, token, key)
        if zip_url:
            break
    if zip_url is None:
        task.set_progress(
            status=ArchiveImportStatus.FAILED,
            stage="解析归档链接失败",
            error="无法从档案 API 获取归档链接（所有 key 均已尝试）",
        )
        return f"失败：归档解析失败 Project<{project_id}>"

    task.set_progress(
        status=ArchiveImportStatus.DOWNLOADING,
        stage="下载归档",
        zip_url=zip_url,
        error="",
    )
    try:
        zip_path = _download_zip(zip_url, max_zip_bytes)
    except ValueError as exc:
        task.set_progress(
            status=ArchiveImportStatus.FAILED, stage="下载失败", error=str(exc)
        )
        return f"失败：下载失败 {exc}"
    except requests.RequestException as exc:
        task.set_progress(
            status=ArchiveImportStatus.FAILED,
            stage="下载失败",
            error=f"归档下载异常：{exc}",
        )
        return f"失败：下载异常 {exc}"
    except OSError as exc:  # 临时目录缺失/磁盘问题等——必须落 FAILED 而不是毛糙崩溃
        task.set_progress(
            status=ArchiveImportStatus.FAILED,
            stage="下载失败",
            error=f"本地临时目录不可用：{exc}",
        )
        return f"失败：本地临时目录不可用 {exc}"

    try:
        task.set_progress(status=ArchiveImportStatus.VALIDATING, stage="校验归档")
        _stream_test_zip(
            zip_path,
            max_entries=max_entries,
            max_uncompressed_bytes=max_uncompressed_bytes,
            max_entry_bytes=max_entry_bytes,
        )

        entries = _zip_image_entries(zip_path, max_entries)
        if not entries:
            raise ValueError("归档内没有可导入的图片")

        total = len(entries)
        task.set_progress(
            status=ArchiveImportStatus.IMPORTING,
            stage="导入图片",
            total=total,
            completed=0,
        )
        imported = 0
        skipped = 0
        with zipfile.ZipFile(zip_path) as archive:
            for index, (info, name) in enumerate(entries):
                if info.file_size > max_entry_bytes:
                    skipped += 1
                    continue
                # Filename 校验（拒绝 \ / : * ? " < > | 等），失败条目跳过
                try:
                    Filename(name)  # noqa: F401
                except Exception:
                    skipped += 1
                    continue
                try:
                    with archive.open(info) as member:
                        data = io.BytesIO(member.read(max_entry_bytes + 1))
                    if data.getbuffer().nbytes > max_entry_bytes:
                        raise ValueError(
                            f"归档单条目大小超过上限 {max_entry_bytes} bytes"
                        )
                    project.upload(name, data)
                    imported += 1
                except Exception as exc:  # 单图失败不阻断整体导入
                    logger.warning("archive import skipped %s: %s", name, exc)
                    skipped += 1
                task.set_progress(completed=index + 1)
        task.set_progress(
            status=ArchiveImportStatus.SUCCEEDED,
            stage=f"导入完成（{imported} 张）",
            completed=imported,
            error="",
        )
        return f"成功：导入 {imported} 张图片 Project<{project_id}>"
    except ValueError as exc:
        task.set_progress(
            status=ArchiveImportStatus.FAILED, stage="导入失败", error=str(exc)
        )
        return f"失败：{exc}"
    except Exception as exc:  # 兜底：任何意外异常都必须落 FAILED，不能让任务停在中间态
        logger.exception("archive import crashed for project %s", project_id)
        task.set_progress(
            status=ArchiveImportStatus.FAILED, stage="导入失败", error=str(exc)
        )
        return f"失败：{exc}"
    finally:
        work_dir = os.path.dirname(zip_path)
        shutil.rmtree(work_dir, ignore_errors=True)


def import_archive_from_gallery(
    project_id, gid, token, /, *, task_id=None, run_sync=False
):
    """触发归档导入：测试强制同步；broker 不可达时回退同步执行。"""
    if _FORCE_SYNC_TASK or run_sync:
        archive_import_task(project_id, gid, token, task_id)
        return SyncResult()
    try:
        return archive_import_task.delay(project_id, gid, token, task_id)
    except Exception:
        logger.exception("Failed to queue archive import; running synchronously")
        archive_import_task(project_id, gid, token, task_id)
        return SyncResult()
