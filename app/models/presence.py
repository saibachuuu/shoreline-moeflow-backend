import datetime
from mongoengine import (
    DateTimeField,
    Document,
    ReferenceField,
    StringField,
)
from app.models.project import Project
from app.models.team import Team
from app.models.user import User

# Heartbeat interval on frontend is 15s.
# A presence is considered stale / expired after 45 seconds of no heartbeat.
PRESENCE_TIMEOUT_SECONDS = 45


class ProjectPresence(Document):
    meta = {
        "collection": "project_presence",
        "indexes": [
            "project",
            "team",
            "user",
            {"fields": ["expire_at"], "expireAfterSeconds": 0},
            {"fields": ["project", "user"], "unique": True},
            {"fields": ["team", "expire_at"]},
            {"fields": ["project", "expire_at"]},
        ],
    }

    project = ReferenceField(Project, required=True)
    team = ReferenceField(Team, required=True)
    user = ReferenceField(User, required=True)
    action = StringField(default="working", max_length=64)
    last_heartbeat = DateTimeField(default=datetime.datetime.utcnow)
    expire_at = DateTimeField(required=True)

    @classmethod
    def heartbeat(cls, project, user, action="working"):
        now = datetime.datetime.utcnow()
        expire_at = now + datetime.timedelta(seconds=PRESENCE_TIMEOUT_SECONDS)
        cls.objects(project=project, user=user).update_one(
            set__team=project.team,
            set__action=(action or "working")[:64],
            set__last_heartbeat=now,
            set__expire_at=expire_at,
            upsert=True,
        )

    @classmethod
    def leave(cls, project, user):
        cls.objects(project=project, user=user).delete()

    @classmethod
    def active_users_for_project(cls, project):
        now = datetime.datetime.utcnow()
        presences = list(
            cls.objects(project=project, expire_at__gt=now).select_related(max_depth=1)
        )
        if not presences:
            return []

        user_ids = [p.user.id for p in presences if p.user]
        from app.models.project_member import ProjectMember

        pms = {
            str(pm.user.id): pm.display_name
            for pm in ProjectMember.objects(
                project=project, user__in=user_ids, status="active"
            )
        }
        users = []
        for p in presences:
            u = p.user
            if not u:
                continue
            name = (
                pms.get(str(u.id))
                or getattr(u, "default_display_name", "")
                or u.name
            )
            users.append({
                "id": str(u.id),
                "name": name,
                "avatar": u.avatar or "",
                "action": p.action or "working",
                "last_heartbeat": (
                    p.last_heartbeat.isoformat() if p.last_heartbeat else ""
                ),
            })
        return users

    @classmethod
    def active_presence_for_team(cls, team):
        now = datetime.datetime.utcnow()
        presences = list(
            cls.objects(team=team, expire_at__gt=now).select_related(max_depth=1)
        )
        if not presences:
            return {}

        project_ids = list({p.project.id for p in presences if p.project})
        user_ids = list({p.user.id for p in presences if p.user})

        from app.models.project_member import ProjectMember

        pm_map = {}
        for pm in ProjectMember.objects(
            project__in=project_ids, user__in=user_ids, status="active"
        ):
            pm_map[(str(pm.project.id), str(pm.user.id))] = pm.display_name

        active_projects = {}
        for p in presences:
            if not p.project or not p.user:
                continue
            pid = str(p.project.id)
            if pid not in active_projects:
                active_projects[pid] = {
                    "project_id": pid,
                    "user_count": 0,
                    "users": [],
                }
            u = p.user
            name = (
                pm_map.get((pid, str(u.id)))
                or getattr(u, "default_display_name", "")
                or u.name
            )
            active_projects[pid]["users"].append({
                "id": str(u.id),
                "name": name,
                "avatar": u.avatar or "",
                "action": p.action or "working",
                "last_heartbeat": (
                    p.last_heartbeat.isoformat() if p.last_heartbeat else ""
                ),
            })

        for pid in active_projects:
            active_projects[pid]["user_count"] = len(active_projects[pid]["users"])

        return active_projects
