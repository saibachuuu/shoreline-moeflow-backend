"""Database-driven scans: publishing never depends on a successful broker write."""

import uuid
import time
from datetime import timedelta
from flask import current_app
from app import celery
from app.models.notification import (
    Notification,
    NotificationAudienceChunk,
    NotificationReceipt,
    NotificationDelivery,
)
from app.models.user import User
from app.services import notifications as svc
from app.services.notification_content import NotificationError, render_html, plain
from app.services.notification_mail import send_notification_mail

BATCH = 100


def dispatch(notification_id):
    now = svc.utcnow()
    token = str(uuid.uuid4())
    note = Notification.objects(
        id=notification_id,
        revoked_at=None,
        state__in=["scheduled", "preparing", "dispatching"],
        publish_at__lte=now,
        lease_until__lte=now,
    ).modify(
        new=True, set__lease_token=token, set__lease_until=now + timedelta(seconds=120)
    )
    if not note:
        return

    def guard():
        return Notification.objects(id=note.id, lease_token=token, revoked_at=None)

    try:
        if not note.publish_audited:
            svc.audit(note.actor_id, "publish", note, key=note.key + ":publish")
            note.update(set__publish_audited=True)
        if note.expires_at and note.expires_at <= now:
            guard().update_one(set__state="cancelled", set__error="expired")
            return
        if note.state == "scheduled":
            if now - note.publish_at > timedelta(hours=1):
                guard().update_one(set__state="draft", set__error="schedule_late")
                return
            guard().update_one(set__state="preparing")
            note.state = "preparing"
        if not svc.author_allowed(note):
            guard().update_one(
                set__state="cancelled", set__error="sender_permission_lost"
            )
            return
        valid = svc.recipient_ids(note)
        if note.state == "preparing":
            for _ in range(4):
                saved = NotificationAudienceChunk.objects(
                    notification_id=note.id, number=note.next_chunk
                ).first()
                if saved is None:
                    users = [
                        u for u in valid if not note.cursor or str(u) > note.cursor
                    ][:BATCH]
                    if not users:
                        guard().update_one(set__state="dispatching")
                        break
                    # The unique chunk is the durable checkpoint; recover it before advancing.
                    NotificationAudienceChunk.objects(
                        notification_id=note.id, number=note.next_chunk
                    ).update_one(
                        upsert=True,
                        set_on_insert__users=users,
                        set_on_insert__cursor=str(users[-1]),
                        set_on_insert__done=False,
                    )
                    saved = NotificationAudienceChunk.objects(
                        notification_id=note.id, number=note.next_chunk
                    ).first()
                changed = (
                    guard()
                    .filter(state="preparing", next_chunk=note.next_chunk)
                    .update_one(
                        set__cursor=saved.cursor,
                        inc__next_chunk=1,
                        inc__recipient_count=len(saved.users),
                    )
                )
                if not changed:
                    return
                note.cursor, note.next_chunk = saved.cursor, note.next_chunk + 1
            return
        valid = set(valid)
        for chunk in (
            NotificationAudienceChunk.objects(notification_id=note.id, done=False)
            .order_by("number")
            .limit(4)
        ):
            for user_id in chunk.users:
                if not guard().filter(state="dispatching").first():
                    return
                if user_id not in valid:
                    continue
                NotificationReceipt.objects(
                    notification_id=note.id, user_id=user_id
                ).update_one(
                    upsert=True,
                    set_on_insert__created_at=svc.utcnow(),
                    set_on_insert__archived=False,
                )
                if note.requested_email:
                    NotificationDelivery.objects(
                        notification_id=note.id, user_id=user_id
                    ).update_one(
                        upsert=True,
                        set_on_insert__state="pending",
                        set_on_insert__attempts=0,
                        set_on_insert__next_attempt_at=svc.utcnow(),
                        set_on_insert__lease_until=svc.utcnow(),
                        set_on_insert__message_id=f"<notification.{note.id}.{user_id}@moeflow.invalid>",
                    )
            chunk.update(set__done=True)
        if not NotificationAudienceChunk.objects(
            notification_id=note.id, done=False
        ).first():
            guard().filter(state="dispatching").update_one(
                set__state="published",
                set__recipient_count=NotificationReceipt.objects(
                    notification_id=note.id
                ).count(),
            )
    except NotificationError:
        guard().update_one(set__state="cancelled", set__error="invalid_audience")
    finally:
        Notification.objects(id=note.id, lease_token=token).update_one(
            set__lease_until=svc.utcnow(), set__lease_token=""
        )


