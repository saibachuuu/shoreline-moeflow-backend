"""Project-scoped identity members."""

import datetime

from mongoengine import (
    CASCADE,
    DateTimeField,
    Document,
    IntField,
    ListField,
    ReferenceField,
    StringField,
)

from app.models.identity_tag import normalize_tag_list
from app.models._indexing import ensure_named_indexes
from app.utils.search import normalize_search_text


PROJECT_MEMBER_STATUSES = frozenset(("active", "invited", "removed"))


class ProjectMember(Document):
    INDEX_DEFINITIONS = (
        (
            "project_member_identity_key_v1",
            [("ik", 1)],
            {"unique": True},
            None,
        ),
        (
            "project_member_project_user_v1",
            [("project", 1), ("user", 1)],
            {
                "unique": True,
                "partialFilterExpression": {"user": {"$type": "objectId"}},
            },
            None,
        ),
        (
            "project_member_project_external_v1",
            [("project", 1), ("external_id", 1)],
            {
                "unique": True,
                "partialFilterExpression": {
                    "external_id": {"$type": "string"}
                },
            },
            (
                [("external_id", 1)],
                {"unique": True, "sparse": True},
            ),
        ),
        (
            "project_member_status_v1",
            [("project", 1), ("status", 1)],
            {},
            None,
        ),
        (
            "project_member_tags_v1",
            [("project", 1), ("tags", 1)],
            {},
            None,
        ),
        (
            "project_member_display_name_v1",
            [("project", 1), ("display_name", 1)],
            {},
            None,
        ),
        (
            "project_member_display_name_search_v1",
            [("project", 1), ("status", 1), ("display_name_search", 1)],
            {},
            None,
        ),
    )

    project = ReferenceField("Project", required=True, reverse_delete_rule=CASCADE)
    user = ReferenceField("User", null=True, reverse_delete_rule=CASCADE)
    external_id = StringField(null=True)
    # A deterministic single-field key avoids the null semantics of compound
    # sparse indexes while enforcing the logical (project, subject) unique
    # constraint for both registered and external members.
    identity_key = StringField(required=True, db_field="ik")
    display_name = StringField(required=True, min_length=1, max_length=140)
    display_name_search = StringField(default="", db_field="dns")
    tags = ListField(StringField(), default=list)
    status = StringField(default="active", choices=tuple(PROJECT_MEMBER_STATUSES))
    create_time = DateTimeField(default=datetime.datetime.utcnow)
    edit_time = DateTimeField(default=datetime.datetime.utcnow)
    version = IntField(default=0, required=True, min_value=0)
    removed_time = DateTimeField(null=True)

    meta = {
        "indexes": [
            {
                "fields": ["identity_key"],
                "unique": True,
                "name": "project_member_identity_key_v1",
            },
            {
                "fields": ["project", "user"],
                "unique": True,
                "partialFilterExpression": {"user": {"$type": "objectId"}},
                "name": "project_member_project_user_v1",
            },
            {
                "fields": ["project", "external_id"],
                "unique": True,
                "partialFilterExpression": {"external_id": {"$type": "string"}},
                "name": "project_member_project_external_v1",
            },
            {"fields": ["project", "status"], "name": "project_member_status_v1"},
            {"fields": ["project", "tags"], "name": "project_member_tags_v1"},
            {
                "fields": ["project", "display_name"],
                "name": "project_member_display_name_v1",
            },
            {
                "fields": ["project", "status", "display_name_search"],
                "name": "project_member_display_name_search_v1",
            },
        ]
    }

    def clean(self):
        has_user = self.user is not None
        has_external = bool(self.external_id)
        if has_user == has_external:
            raise ValueError(
                "a ProjectMember must have exactly one of user or external_id"
            )
        if self.status not in PROJECT_MEMBER_STATUSES:
            raise ValueError("invalid project member status")
        if not self.display_name or not self.display_name.strip():
            raise ValueError("display_name is required")
        subject = f"u:{self.user.id}" if has_user else f"e:{self.external_id}"
        self.identity_key = f"{self.project.id}:{subject}"
        self.display_name = self.display_name.strip()
        self.display_name_search = normalize_search_text(self.display_name)
        self.tags = normalize_tag_list(self.tags)
        if self.status == "removed" and self.removed_time is None:
            self.removed_time = datetime.datetime.utcnow()
        if self.status != "removed":
            self.removed_time = None

    def to_mongo(self, *args, **kwargs):
        """Keep optional subject fields absent instead of storing nulls.

        MongoDB partial indexes correctly exclude nulls, but some test drivers
        treat a null compound-key component as an indexed value.  Omitting the
        unused subject also matches the migration output and keeps sparse
        fallback indexes equivalent to the production partial indexes.
        """
        data = super().to_mongo(*args, **kwargs)
        for field in ("user", "external_id"):
            if field in data and data[field] is None:
                del data[field]
        return data

    @classmethod
    def ensure_indexes(cls):
        ensure_named_indexes(cls, cls.INDEX_DEFINITIONS)

    @property
    def is_external(self) -> bool:
        return self.user is None

    def to_api(self, *, viewer=None, include_permissions=True) -> dict:
        from app.services.identity_permission import IdentityPermissionService

        permissions = []
        if include_permissions and self.user is not None:
            permissions = sorted(
                IdentityPermissionService.project_snapshot(self.user, self.project)
                .effective_permissions
            )
        owner = (
            self.user is not None
            and self.project.owner_user is not None
            and self.project.owner_user == self.user
            and self.status == "active"
            and "creator" in self.tags
        )
        return {
            "id": str(self.id),
            "project_id": str(self.project.id),
            "user_id": str(self.user.id) if self.user else None,
            "external_id": self.external_id,
            # Registered members carry their site identity here so member
            # management can tell users apart (display_name is a per-project
            # alias and may be identical across subjects).  Email is
            # deliberately not included: it is admin-only in User.to_api too.
            "user": (
                {
                    "id": str(self.user.id),
                    "name": self.user.name,
                    "avatar": self.user.avatar,
                    "has_avatar": self.user.has_avatar(),
                    "aliases": list(self.user.aliases or []),
                }
                if self.user is not None
                else None
            ),
            "display_name": self.display_name,
            "tags": list(self.tags),
            "effective_permissions": permissions,
            "status": self.status,
            "version": self.version,
            "is_owner": owner,
            "create_time": self.create_time.isoformat(),
            "edit_time": self.edit_time.isoformat(),
        }
