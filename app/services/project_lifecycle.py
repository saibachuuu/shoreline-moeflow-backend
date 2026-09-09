"""Project lifecycle and ownership operations for the new three-state model."""

import datetime

from mongoengine.errors import NotUniqueError, SaveConditionError, ValidationError

from app.exceptions import (
    ClearOperationRetryableError,
    IdentityUserNotFoundError,
    InvalidIdentityRequestError,
    NoPermissionError,
    OwnerConflictError,
    ProjectStateConflictError,
)
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.user import User
from app.services.identity_audit import record_audit
from app.services.identity_permission import (
    CLEARED,
    COMPLETED,
    NORMAL,
    IdentityPermissionService,
    PROJECT_STATUS_NAMES,
)


def _now():
    return datetime.datetime.utcnow()


def _status_name(project):
    return PROJECT_STATUS_NAMES.get(project.status)


def _project_state(project):
    return {
        "status": _status_name(project),
        "status_version": project.status_version,
        "owner_user_id": str(project.owner_user.id) if project.owner_user else None,
    }


def _member_state(member):
    if member is None:
        return {}
    return {
        "id": str(member.id),
        "user_id": str(member.user.id) if member.user else None,
        "tags": list(member.tags),
        "status": member.status,
        "version": member.version,
    }


class ProjectLifecycleService:
    @staticmethod
    def status_name(project):
        return _status_name(project)

    @staticmethod
    def _payload(payload):
        if payload is None:
            return {}
        if not isinstance(payload, dict):
            raise InvalidIdentityRequestError
        return payload

    @classmethod
    def _expected(cls, project, payload, *, allowed_statuses):
        expected_status = payload.get("expected_status")
        if expected_status is not None:
            if expected_status not in allowed_statuses:
                raise InvalidIdentityRequestError("invalid expected_status")
            if expected_status != _status_name(project):
                raise ProjectStateConflictError
        expected_version = payload.get("expected_version")
        if expected_version is not None and not isinstance(expected_version, int):
            raise InvalidIdentityRequestError("expected_version must be an integer")
        if expected_version is not None and expected_version != project.status_version:
            raise ProjectStateConflictError
        return expected_status, expected_version

    @classmethod
    def _require_action(cls, operator, project, action):
        if not IdentityPermissionService.can_project_action(operator, project, action):
            raise NoPermissionError

    @staticmethod
    def _ensure_clear_not_in_progress(project):
        if Project.objects(id=project.id, clear_in_progress=True).only("id").first():
            raise ProjectStateConflictError

    @classmethod
    def complete(cls, project, operator, payload=None, *, request_id=None):
        payload = cls._payload(payload)
        cls._require_action(operator, project, "complete")
        cls._ensure_clear_not_in_progress(project)
        current = _status_name(project)
        if current == "COMPLETED":
            cls._expected(project, payload, allowed_statuses={"COMPLETED"})
            return project, False
        if current != "NORMAL":
            raise ProjectStateConflictError
        _, expected_version = cls._expected(
            project, payload, allowed_statuses={"NORMAL"}
        )
        old_state = _project_state(project)
        expected_version = (
            project.status_version if expected_version is None else expected_version
        )
        now = _now()
        updated = Project.objects(
            id=project.id,
            status=NORMAL,
            status_version=expected_version,
            clear_in_progress__ne=True,
        ).update_one(
            set__status=COMPLETED,
            set__completed_time=now,
            inc__status_version=1,
        )
        if updated != 1:
            raise ProjectStateConflictError
        project.reload()
        record_audit(
            actor=operator,
            scope="project",
            action="project_complete",
            project=project,
            before=old_state,
            after=_project_state(project),
            request_id=request_id,
            permission_sources=IdentityPermissionService.project_snapshot(
                operator, project
            ).to_api()["permission_sources"],
        )
        return project, True

    @classmethod
    def reopen(cls, project, operator, payload=None, *, request_id=None):
        payload = cls._payload(payload)
        cls._require_action(operator, project, "reopen")
        cls._ensure_clear_not_in_progress(project)
        current = _status_name(project)
        if current == "NORMAL":
            cls._expected(project, payload, allowed_statuses={"NORMAL"})
            return project, False
        if current != "COMPLETED":
            raise ProjectStateConflictError
        _, expected_version = cls._expected(
            project, payload, allowed_statuses={"COMPLETED"}
        )
        old_state = _project_state(project)
        expected_version = (
            project.status_version if expected_version is None else expected_version
        )
        updated = Project.objects(
            id=project.id,
            status=COMPLETED,
            status_version=expected_version,
            clear_in_progress__ne=True,
        ).update_one(
            set__status=NORMAL,
            unset__completed_time=1,
            inc__status_version=1,
        )
        if updated != 1:
            raise ProjectStateConflictError
        project.reload()
        record_audit(
            actor=operator,
            scope="project",
            action="project_reopen",
            project=project,
            before=old_state,
            after=_project_state(project),
            request_id=request_id,
            permission_sources=IdentityPermissionService.project_snapshot(
                operator, project
            ).to_api()["permission_sources"],
        )
        return project, True

    @classmethod
    def clear_contents(cls, project):
        """Delete content documents and backing objects, but keep the project."""

        from app.constants.file import FileNotExistReason, FileType
        from app.models.file import File
        from app.models.output import Output

        files = list(File.objects(project=project))
        # Delete real files first.  The operation is intentionally repeatable:
        # after an interruption the remaining documents are retried next time.
        for file in files:
            if file.type != FileType.FOLDER:
                file.delete_real_file(
                    update_cache=False,
                    file_not_exist_reason=FileNotExistReason.FINISH,
                )
            file.delete()
        outputs = list(Output.objects(project=project))
        if outputs:
            Output.delete_real_files_strict(outputs)
            Output.objects(project=project).delete()
        project.update(
            set__file_size=0,
            set__folder_count=0,
            set__file_count=0,
            set__source_count=0,
            set__translated_source_count=0,
            set__checked_source_count=0,
        )

    @classmethod
    def clear(cls, project, operator, payload=None, *, request_id=None):
        payload = cls._payload(payload)
        cls._require_action(operator, project, "clear")
        cls._ensure_clear_not_in_progress(project)
        current = _status_name(project)
        if current == "CLEARED":
            cls._expected(project, payload, allowed_statuses={"CLEARED"})
            return project, False
        if current not in ("NORMAL", "COMPLETED"):
            raise ProjectStateConflictError
        _, expected_version = cls._expected(
            project, payload, allowed_statuses={"NORMAL", "COMPLETED"}
        )
        old_state = _project_state(project)
        expected_version = (
            project.status_version if expected_version is None else expected_version
        )

        locked = Project.objects(
            id=project.id,
            status__in=(NORMAL, COMPLETED),
            status_version=expected_version,
            clear_in_progress__ne=True,
        ).update_one(set__clear_in_progress=True)
        if locked != 1:
            raise ProjectStateConflictError

        try:
            cls.clear_contents(project)
        except Exception as error:
            Project.objects(id=project.id, clear_in_progress=True).update_one(
                set__clear_in_progress=False
            )
            record_audit(
                actor=operator,
                scope="project",
                action="project_clear_retryable_failure",
                project=project,
                before=old_state,
                after=_project_state(project),
                request_id=request_id,
            )
            raise ClearOperationRetryableError from error

        updated = Project.objects(
            id=project.id,
            status__in=(NORMAL, COMPLETED),
            status_version=expected_version,
            clear_in_progress=True,
        ).update_one(
            set__status=CLEARED,
            set__clear_in_progress=False,
            inc__status_version=1,
        )
        if updated != 1:
            Project.objects(id=project.id, clear_in_progress=True).update_one(
                set__clear_in_progress=False
            )
            raise ProjectStateConflictError
        project.reload()
        record_audit(
            actor=operator,
            scope="project",
            action="project_clear",
            project=project,
            before=old_state,
            after=_project_state(project),
            request_id=request_id,
            permission_sources=IdentityPermissionService.project_snapshot(
                operator, project
            ).to_api()["permission_sources"],
        )
        return project, True

    @classmethod
    def transfer_owner(cls, project, operator, payload, *, request_id=None):
        payload = cls._payload(payload)
        new_owner_id = payload.get("new_owner_user_id")
        expected_owner_id = payload.get("expected_owner_user_id")
        expected_version = payload.get("expected_version")
        if not isinstance(new_owner_id, str) or not new_owner_id:
            raise InvalidIdentityRequestError("new_owner_user_id is required")
        if expected_version is not None and not isinstance(expected_version, int):
            raise InvalidIdentityRequestError("expected_version must be an integer")
        if expected_owner_id is not None and not isinstance(expected_owner_id, str):
            raise InvalidIdentityRequestError("expected_owner_user_id must be a string")

        snapshot = IdentityPermissionService.project_snapshot(operator, project)
        team_manager = IdentityPermissionService.is_team_manager(operator, project.team)
        if (
            not snapshot.is_owner
            and not snapshot.has("project:MANAGE_MEMBERS")
            and not team_manager
        ):
            raise NoPermissionError
        current_owner_id = str(project.owner_user.id) if project.owner_user else None
        if expected_owner_id is not None and expected_owner_id != current_owner_id:
            raise OwnerConflictError
        owner_version = getattr(project, "owner_version", 0)
        if expected_version is not None and expected_version != owner_version:
            raise OwnerConflictError

        try:
            new_owner = User.objects(id=new_owner_id).first()
        except ValidationError:
            new_owner = None
        if new_owner is None:
            raise IdentityUserNotFoundError
        if (
            IdentityPermissionService.is_active_team_member(new_owner, project.team)
            is None
        ):
            raise OwnerConflictError
        new_member = ProjectMember.objects(
            project=project, user=new_owner, status="active"
        ).first()
        if new_member is None:
            raise OwnerConflictError
        if current_owner_id == new_owner_id:
            return project, new_member, False

        old_member = (
            ProjectMember.objects(project=project, user=project.owner_user).first()
            if project.owner_user
            else None
        )
        if (
            old_member is None
            or old_member.status != "active"
            or "creator" not in old_member.tags
        ):
            raise OwnerConflictError

        before_project = _project_state(project)
        before_old = _member_state(old_member)
        before_new = _member_state(new_member)
        old_member_before = {
            "tags": list(old_member.tags),
            "version": old_member.version,
            "edit_time": old_member.edit_time,
            "removed_time": old_member.removed_time,
        }
        new_member_before = {
            "tags": list(new_member.tags),
            "version": new_member.version,
            "edit_time": new_member.edit_time,
            "removed_time": new_member.removed_time,
        }
        keep_admin = bool(payload.get("keep_old_owner_admin", False))
        old_tags = set(old_member.tags)
        old_tags.discard("creator")
        if keep_admin:
            old_tags.add("admin")
        new_tags = set(new_member.tags)
        new_tags.add("creator")
        # A failed second write is repaired from these snapshots below.  This
        # is the portable fallback for deployments without Mongo transactions.
        old_saved = False
        new_saved = False
        try:
            old_member.tags = sorted(old_tags)
            old_member.version += 1
            old_member.edit_time = _now()
            old_member.save(save_condition={"version": old_member_before["version"]})
            old_saved = True
            new_member.tags = sorted(new_tags)
            new_member.version += 1
            new_member.edit_time = _now()
            new_member.save(save_condition={"version": new_member_before["version"]})
            new_saved = True
            updated = Project.objects(
                id=project.id,
                owner_user=project.owner_user,
                owner_version=owner_version,
                clear_in_progress__ne=True,
            ).update_one(
                set__owner_user=new_owner,
                inc__owner_version=1,
                inc__status_version=1,
            )
            if updated != 1:
                raise OwnerConflictError
        except (OwnerConflictError, NotUniqueError, SaveConditionError):
            if old_saved:
                old_member.tags = old_member_before["tags"]
                old_member.version = old_member_before["version"]
                old_member.edit_time = old_member_before["edit_time"]
                old_member.removed_time = old_member_before["removed_time"]
                try:
                    old_member.save(
                        save_condition={"version": old_member_before["version"] + 1}
                    )
                except SaveConditionError:
                    pass
            if new_saved:
                new_member.tags = new_member_before["tags"]
                new_member.version = new_member_before["version"]
                new_member.edit_time = new_member_before["edit_time"]
                new_member.removed_time = new_member_before["removed_time"]
                try:
                    new_member.save(
                        save_condition={"version": new_member_before["version"] + 1}
                    )
                except SaveConditionError:
                    pass
            raise OwnerConflictError
        project.reload()
        record_audit(
            actor=operator,
            scope="project",
            action="project_owner_transfer",
            project=project,
            member=new_member,
            target_user=new_owner,
            before={
                "project": before_project,
                "old_member": before_old,
                "new_member": before_new,
            },
            after={
                "project": _project_state(project),
                "old_member": _member_state(old_member),
                "new_member": _member_state(new_member),
            },
            request_id=request_id,
        )
        return project, new_member, True
