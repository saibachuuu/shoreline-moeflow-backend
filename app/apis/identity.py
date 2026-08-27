"""HTTP endpoints for the project identity-tag runtime."""

from flask import request
from app.core.api import APIResponse

from app.core.responses import MoePagination
from app.core.views import MoeAPIView
from app.decorators.auth import admin_required, token_required
from app.decorators.url import fetch_model
from app.models.project import Project
from app.models.team import Team
from app.models.user import User
from app.services.identity_permission import IdentityPermissionService
from app.services.project_lifecycle import ProjectLifecycleService
from app.services.project_member import ProjectMemberService
from app.services.team_member import IdentityTagPolicyService, TeamMemberService
from app.services.user_alias import UserAliasService


def _json():
    data = request.get_json(silent=True)
    if data is None:
        return {}
    return data


def _request_id():
    return request.headers.get("X-Request-ID") or request.headers.get("Idempotency-Key")


def _member_page(members):
    page = MoePagination()
    # Member-management lists consume identity (user.avatar/name) but never
    # the per-member permission snapshot; computing it here runs a full
    # project-permission projection for every row (the frontend loads up to
    # 3x1000 rows on the member screen).  Keep the field present and empty
    # for a uniform response shape — single-member responses still carry the
    # real permissions via member.to_api().
    data = [member.to_api(include_permissions=False) for member in members]
    return page.set_data(data=data[page.skip : page.skip + page.limit], count=len(data))


def _project_api(project, user):
    data = project.to_api(user=user)
    # Single-project responses (lifecycle/owner transfers) carry the member
    # summary just like the list endpoints, so the frontend member stats never
    # see an undefined memberSummary.
    data["member_summary"] = ProjectMemberService.member_summaries([project]).get(
        str(project.id), []
    )
    return data


class ProjectMemberListAPI(MoeAPIView):
    @token_required
    @fetch_model(Project)
    def get(self, project):
        members = ProjectMemberService.list_members(
            project,
            self.current_user,
            status=request.args.get("status", "active"),
            tag=request.args.get("tag"),
            word=request.args.get("word"),
        )
        return _member_page(members)


class ProjectMemberChangesAPI(MoeAPIView):
    @token_required
    @fetch_model(Project)
    def post(self, project):
        result = ProjectMemberService.apply_changes(
            project,
            self.current_user,
            _json(),
            request_id=_request_id(),
        )
        if result.get("failed"):
            return APIResponse(result, status_code=result.get("http_status", 400))
        return result


class ProjectMemberBindAPI(MoeAPIView):
    @token_required
    @fetch_model(Project)
    def post(self, project, member_id):
        member, event = ProjectMemberService.bind(
            project,
            member_id,
            self.current_user,
            _json(),
            request_id=_request_id(),
        )
        return {
            "member": member.to_api(),
            "audit_event_id": str(event.id),
        }


class ProjectMemberMergeAPI(MoeAPIView):
    @token_required
    @fetch_model(Project)
    def post(self, project, member_id):
        member, source, event = ProjectMemberService.merge(
            project,
            member_id,
            self.current_user,
            _json(),
            request_id=_request_id(),
        )
        return {
            "member": member.to_api(),
            "source_member": source.to_api(),
            "audit_event_id": str(event.id),
        }


class ProjectOwnerTransferAPI(MoeAPIView):
    @token_required
    @fetch_model(Project)
    def post(self, project):
        project, member, changed = ProjectLifecycleService.transfer_owner(
            project,
            self.current_user,
            _json(),
            request_id=_request_id(),
        )
        return {
            "project": _project_api(project, self.current_user),
            "member": member.to_api(),
            "changed": changed,
        }


class _ProjectLifecycleAPI(MoeAPIView):
    action = None

    @token_required
    @fetch_model(Project)
    def post(self, project):
        service = getattr(ProjectLifecycleService, self.action)
        project, changed = service(
            project,
            self.current_user,
            _json(),
            request_id=_request_id(),
        )
        return {
            "project": _project_api(project, self.current_user),
            "status": ProjectLifecycleService.status_name(project),
            "status_version": project.status_version,
            "changed": changed,
        }


class ProjectCompleteAPI(_ProjectLifecycleAPI):
    action = "complete"


