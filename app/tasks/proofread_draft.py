"""校对稿后台任务：生成带定位标记的页面图并完成寄送。

受理请求只做快查询并创建 draft 通知（draft 不会被 notification_scan
自动派发）；本任务逐页烤图、把进度写进通知标题，完成后渲染邮件内
容、把通知翻转为 preparing，并直接执行 dispatch/deliver 完成寄送
——不依赖 beat，notification_scan 仍是任务崩溃后的兜底恢复路径。
"""

import json
import logging
import types

from flask import current_app, render_template
from flask_babel import force_locale, gettext

from app import celery
from app.models.notification import Notification, NotificationDelivery
from app.services.proofread_image import marked_image_url

logger = logging.getLogger(__name__)

MAX_PAYLOAD_BYTES = 8 * 1024 * 1024


@celery.task(name="tasks.proofread_draft_bake", time_limit=900)
def bake_and_dispatch(notification_id):
    note = Notification.objects(
        id=notification_id, state="draft", event_type="proofread_feedback"
    ).first()
    if not note:
        return
    pending = note.payload.get("pending") or {}
    pages = pending.get("changed_pages") or []
    project_name = pending.get("project_name", "")
    # 邮件在 worker 内渲染没有请求上下文语言，显式固定为站点默认语言；
    # 将来若按收件人语言分信，只需在此处替换 locale 来源
    with force_locale(current_app.config["BABEL_DEFAULT_LOCALE"]):
        try:
            from app.models.file import File

            for index, page in enumerate(pages):
                file = File.objects(id=page.get("file_id")).first()
                if file is not None:
                    page["marked_image_url"] = marked_image_url(
                        file, page.get("labels") or []
                    )
                Notification.objects(id=note.id).update_one(
                    set__title=gettext(
                        "「%(project)s」校对稿 · 正在生成标注图（%(done)d/%(total)d）",
                        project=project_name,
                        done=index + 1,
                        total=len(pages),
                    )
                )

            # 模板只引用 project.name，用轻量对象省一次查询并规避文档已删除的边界
            project = types.SimpleNamespace(name=project_name)
            template_data = {
                "site_name": pending.get("site_name", ""),
                "project": project,
                "target_language_name": pending.get("target_language_name", ""),
                "sender_name": pending.get("sender_name", ""),
                "sender_email": pending.get("sender_email"),
                "send_time": pending.get("send_time", ""),
                "total_changed_labels": pending.get("total_changed_labels", 0),
                "total_tips": pending.get("total_tips", 0),
                "changed_pages": pages,
                "project_url": pending.get("project_url", ""),
                "reply_address": pending.get("reply_address"),
            }
            payload = {
                "html": render_template(
                    "email/proofread_draft.html", **template_data
                ),
                "text": render_template("email/proofread_draft.txt", **template_data),
                "reply_to": note.payload.get("reply_to"),
                "cc_myself": note.payload.get("cc_myself", False),
                "operation": note.payload.get("operation"),
            }
            if len(json.dumps(payload, default=str).encode("utf-8")) > MAX_PAYLOAD_BYTES:
                Notification.objects(id=note.id, state="draft").update_one(
                    set__state="cancelled",
                    set__title=gettext(
                        "「%(project)s」校对稿过大，已取消寄送", project=project_name
                    ),
                    set__error="payload_too_large",
                )
                return
            flipped = Notification.objects(id=note.id, state="draft").update_one(
                set__payload=payload,
                set__title=gettext("「%(project)s」校对稿已寄送", project=project_name),
                set__state="preparing",
            )
            if not flipped:
                return  # 状态已被其他执行者处理，避免重复寄送
            from app.tasks.notification import deliver, dispatch

            dispatch(str(note.id))
            for row in NotificationDelivery.objects(
                notification_id=note.id, state__in=["pending", "retry_wait"]
            ):
                deliver(row.id)
        except Exception:
            logger.exception("proofread draft bake failed: %s", notification_id)
            Notification.objects(id=note.id, state="draft").update_one(
                set__title=gettext(
                    "「%(project)s」校对稿生成失败（请重新寄送）", project=project_name
                )
            )
