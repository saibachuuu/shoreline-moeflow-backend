import datetime

from flask_babel import gettext
from mongoengine import (
    Document,
    ReferenceField,
    GenericReferenceField,
    IntField,
    StringField,
    DateTimeField,
)

from app.exceptions import InvitationFinishedError
from app.utils.mongo import mongo_slice


class InvitationStatus:
    PENDING = 1
    ALLOW = 2
    DENY = 3


def _role_code_to_tags(role_code):
    """Map a legacy project role to the current identity position tags."""
    return {
        "creator": ["creator"],
        "admin": ["admin"],
        "coordinator": ["proofreader"],
        "proofreader": ["proofreader"],
        "translator": ["translator"],
        "picture_editor": ["typesetter"],
        "supporter": ["translator"],
    }.get(role_code, [])


class Invitation(Document):
    user = ReferenceField("User", db_field="u", required=True)
    operator = ReferenceField("User", db_field="o", required=True)
    group = GenericReferenceField(
        choices=["Team", "Project"], db_field="g", required=True
    )
    role = GenericReferenceField(
        choices=["TeamRole", "ProjectRole"], db_field="r", required=True
    )
    status: int = IntField(
        required=True, db_field="s", default=InvitationStatus.PENDING
    )
    message: str = StringField(required=True, db_field="m", default="")
    create_time = DateTimeField(db_field="c", default=datetime.datetime.utcnow)

    @classmethod
    def get(cls, user=None, group=None, status=None, skip=None, limit=None):
        invitations: list[Invitation] = cls.objects()
        if user:
            invitations = invitations.filter(user=user)
        if group:
            invitations = invitations.filter(group=group)
        if status:
            # 如果是[1]这种只有一个参数的,则提取数组第一个元素，不使用in查询
            if isinstance(status, list) and len(status) == 1:
                status = status[0]
            # 数组使用in查询
            if isinstance(status, list):
                invitations = invitations.filter(status__in=status)
            else:
                invitations = invitations.filter(status=status)
        # 排序
        invitations = invitations.order_by("status", "-id")
        # 处理分页
        invitations = mongo_slice(invitations, skip, limit)
        return invitations

    def can_change_status(self):
        """检查邀请是否可转变状态"""
        if self.status == InvitationStatus.DENY:
            raise InvitationFinishedError(gettext("已被拒绝"))
        if self.status == InvitationStatus.ALLOW:
            raise InvitationFinishedError(gettext("已被同意"))

    def allow(self):
        self.can_change_status()
        project_tags = None
        project_display_name = None
        if self.group.__class__.__name__ == "Project":
            from app.services.project_invitation import ProjectInvitationAdapter

            # ``User.join`` still receives the legacy role for lifecycle
            # compatibility and may rewrite a removed/pending member's tags.
            # Capture the new projection before that call and restore it after
            # the legacy operation has completed.  When no projection exists
            # yet (legacy pending invitation), keep tags=None so the adapter
            # keeps the tags the join just wrote instead of clearing them.
            member = ProjectInvitationAdapter._project_member(self.group, self.user)
            if member is not None:
                project_tags = list(member.tags)
                project_display_name = member.display_name
        self.user.join(self.group, self.role)
        self.status = InvitationStatus.ALLOW
        self.save()
        if self.group.__class__.__name__ == "Project":
            from app.services.project_invitation import ProjectInvitationAdapter

            ProjectInvitationAdapter.project_active(
                self,
                tags=project_tags,
                display_name=project_display_name,
            )

    def deny(self):
        self.can_change_status()
        self.status = InvitationStatus.DENY
        self.save()
        if self.group.__class__.__name__ == "Project":
            from app.services.project_invitation import ProjectInvitationAdapter

            ProjectInvitationAdapter.project_removed(self)

    def delete(self, *args, **kwargs):
        """Project invitation cancellation also removes its member projection."""

        if (
            getattr(self, "group", None) is not None
            and self.group.__class__.__name__ == "Project"
            and self.status == InvitationStatus.PENDING
        ):
            from app.services.project_invitation import ProjectInvitationAdapter

            ProjectInvitationAdapter.project_removed(self)
        return super().delete(*args, **kwargs)

    def to_api(self):
        """
        @apiDefine InvitationInfoModel
        @apiSuccess {String} id ID
        @apiSuccess {Object} user 用户公共信息
        @apiSuccess {Object} group 团体
        @apiSuccess {String} group_type 目标类型,有team,project
        @apiSuccess {Object} operator 操作人
        @apiSuccess {String} create_time 创建时间
        @apiSuccess {Number} status 状态
        """
        tags = None
        if self.group.__class__.__name__ == "Project":
            # Project invitations expose the current identity position tags
            # instead of the legacy compatibility role.  The projected member
            # is authoritative; legacy-only invitations fall back to the role
            # mapping so the frontend never sees the old role names.
            member = None
            try:
                from app.services.project_invitation import ProjectInvitationAdapter

                member = ProjectInvitationAdapter._project_member(
                    self.group, self.user
                )
            except Exception:
                # The projection collection may be unavailable during early
                # startup; degrade to the legacy role mapping.
                member = None
            tags = _role_code_to_tags(
                getattr(self.role, "system_code", None)
            ) if member is None else list(member.tags)
        return {
            "id": str(self.id),
            "user": self.user.to_api(),
            "group": self.group.to_api(),
            "group_type": self.group.group_type,
            "role": self.role.to_api(),
            "tags": tags,
            "operator": self.operator.to_api() if self.operator else None,
            "create_time": self.create_time.isoformat(),
            "status": self.status,
        }
