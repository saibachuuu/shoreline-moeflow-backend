"""Team-scoped identity members."""

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

from app.models.identity_tag import TEAM_BASE_TAGS, WORKER_TAGS, normalize_tag_list
from app.models._indexing import ensure_named_indexes
from app.utils.search import normalize_search_text


TEAM_MEMBER_STATUSES = frozenset(("active", "removed"))


class TeamMember(Document):
    INDEX_DEFINITIONS = (
        (
            "team_member_team_user_v1",
            [("team", 1), ("user", 1)],
            {"unique": True},
            None,
        ),
        (
            "team_member_status_v1",
            [("team", 1), ("status", 1)],
            {},
            None,
        ),
        (
            "team_member_aliases_v1",
            [("team", 1), ("aliases", 1)],
            {},
            None,
        ),
        (
            "team_member_worker_qualifications_v1",
            [("team", 1), ("worker_qualifications", 1)],
            {},
            None,
        ),
        (
            "team_member_alias_search_v1",
            [("team", 1), ("status", 1), ("aliases_search", 1)],
            {},
            None,
        ),
    )

    team = ReferenceField("Team", required=True, reverse_delete_rule=CASCADE)
    user = ReferenceField("User", required=True, reverse_delete_rule=CASCADE)
    base_tag = StringField(default="member", choices=tuple(TEAM_BASE_TAGS))
    tags = ListField(StringField(), default=list)
    worker_qualifications = ListField(StringField(), default=list)
    aliases = ListField(StringField(max_length=64), default=list)
    aliases_search = ListField(StringField(), default=list, db_field="asrch")
    # Per-team preference for the display alias used when the user joins a
    # project of this team (actively or by invitation).  Empty means unset:
    # new project members then fall back to the registered site name.
    default_display_name = StringField(default="", max_length=140)
    version = IntField(default=0, required=True, min_value=0)
    status = StringField(default="active", choices=tuple(TEAM_MEMBER_STATUSES))
    create_time = DateTimeField(default=datetime.datetime.utcnow)
    edit_time = DateTimeField(default=datetime.datetime.utcnow)
    removed_time = DateTimeField(null=True)

    meta = {
        "indexes": [
            {
                "fields": ["team", "user"],
                "unique": True,
                "name": "team_member_team_user_v1",
            },
            ("team", "status"),
            ("team", "aliases"),
            ("team", "worker_qualifications"),
        ]
    }

    @classmethod
    def ensure_indexes(cls):
        ensure_named_indexes(cls, cls.INDEX_DEFINITIONS)

    def clean(self):
        if self.base_tag not in TEAM_BASE_TAGS:
            raise ValueError("invalid team base tag")
        if self.status not in TEAM_MEMBER_STATUSES:
            raise ValueError("invalid team member status")
        self.tags = normalize_tag_list(self.tags)
        self.worker_qualifications = sorted(
            {
                qualification.strip()
                for qualification in (self.worker_qualifications or [])
                if isinstance(qualification, str) and qualification.strip()
            }
        )
        from app.services.identity_permission import normalize_aliases

        try:
            from flask import current_app

            max_aliases = int(
                current_app.config.get(
                    "MAX_TEAM_ALIASES",
                    current_app.config.get("max_team_aliases", 10),
                )
            )
        except RuntimeError:
            max_aliases = 10

        self.aliases = normalize_aliases(
            list(self.aliases or []), name=self.user.name, max_count=max_aliases
        )
        self.aliases_search = [
            normalized
            for normalized in (
                normalize_search_text(alias) for alias in (self.aliases or [])
            )
            if normalized
        ]
        if self.default_display_name:
            self.default_display_name = self.default_display_name.strip()
            if len(self.default_display_name) > 140:
                raise ValueError("default display name is too long")
        else:
            self.default_display_name = ""
        invalid_qualifications = set(self.worker_qualifications) - set(WORKER_TAGS)
        if invalid_qualifications:
            raise ValueError("invalid worker qualification")
        if self.status == "removed" and self.removed_time is None:
            self.removed_time = datetime.datetime.utcnow()
        if self.status != "removed":
            self.removed_time = None

    def to_api(self, user_map: dict | None = None) -> dict:
        """Serialize one team member.

        ``user_map`` is an optional ``{user_id_str: User}`` batch context.  The
        member-management list loads up to a few thousand members in one
        request; without it every ``self.user`` access lazily dereferences the
        user document (one query per member → N+1).  Pass the page's users
        fetched in a single ``User.objects(id__in=...)`` to keep the list
        linear in database round-trips.
        """
        user = None
        if self.user is not None:
            user = (
                user_map.get(str(self.user.id)) if user_map else None
            ) or self.user
        return {
            "id": str(self.id),
            "team_id": str(self.team.id),
            "user_id": str(user.id) if user is not None else None,
            "user": user.to_api() if user is not None else None,
            "base_tag": self.base_tag,
            "tags": list(self.tags),
            "worker_qualifications": list(self.worker_qualifications),
            "aliases": list(self.aliases),
            "default_display_name": self.default_display_name or "",
            "version": self.version,
            "status": self.status,
            "create_time": self.create_time.isoformat(),
            "edit_time": self.edit_time.isoformat(),
        }
