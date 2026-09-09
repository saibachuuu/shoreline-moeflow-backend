"""Runtime identity-tag definitions and team policy storage.

The migration input remains in the legacy models.  These definitions are the
only source used by the new runtime permission service.
"""

import datetime
from typing import Iterable

from app.translations import lazy_gettext
from mongoengine import (
    BooleanField,
    DateTimeField,
    Document,
    DictField,
    IntField,
    ListField,
    ReferenceField,
    StringField,
)

from app.exceptions import InvalidIdentityRequestError


PROJECT_TAGS = (
    "creator",
    "admin",
    "raw_provider",
    "scanner",
    "cropper",
    "cleaner",
    "translator",
    "proofreader",
    "typesetter",
)
WORKER_TAGS = frozenset(PROJECT_TAGS[2:])
TEAM_BASE_TAGS = frozenset(("creator", "admin", "member"))

# 工作人员职位的有序列表与展示名（与前端 PROJECT_WORKER_ROLES 一致）。
WORKER_TAG_ORDER = tuple(PROJECT_TAGS[2:])
WORKER_TAG_LABELS = {
    "raw_provider": lazy_gettext("图源"),
    "scanner": lazy_gettext("扫图"),
    "cropper": lazy_gettext("裁切"),
    "cleaner": lazy_gettext("修图"),
    "translator": lazy_gettext("翻译"),
    "proofreader": lazy_gettext("校对"),
    "typesetter": lazy_gettext("嵌字"),
}

PROJECT_PERMISSION_NAMES = (
    "ACCESS",
    "CHANGE",
    "COMPLETE_PROJECT",
    "ADD_FILE",
    "MOVE_FILE",
    "RENAME_FILE",
    "DELETE_FILE",
    "OUTPUT_TRA",
    "ADD_LABEL",
    "MOVE_LABEL",
    "DELETE_LABEL",
    "ADD_TRA",
    "DELETE_TRA",
    "PROOFREAD_TRA",
    "CHECK_TRA",
    "ADD_TARGET",
    "CHANGE_TARGET",
    "DELETE_TARGET",
    "CHECK_USER",
    "INVITE_USER",
    "CHANGE_USER_REMARK",
    "CHANGE_USER_ROLE",
    "DELETE_USER",
    "MANAGE_MEMBERS",
)
PROJECT_PERMISSION_CODES = tuple(f"project:{name}" for name in PROJECT_PERMISSION_NAMES)

_FILE_PERMISSIONS = frozenset(("ADD_FILE", "MOVE_FILE", "RENAME_FILE", "DELETE_FILE"))
_TRANSLATION_PERMISSIONS = frozenset(
    ("OUTPUT_TRA", "ADD_LABEL", "MOVE_LABEL", "DELETE_LABEL", "ADD_TRA")
)
_PROOFREAD_PERMISSIONS = frozenset(
    (
        "OUTPUT_TRA",
        "ADD_LABEL",
        "MOVE_LABEL",
        "DELETE_LABEL",
        "ADD_TRA",
        "DELETE_TRA",
        "PROOFREAD_TRA",
        "CHECK_TRA",
    )
)
_TRANSLATOR_PERMISSIONS = frozenset(
    ("OUTPUT_TRA", "ADD_LABEL", "MOVE_LABEL", "DELETE_LABEL", "ADD_TRA", "DELETE_TRA")
)
_TYPESETTER_PERMISSIONS = frozenset(
    ("OUTPUT_TRA", "ADD_LABEL", "MOVE_LABEL", "DELETE_LABEL")
)


def _project_permissions(*names: str) -> frozenset[str]:
    return frozenset(f"project:{name}" for name in names)


PROJECT_TAG_PERMISSIONS = {
    "creator": _project_permissions(*PROJECT_PERMISSION_NAMES),
    "admin": _project_permissions(
        "ACCESS",
        "CHANGE",
        "COMPLETE_PROJECT",
        "ADD_FILE",
        "MOVE_FILE",
        "RENAME_FILE",
        "DELETE_FILE",
        "OUTPUT_TRA",
        "ADD_LABEL",
        "MOVE_LABEL",
        "DELETE_LABEL",
        "ADD_TRA",
        "DELETE_TRA",
        "PROOFREAD_TRA",
        "CHECK_TRA",
        "ADD_TARGET",
        "CHANGE_TARGET",
        "DELETE_TARGET",
        "CHECK_USER",
        "INVITE_USER",
        "CHANGE_USER_REMARK",
        "CHANGE_USER_ROLE",
        "DELETE_USER",
        "MANAGE_MEMBERS",
    ),
    "raw_provider": _project_permissions("ACCESS", *_FILE_PERMISSIONS),
    "scanner": _project_permissions("ACCESS", *_FILE_PERMISSIONS),
    "cropper": _project_permissions("ACCESS", *_FILE_PERMISSIONS),
    "cleaner": _project_permissions("ACCESS", *_FILE_PERMISSIONS),
    "translator": _project_permissions(
        "ACCESS", *_FILE_PERMISSIONS, *_TRANSLATOR_PERMISSIONS
    ),
    "proofreader": _project_permissions(
        "ACCESS", *_FILE_PERMISSIONS, *_PROOFREAD_PERMISSIONS
    ),
    "typesetter": _project_permissions(
        "ACCESS", *_FILE_PERMISSIONS, *_TYPESETTER_PERMISSIONS
    ),
}

