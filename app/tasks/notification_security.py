"""Isolated security-mail queue. Queue payloads contain only verification identity/version."""

import logging
import hmac
import hashlib
from datetime import datetime
from flask import current_app, render_template
from app import celery
from app.services.notification_mail import send_notification_mail

logger = logging.getLogger(__name__)


def verification_version(code):
    # MongoDB stores datetimes at millisecond precision. A keyed digest also
    # invalidates queued tasks when a replacement code has the same expiry ms.
    value = (
        f"{code.id}:{code.expires.isoformat(timespec='milliseconds')}:{code.content}"
    )
    return hmac.new(
        str(current_app.config["SECRET_KEY"]).encode(), value.encode(), hashlib.sha256
    ).hexdigest()


@celery.task(
    name="tasks.notification_security_email",
    bind=True,
    max_retries=4,
    time_limit=35,
    ignore_result=True,
)
def security_email(self, code_id, generation):
    from app.models.v_code import VCode
    from app.constants.v_code import VCodeType

    code = VCode.objects(id=code_id).first()
    if (
        not code
        or not hmac.compare_digest(verification_version(code), generation)
        or code.expires <= datetime.utcnow()
    ):
        return "expired_or_replaced"
    definitions = {
        VCodeType.CONFIRM_EMAIL: ("确认您的安全邮箱", "confirm_email"),
        VCodeType.RESET_EMAIL: ("重置您的安全邮箱", "reset_email"),
        VCodeType.RESET_PASSWORD: ("重置您的密码", "reset_password"),
    }
    if code.type not in definitions:
        return "invalid_type"
    title, template = definitions[code.type]
    data = {
        "code": code.content,
        "site_name": current_app.config.get("SITE_NAME"),
        "site_url": current_app.config.get("SITE_ORIGIN"),
    }
    state, reason = send_notification_mail(
        code.info,
        title,
        render_template("email/" + template + ".txt", **data),
        render_template("email/" + template + ".html", **data),
        message_id=f"<security.{code.id}.{generation[:24]}@moeflow.invalid>",
    )
    logger.info("Security mail code_id=%s state=%s reason=%s", code_id, state, reason)
    if state == "retry_wait":
        # Retry reconstructs the template from the still-valid record, never stale secrets.
        raise self.retry(countdown=min(120, 15 * 2**self.request.retries))
    return state
