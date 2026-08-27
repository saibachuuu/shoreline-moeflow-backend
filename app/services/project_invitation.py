"""Project-member projection for the existing invitation lifecycle."""

import datetime

from app.exceptions import InvitationAlreadyExistError, NoPermissionError
from mongoengine.errors import NotUniqueError, SaveConditionError
from app.models.invitation import Invitation, InvitationStatus
from app.models.project import ProjectRole
from app.services.identity_audit import record_audit


class ProjectInvitationAdapter:
    """Keep invitation business semantics in the legacy workflow.

    The fixed legacy role is only a compatibility argument.  It is never read
    by ``IdentityPermissionService`` and cannot be supplied by the client.
    """

    @staticmethod
    def _project_member(project, user):
        from app.services.project_member import ProjectMemberService

        return ProjectMemberService.for_user(project, user)

    @classmethod
    def _ensure_projection(
        cls, project, user, *, tags, display_name, status, operator=None, request_id=None
    ):
        from app.models.project_member import ProjectMember
        from app.services.project_member import _member_state, ProjectMemberService

        member = cls._project_member(project, user)
        before = _member_state(member) if member else {}
        previous_version = member.version if member is not None else None
        desired_display_name = display_name or (
            member.display_name
            if member is not None
            # A fresh projection has no alias yet: the user's per-team
            # default display name applies (falling back to the site name).
            else ProjectMemberService.team_default_display_name(project, user)
        )
        desired_tags = list(tags)
        if member is None:
            member = ProjectMember(
                project=project,
                user=user,
                display_name=desired_display_name,
                tags=desired_tags,
                status=status,
            )
        else:
            # A stale pending invitation can be deleted after another legacy
            # path has already joined the user.  Never downgrade that active
            # membership while projecting cancellation.
            if status in ("invited", "removed") and member.status == "active":
                return member
            # Replaying a recovered member operation must not create a new
            # version or audit event when the projection is already complete.
            if (
                member.status == status
                and member.display_name == desired_display_name
                and member.tags == desired_tags
            ):
                ProjectMemberService._sync_count(project)
                return member
            member.display_name = desired_display_name
            member.tags = desired_tags
            member.status = status
            member.removed_time = (
                datetime.datetime.utcnow() if status == "removed" else None
            )
            member.edit_time = datetime.datetime.utcnow()
            member.version += 1
        if previous_version is None:
            try:
                member.save()
            except NotUniqueError:
                # Another invitation callback won the logical (project, user)
                # insert race.  Its projection is the idempotent result.
                existing = cls._project_member(project, user)
                if existing is None:
                    raise
                return existing
        else:
            try:
                member.save(save_condition={"version": previous_version})
            except SaveConditionError:
                return cls._project_member(project, user)
        ProjectMemberService._sync_count(project)
        if before != _member_state(member):
            record_audit(
                actor=operator,
                scope="project",
                action=f"project_invitation_projection_{status}",
                project=project,
                member=member,
                target_user=user,
                before=before,
                after=_member_state(member),
                request_id=request_id,
            )
        return member

    @classmethod
    def create_or_reuse(
        cls,
        project,
        operator,
        user,
        *,
        tags,
        display_name=None,
        request_id=None,
        message="",
    ):
        """Call the existing invitation entry point and project its result."""

        role = ProjectRole.by_system_code("translator")
        existing_member = cls._project_member(project, user)
        if existing_member is not None and existing_member.status == "active":
            return existing_member

        # The new collection may have been introduced after an old pending
        # invitation was created.  Reuse that invitation instead of asking the
        # legacy method to create a duplicate and then translating its error.
        invitation = Invitation.objects(
            user=user,
            group=project,
            status=InvitationStatus.PENDING,
        ).order_by("-id").first()

        if invitation is None:
            try:
                operator.invite(user, project, role, message)
            except NoPermissionError:
                # A new TeamMember may not have a legacy relation yet.  Preserve
                # the invitation lifecycle by creating the same pending object,
                # while still requiring the new service to have authorized the
                # operation before reaching this adapter.
                try:
                    invitation = Invitation(
                        user=user,
                        operator=operator,
                        group=project,
                        role=role,
                        message=message,
                        status=InvitationStatus.PENDING,
                    ).save()
                except NotUniqueError:
                    invitation = Invitation.objects(
                        user=user,
                        group=project,
                        status=InvitationStatus.PENDING,
                    ).order_by("-id").first()
            except InvitationAlreadyExistError:
                # Another request won the invitation race.  The pending record
                # is the source of truth for the projection.
                invitation = Invitation.objects(
                    user=user,
                    group=project,
                    status=InvitationStatus.PENDING,
                ).order_by("-id").first()
            else:
                invitation = Invitation.objects(
                    user=user,
                    group=project,
                    status=InvitationStatus.PENDING,
                ).order_by("-id").first()

        # ``User.invite`` may join the user immediately (for example when the
        # user already belongs to the project team).  No Invitation object is
        # created in that branch, so project the active member directly.
        existing_member = cls._project_member(project, user)
        if existing_member is not None and existing_member.status == "active":
            return cls._ensure_projection(
                project,
                user,
                tags=tags,
                display_name=display_name,
                status="active",
                operator=operator,
                request_id=request_id,
            )

        if invitation is None:
            # The old workflow returned without a relation or pending invite;
            # do not report an active member in that ambiguous case.
            raise RuntimeError("project invitation projection was not created")
        return cls.project_pending(
            invitation,
            tags=tags,
            display_name=display_name,
            operator=operator,
            request_id=request_id,
        )

    @classmethod
    def update_pending_tags(
        cls, invitation, tags, *, operator=None, request_id=None
    ):
        """Update the position tags of a pending project invitation.

        The legacy ``Invitation.role`` stays as the compatibility argument;
        the projected ``ProjectMember`` is the current identity source of
        truth.  Callers must validate ``tags`` (qualification mode, tag
        assignability) before reaching this helper.
        """
        from app.services.project_member import _member_state, ProjectMemberService

        project = invitation.group
        user = invitation.user
        member = cls._project_member(project, user)
        before = _member_state(member) if member else {}
        previous_version = member.version if member is not None else None
        if member is None or member.status in ("invited", "removed"):
            # Re-anchor the pending invitation to the requested positions even
            # when a stale removed projection exists.
            return cls._ensure_projection(
                project,
                user,
                tags=tags,
                display_name=member.display_name
                if member is not None
                else ProjectMemberService.team_default_display_name(project, user),
                status="invited",
                operator=operator or invitation.operator,
                request_id=request_id,
            )
        # The user already joined (direct entry or accepted elsewhere): apply
        # the position change to the live member exactly like a member update.
        desired_tags = list(tags)
        if member.tags == desired_tags:
            ProjectMemberService._sync_count(project)
            return member
        member.tags = desired_tags
        member.edit_time = datetime.datetime.utcnow()
        member.version += 1
        try:
            member.save(save_condition={"version": previous_version})
        except SaveConditionError:
            current = cls._project_member(project, user)
            if current is not None:
                return current
            raise
        ProjectMemberService._sync_count(project)
        record_audit(
            actor=operator or invitation.operator,
            scope="project",
            action="project_invitation_position_update",
            project=project,
            member=member,
            target_user=user,
            before=before,
            after=_member_state(member),
            request_id=request_id,
        )
        return member

    @classmethod
    def project_pending(
        cls, invitation, *, tags=None, display_name=None, operator=None, request_id=None
    ):
        member = cls._project_member(invitation.group, invitation.user)
        if tags is None:
            tags = member.tags if member else []
        return cls._ensure_projection(
            invitation.group,
            invitation.user,
            tags=tags,
            display_name=display_name,
            status="invited",
            operator=operator or invitation.operator,
            request_id=request_id,
        )

    @classmethod
    def project_active(
        cls, invitation_or_relation, *, tags=None, display_name=None, operator=None, request_id=None
    ):
        if isinstance(invitation_or_relation, Invitation):
            project = invitation_or_relation.group
            user = invitation_or_relation.user
            operator = operator or invitation_or_relation.operator
        else:
            project = invitation_or_relation.group
            user = invitation_or_relation.user
        member = cls._project_member(project, user)
        if tags is None:
            tags = member.tags if member else []
        return cls._ensure_projection(
            project,
            user,
            tags=tags,
            display_name=display_name,
            status="active",
            operator=operator,
            request_id=request_id,
        )

    @classmethod
    def project_removed(cls, invitation, *, request_id=None):
        member = cls._project_member(invitation.group, invitation.user)
        if member is None:
            return cls._ensure_projection(
                invitation.group,
                invitation.user,
                tags=[],
                display_name=invitation.user.name,
                status="removed",
                operator=getattr(invitation, "operator", None),
                request_id=request_id,
            )
        if member.status in ("removed", "active"):
            return member
        return cls._ensure_projection(
            invitation.group,
            invitation.user,
            tags=member.tags,
            display_name=member.display_name,
            status="removed",
            operator=invitation.operator,
            request_id=request_id,
        )