# Team custom policy may only grant permissions from this allow-list.  The
# project inheritance permissions are deliberately handled by the permission
# service and cannot be granted by a team tag.
TEAM_PERMISSION_CODES = frozenset(
    {
        "team:ACCESS",
        "team:DELETE",
        "team:CHANGE",
        "team:CREATE_ROLE",
        "team:DELETE_ROLE",
        "team:CHECK_USER",
        "team:INVITE_USER",
        "team:DELETE_USER",
        "team:CHANGE_USER_ROLE",
        "team:CHANGE_USER_REMARK",
        "team:CREATE_PROJECT",
        "team:CREATE_PROJECT_SET",
        "team:CHANGE_PROJECT_SET",
        "team:DELETE_PROJECT_SET",
        "team:USE_OCR_QUOTA",
        "team:USE_MT_QUOTA",
        "team:INSIGHT",
        "team:ACCESS_TERM_BANK",
        "team:CREATE_TERM_BANK",
        "team:CHANGE_TERM_BANK",
        "team:DELETE_TERM_BANK",
        "team:CREATE_TERM",
        "team:CHANGE_TERM",
        "team:DELETE_TERM",
    }
)

TEAM_BASE_PERMISSIONS = {
    "creator": frozenset(
        {
            "team:ACCESS",
            "team:DELETE",
            "team:CHANGE",
            "team:CREATE_ROLE",
            "team:DELETE_ROLE",
            "team:CHECK_USER",
            "team:INVITE_USER",
            "team:DELETE_USER",
            "team:CHANGE_USER_ROLE",
            "team:CHANGE_USER_REMARK",
            "team:CREATE_TERM_BANK",
            "team:ACCESS_TERM_BANK",
            "team:CHANGE_TERM_BANK",
            "team:DELETE_TERM_BANK",
            "team:CREATE_TERM",
            "team:CHANGE_TERM",
            "team:DELETE_TERM",
            "team:CREATE_PROJECT",
            "team:CREATE_PROJECT_SET",
            "team:CHANGE_PROJECT_SET",
            "team:DELETE_PROJECT_SET",
            "team:USE_OCR_QUOTA",
            "team:USE_MT_QUOTA",
            "team:INSIGHT",
        }
    ),
    "admin": frozenset(
        {
            "team:ACCESS",
            "team:CHANGE",
            "team:CREATE_ROLE",
            "team:DELETE_ROLE",
            "team:CHECK_USER",
            "team:INVITE_USER",
            "team:DELETE_USER",
            "team:CHANGE_USER_ROLE",
            "team:CHANGE_USER_REMARK",
            "team:CREATE_TERM_BANK",
            "team:ACCESS_TERM_BANK",
            "team:CHANGE_TERM_BANK",
            "team:DELETE_TERM_BANK",
            "team:CREATE_TERM",
            "team:CHANGE_TERM",
            "team:DELETE_TERM",
            "team:CREATE_PROJECT",
            "team:CREATE_PROJECT_SET",
            "team:CHANGE_PROJECT_SET",
            "team:DELETE_PROJECT_SET",
            "team:USE_OCR_QUOTA",
            "team:USE_MT_QUOTA",
            "team:INSIGHT",
        }
    ),
    "member": frozenset({"team:ACCESS"}),
}

SYSTEM_PROJECT_TAG_DEFINITIONS = {
    tag: {
        "name": tag,
        "permissions": sorted(PROJECT_TAG_PERMISSIONS[tag]),
        "assignable": tag not in {"creator"},
        "source": "site",
    }
    for tag in PROJECT_TAGS
}


class IdentityTagDefinition(Document):
    """Optional site definition document kept for future policy tooling."""

    scope = StringField(required=True, choices=("team", "project"))
    code = StringField(required=True)
    name = StringField(required=True)
    permissions = ListField(StringField(), default=list)
    assignable = BooleanField(default=True)
    create_time = DateTimeField(default=datetime.datetime.utcnow)

    meta = {"indexes": [{"fields": ["scope", "code"], "unique": True}]}


class IdentityTagPolicy(Document):
    """Per-team overrides for the read-only site tag policy."""

    team = ReferenceField("Team", required=True)
    team_tags = DictField(default=dict)
    project_tags = DictField(default=dict)
    version = IntField(default=0, required=True)
    create_time = DateTimeField(default=datetime.datetime.utcnow)
    edit_time = DateTimeField(default=datetime.datetime.utcnow)

    meta = {"indexes": [{"fields": ["team"], "unique": True}]}

    def effective(self, scope: str) -> dict:
        if scope == "team":
            return self.team_tags
        if scope == "project":
            return self.project_tags
        raise ValueError("scope must be team or project")


def normalize_tag_list(tags: Iterable[str] | None) -> list[str]:
    """Normalize tag order without granting or validating any tag."""

    if tags is None:
        return []
    if not isinstance(tags, (list, tuple, set, frozenset)):
        raise InvalidIdentityRequestError("tags must be an array")
    values = set()
    for tag in tags:
        if not isinstance(tag, str):
            raise InvalidIdentityRequestError("tags must contain strings")
        tag = tag.strip()
        if tag:
            values.add(tag)
    return sorted(values)
