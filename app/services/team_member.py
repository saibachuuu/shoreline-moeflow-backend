"""Team member lifecycle, aliases and identity-tag policy operations."""

import datetime
import re

from mongoengine.errors import SaveConditionError, ValidationError
from mongoengine import Q

from app.exceptions import (
    IdentityPolicyInUseError,
    IdentityTeamMemberNotFoundError,
    IdentityUserNotFoundError,
    IdentityVersionConflictError,
    InvalidIdentityRequestError,
    InvalidIdentityTagError,
    MemberCapacityReachedError,
    NoPermissionError,
    ProtectedIdentityTagError,
)
from app.models.identity_tag import (
    PROJECT_TAGS,
    PROJECT_PERMISSION_CODES,
    TEAM_BASE_PERMISSIONS,
    TEAM_BASE_TAGS,
    TEAM_PERMISSION_CODES,
    IdentityTagPolicy,
    normalize_tag_list,
)
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.team import Team
from app.models.team_member import TeamMember
from app.models.user import User
from app.services.identity_audit import record_audit
from app.services.identity_permission import (
    IdentityPermissionService,
    normalize_aliases,
    normalize_search_text,
)


_TAG_CODE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


def _team_member_state(member: TeamMember) -> dict:
    return {
        "id": str(member.id),
        "user_id": str(member.user.id),
        "base_tag": member.base_tag,
        "tags": list(member.tags),
        "worker_qualifications": list(member.worker_qualifications),
        "aliases": list(member.aliases),
        "default_display_name": member.default_display_name or "",
        "status": member.status,
        "version": member.version,
    }


