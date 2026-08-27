"""Immutable runtime audit events for identity and lifecycle changes."""

import datetime

from mongoengine import DateTimeField, DictField, Document, ReferenceField, StringField


class IdentityAuditEvent(Document):
    actor = ReferenceField("User", null=True)
    scope = StringField(required=True, choices=("user", "team", "project"))
    action = StringField(required=True)
    project_id = StringField(null=True)
    team_id = StringField(null=True)
    member_id = StringField(null=True)
    target_user_id = StringField(null=True)
    request_id = StringField(default="")
    source = StringField(default="runtime")
    before = DictField(default=dict)
    after = DictField(default=dict)
    permission_sources = DictField(default=dict)
    create_time = DateTimeField(default=datetime.datetime.utcnow)

    meta = {
        "indexes": [
            ("scope", "action"),
            ("project_id", "create_time"),
            ("team_id", "create_time"),
            ("target_user_id", "create_time"),
        ]
    }

    def to_api(self) -> dict:
        return {
            "id": str(self.id),
            "scope": self.scope,
            "action": self.action,
            "project_id": self.project_id,
            "team_id": self.team_id,
            "member_id": self.member_id,
            "target_user_id": self.target_user_id,
            "request_id": self.request_id,
            "source": self.source,
            "before": self.before,
            "after": self.after,
            "create_time": self.create_time.isoformat(),
        }


# The design names these events by their target.  They intentionally share a
# collection so audit queries can be handled consistently.
UserAuditEvent = IdentityAuditEvent
MemberAuditEvent = IdentityAuditEvent
ProjectAuditEvent = IdentityAuditEvent
