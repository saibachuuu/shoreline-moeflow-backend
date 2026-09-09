"""Project member lifecycle and the compatibility boundary for invitations.

The service deliberately owns all new project-member writes.  The existing
invitation workflow is adapted through ``ProjectInvitationAdapter``; legacy
relations are read only by the offline migration and never projected at
runtime.
"""

import datetime
import re
import logging
import uuid

from bson import ObjectId
from mongoengine.errors import NotUniqueError, SaveConditionError, ValidationError

from app.exceptions import (
    IdentityMemberNotFoundError,
    IdentityOperationInProgressError,
    IdentityUserNotFoundError,
    InvalidIdentityTagError,
    InvalidIdentityRequestError,
    MemberAlreadyExistsError,
    MemberAlreadyOwnerError,
    MemberCapacityReachedError,
    MemberMergeRequiredError,
    NoPermissionError,
    ProjectMemberVersionConflictError,
    ProjectStateConflictError,
    ProtectedIdentityTagError,
)
from app.models.identity_operation import IdentityOperation
from app.models.project_member import ProjectMember
from app.models.project import Project
from app.models.team_member import TeamMember
from app.models.user import User
from app.services.identity_audit import record_audit
from app.services.identity_permission import (
    COMPLETED,
    NORMAL,
    CLEARED,
    IdentityPermissionService,
)
from app.utils.search import normalize_search_text

logger = logging.getLogger(__name__)


def _reference_id(value):
    """Return an ObjectId from an ObjectId, DBRef, or dereferenced document."""

    if value is None:
        return None
    identifier = getattr(value, "id", None)
    if isinstance(identifier, ObjectId):
        return identifier
    try:
        return ObjectId(str(identifier if identifier is not None else value))
    except (TypeError, ValueError):
        return None


def _now():
    return datetime.datetime.utcnow()


IDENTITY_OPERATION_LEASE_SECONDS = 300


def _member_state(member: ProjectMember) -> dict:
    return {
        "id": str(member.id),
        "project_id": str(member.project.id),
        "user_id": str(member.user.id) if member.user else None,
        "external_id": member.external_id,
        "display_name": member.display_name,
        "tags": list(member.tags),
        "status": member.status,
        "version": member.version,
    }


def _exception_data(error: Exception) -> dict:
    identity_code = getattr(error, "identity_code", None)
    if identity_code is None:
        if isinstance(error, NoPermissionError):
            identity_code = "NO_PERMISSION"
        else:
            identity_code = "INVALID_REQUEST"
    message = getattr(error, "message", None)
    if message is None:
        message = str(error) or identity_code
    return {
        "code": identity_code,
        "error_code": getattr(error, "code", None),
        "message": str(message),
        "http_status": getattr(
            error,
            "status_code",
            403 if isinstance(error, NoPermissionError) else 400,
        ),
    }