def deliver(delivery_id):
    now = svc.utcnow()
    token = str(uuid.uuid4())
    delivery = NotificationDelivery.objects(
        id=delivery_id, state__in=["pending", "retry_wait"], next_attempt_at__lte=now
    ).modify(
        new=True,
        set__state="leased",
        set__lease_token=token,
        set__lease_until=now + timedelta(seconds=120),
        inc__attempts=1,
    )
    if not delivery:
        return
    note = Notification.objects(id=delivery.notification_id).first()
    user = User.objects(id=delivery.user_id).first()
    state, reason = "skipped", "recipient_ineligible"
    if not note or not svc.live(note):
        state, reason = "cancelled", "revoked_or_expired"
    elif not svc.author_allowed(note):
        state, reason = "cancelled", "sender_permission_lost"
    elif svc.recipient_eligible(note, user):
        reason = svc.email_reason(user, note.category)
        if not reason:
            # Re-resolve the current address immediately before transport. Never use a stored To list.
            url = (
                current_app.config.get("SITE_ORIGIN", "").rstrip("/")
                + "/dashboard/notifications/"
                + str(note.id)
            )
            text = "您收到一条通知，请登录查看：" + url
            html = None
            subject = "萌翻通知"
            reply = None
            if note.category == "system":
                subject = note.title
                text = plain(note.nodes) + "\n" + url
                html = render_html(note.nodes) + "<p>" + url + "</p>"
            elif note.event_type == "proofread_feedback":
                subject = note.title
                text = note.payload.get("text", "")
                html = note.payload.get("html")
                reply = note.payload.get("reply_to")
            # Last global-state check; in-flight SMTP cannot be recalled after this boundary.
            if Notification.objects(
                id=note.id, revoked_at=None, state__in=["dispatching", "published"]
            ).first():
                state, reason = send_notification_mail(
                    user.email,
                    subject,
                    text,
                    html,
                    message_id=delivery.message_id,
                    reply_to=reply,
                )
            else:
                state, reason = "cancelled", "revoked"
    updates = {"set__state": state, "set__reason": reason, "set__lease_token": ""}
    if state == "retry_wait":
        if delivery.attempts >= 5:
            updates["set__state"] = "failed"
        else:
            updates["set__next_attempt_at"] = now + timedelta(
                seconds=min(3600, 30 * 2**delivery.attempts)
            )
    if state == "accepted":
        updates["set__accepted_at"] = svc.utcnow()
    NotificationDelivery.objects(
        id=delivery.id, lease_token=token, state="leased"
    ).update_one(**updates)


@celery.task(name="tasks.notification_scan", time_limit=180, ignore_result=True)
def notification_scan():
    if not current_app.config.get("ENABLE_NOTIFICATIONS"):
        return
    now = svc.utcnow()
    started = time.monotonic()
    # A crashed sender may have crossed the SMTP acceptance boundary: never blindly retry it.
    NotificationDelivery.objects(state="leased", lease_until__lte=now).update(
        set__state="unknown", set__reason="lease_expired"
    )
    for note in (
        Notification.objects(
            state__in=["scheduled", "preparing", "dispatching"],
            revoked_at=None,
            publish_at__lte=now,
            lease_until__lte=now,
        )
        .order_by("publish_at")
        .limit(10)
    ):
        if time.monotonic() - started > 45:
            break
        dispatch(note.id)
    for row in NotificationDelivery.objects(
        state__in=["pending", "retry_wait"], next_attempt_at__lte=now
    ).limit(50):
        if time.monotonic() - started > 75:
            break
        deliver(row.id)
    # Audit intents are also recoverable if the process died after a committed state update.
    for note in (
        Notification.objects(revoked_at__ne=None, moderation_audited=False)
        .order_by("revoked_at")
        .limit(100)
    ):
        svc.audit(
            note.revoked_by,
            note.moderation_action,
            note,
            {"reason": note.revoke_reason},
            key=note.moderation_key + ":revoke",
        )
        note.update(set__moderation_audited=True)
