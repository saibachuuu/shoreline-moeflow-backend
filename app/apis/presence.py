from flask import request
from app.core.views import MoeAPIView
from app.decorators.auth import token_required
from app.decorators.url import fetch_model
from app.models.presence import ProjectPresence
from app.models.project import Project
from app.models.team import Team


class ProjectPresenceHeartbeatAPI(MoeAPIView):
    @token_required
    @fetch_model(Project)
    def post(self, project):
        data = request.get_json(silent=True) or {}
        action = (
            data.get("action", "working") if isinstance(data, dict) else "working"
        )
        if not isinstance(action, str):
            action = "working"
        ProjectPresence.heartbeat(project, self.current_user, action=action[:64])
        active_users = ProjectPresence.active_users_for_project(project)
        return {
            "message": "ok",
            "project_id": str(project.id),
            "active_users": active_users,
        }


class ProjectPresenceLeaveAPI(MoeAPIView):
    @token_required
    @fetch_model(Project)
    def post(self, project):
        ProjectPresence.leave(project, self.current_user)
        return {
            "message": "ok",
            "project_id": str(project.id),
        }


class ProjectPresenceAPI(MoeAPIView):
    @token_required
    @fetch_model(Project)
    def get(self, project):
        active_users = ProjectPresence.active_users_for_project(project)
        return {
            "project_id": str(project.id),
            "active_users": active_users,
        }


class TeamProjectsActivePresenceAPI(MoeAPIView):
    @token_required
    @fetch_model(Team)
    def get(self, team):
        active_projects = ProjectPresence.active_presence_for_team(team)
        return {
            "active_projects": active_projects,
        }
