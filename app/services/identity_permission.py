"""Real-time permission calculation for project and team identities."""

import datetime
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from app.constants.project import ProjectStatus
from app.exceptions import (
    InvalidIdentityTagError,
    NoPermissionError,
    ProtectedIdentityTagError,
    TeamQualificationRequiredError,
)
from app.models.identity_tag import (
    PROJECT_TAG_PERMISSIONS,
    PROJECT_TAGS,
    SYSTEM_PROJECT_TAG_DEFINITIONS,
    TEAM_BASE_PERMISSIONS,
    WORKER_TAGS,
    IdentityTagPolicy,
    normalize_tag_list,
)
from app.models.project_member import ProjectMember
from app.models.team_member import TeamMember
from app.utils.search import normalize_search_text


NORMAL = ProjectStatus.WORKING
CLEARED = ProjectStatus.CLEARED
COMPLETED = ProjectStatus.COMPLETED
PROJECT_STATUS_NAMES = {
    NORMAL: "NORMAL",
    CLEARED: "CLEARED",
    COMPLETED: "COMPLETED",
}

_NORMAL_ONLY_PERMISSIONS = frozenset(
    {
        "project:ADD_FILE",
        "project:MOVE_FILE",
        "project:RENAME_FILE",
        "project:DELETE_FILE",
        "project:OUTPUT_TRA",
        "project:ADD_LABEL",
        "project:MOVE_LABEL",
        "project:DELETE_LABEL",
        "project:ADD_TRA",
        "project:DELETE_TRA",
        "project:PROOFREAD_TRA",
        "project:CHECK_TRA",
        "project:ADD_TARGET",
        "project:CHANGE_TARGET",
        "project:DELETE_TARGET",
    }
)


def normalize_permission(permission: Any) -> str:
    """Normalize stable codes, including the public numeric permission constants."""

    if isinstance(permission, str):
        if permission.startswith("project:") or permission.startswith("team:"):
            return permission
        return f"project:{permission}"
    legacy_project_permissions = {
        1: "ACCESS",
        5: "DELETE",
        10: "CHANGE",
        101: "CHECK_USER",
        105: "INVITE_USER",
        110: "DELETE_USER",
        115: "CHANGE_USER_ROLE",
        120: "CHANGE_USER_REMARK",
        1010: "COMPLETE_PROJECT",
        1020: "ADD_FILE",
        1030: "MOVE_FILE",
        1040: "RENAME_FILE",
        1050: "DELETE_FILE",
        1060: "OUTPUT_TRA",
        1080: "ADD_LABEL",
        1090: "MOVE_LABEL",
        1100: "DELETE_LABEL",
        1110: "ADD_TRA",
        1120: "DELETE_TRA",
        1130: "PROOFREAD_TRA",
        1140: "CHECK_TRA",
        1150: "ADD_TARGET",
        1160: "CHANGE_TARGET",
        1170: "DELETE_TARGET",
    }
    if permission in legacy_project_permissions:
        return f"project:{legacy_project_permissions[permission]}"
    return str(permission)


def normalize_aliases(
    aliases: Any,
    *,
    name: str | None = None,
    max_count: int = 10,
    max_length: int = 64,
) -> list[str]:
    """Normalize an entire alias replacement request."""

    from app.exceptions import AliasValidationError

    if not isinstance(aliases, list):
        raise AliasValidationError("aliases must be an array")
    normalized: list[str] = []
    seen: set[str] = set()
    normalized_name = normalize_search_text(name)
    for value in aliases:
        if not isinstance(value, str):
            raise AliasValidationError("alias must be a string")
        alias = unicodedata.normalize("NFC", value).strip()
        if not alias:
            continue
        if len(alias) > max_length:
            raise AliasValidationError("alias is too long")
        key = alias.casefold()
        if key == normalized_name:
            raise AliasValidationError("alias cannot equal the user's name")
        if key not in seen:
            seen.add(key)
            normalized.append(alias)
    if len(normalized) > max_count:
        raise AliasValidationError("too many aliases")
    return normalized


@dataclass(frozen=True)
class PermissionSnapshot:
    project_member_id: str | None = None
    user_id: str | None = None
    project_id: str | None = None
    team_id: str | None = None
    source_tags: tuple[str, ...] = ()
    effective_permissions: frozenset[str] = frozenset()
    permission_sources: dict[str, tuple[str, ...]] = field(default_factory=dict)
    is_owner: bool = False
    generated_at: datetime.datetime = field(default_factory=datetime.datetime.utcnow)

    def has(self, permission: Any) -> bool:
        return normalize_permission(permission) in self.effective_permissions

    def to_api(self) -> dict:
        return {
            "project_member_id": self.project_member_id,
            "user_id": self.user_id,
            "project_id": self.project_id,
            "team_id": self.team_id,
            "source_tags": list(self.source_tags),
            "effective_permissions": sorted(self.effective_permissions),
            "permission_sources": {
                key: list(value) for key, value in self.permission_sources.items()
            },
            "is_owner": self.is_owner,
            "generated_at": self.generated_at.isoformat(),
        }