class ProjectClearAPI(_ProjectLifecycleAPI):
    action = "clear"


class ProjectReopenAPI(_ProjectLifecycleAPI):
    action = "reopen"


class TeamMemberListAPI(MoeAPIView):
    @token_required
    @fetch_model(Team)
    def get(self, team):
        members = TeamMemberService.list_members(
            team,
            self.current_user,
            status=request.args.get("status"),
            word=request.args.get("word"),
        )
        page = MoePagination()
        # Batch-prefetch every referenced user in ONE query: ``TeamMember.user``
        # lazily dereferences per member in to_api(), so a several-thousand
        # member team list would otherwise issue one user lookup per row
        # (N+1).  Only the page slice is serialized (``count`` still reflects
        # the full match set), so a deep page never materializes the whole
        # member document set.
        from app.models.user import User

        paged_members = members[page.skip : page.skip + page.limit]
        user_ids = {
            member.user.id
            for member in members
            if member.user is not None
        }
        user_map = {
            str(user.id): user
            for user in User.objects(id__in=list(user_ids))
        }
        data = [member.to_api(user_map=user_map) for member in paged_members]
        return page.set_data(data=data, count=len(members))

    @token_required
    @fetch_model(Team)
    def post(self, team):
        member = TeamMemberService.add(
            team,
            self.current_user,
            _json(),
            request_id=_request_id(),
        )
        return {"member": member.to_api()}


class TeamMemberAPI(MoeAPIView):
    @token_required
    @fetch_model(Team)
    def patch(self, team, member_id):
        member = TeamMemberService.update(
            team,
            member_id,
            self.current_user,
            _json(),
            request_id=_request_id(),
        )
        return {"member": member.to_api()}

    @token_required
    @fetch_model(Team)
    def delete(self, team, member_id):
        member = TeamMemberService.remove(
            team,
            member_id,
            self.current_user,
            request_id=_request_id(),
        )
        return {"member": member.to_api()}


class TeamMemberAliasesAPI(MoeAPIView):
    @token_required
    @fetch_model(Team)
    def patch(self, team, member_id):
        data = _json()
        member = TeamMemberService.update_aliases(
            team,
            member_id,
            self.current_user,
            data.get("aliases") if isinstance(data, dict) else None,
            expected_version=data.get("expected_version") if isinstance(data, dict) else None,
            request_id=_request_id(),
        )
        return {"member": member.to_api()}


class TeamMemberDefaultDisplayNameAPI(MoeAPIView):
    @token_required
    @fetch_model(Team)
    def patch(self, team, member_id):
        data = _json()
        member = TeamMemberService.update_default_display_name(
            team,
            member_id,
            self.current_user,
            data.get("default_display_name") if isinstance(data, dict) else None,
            expected_version=data.get("expected_version") if isinstance(data, dict) else None,
            request_id=_request_id(),
        )
        return {"member": member.to_api()}


class IdentityTagPolicyAPI(MoeAPIView):
    @token_required
    @fetch_model(Team)
    def get(self, team):
        if not IdentityPermissionService.is_team_manager(self.current_user, team):
            from app.exceptions import NoPermissionError

            raise NoPermissionError
        return IdentityTagPolicyService.response(team)

    @token_required
    @fetch_model(Team)
    def patch(self, team):
        IdentityTagPolicyService.update(
            team,
            self.current_user,
            _json(),
            request_id=_request_id(),
        )
        return IdentityTagPolicyService.response(team)


class MeAliasesAPI(MoeAPIView):
    @token_required
    def patch(self):
        data = _json()
        user, event = UserAliasService.replace(
            self.current_user,
            self.current_user,
            data.get("aliases") if isinstance(data, dict) else None,
            request_id=_request_id(),
        )
        return {"user": user.to_api(), "audit_event_id": str(event.id)}


class UserAliasesAPI(MoeAPIView):
    @admin_required
    @fetch_model(User, from_name="user_id", to_name="user")
    def patch(self, user):
        data = _json()
        target, event = UserAliasService.replace(
            user,
            self.current_user,
            data.get("aliases") if isinstance(data, dict) else None,
            request_id=_request_id(),
        )
        return {"user": target.to_api(), "audit_event_id": str(event.id)}
