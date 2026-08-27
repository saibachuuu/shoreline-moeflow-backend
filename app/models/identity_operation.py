"""Idempotency records used by ordered member operations."""

import datetime

from mongoengine import (
    DateTimeField,
    DictField,
    Document,
    ReferenceField,
    StringField,
)


class IdentityOperation(Document):
    project = ReferenceField("Project", required=True)
    operation_id = StringField(required=True)
    status = StringField(
        required=True, choices=("processing", "succeeded", "failed")
    )
    result = DictField(default=dict)
    error_code = StringField(null=True)
    create_time = DateTimeField(default=datetime.datetime.utcnow)
    edit_time = DateTimeField(default=datetime.datetime.utcnow)
    # A processing record is recoverable after its lease expires.  These
    # fields are optional so records created before lease support remain
    # readable and can use ``edit_time`` as their legacy lease heartbeat.
    lease_expires_at = DateTimeField(null=True)
    claim_token = StringField(null=True)

    meta = {"indexes": [{"fields": ["project", "operation_id"], "unique": True}]}