class TeamMemberService:
    @staticmethod
    def get(team, member_id) -> TeamMember:
        try:
            member = TeamMember.objects(team=team, id=member_id).first()
        except ValidationError:
            member = None
        if member is None:
            raise IdentityTeamMemberNotFoundError
        return member

    @staticmethod
    def for_user(team, user, *, active_only=False) -> TeamMember | None:
        query = TeamMember.objects(team=team, user=user)
        if active_only:
            query = query.filter(status="active")
        return query.first()

    @classmethod
    def default_display_name(cls, team, user) -> str:
        """The user's preferred project display alias in this team.

        Returns the trimmed preference or ``""`` when unset (callers fall
        back to the registered site name themselves).
        """
        member = cls.for_user(team, user)
        if member is None:
            return ""
        return (member.default_display_name or "").strip()

    @staticmethod
    def _sync_count(team):
        count = TeamMember.objects(team=team, status="active").count()
        Team.objects(id=team.id).update_one(set__user_count=count)
        team.user_count = count

    @staticmethod
    def _reserve_capacity(team):
        """Reserve one active-member slot with a conditional Mongo update."""

        updated = Team.objects(
            id=team.id, user_count__lt=int(team.max_user)
        ).update_one(inc__user_count=1)
        if updated:
            return

        # Repair a stale counter once before deciding that the team is full.
        # The conditional increment remains the final admission gate.
        count = TeamMember.objects(team=team, status="active").count()
        Team.objects(id=team.id).update_one(set__user_count=count)
        team.user_count = count
        updated = Team.objects(
            id=team.id, user_count__lt=int(team.max_user)
        ).update_one(inc__user_count=1)
        if not updated:
            raise MemberCapacityReachedError

    @staticmethod
    def _operator_relation(operator, team):
        return IdentityPermissionService.is_active_team_member(operator, team)

    @classmethod
    def _require_manager(cls, operator, team):
        relation = cls._operator_relation(operator, team)
        if relation is None or relation.base_tag not in ("creator", "admin"):
            raise NoPermissionError
        return relation

    @classmethod
    def _can_manage_target(cls, operator, target: TeamMember) -> bool:
        relation = cls._operator_relation(operator, target.team)
        if relation is None:
            return False
        # Management of other members is restricted to team managers.
        # A plain member must never manage (edit/remove/alias) another member.
        if relation.base_tag not in ("creator", "admin"):
            return False
        # Managers may still update their own tags and qualifications.  Own
        # base identity stays protected (update rejects operator == target
        # there), own aliases go through update_aliases, and self-removal
        # through remove().
        if operator == target.user:
            return True
        if relation.base_tag == "creator":
            return target.base_tag != "creator"
        return target.base_tag == "member"

    @staticmethod
    def _validate_qualifications(values) -> list[str]:
        from app.models.identity_tag import WORKER_TAGS

        if values is None:
            return []
        if not isinstance(values, list):
            raise InvalidIdentityRequestError("worker_qualifications must be an array")
        values = normalize_tag_list(values)
        invalid = set(values) - set(WORKER_TAGS)
        if invalid:
            raise InvalidIdentityTagError(", ".join(sorted(invalid)))
        return values

    @classmethod
    def add(cls, team, operator, payload, *, request_id=None, source="runtime"):
        cls._require_manager(operator, team)
        if not isinstance(payload, dict):
            raise InvalidIdentityRequestError
        user_id = payload.get("user_id")
        if not user_id:
            raise InvalidIdentityRequestError("user_id is required")
        try:
            user = User.objects(id=user_id).first()
        except ValidationError:
            user = None
        if user is None:
            raise IdentityUserNotFoundError
        member = cls.for_user(team, user)
        if member is not None and member.status == "active":
            return member
        base_tag = payload.get("base_tag", "member")
        if base_tag not in TEAM_BASE_TAGS:
            raise InvalidIdentityTagError
        if base_tag == "creator":
            raise ProtectedIdentityTagError
        operator_relation = cls._operator_relation(operator, team)
        if base_tag == "admin" and operator_relation.base_tag != "creator":
            raise NoPermissionError
        tags = IdentityPermissionService.validate_team_tags(
            team,
            payload.get("tags", []),
            operator=operator,
            target=member,
        )
        qualifications = cls._validate_qualifications(
            payload.get("worker_qualifications", [])
        )
        needs_slot = member is None or member.status != "active"
        if needs_slot:
            cls._reserve_capacity(team)
        before = _team_member_state(member) if member else {}
        previous_version = member.version if member is not None else None
        try:
            if member is None:
                member = TeamMember(
                    team=team,
                    user=user,
                    base_tag=base_tag,
                    tags=tags,
                    worker_qualifications=qualifications,
                    status="active",
                )
            else:
                member.base_tag = base_tag
                member.tags = tags
                member.worker_qualifications = qualifications
                member.status = "active"
                member.removed_time = None
                member.version += 1
                member.edit_time = datetime.datetime.utcnow()
            if previous_version is None:
                member.save()
            else:
                try:
                    member.save(save_condition={"version": previous_version})
                except SaveConditionError:
                    raise IdentityVersionConflictError
        except Exception:
            if needs_slot:
                cls._sync_count(team)
            raise
        cls._sync_count(team)
        record_audit(
            actor=operator,
            scope="team",
            action="team_member_add_or_restore",
            team=team,
            member=member,
            target_user=user,
            before=before,
            after=_team_member_state(member),
            source=source,
            request_id=request_id,
        )
        return member

    @classmethod
    def update(cls, team, member_id, operator, payload, *, request_id=None):
        if not isinstance(payload, dict):
            raise InvalidIdentityRequestError
        member = cls.get(team, member_id)
        expected = payload.get("expected_version")
        if expected is None or not isinstance(expected, int):
            raise InvalidIdentityRequestError("expected_version is required")
        if member.version != expected:
            raise IdentityVersionConflictError
        relation = cls._operator_relation(operator, team)
        if relation is None or relation.base_tag not in ("creator", "admin"):
            raise NoPermissionError
        # A member may only edit the own team alias (PATCH .../aliases) or
        # leave the team (DELETE self).  Team tags and worker qualifications
        # are manager-only fields, matching the documented role hierarchy.
        if not cls._can_manage_target(operator, member):
            raise NoPermissionError
        before = _team_member_state(member)
        if "base_tag" in payload:
            if operator == member.user:
                raise NoPermissionError
            base_tag = payload["base_tag"]
            if base_tag not in TEAM_BASE_TAGS or base_tag == "creator":
                raise ProtectedIdentityTagError
            if relation is None or (
                base_tag == "admin" and relation.base_tag != "creator"
            ):
                raise NoPermissionError
            if member.base_tag == "creator":
                raise NoPermissionError
            member.base_tag = base_tag
        if "tags" in payload:
            member.tags = IdentityPermissionService.validate_team_tags(
                team,
                payload["tags"],
                operator=operator,
                target=member,
            )
        if "worker_qualifications" in payload:
            member.worker_qualifications = cls._validate_qualifications(
                payload["worker_qualifications"]
            )
        if not any(
            key in payload for key in ("base_tag", "tags", "worker_qualifications")
        ):
            raise InvalidIdentityRequestError("no editable team member fields")
        member.version += 1
        member.edit_time = datetime.datetime.utcnow()
        try:
            member.save(save_condition={"version": expected})
        except SaveConditionError:
            raise IdentityVersionConflictError
        record_audit(
            actor=operator,
            scope="team",
            action="team_member_update",
            team=team,
            member=member,
            target_user=member.user,
            before=before,
            after=_team_member_state(member),
            request_id=request_id,
        )
        return member

    @classmethod
    def update_aliases(
        cls, team, member_id, operator, aliases, *, expected_version=None, request_id=None
    ):
        member = cls.get(team, member_id)
        if member.status != "active":
            raise NoPermissionError
        if expected_version is None:
            raise InvalidIdentityRequestError("expected_version is required")
        if member.version != expected_version:
            raise IdentityVersionConflictError
        allowed = operator == member.user or cls._can_manage_target(operator, member)
        if not allowed:
            raise NoPermissionError
        before = _team_member_state(member)
        max_count = 10
        try:
            from flask import current_app

            max_count = int(
                current_app.config.get(
                    "MAX_TEAM_ALIASES",
                    current_app.config.get("max_team_aliases", 10),
                )
            )
        except RuntimeError:
            pass
        member.aliases = normalize_aliases(
            aliases, name=member.user.name, max_count=max_count
        )
        member.version += 1
        member.edit_time = datetime.datetime.utcnow()
        try:
            member.save(save_condition={"version": expected_version})
        except SaveConditionError:
            raise IdentityVersionConflictError
        record_audit(
            actor=operator,
            scope="team",
            action="team_member_aliases_replace",
            team=team,
            member=member,
            target_user=member.user,
            before=before,
            after=_team_member_state(member),
            request_id=request_id,
        )
        return member

    @classmethod
    def update_default_display_name(
        cls, team, member_id, operator, value, *, expected_version=None, request_id=None
    ):
        """Edit the member's per-team project display-name preference.

        The preference belongs to the member themselves: a plain member may
        change only their own (mirroring ``update_aliases``); team managers
        may change it for any member they can manage.
        """
        member = cls.get(team, member_id)
        if member.status != "active":
            raise NoPermissionError
        if expected_version is None:
            raise InvalidIdentityRequestError("expected_version is required")
        if member.version != expected_version:
            raise IdentityVersionConflictError
        allowed = operator == member.user or cls._can_manage_target(operator, member)
        if not allowed:
            raise NoPermissionError
        before = _team_member_state(member)
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise InvalidIdentityRequestError("default_display_name must be a string")
        value = value.strip()
        if len(value) > 140:
            raise InvalidIdentityRequestError("default_display_name is too long")
        member.default_display_name = value
        member.version += 1
        member.edit_time = datetime.datetime.utcnow()
        try:
            member.save(save_condition={"version": expected_version})
        except SaveConditionError:
            raise IdentityVersionConflictError
        record_audit(
            actor=operator,
            scope="team",
            action="team_member_default_display_name_update",
            team=team,
            member=member,
            target_user=member.user,
            before=before,
            after=_team_member_state(member),
            request_id=request_id,
        )
        return member

    @classmethod
    def remove(cls, team, member_id, operator, *, request_id=None):
        member = cls.get(team, member_id)
        if member.base_tag == "creator":
            raise ProtectedIdentityTagError
        if operator != member.user and not cls._can_manage_target(operator, member):
            raise NoPermissionError
        if member.status == "removed":
            return member
        before = _team_member_state(member)
        member.status = "removed"
        member.removed_time = datetime.datetime.utcnow()
        member.edit_time = datetime.datetime.utcnow()
        member.version += 1
        try:
            member.save(save_condition={"version": member.version - 1})
        except SaveConditionError:
            raise IdentityVersionConflictError
        cls._sync_count(team)
        record_audit(
            actor=operator,
            scope="team",
            action="team_member_remove",
            team=team,
            member=member,
            target_user=member.user,
            before=before,
            after=_team_member_state(member),
            request_id=request_id,
        )
        return member

    @classmethod
    def list_members(cls, team, operator, *, status=None, word=None):
        if not IdentityPermissionService.team_snapshot(operator, team).has(
            "team:ACCESS"
        ):
            raise NoPermissionError
        from app.models.user import User

        statuses = ["active"] if status is None else status
        if isinstance(statuses, str):
            statuses = [item.strip() for item in statuses.split(",") if item.strip()]
        if not statuses:
            statuses = ["active"]
        needle = normalize_search_text(word)
        member_query = TeamMember.objects(team=team, status__in=statuses)
        if needle:
            # Search projections keep the request path from dereferencing one
            # User document per member.  User ids are resolved in one query,
            # then the member filter and status constraint stay in MongoDB.
            matching_user_ids = list(
                User.objects(
                    Q(name_search__icontains=needle)
                    | Q(aliases_search__icontains=needle)
                ).scalar("id")
            )
            search_filter = Q(aliases_search__icontains=needle)
            if matching_user_ids:
                search_filter |= Q(user__in=matching_user_ids)
            member_query = member_query.filter(search_filter)
        # Resolve references in one batch before sorting/serialization.  This
        # is now limited to the filtered result set rather than the whole team.
        members = list(member_query.select_related(max_depth=1))
        # The stable sort below reads ``member.user.name``.  ``TeamMember.user``
        # is a lazy reference, so sorting a several-thousand member team would
        # otherwise dereference the user document once per row (N+1) before the
        # API layer ever gets a chance to batch-prefetch for serialization.
        # Batch-prefetch here so the sort never touches storage per member.
        user_ids = {member.user.id for member in members if member.user is not None}
        user_lookup = {
            str(user.id): user for user in User.objects(id__in=list(user_ids))
        }

        def sort_key(item):
            if item.user is not None:
                user = user_lookup.get(str(item.user.id))
                name = user.name if user is not None else ""
            else:
                name = ""
            return (name.casefold(), str(item.id))

        return sorted(members, key=sort_key)