class IdentityPermissionService:
    """The sole runtime authority for new identity-tag permissions."""

    @staticmethod
    def project_status_name(project_or_status) -> str | None:
        status = getattr(project_or_status, "status", project_or_status)
        return PROJECT_STATUS_NAMES.get(status)

    @staticmethod
    def is_active_team_member(user, team) -> TeamMember | None:
        if user is None or team is None:
            return None
        return TeamMember.objects(team=team, user=user, status="active").first()

    @staticmethod
    def project_member(project, user) -> ProjectMember | None:
        if user is None:
            return None
        return ProjectMember.objects(project=project, user=user).first()

    @staticmethod
    def team_member(team, user) -> TeamMember | None:
        if user is None:
            return None
        return TeamMember.objects(team=team, user=user).first()

    @staticmethod
    def _policy(team):
        policy = IdentityTagPolicy.objects(team=team).first()
        return policy

    @classmethod
    def _policy_data(cls, team, scope: str) -> dict[str, dict]:
        policy = cls._policy(team)
        if policy is None:
            return {}
        return policy.team_tags if scope == "team" else policy.project_tags

    @classmethod
    def tag_definition(cls, team, scope: str, tag: str) -> dict | None:
        return cls._tag_definition_with_policy(team, scope, tag, cls._policy(team))

    @staticmethod
    def _tag_definition_with_policy(
        team, scope: str, tag: str, policy: IdentityTagPolicy | None
    ) -> dict | None:
        if scope not in ("team", "project"):
            raise ValueError("scope must be team or project")
        override = policy.effective(scope).get(tag) if policy is not None else None
        if scope == "project" and tag in SYSTEM_PROJECT_TAG_DEFINITIONS:
            return {
                **SYSTEM_PROJECT_TAG_DEFINITIONS[tag],
                **(override or {}),
                "source": "team_override" if override else "site",
            }
        if scope == "team" and tag in TEAM_BASE_PERMISSIONS:
            return {
                "name": tag,
                "permissions": sorted(
                    (override or {}).get("permissions", TEAM_BASE_PERMISSIONS[tag])
                ),
                "assignable": False,
                "source": "team_override" if override else "site",
            }
        if override is None:
            return None
        return {**override, "source": "team"}

    @classmethod
    def project_permissions_for_tags(cls, team, tags) -> dict[str, set[str]]:
        return cls._permissions_for_tags_with_policy(
            team, tags, scope="project", policy=cls._policy(team)
        )

    @classmethod
    def team_permissions_for_tags(cls, team, tags) -> dict[str, set[str]]:
        return cls._permissions_for_tags_with_policy(
            team, tags, scope="team", policy=cls._policy(team)
        )

    @classmethod
    def _permissions_for_tags_with_policy(
        cls, team, tags, *, scope: str, policy: IdentityTagPolicy | None
    ) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        for tag in normalize_tag_list(tags):
            definition = cls._tag_definition_with_policy(team, scope, tag, policy)
            if definition is None:
                continue
            result[tag] = set(definition.get("permissions", []))
        return result

    @classmethod
    def team_snapshot(cls, user, team) -> PermissionSnapshot:
        relation = cls.is_active_team_member(user, team)
        if relation is None:
            return PermissionSnapshot(
                user_id=str(user.id) if user else None,
                team_id=str(team.id) if team else None,
            )
        base_definition = cls.tag_definition(team, "team", relation.base_tag)
        permissions = set(
            base_definition.get("permissions", ())
            if base_definition is not None
            else TEAM_BASE_PERMISSIONS.get(relation.base_tag, ())
        )
        sources: dict[str, list[str]] = {}
        for permission in permissions:
            sources.setdefault(permission, []).append(f"team_base:{relation.base_tag}")
        for tag, tag_permissions in cls.team_permissions_for_tags(
            team, relation.tags
        ).items():
            for permission in tag_permissions:
                permissions.add(permission)
                sources.setdefault(permission, []).append(f"team_tag:{tag}")
        return PermissionSnapshot(
            user_id=str(user.id),
            team_id=str(team.id),
            source_tags=tuple([relation.base_tag, *relation.tags]),
            effective_permissions=frozenset(permissions),
            permission_sources={key: tuple(value) for key, value in sources.items()},
        )

    @classmethod
    def _project_snapshot_with_context(
        cls,
        user,
        project,
        *,
        team_relation: TeamMember | None,
        member: ProjectMember | None,
        policy: IdentityTagPolicy | None,
    ) -> PermissionSnapshot:
        if user is None or project is None:
            return PermissionSnapshot(
                project_id=str(project.id) if project else None,
                team_id=str(project.team.id) if project else None,
            )
        access_sources: list[str] = []
        permissions: set[str] = set()
        sources: dict[str, list[str]] = {}

        # A soft-removed project member has no project permissions (doc 2.1):
        # team inheritance (including creator/admin levels) must not restore
        # access that the member-management screen just revoked.
        team_inheritance_applies = team_relation is not None and (
            member is None or member.status != "removed"
        )
        if team_inheritance_applies:
            access_sources.append("team_access")
            permissions.add("project:ACCESS")
            sources.setdefault("project:ACCESS", []).append("team_access")
            for tag, tag_permissions in cls._permissions_for_tags_with_policy(
                project.team,
                team_relation.tags,
                scope="team",
                policy=policy,
            ).items():
                for permission in tag_permissions:
                    sources.setdefault(permission, []).append(f"team_tag:{tag}")
                    # Team tag permissions are team-scoped.  A tag can never
                    # smuggle a project permission into this branch.
                    if permission.startswith("project:"):
                        permissions.add(permission)
            if team_relation.base_tag in ("creator", "admin"):
                inherited_tag = team_relation.base_tag
                access_sources.append(f"team_inherited:{inherited_tag}")
                for permission in PROJECT_TAG_PERMISSIONS[inherited_tag]:
                    permissions.add(permission)
                    sources.setdefault(permission, []).append(
                        f"team_inherited:{inherited_tag}"
                    )

        if member is not None and member.status in ("active", "invited"):
            access_sources.append(f"project_member:{member.status}")
            permissions.add("project:ACCESS")
            sources.setdefault("project:ACCESS", []).append(
                f"project_member:{member.status}"
            )
            if member.status == "active":
                for tag, tag_permissions in cls._permissions_for_tags_with_policy(
                    project.team,
                    member.tags,
                    scope="project",
                    policy=policy,
                ).items():
                    for permission in tag_permissions:
                        permissions.add(permission)
                        sources.setdefault(permission, []).append(f"project_tag:{tag}")

        owner = (
            project.owner_user is not None
            and project.owner_user == user
            and member is not None
            and member.status == "active"
            and "creator" in member.tags
        )
        return PermissionSnapshot(
            project_member_id=str(member.id) if member else None,
            user_id=str(user.id),
            project_id=str(project.id),
            team_id=str(project.team.id),
            source_tags=tuple(member.tags) if member else (),
            effective_permissions=frozenset(permissions),
            permission_sources={key: tuple(value) for key, value in sources.items()},
            is_owner=owner,
        )

    @classmethod
    def project_snapshot(cls, user, project) -> PermissionSnapshot:
        if user is None or project is None:
            return cls._project_snapshot_with_context(
                user,
                project,
                team_relation=None,
                member=None,
                policy=None,
            )
        return cls._project_snapshot_with_context(
            user,
            project,
            team_relation=cls.is_active_team_member(user, project.team),
            member=cls.project_member(project, user),
            policy=cls._policy(project.team),
        )

    @classmethod
    def project_snapshots(
        cls,
        user,
        projects,
        *,
        project_members=None,
        team_members=None,
        policies=None,
    ) -> dict[str, PermissionSnapshot]:
        """Build one snapshot per page without per-row reference queries."""

        projects = list(projects)
        if user is None or not projects:
            return {
                str(project.pk): cls.project_snapshot(user, project)
                for project in projects
            }

        if project_members is None:
            project_members = ProjectMember.objects(
                user=user, project__in=[project.pk for project in projects]
            )
        members_by_project = {
            str(member.project.id): member for member in project_members
        }

        team_ids = {project.team.pk for project in projects}
        if team_members is None and team_ids:
            team_members = TeamMember.objects(
                user=user, team__in=list(team_ids), status="active"
            )
        relations_by_team = {
            str(relation.team.id): relation for relation in (team_members or ())
        }

        if policies is None and team_ids:
            policies = IdentityTagPolicy.objects(team__in=list(team_ids))
        policies_by_team = {str(policy.team.id): policy for policy in (policies or ())}

        return {
            str(project.pk): cls._project_snapshot_with_context(
                user,
                project,
                team_relation=relations_by_team.get(str(project.team.pk)),
                member=members_by_project.get(str(project.pk)),
                policy=policies_by_team.get(str(project.team.pk)),
            )
            for project in projects
        }

    @classmethod
    def can_project(cls, user, project, permission: Any) -> bool:
        normalized = normalize_permission(permission)
        snapshot = cls.project_snapshot(user, project)
        if normalized not in snapshot.effective_permissions:
            return False
        if (
            normalized in _NORMAL_ONLY_PERMISSIONS
            and cls.project_status_name(project) != "NORMAL"
        ):
            return False
        return True

    @classmethod
    def can_project_action(cls, user, project, action: str) -> bool:
        snapshot = cls.project_snapshot(user, project)
        if action == "clear":
            relation = cls.is_active_team_member(user, project.team)
            return relation is not None and relation.base_tag == "creator"
        if action in ("complete", "reopen"):
            return snapshot.has("project:COMPLETE_PROJECT")
        return False

    @classmethod
    def require_project_access(cls, user, project):
        if not cls.can_project(user, project, "project:ACCESS"):
            raise NoPermissionError

    @classmethod
    def is_project_manager(cls, user, project) -> bool:
        snapshot = cls.project_snapshot(user, project)
        return snapshot.has("project:MANAGE_MEMBERS")

    @classmethod
    def is_project_creator(cls, user, project) -> bool:
        member = cls.project_member(project, user)
        return (
            member is not None
            and member.status == "active"
            and "creator" in member.tags
        )

    @classmethod
    def is_team_manager(cls, user, team) -> bool:
        relation = cls.is_active_team_member(user, team)
        return relation is not None and relation.base_tag in ("creator", "admin")

    @classmethod
    def is_team_creator(cls, user, team) -> bool:
        relation = cls.is_active_team_member(user, team)
        return relation is not None and relation.base_tag == "creator"

    @classmethod
    def assignable_project_tags(cls, operator, project, target) -> set[str]:
        """Return the tags the operator may assign to a target member."""

        target_user = getattr(target, "user", target)
        project_manager = cls.is_project_manager(operator, project)
        team_relation = cls.is_active_team_member(operator, project.team)
        team_base = team_relation.base_tag if team_relation else None
        manager = project_manager or team_base in ("creator", "admin")
        if not manager:
            if target_user is not None and target_user == operator:
                return set(PROJECT_TAGS[2:])
            return set()
        assignable = set(WORKER_TAGS)
        assignable.update(
            code
            for code, definition in cls._policy_data(project.team, "project").items()
            if definition.get("assignable", True)
        )
        if cls.is_project_creator(operator, project) or team_base == "creator":
            assignable.add("admin")
        return assignable

    @classmethod
    def validate_project_tags(
        cls,
        operator,
        project,
        target_user,
        tags,
        *,
        external: bool = False,
        allow_creator: bool = False,
        source: str = "runtime",
    ) -> list[str]:
        normalized = normalize_tag_list(tags)
        valid_custom = set(cls._policy_data(project.team, "project"))
        valid = set(PROJECT_TAGS) | valid_custom
        invalid = set(normalized) - valid
        if invalid:
            raise InvalidIdentityTagError(", ".join(sorted(invalid)))
        if external:
            if set(normalized) - set(WORKER_TAGS):
                raise InvalidIdentityTagError
            return normalized
        if target_user is None:
            raise InvalidIdentityTagError
        if "creator" in normalized and not allow_creator:
            raise ProtectedIdentityTagError
        for tag in normalized:
            definition = cls.tag_definition(project.team, "project", tag)
            if definition is None:
                continue
            if not definition.get("assignable", True) and tag not in {
                "creator",
                "admin",
            }:
                raise InvalidIdentityTagError
        if "admin" in normalized and not (
            cls.is_project_creator(operator, project)
            or cls.is_team_creator(operator, project.team)
        ):
            raise InvalidIdentityTagError
        assignable = cls.assignable_project_tags(operator, project, target_user)
        if allow_creator:
            assignable.add("creator")
        if set(normalized) - assignable:
            raise InvalidIdentityTagError
        worker_tags = set(normalized) & set(WORKER_TAGS)
        if worker_tags:
            relation = cls.is_active_team_member(target_user, project.team)
            # Project/team managers already have the complete project editing
            # permission set. They may add a worker tag to their own identity
            # without maintaining a separate qualification list; ordinary
            # registered users remain qualification-gated.
            privileged_self = target_user == operator and cls.can_project(
                target_user, project, "project:MANAGE_MEMBERS"
            )
            if relation is None and not privileged_self:
                raise TeamQualificationRequiredError
            if (
                getattr(project.team, "worker_qualification_mode", "qualified")
                != "open"
                and not privileged_self
                and not worker_tags.issubset(set(relation.worker_qualifications))
            ):
                raise TeamQualificationRequiredError
        return normalized

    @classmethod
    def validate_team_tags(cls, team, tags, *, operator=None, target=None) -> list[str]:
        normalized = normalize_tag_list(tags)
        valid = set(cls._policy_data(team, "team"))
        invalid = set(normalized) - valid
        if invalid:
            raise InvalidIdentityTagError(", ".join(sorted(invalid)))
        for tag in normalized:
            definition = cls.tag_definition(team, "team", tag)
            if definition is None:
                continue
            if not definition.get("assignable", True):
                raise ProtectedIdentityTagError
        return normalized
