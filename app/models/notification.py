"""Core notification records. No foreign-key cascades: moderation history survives deletion."""

from datetime import datetime
from mongoengine import (
    Document,
    StringField,
    ObjectIdField,
    DateTimeField,
    IntField,
    BooleanField,
    DictField,
    ListField,
)


class Notification(Document):
    category = StringField(
        required=True, choices=("system", "team", "project", "personal")
    )
    source = StringField(default="core")
    event_type = StringField(default="announcement")
    actor_id = ObjectIdField(required=True)
    team_id = ObjectIdField(null=True)
    project_id = ObjectIdField(null=True)
    title = StringField(required=True, max_length=200)
    body = StringField(default="", max_length=10000)
    nodes = ListField(DictField())
    search_text = StringField(default="")
    search_tokens = ListField(StringField())
    audience = DictField(default=dict)
    requested_email = BooleanField(default=False)
    # Only the existing proofread template may carry this internal payload.
    payload = DictField(default=dict)
    state = StringField(
        default="preparing",
        choices=(
            "draft",
            "scheduled",
            "preparing",
            "dispatching",
            "published",
            "cancelled",
        ),
    )
    created_at = DateTimeField(default=datetime.utcnow)
    publish_at = DateTimeField(default=datetime.utcnow)
    expires_at = DateTimeField(null=True)
    revoked_at = DateTimeField(null=True)
    revoked_by = ObjectIdField(null=True)
    revoke_reason = StringField(default="", max_length=500)
    moderation_key = StringField(default="")
    moderation_action = StringField(default="revoke")
    moderation_audited = BooleanField(default=False)
    publish_audited = BooleanField(default=False)
    version = IntField(default=0)
    key = StringField(required=True, unique=True)
    fingerprint = StringField(required=True)
    cursor = StringField(default="")
    next_chunk = IntField(default=0)
    recipient_count = IntField(default=0)
    error = StringField(default="")
    lease_token = StringField(default="")
    lease_until = DateTimeField(default=datetime.min)
    meta = {
        "indexes": [
            ("state", "publish_at"),
            ("category", "-created_at"),
            ("team_id", "-created_at"),
            ("project_id", "-created_at"),
            ("actor_id", "-created_at"),
            "search_tokens",
            ("moderation_audited", "revoked_at"),
        ]
    }


class NotificationAudienceChunk(Document):
    notification_id = ObjectIdField(required=True)
    number = IntField(required=True)
    users = ListField(ObjectIdField())
    cursor = StringField(default="")
    done = BooleanField(default=False)
    meta = {"indexes": [{"fields": ["notification_id", "number"], "unique": True}]}


class NotificationReceipt(Document):
    notification_id = ObjectIdField(required=True)
    user_id = ObjectIdField(required=True)
    created_at = DateTimeField(default=datetime.utcnow)
    read_at = DateTimeField(null=True)
    archived = BooleanField(default=False)
    meta = {
        "indexes": [
            {"fields": ["notification_id", "user_id"], "unique": True},
            ("user_id", "-id"),
        ]
    }


class NotificationDelivery(Document):
    notification_id = ObjectIdField(required=True)
    user_id = ObjectIdField(required=True)
    state = StringField(default="pending")
    attempts = IntField(default=0)
    next_attempt_at = DateTimeField(default=datetime.utcnow)
    lease_until = DateTimeField(default=datetime.min)
    lease_token = StringField(default="")
    message_id = StringField(required=True)
    reason = StringField(default="")
    accepted_at = DateTimeField(null=True)
    meta = {
        "indexes": [
            {"fields": ["notification_id", "user_id"], "unique": True},
            ("state", "next_attempt_at"),
            "lease_until",
        ]
    }


class NotificationPreference(Document):
    user_id = ObjectIdField(required=True, unique=True)
    categories = DictField(default=dict)
    version = IntField(default=0)


class NotificationPolicy(Document):
    team_id = ObjectIdField(required=True, unique=True)
    team_admin = BooleanField(default=False)
    project_admin = BooleanField(default=False)
    version = IntField(default=0)
    updated_by = ObjectIdField(null=True)


class NotificationAudit(Document):
    actor_id = ObjectIdField(required=True)
    notification_id = ObjectIdField(null=True)
    action = StringField(required=True)
    created_at = DateTimeField(default=datetime.utcnow)
    detail = DictField(default=dict)
    key = StringField(required=True, unique=True)
    meta = {
        "indexes": [("notification_id", "-created_at"), ("actor_id", "-created_at")]
    }


class NotificationVerifiedEmail(Document):
    # New successful confirmation flows write a digest, never the code or address.
    digest = StringField(required=True, unique=True)
    verified_at = DateTimeField(default=datetime.utcnow)


class NotificationThrottle(Document):
    key = StringField(required=True, unique=True)
    count = IntField(default=0)
    expires_at = DateTimeField(required=True)
    meta = {"indexes": [{"fields": ["expires_at"], "expireAfterSeconds": 0}]}


MODELS = (
    Notification,
    NotificationAudienceChunk,
    NotificationReceipt,
    NotificationDelivery,
    NotificationPreference,
    NotificationPolicy,
    NotificationAudit,
    NotificationVerifiedEmail,
    NotificationThrottle,
)


def ensure_notification_indexes():
    for model in MODELS:
        model.ensure_indexes()