class IdentityTagPolicyService:
    @staticmethod
    def get_or_create(team) -> IdentityTagPolicy:
        policy = IdentityTagPolicy.objects(team=team).first()
        if policy is None:
            policy = IdentityTagPolicy(team=team).save()
        return policy

    @classmethod
    def response(cls, team) -> dict:
        policy = cls.get_or_create(team)
        team_tags = {
            code: {
                "code": code,
                "name": code,
                "permissions": sorted(TEAM_BASE_PERMISSIONS.get(code, ())),
                "assignable": False,
                "source": "site",
            }
            for code in sorted(TEAM_BASE_TAGS)
        }
        for code, value in policy.team_tags.items():
            if code in TEAM_BASE_TAGS:
                team_tags[code] = {
                    **team_tags[code],
                    **value,
                    "code": code,
                    "source": "team_override",
                    "initial_permissions": sorted(TEAM_BASE_PERMISSIONS.get(code, ())),
                    "initial_assignable": False,
                }
        team_tags.update(
            {
                code: {**value, "code": code, "source": "team"}
                for code, value in policy.team_tags.items()
                if code not in TEAM_BASE_TAGS
            }
        )
        system_project_tags = {
            code: {
                **value,
                "code": code,
                "source": "site",
            }
            for code, value in __import__(
                "app.models.identity_tag", fromlist=["SYSTEM_PROJECT_TAG_DEFINITIONS"]
            ).SYSTEM_PROJECT_TAG_DEFINITIONS.items()
        }
        project_tags = {}
        for code, value in system_project_tags.items():
            override = policy.project_tags.get(code)
            project_tags[code] = {
                **value,
                **(override or {}),
                "code": code,
                "source": "team_override" if override else "site",
                **({
                    "initial_permissions": list(value["permissions"]),
                    "initial_assignable": value["assignable"],
                } if override else {}),
            }
        project_tags.update(
            {
                code: {**value, "code": code, "source": "team"}
                for code, value in policy.project_tags.items()
                if code not in PROJECT_TAGS
            }
        )
        return {
            "version": policy.version,
            "team_tags": team_tags,
            "project_tags": project_tags,
        }

    @classmethod
    def update(cls, team, operator, payload, *, request_id=None):
        TeamMemberService._require_manager(operator, team)
        if not isinstance(payload, dict):
            raise InvalidIdentityRequestError
        policy = cls.get_or_create(team)
        expected = payload.get("expected_version")
        if not isinstance(expected, int):
            raise InvalidIdentityRequestError("expected_version is required")
        if expected != policy.version:
            raise IdentityVersionConflictError
        upserts = payload.get("upserts", [])
        removes = payload.get("removes", [])
        if not isinstance(upserts, list) or not isinstance(removes, list):
            raise InvalidIdentityRequestError
        before = cls.response(team)
        for definition in upserts:
            if not isinstance(definition, dict):
                raise InvalidIdentityRequestError
            scope = definition.get("scope")
            code = definition.get("code")
            if scope not in ("team", "project") or not isinstance(code, str):
                raise InvalidIdentityRequestError
            if not _TAG_CODE.fullmatch(code):
                raise InvalidIdentityTagError
            is_system_team_tag = scope == "team" and code in TEAM_BASE_TAGS
            is_system_project_tag = scope == "project" and code in PROJECT_TAGS
            permissions = definition.get("permissions", [])
            if not isinstance(permissions, list) or not all(
                isinstance(permission, str) for permission in permissions
            ):
                raise InvalidIdentityRequestError
            allowed = TEAM_PERMISSION_CODES if scope == "team" else set(PROJECT_PERMISSION_CODES)
            if set(permissions) - set(allowed):
                raise InvalidIdentityTagError
            # System tags may override their complete built-in permission set.
            # The protected permissions remain unavailable to custom tags.
            if not is_system_project_tag and set(permissions) & {
                "project:COMPLETE_PROJECT",
                "project:MANAGE_MEMBERS",
            }:
                raise ProtectedIdentityTagError
            if is_system_team_tag:
                # Base hierarchy tags (creator/admin/member) define the team's
                # level system.  Only the team creator may override them, so an
                # admin cannot grant team:DELETE to member-level tags (or strip
                # creator capabilities) and bypass the documented hierarchy.
                operator_relation = TeamMemberService._operator_relation(operator, team)
                if operator_relation is None or operator_relation.base_tag != "creator":
                    raise NoPermissionError
                if definition.get("name", code) != code:
                    raise ProtectedIdentityTagError
                policy.team_tags[code] = {
                    "name": code,
                    "permissions": sorted(set(permissions)),
                    "assignable": False,
                }
                continue
            if is_system_project_tag:
                if definition.get("name", code) != code:
                    raise ProtectedIdentityTagError
                policy.project_tags[code] = {
                    "name": code,
                    "permissions": sorted(set(permissions)),
                    "assignable": bool(definition.get("assignable", True)),
                }
                continue
            if scope == "team":
                policy.team_tags[code] = {
                    "name": definition.get("name", code),
                    "permissions": sorted(set(permissions)),
                    "assignable": bool(definition.get("assignable", True)),
                }
            else:
                policy.project_tags[code] = {
                    "name": definition.get("name", code),
                    "permissions": sorted(set(permissions)),
                    "assignable": bool(definition.get("assignable", True)),
                }
        for removal in removes:
            if not isinstance(removal, dict):
                raise InvalidIdentityRequestError
            scope = removal.get("scope")
            code = removal.get("code")
            if scope not in ("team", "project") or not isinstance(code, str):
                raise InvalidIdentityRequestError
            is_system_team_tag = scope == "team" and code in TEAM_BASE_TAGS
            is_system_project_tag = scope == "project" and code in PROJECT_TAGS
            if is_system_team_tag or is_system_project_tag:
                if is_system_team_tag:
                    # Removing a base-tag override is also a hierarchy edit
                    # and therefore creator-only, mirroring the upsert rule.
                    operator_relation = TeamMemberService._operator_relation(operator, team)
                    if operator_relation is None or operator_relation.base_tag != "creator":
                        raise NoPermissionError
                target = policy.team_tags if is_system_team_tag else policy.project_tags
                target.pop(code, None)
                continue
            if scope == "team":
                used = TeamMember.objects(team=team, tags=code).count()
                if used:
                    raise IdentityPolicyInUseError
                policy.team_tags.pop(code, None)
            else:
                project_ids = Project.objects(team=team).scalar("id")
                used = ProjectMember.objects(
                    project__in=list(project_ids), tags=code
                ).count()
                if used:
                    raise IdentityPolicyInUseError
                policy.project_tags.pop(code, None)
        policy.team_tags = dict(policy.team_tags)
        policy.project_tags = dict(policy.project_tags)
        policy.version += 1
        policy.edit_time = datetime.datetime.utcnow()
        try:
            policy.save(save_condition={"version": expected})
        except SaveConditionError:
            raise IdentityVersionConflictError
        after = cls.response(team)
        record_audit(
            actor=operator,
            scope="team",
            action="identity_tag_policy_update",
            team=team,
            before=before,
            after=after,
            request_id=request_id,
        )
        return policy