class ProjectMemberService:
    """Single write authority for ``ProjectMember``."""

    @staticmethod
    def get(project, member_id) -> ProjectMember:
        try:
            member = ProjectMember.objects(project=project, id=member_id).first()
        except ValidationError:
            member = None
        if member is None:
            raise IdentityMemberNotFoundError
        return member

    @staticmethod
    def for_user(project, user) -> ProjectMember | None:
        if project is None or user is None:
            return None
        return ProjectMember.objects(project=project, user=user).first()

    @staticmethod
    def _find_user(user_id) -> User | None:
        """Resolve a user id without leaking mongoengine ValidationError as 500."""
        try:
            return User.objects(id=user_id).first()
        except ValidationError:
            return None

    @staticmethod
    def _sync_count(project):
        count = ProjectMember.objects(project=project, status="active").count()
        project.update(set__user_count=count)

    @staticmethod
    def _active_operator(operator, project) -> bool:
        team_relation = IdentityPermissionService.is_active_team_member(
            operator, project.team
        )
        project_member = ProjectMemberService.for_user(project, operator)
        return team_relation is not None or (
            project_member is not None and project_member.status == "active"
        )

    @staticmethod
    def _is_manager(operator, project) -> bool:
        if IdentityPermissionService.is_project_manager(operator, project):
            return True
        relation = IdentityPermissionService.is_active_team_member(
            operator, project.team
        )
        return relation is not None and relation.base_tag in ("creator", "admin")

    @classmethod
    def _require_operator(cls, operator, project, *, write=True):
        if operator is None or not IdentityPermissionService.can_project(
            operator, project, "project:ACCESS"
        ):
            raise NoPermissionError
        if write and not cls._active_operator(operator, project):
            raise NoPermissionError

    @staticmethod
    def _display_name(value, *, fallback=None) -> str:
        if value is None:
            value = fallback
        if not isinstance(value, str):
            raise InvalidIdentityRequestError("display_name must be a string")
        value = value.strip()
        if not value:
            raise InvalidIdentityRequestError("display_name is required")
        if len(value) > 140:
            raise InvalidIdentityRequestError("display_name is too long")
        return value

    @staticmethod
    def team_default_display_name(project, user) -> str:
        """Resolve the display alias a registered user should get on joining.

        The user's per-team preference (``TeamMember.default_display_name``)
        wins; when unset the registered site name is used.  Only meaningful
        for users with a TeamMember record in the project's team.
        """
        from app.services.team_member import TeamMemberService

        team_pref = TeamMemberService.default_display_name(project.team, user)
        if team_pref:
            return team_pref
        user_pref = (getattr(user, "default_display_name", "") or "").strip()
        return user_pref or user.name

    @classmethod
    def _add_user_display_name(cls, project, user, display_name) -> str:
        """Resolve the alias for a registered user being added to a project.

        An explicitly provided alias wins.  When the caller sent nothing --
        or only the user's site name, which the member-management frontend
        always pre-fills for a new member -- the user's team-level default
        display name is used, falling back to the site name itself.
        """
        if (
            display_name is not None
            and display_name.strip()
            and display_name.strip() != user.name
        ):
            return cls._display_name(display_name)
        return cls._display_name(cls.team_default_display_name(project, user))

    @staticmethod
    def _project_state(project):
        return project.status

    @classmethod
    def _check_add_state(cls, project, *, external):
        if project.status == CLEARED:
            raise ProjectStateConflictError
        if not external and project.status != NORMAL:
            raise ProjectStateConflictError

    @classmethod
    def _validate_tags(
        cls,
        operator,
        project,
        target_user,
        tags,
        *,
        external=False,
        allow_creator=False,
    ):
        return IdentityPermissionService.validate_project_tags(
            operator,
            project,
            target_user,
            tags,
            external=external,
            allow_creator=allow_creator,
        )

    @staticmethod
    def _external_id_for_operation(project, operation_id):
        if not operation_id:
            return None
        return str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"moeflow:project-member:{project.id}:{operation_id}",
            )
        )

    @classmethod
    def _add_external(
        cls,
        project,
        operator,
        display_name,
        tags,
        *,
        request_id=None,
        operation_id=None,
    ):
        if not cls._is_manager(operator, project) and tags:
            raise NoPermissionError
        tags = cls._validate_tags(operator, project, None, tags, external=True)

        # The operation record is finalized after the member side effect.  A
        # worker crash in between must be able to find and reuse that member
        # instead of creating a second external identity on retry.
        operation_external_id = cls._external_id_for_operation(project, operation_id)
        if operation_external_id:
            existing = ProjectMember.objects(
                project=project, external_id=operation_external_id
            ).first()
            if existing is not None:
                cls._sync_count(project)
                return existing

        cls._check_add_state(project, external=True)
        if (
            ProjectMember.objects(project=project, status="active").count()
            >= project.max_user
        ):
            raise MemberCapacityReachedError
        member = ProjectMember(
            project=project,
            external_id=operation_external_id or str(uuid.uuid4()),
            display_name=display_name,
            tags=tags,
            status="active",
        )
        before = {}
        member.save()
        cls._sync_count(project)
        record_audit(
            actor=operator,
            scope="project",
            action="project_external_member_add",
            project=project,
            member=member,
            before=before,
            after=_member_state(member),
            source="runtime",
            request_id=request_id,
        )
        return member

    @classmethod
    def _add_user(
        cls,
        project,
        operator,
        user,
        display_name,
        tags,
        *,
        request_id=None,
        operation_id=None,
    ):
        existing = cls.for_user(project, user)
        if existing is not None and existing.status in ("active", "invited"):
            if operation_id and existing.status == "invited":
                cls._check_add_state(project, external=False)
                manager = cls._is_manager(operator, project)
                if tags and not manager:
                    raise NoPermissionError
                tags = cls._validate_tags(operator, project, user, tags)
                display_name = cls._add_user_display_name(project, user, display_name)
                from app.services.project_invitation import ProjectInvitationAdapter

                return ProjectInvitationAdapter.create_or_reuse(
                    project,
                    operator,
                    user,
                    tags=tags,
                    display_name=display_name,
                    request_id=request_id,
                )
            raise MemberAlreadyExistsError
        cls._check_add_state(project, external=False)
        manager = cls._is_manager(operator, project)
        if tags and not manager:
            raise NoPermissionError
        tags = cls._validate_tags(operator, project, user, tags)
        # Capacity is checked whenever this operation adds one active member,
        # including restoring a soft-removed registered user (both the brand
        # new member and the removal-recovery paths must honor max_user).
        if (
            ProjectMember.objects(project=project, status="active").count()
            >= project.max_user
        ):
            raise MemberCapacityReachedError
        display_name = cls._add_user_display_name(project, user, display_name)

        if existing is None:
            existing = ProjectMember(
                project=project,
                user=user,
                display_name=display_name,
                tags=tags,
                status="removed",
            )
        else:
            existing.user = user
            existing.external_id = None
            existing.display_name = display_name
            existing.tags = tags

        team_relation = IdentityPermissionService.is_active_team_member(
            operator, project.team
        )
        team_creator = team_relation is not None and team_relation.base_tag == "creator"
        if team_creator:
            before = _member_state(existing) if existing.id else {}
            previous_version = existing.version if existing.id else None
            existing.status = "active"
            existing.removed_time = None
            existing.edit_time = _now()
            existing.version += 1 if existing.id else 0
            if previous_version is None:
                existing.save()
            else:
                try:
                    existing.save(save_condition={"version": previous_version})
                except SaveConditionError:
                    raise ProjectMemberVersionConflictError
            cls._sync_count(project)
            record_audit(
                actor=operator,
                scope="project",
                action="project_member_add",
                project=project,
                member=existing,
                target_user=user,
                before=before,
                after=_member_state(existing),
                source="runtime",
                request_id=request_id,
            )
            return existing

        # All other registration-user additions go through the old invitation
        # lifecycle.  The adapter persists the pending projection only after
        # the legacy call has succeeded.
        from app.services.project_invitation import ProjectInvitationAdapter

        return ProjectInvitationAdapter.create_or_reuse(
            project,
            operator,
            user,
            tags=tags,
            display_name=display_name,
            request_id=request_id,
        )

    @classmethod
    def add(cls, project, operator, payload, *, request_id=None, operation_id=None):
        cls._require_operator(operator, project)
        if not isinstance(payload, dict):
            raise InvalidIdentityRequestError
        # ``add`` also carries the explicit restore operation used by the
        # member-management screen.  Keeping it in the idempotent changes
        # envelope means a removed external identity can be restored without
        # creating a second record.
        if payload.get("member_id") is not None:
            member = cls.get(project, payload.get("member_id"))
            if member.status != "removed":
                raise MemberAlreadyExistsError
            if not cls._is_manager(operator, project):
                raise NoPermissionError
            expected = payload.get("expected_member_version")
            if not isinstance(expected, int):
                raise InvalidIdentityRequestError("expected_member_version is required")
            if member.version != expected:
                raise ProjectMemberVersionConflictError
            user_id = payload.get("user_id")
            if user_id is not None:
                if member.user is None or str(member.user.id) != str(user_id):
                    raise InvalidIdentityRequestError(
                        "user_id does not match the member"
                    )
            elif member.user is not None:
                user_id = str(member.user.id)
            elif payload.get("external_id") is not None:
                if str(payload["external_id"]) != str(member.external_id):
                    raise InvalidIdentityRequestError(
                        "external_id does not match the member"
                    )
            cls._check_add_state(project, external=member.user is None)
            if (
                ProjectMember.objects(project=project, status="active").count()
                >= project.max_user
            ):
                raise MemberCapacityReachedError
            display_name = cls._display_name(
                payload.get("display_name"), fallback=member.display_name
            )
            tags = cls._validate_tags(
                operator,
                project,
                member.user,
                payload.get("tags", member.tags),
                external=member.user is None,
            )
            before = _member_state(member)
            member.display_name = display_name
            member.tags = tags
            member.status = "active"
            member.removed_time = None
            member.edit_time = _now()
            member.version += 1
            try:
                member.save(save_condition={"version": expected})
            except SaveConditionError:
                raise ProjectMemberVersionConflictError
            cls._sync_count(project)
            record_audit(
                actor=operator,
                scope="project",
                action="project_member_restore",
                project=project,
                member=member,
                target_user=member.user,
                before=before,
                after=_member_state(member),
                source="runtime",
                request_id=request_id,
            )
            return member
        user_id = payload.get("user_id")
        display_name = payload.get("display_name")
        if not user_id and display_name is None:
            raise InvalidIdentityRequestError("user_id or display_name is required")
        if display_name is not None:
            display_name = cls._display_name(display_name)
        tags = payload.get("tags", [])
        if user_id:
            user = cls._find_user(user_id)
            if user is None:
                raise IdentityUserNotFoundError
            return cls._add_user(
                project,
                operator,
                user,
                display_name,
                tags,
                request_id=request_id,
                operation_id=operation_id,
            )
        return cls._add_external(
            project,
            operator,
            display_name,
            tags,
            request_id=request_id,
            operation_id=operation_id,
        )

    @classmethod
    def update(cls, project, operator, payload, *, request_id=None):
        cls._require_operator(operator, project)
        if not isinstance(payload, dict):
            raise InvalidIdentityRequestError
        member = cls.get(project, payload.get("member_id"))
        expected = payload.get("expected_member_version")
        if not isinstance(expected, int):
            raise InvalidIdentityRequestError("expected_member_version is required")
        if member.version != expected:
            raise ProjectMemberVersionConflictError
        changes = payload.get("changes")
        if not isinstance(changes, dict) or not changes:
            raise InvalidIdentityRequestError("changes is required")
        if set(changes) - {"display_name", "tags", "status"}:
            raise InvalidIdentityRequestError("unsupported member fields")
        if "status" in changes and changes["status"] != "removed":
            raise InvalidIdentityRequestError("only status=removed is supported")

        manager = cls._is_manager(operator, project)
        if member.user != operator and not manager:
            raise NoPermissionError
        if member.user is None and not manager:
            raise NoPermissionError
        if (
            member.user is not None
            and member.user == operator
            and member.status != "active"
        ):
            raise NoPermissionError

        if member.user is not None and project.owner_user == member.user:
            if "status" in changes:
                raise MemberAlreadyOwnerError
            if "tags" in changes and "creator" not in set(changes["tags"] or []):
                raise MemberAlreadyOwnerError

        before = _member_state(member)
        if "display_name" in changes:
            member.display_name = cls._display_name(changes["display_name"])
        if "tags" in changes:
            if member.user is None:
                tags = cls._validate_tags(
                    operator, project, None, changes["tags"], external=True
                )
            else:
                owner_tags = (
                    member.user is not None and project.owner_user == member.user
                )
                tags = cls._validate_tags(
                    operator,
                    project,
                    member.user,
                    changes["tags"],
                    allow_creator=owner_tags,
                )
            # A creator/admin must not silently demote themselves by dropping
            # their own identity tag through a member edit.  The owner-facing
            # creator check above already raises MemberAlreadyOwnerError; this
            # covers admins (and an owner dropping their own admin tag).
            if member.user is not None and member.user == operator:
                own_identity_tags = {
                    tag for tag in ("creator", "admin") if tag in before.get("tags", [])
                }
                if own_identity_tags and not own_identity_tags.issubset(set(tags)):
                    raise ProtectedIdentityTagError
            # Changing another member's identity (dropping their creator/admin
            # tag) is restricted to the project creator; granting one is already
            # limited by the assignable set above.
            if member.user is not None and member.user != operator:
                removed_identity_tags = {
                    tag
                    for tag in ("creator", "admin")
                    if tag in before.get("tags", []) and tag not in set(tags)
                }
                if removed_identity_tags and project.owner_user != operator:
                    raise ProtectedIdentityTagError
            member.tags = tags
        if "status" in changes:
            member.status = "removed"
            member.removed_time = _now()
        member.version += 1
        member.edit_time = _now()
        try:
            member.save(save_condition={"version": expected})
        except SaveConditionError:
            raise ProjectMemberVersionConflictError
        if "status" in changes and member.user is not None:
            # The identity member is the sole runtime membership record.
            pass
        cls._sync_count(project)
        record_audit(
            actor=operator,
            scope="project",
            action="project_member_update",
            project=project,
            member=member,
            target_user=member.user,
            before=before,
            after=_member_state(member),
            request_id=request_id,
        )
        return member

    @classmethod
    def bind(cls, project, member_id, operator, payload, *, request_id=None):
        cls._require_operator(operator, project)
        if not cls._is_manager(operator, project):
            raise NoPermissionError
        member = cls.get(project, member_id)
        expected = (
            payload.get("expected_version") if isinstance(payload, dict) else None
        )
        if not isinstance(expected, int):
            raise InvalidIdentityRequestError("expected_version is required")
        if member.version != expected:
            raise ProjectMemberVersionConflictError
        if (
            member.status != "active"
            or member.user is not None
            or not member.external_id
        ):
            raise InvalidIdentityRequestError("only an active external member can bind")
        user = cls._find_user(payload.get("user_id"))
        if user is None:
            raise IdentityUserNotFoundError
        if IdentityPermissionService.is_active_team_member(user, project.team) is None:
            raise NoPermissionError
        other = cls.for_user(project, user)
        if other is not None:
            raise MemberMergeRequiredError
        tags = cls._validate_tags(operator, project, user, member.tags)
        before = _member_state(member)
        old_external_id = member.external_id
        member.user = user
        member.external_id = None
        member.tags = tags
        member.version += 1
        member.edit_time = _now()
        try:
            member.save(save_condition={"version": expected})
        except NotUniqueError:
            raise MemberMergeRequiredError
        except SaveConditionError:
            raise ProjectMemberVersionConflictError
        cls._sync_count(project)
        event = record_audit(
            actor=operator,
            scope="project",
            action="project_external_member_bind",
            project=project,
            member=member,
            target_user=user,
            before={**before, "external_id": old_external_id},
            after=_member_state(member),
            request_id=request_id,
        )
        return member, event

    @classmethod
    def _rollback_merge_write(
        cls,
        source,
        target,
        source_snapshot,
        target_snapshot,
        source_saved,
        target_saved,
        expected_source,
        expected_target,
    ):
        """Best-effort rollback of the first merge write (no Mongo transactions).

        A failed rollback is logged instead of silently swallowed: the caller
        must not claim full consistency when MongoDB 4.4 cannot guarantee it.
        """

        if source_saved:
            source.status = source_snapshot["status"]
            source.version = source_snapshot["version"]
            source.edit_time = source_snapshot["edit_time"]
            source.removed_time = source_snapshot["removed_time"]
            try:
                source.save(save_condition={"version": expected_source + 1})
            except SaveConditionError:
                logger.warning("merge rollback failed for source member %s", source.id)
        if target_saved:
            target.display_name = target_snapshot["display_name"]
            target.tags = list(target_snapshot["tags"])
            target.version = target_snapshot["version"]
            target.edit_time = target_snapshot["edit_time"]
            target.removed_time = target_snapshot["removed_time"]
            try:
                target.save(save_condition={"version": expected_target + 1})
            except SaveConditionError:
                logger.warning("merge rollback failed for target member %s", target.id)

    @classmethod
    def merge(cls, project, source_member_id, operator, payload, *, request_id=None):
        """Merge an external member into an existing registered-user member.

        Binding intentionally refuses duplicate users.  This separate write
        path makes the two-member decision explicit and protects both records
        with their own versions before changing either identity.
        """

        cls._require_operator(operator, project)
        if not cls._is_manager(operator, project):
            raise NoPermissionError
        if not isinstance(payload, dict):
            raise InvalidIdentityRequestError

        source = cls.get(project, source_member_id)
        target = cls.get(project, payload.get("target_member_id"))
        if source.id == target.id:
            raise InvalidIdentityRequestError("source and target must differ")
        expected_source = payload.get("expected_source_version")
        expected_target = payload.get("expected_target_version")
        if not isinstance(expected_source, int) or not isinstance(expected_target, int):
            raise InvalidIdentityRequestError(
                "expected_source_version and expected_target_version are required"
            )
        if source.version != expected_source or target.version != expected_target:
            raise ProjectMemberVersionConflictError
        if (
            source.status != "active"
            or source.user is not None
            or not source.external_id
        ):
            raise InvalidIdentityRequestError(
                "source must be an active external member"
            )
        if target.status not in ("active", "invited") or target.user is None:
            raise InvalidIdentityRequestError(
                "target must be a registered project member"
            )
        if (
            IdentityPermissionService.is_active_team_member(target.user, project.team)
            is None
        ):
            raise NoPermissionError

        display_name = cls._display_name(
            payload.get("display_name"), fallback=target.display_name
        )
        tags = payload.get("tags")
        if not isinstance(tags, list):
            raise InvalidIdentityRequestError("tags must be an array")
        tags = cls._validate_tags(operator, project, target.user, tags)
        # Owner invariant (doc 3.3): merge must never strip the creator tag
        # from the current owner.  The dedicated owner-transfer endpoint is
        # the only supported way to change ownership.
        if target.user is not None and project.owner_user == target.user:
            if "creator" not in set(tags):
                raise MemberAlreadyOwnerError

        before_source = _member_state(source)
        before_target = _member_state(target)
        source_snapshot = {
            "status": source.status,
            "version": source.version,
            "edit_time": source.edit_time,
            "removed_time": source.removed_time,
        }
        target_snapshot = {
            "display_name": target.display_name,
            "tags": list(target.tags),
            "version": target.version,
            "edit_time": target.edit_time,
            "removed_time": target.removed_time,
        }
        target_saved = False
        source_saved = False
        try:
            target.display_name = display_name
            target.tags = tags
            target.version += 1
            target.edit_time = _now()
            target.save(save_condition={"version": expected_target})
            target_saved = True

            source.status = "removed"
            source.removed_time = _now()
            source.version += 1
            source.edit_time = _now()
            source.save(save_condition={"version": expected_source})
            source_saved = True
        except Exception as error:
            cls._rollback_merge_write(
                source,
                target,
                source_snapshot,
                target_snapshot,
                source_saved,
                target_saved,
                expected_source,
                expected_target,
            )
            if isinstance(error, SaveConditionError):
                raise ProjectMemberVersionConflictError from error
            raise

        cls._sync_count(project)
        event = record_audit(
            actor=operator,
            scope="project",
            action="project_external_member_merge",
            project=project,
            member=target,
            target_user=target.user,
            before={"source": before_source, "target": before_target},
            after={"source": _member_state(source), "target": _member_state(target)},
            request_id=request_id,
        )
        return target, source, event

    @classmethod
    def list_members(
        cls, project, operator, *, status="active", tag=None, word=None
    ) -> list[ProjectMember]:
        IdentityPermissionService.require_project_access(operator, project)
        statuses = ["active"] if status is None else status
        if isinstance(statuses, str):
            statuses = [item.strip() for item in statuses.split(",") if item.strip()]
        if not statuses:
            statuses = ["active"]
        members = list(ProjectMember.objects(project=project, status__in=statuses))
        if tag:
            members = [member for member in members if tag in member.tags]
        needle = normalize_search_text(word)
        if needle:
            from app.models.team_member import TeamMember

            team_members = {
                str(item.user.id): item
                for item in TeamMember.objects(team=project.team, status="active")
            }

            def matches(member):
                values = [member.display_name]
                if member.user is not None:
                    values.extend([member.user.name, *member.user.aliases])
                    relation = team_members.get(str(member.user.id))
                    if relation:
                        values.extend(relation.aliases)
                return any(needle in normalize_search_text(value) for value in values)

            members = [member for member in members if matches(member)]
        return sorted(
            members, key=lambda item: (item.display_name.casefold(), str(item.id))
        )

    @classmethod
    def member_summaries(cls, projects, *, compact=False) -> dict[str, list[dict]]:
        """Load list-page summaries with one member query and one user query.

        Invited (pending) members are included so an invited-only role renders as
        distinct (e.g. orange) in the frontend member stats; each summary item
        carries its own ``status`` for the client to distinguish.
        """

        projects = list(projects)
        if not projects:
            return {}
        summaries = {str(project.id): [] for project in projects}

        project_ids = [ObjectId(project.pk) for project in projects]
        owners = {}
        if not compact:
            owners = {
                str(project.pk): _reference_id(project.owner_user)
                for project in projects
            }
        projection = {
            "project": 1,
            "display_name": 1,
            "tags": 1,
            "status": 1,
        }
        if not compact:
            projection.update(
                {
                    "user": 1,
                    "external_id": 1,
                    "version": 1,
                    "create_time": 1,
                    "edit_time": 1,
                }
            )
        raw_members = list(
            ProjectMember._get_collection()
            .find(
                {
                    "project": {"$in": project_ids},
                    "status": {"$in": ["active", "invited"]},
                },
                projection,
            )
            .sort([("display_name", 1), ("_id", 1)])
        )

        if compact:
            for member in raw_members:
                project_id = _reference_id(member.get("project"))
                summaries[str(project_id)].append(
                    {
                        "id": str(member["_id"]),
                        "display_name": member["display_name"],
                        "tags": list(member.get("tags") or []),
                        "status": member["status"],
                    }
                )
            return summaries

        user_ids = {
            _reference_id(member.get("user"))
            for member in raw_members
            if member.get("user") is not None
        }
        user_ids.discard(None)
        users_by_id = {
            user.pk: user
            for user in User.objects(id__in=list(user_ids)).only(
                "id", "name", "_avatar", "aliases"
            )
        }

        for member in raw_members:
            project_id = _reference_id(member.get("project"))
            user_id = _reference_id(member.get("user"))
            user = users_by_id.get(user_id)
            status = member["status"]
            tags = list(member.get("tags") or [])
            summaries[str(project_id)].append(
                {
                    "id": str(member["_id"]),
                    "project_id": str(project_id),
                    "user_id": str(user_id) if user_id else None,
                    "external_id": member.get("external_id"),
                    "user": (
                        {
                            "id": str(user.pk),
                            "name": user.name,
                            "avatar": user.avatar,
                            "has_avatar": bool(user._avatar),
                            "aliases": list(user.aliases or []),
                        }
                        if user is not None
                        else None
                    ),
                    "display_name": member["display_name"],
                    "tags": tags,
                    "effective_permissions": [],
                    "status": status,
                    "version": member["version"],
                    "is_owner": (
                        user_id is not None
                        and owners.get(str(project_id)) == user_id
                        and status == "active"
                        and "creator" in tags
                    ),
                    "create_time": member["create_time"].isoformat(),
                    "edit_time": member["edit_time"].isoformat(),
                }
            )
        return summaries

    @classmethod
    def search_team_projects(
        cls,
        team,
        operator,
        *,
        mode="search-project-name",
        word=None,
        worker_name=None,
        tag=None,
        status=None,
        project_set=None,
        project_sets=None,
    ):
        """Build a database-filtered team project search.

        Worker search resolves matching members first, but even that path
        returns a lazy Project queryset so callers can count and paginate in
        MongoDB instead of materializing every team project.
        """

        if IdentityPermissionService.is_active_team_member(operator, team) is None:
            raise NoPermissionError
        if mode not in ("search-project-name", "search-worker"):
            raise InvalidIdentityRequestError("invalid project search mode")
        if tag is not None:
            tag = tag.strip()
            if (
                not tag
                or IdentityPermissionService.tag_definition(team, "project", tag)
                is None
            ):
                raise InvalidIdentityTagError

        def status_values(value):
            if value is None or value == "":
                return None
            values = value if isinstance(value, (list, tuple, set)) else [value]
            result = []
            names = {
                "NORMAL": {NORMAL},
                "CLEARED": {CLEARED},
                # The project list exposes both terminal states under the
                # single "completed" filter.  A direct CLEARED filter still
                # remains available for API consumers that need it.
                "COMPLETED": {COMPLETED, CLEARED},
            }
            for item in values:
                if isinstance(item, str):
                    for part in item.split(","):
                        part = part.strip()
                        if not part:
                            continue
                        if part not in names:
                            raise InvalidIdentityRequestError("invalid project status")
                        result.extend(names[part])
                else:
                    raise InvalidIdentityRequestError("invalid project status")
            return set(result)

        allowed_statuses = status_values(status)
        wanted_project_sets = project_sets
        if project_set is not None:
            wanted_project_sets = [project_set]
        if wanted_project_sets:
            wanted_project_sets = {
                str(value.id) if hasattr(value, "id") else str(value)
                for value in wanted_project_sets
            }
        wanted_project_set_ids = set()
        for value in wanted_project_sets or ():
            try:
                wanted_project_set_ids.add(ObjectId(value))
            except (TypeError, ValueError):
                continue

        needle = normalize_search_text(word)
        projects = Project.objects(team=team)
        if wanted_project_set_ids:
            projects = projects.filter(project_set__in=wanted_project_set_ids)
        if allowed_statuses is not None:
            projects = projects.filter(status__in=allowed_statuses)
        if needle:
            projects = projects.filter(name_search__icontains=needle)

        if mode == "search-worker":
            worker_needle = normalize_search_text(worker_name)
            # ID resolution reads only scalar values.  Materializing thousands
            # of MongoEngine documents just to obtain their ids dominates the
            # request on large teams, so these stages use the collection API.
            base_project_ids = list(
                Project._get_collection().distinct("_id", projects._query)
            )
            member_filter = {
                "project": {"$in": base_project_ids},
                "status": {"$in": ["active", "invited"]},
            }
            if worker_needle:
                subject_ids = {
                    str(item.id)
                    for item in User.objects(name_search__icontains=worker_needle)
                }
                subject_ids.update(
                    str(item.id) for item in User.objects(aliases_search=worker_needle)
                )
                subject_ids.update(
                    str(item)
                    for item in TeamMember.objects(
                        team=team,
                        status="active",
                        aliases_search=worker_needle,
                    ).scalar("user")
                )
                display_match_filter = {
                    **member_filter,
                    "dns": {
                        "$regex": re.escape(worker_needle),
                        "$options": "i",
                    },
                }
                display_matches = ProjectMember._get_collection().distinct(
                    "_id", display_match_filter
                )
                member_filter["$or"] = [
                    {"_id": {"$in": display_matches}},
                    {
                        "user": {
                            "$in": [ObjectId(value) for value in subject_ids if value]
                        }
                    },
                ]
            if tag:
                member_filter["tags"] = tag
            matched_project_refs = ProjectMember._get_collection().distinct(
                "project", member_filter
            )
            matched_project_ids = {
                reference_id
                for reference_id in (
                    _reference_id(value) for value in matched_project_refs
                )
                if reference_id is not None
            }
            projects = projects.filter(id__in=matched_project_ids)

        return projects.order_by("-edit_time", "-id")

    @classmethod
    def _claim_operation(cls, project, operation_id):
        """Atomically claim an operation before running member side effects."""

        now = _now()
        lease_expires_at = now + datetime.timedelta(
            seconds=IDENTITY_OPERATION_LEASE_SECONDS
        )
        claim_token = str(uuid.uuid4())
        stored = IdentityOperation.objects(
            project=project, operation_id=operation_id
        ).first()
        if stored is None:
            try:
                stored = IdentityOperation(
                    project=project,
                    operation_id=operation_id,
                    status="processing",
                    edit_time=now,
                    lease_expires_at=lease_expires_at,
                    claim_token=claim_token,
                ).save()
                return stored, True, claim_token
            except NotUniqueError:
                stored = IdentityOperation.objects(
                    project=project, operation_id=operation_id
                ).first()

        if stored is not None and stored.status == "succeeded":
            return stored, False, None
        if stored is None:
            raise IdentityOperationInProgressError

        if stored.status == "processing":
            # New records use an explicit lease.  Records written by the
            # previous release have no lease fields, so their edit timestamp
            # is the compatibility heartbeat for one recovery window.
            stale_before = now - datetime.timedelta(
                seconds=IDENTITY_OPERATION_LEASE_SECONDS
            )
            last_touched = stored.edit_time or stored.create_time
            stale = (
                stored.lease_expires_at is not None and stored.lease_expires_at <= now
            ) or (
                stored.lease_expires_at is None
                and last_touched is not None
                and last_touched <= stale_before
            )
            if not stale:
                raise IdentityOperationInProgressError

            stale_filter = {
                "id": stored.id,
                "status": "processing",
                "edit_time": stored.edit_time,
            }
            if stored.lease_expires_at is None:
                stale_filter["lease_expires_at"] = None
            else:
                stale_filter["lease_expires_at"] = stored.lease_expires_at
            if stored.claim_token is None:
                stale_filter["claim_token"] = None
            else:
                stale_filter["claim_token"] = stored.claim_token
            claimed = IdentityOperation.objects(**stale_filter).update_one(
                set__status="processing",
                set__error_code=None,
                set__result={},
                set__edit_time=now,
                set__lease_expires_at=lease_expires_at,
                set__claim_token=claim_token,
            )
            if claimed != 1:
                current = IdentityOperation.objects(id=stored.id).first()
                if current is not None and current.status == "succeeded":
                    return current, False, None
                raise IdentityOperationInProgressError
            stored.reload()
            return stored, True, claim_token

        claimed = IdentityOperation.objects(id=stored.id, status="failed").update_one(
            set__status="processing",
            set__error_code=None,
            set__result={},
            set__edit_time=now,
            set__lease_expires_at=lease_expires_at,
            set__claim_token=claim_token,
        )
        if claimed != 1:
            current = IdentityOperation.objects(id=stored.id).first()
            if current is not None and current.status == "succeeded":
                return current, False, None
            raise IdentityOperationInProgressError
        stored.reload()
        return stored, True, claim_token

    @classmethod
    def _finish_operation(cls, stored, claim_token, *, status, result, error_code=None):
        """Persist a result only if this worker still owns the lease."""

        updated = IdentityOperation.objects(
            id=stored.id,
            status="processing",
            claim_token=claim_token,
        ).update_one(
            set__status=status,
            set__error_code=error_code,
            set__result=result,
            set__edit_time=_now(),
            set__lease_expires_at=None,
            set__claim_token=None,
        )
        if updated == 1:
            return result

        # A second worker may have reclaimed and completed the same operation
        # after the original lease expired.  Its durable result is authoritative.
        current = IdentityOperation.objects(id=stored.id).first()
        if current is not None and current.status in ("succeeded", "failed"):
            return current.result
        raise IdentityOperationInProgressError

    @classmethod
    def apply_changes(cls, project, operator, payload, *, request_id=None) -> dict:
        if not isinstance(payload, dict) or not isinstance(
            payload.get("operations"), list
        ):
            raise InvalidIdentityRequestError("operations must be an array")
        operations = payload["operations"]
        cls._require_operator(operator, project)
        # Validate the whole envelope before executing the first operation so
        # a malformed duplicate ID cannot leave an unexpected partial write.
        operation_ids = set()
        for operation in operations:
            if not isinstance(operation, dict):
                raise InvalidIdentityRequestError
            operation_id = operation.get("operation_id")
            if not isinstance(operation_id, str) or not operation_id.strip():
                raise InvalidIdentityRequestError("operation_id is required")
            if operation_id in operation_ids:
                raise InvalidIdentityRequestError("duplicate operation_id")
            operation_ids.add(operation_id)
        results = []
        for operation in operations:
            operation_id = operation.get("operation_id")
            stored, claimed, claim_token = cls._claim_operation(project, operation_id)
            if not claimed:
                results.append(stored.result)
                continue
            try:
                if operation.get("action") == "add":
                    member = cls.add(
                        project,
                        operator,
                        operation,
                        request_id=request_id,
                        operation_id=operation_id,
                    )
                    result = {
                        "operation_id": operation_id,
                        "status": "succeeded",
                        "member_id": str(member.id),
                        "member_status": member.status,
                        "member_version": member.version,
                    }
                elif operation.get("action") == "update":
                    member = cls.update(
                        project,
                        operator,
                        operation,
                        request_id=request_id,
                    )
                    result = {
                        "operation_id": operation_id,
                        "status": "succeeded",
                        "member_id": str(member.id),
                        "member_status": member.status,
                        "member_version": member.version,
                    }
                else:
                    raise InvalidIdentityRequestError("unsupported member action")
            except Exception as error:
                details = _exception_data(error)
                failed = {
                    "operation_id": operation_id,
                    "status": "failed",
                    **details,
                }
                failed = cls._finish_operation(
                    stored,
                    claim_token,
                    status="failed",
                    result=failed,
                    error_code=details["code"],
                )
                if failed.get("status") == "succeeded":
                    results.append(failed)
                    continue
                return {
                    "results": results,
                    "failed": failed,
                    "stopped": True,
                    "http_status": details["http_status"],
                }
            result = cls._finish_operation(
                stored,
                claim_token,
                status="succeeded",
                result=result,
            )
            results.append(result)
        return {"results": results, "failed": None, "stopped": False}
