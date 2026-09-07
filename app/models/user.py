import datetime
import re
import time
from typing import NoReturn, Optional, Union

from flask import current_app, g
from flask_babel import gettext
from itsdangerous import (
    BadSignature,
    TimedJSONWebSignatureSerializer,
    URLSafeTimedSerializer,
)
from mongoengine import (
    CASCADE,
    NULLIFY,
    BooleanField,
    DateTimeField,
    Document,
    ListField,
    StringField,
)
from werkzeug.security import check_password_hash, generate_password_hash

from app import oss
from app.constants.locale import Locale
from app.exceptions import (
    ApplicationAlreadyExistError,
    BadTokenError,
    CreatorCanNotLeaveError,
    EmailRegexError,
    EmailRegisteredError,
    InvitationAlreadyExistError,
    MemberAlreadyOwnerError,
    NoPermissionError,
    TargetIsFullError,
    UserAlreadyJoinedError,
    UserNameLengthError,
    UserNameRegexError,
    UserNameRegisteredError,
    UserNotExistError,
)
from app.models.application import Application, ApplicationStatus
from app.models.invitation import Invitation, InvitationStatus
from app.models.message import Message
from app.models.output import Output
from app.models.project import Project, ProjectRole, ProjectUserRelation
from app.models.site_setting import SiteSetting
from app.models.team import Team, TeamPermission, TeamRole, TeamUserRelation
from app.regexs import EMAIL_REGEX, USER_NAME_REGEX
from app.utils.hash import md5
from app.utils.mongo import mongo_order, mongo_slice
from app.utils.search import normalize_search_text


class _IdentityRelation:
    """Read-only compatibility view backed exclusively by identity members."""

    def __init__(self, member, role):
        self.member = member
        self.user = member.user
        self.group = member.team if hasattr(member, "team") else member.project
        self.role = role

    def __getattr__(self, name):
        return getattr(self.member, name)

    def delete(self):
        raise RuntimeError("identity relations must be changed through identity services")

    def save(self, *args, **kwargs):
        raise RuntimeError("identity relations must be changed through identity services")


class User(Document):
    meta = {
        "indexes": [
            "email",
            "name",
            "aliases",
            {"fields": ["name_search"], "name": "user_name_search_v1"},
            {"fields": ["aliases_search"], "name": "user_aliases_search_v1"},
        ]
    }

    email = StringField(required=True, unique=True, db_field="e")  # 邮箱
    name = StringField(required=True, unique=True, db_field="n")  # 姓名
    aliases = ListField(StringField(), default=list, db_field="as")
    name_search = StringField(default="", db_field="ns")
    aliases_search = ListField(StringField(), default=list, db_field="asrch")
    signature = StringField(default="", db_field="s")  # 个性签名
    locale = StringField(default=Locale.AUTO, db_field="l")  # 语言 # NOT USED
    timezone = StringField(default="", db_field="t")  # 时区
    _avatar = StringField(default="", db_field="a")  # 头像
    banned = BooleanField(defult=False, db_field="b")  # 是否被封禁
    password_hash = StringField(db_field="p")  # 密码哈希
    admin = BooleanField(default=False)
    create_time = DateTimeField(db_field="c", default=datetime.datetime.utcnow)

    def clean(self):
        # Keep direct model writes subject to the same site-alias rules as the
        # dedicated alias service.  The service applies the configured count
        # limit before saving; this fallback keeps old callers normalized.
        from app.services.identity_permission import normalize_aliases

        try:
            from flask import current_app

            max_count = int(
                current_app.config.get(
                    "MAX_USER_ALIASES",
                    current_app.config.get("max_user_aliases", 10),
                )
            )
        except RuntimeError:
            max_count = 10

        self.aliases = normalize_aliases(
            list(self.aliases or []), name=self.name, max_count=max_count
        )
        self.name_search = normalize_search_text(self.name)
        self.aliases_search = [
            normalized
            for normalized in (
                normalize_search_text(alias) for alias in (self.aliases or [])
            )
            if normalized
        ]

    @classmethod
    def create(cls, name: str, email: str, password: str) -> "User":
        """创建用户"""
        user = cls(name=name, email=email.lower())
        user.password = password
        user.save()
        # 将用户自动加入默认团队
        auto_join_team_ids = SiteSetting.get().auto_join_team_ids
        if auto_join_team_ids and isinstance(auto_join_team_ids, list):
            for team_id in auto_join_team_ids:
                team = Team.objects(id=team_id).first()
                if team is not None:
                    user.join(team)
        return user

    @classmethod
    def by_id(cls, id) -> Union["User", NoReturn]:
        user = cls.objects(id=id).first()
        if user is None:
            raise UserNotExistError(id)
        return user

    @classmethod
    def get_by_email(cls, email: str) -> Union["User", None]:
        return cls.objects(email=email.lower()).first()

    @classmethod
    def by_email(cls, email: str) -> Union["User", NoReturn]:
        user = cls.get_by_email(email)
        if user is None:
            raise UserNotExistError(email)
        return user

    @classmethod
    def by_name(cls, name) -> Union["User", NoReturn]:
        user = cls.objects(name=name).first()
        if user is None:
            raise UserNotExistError(name)
        return user

    @property
    def password(self) -> NoReturn:
        raise AttributeError("password is unreadable")

    @password.setter
    def password(self, password):
        self.password_hash = generate_password_hash(password)

    @classmethod
    def verify_new_email(cls, email):
        """
        验证邮箱是否符合要求

        :param email:
        :return:
        """
        # 检测合法性
        if re.match(EMAIL_REGEX, email) is None:
            raise EmailRegexError
        # 检测唯一性
        if cls.get_by_email(email):
            raise EmailRegisteredError

    @classmethod
    def verify_new_name(cls, name):
        """
        验证名称是否符合要求

        :param name:
        :return:
        """
        # 检测长度
        if not 2 <= len(name) <= 18:
            raise UserNameLengthError
        # 检测合法性
        if re.match(USER_NAME_REGEX, name) is None:
            raise UserNameRegexError
        # 检测唯一性
        if cls.objects(name__iexact=name).count() > 0:
            raise UserNameRegisteredError

    def verify_password(self, password):
        """
        验证密码是否正确
        :param password: 用户密码
        :return: True or False
        """
        return check_password_hash(self.password_hash, password)

    @property
    def password_characteristic(self):
        """
        获取密码特征码
        取用户密码hash后六位,倒序后位进行md5,取md5中16位,作为密码特征码

        :return:
        """
        # 取密码hash后六位倒序
        password_characteristic = self.password_hash[-1:-7:-1]
        # md5后截取16位作为特征码
        password_characteristic = md5(password_characteristic)[2:18]
        return password_characteristic

    def verify_password_characteristic(self, characteristic):
        """
        检查密码特征码是否相符

        :param characteristic:
        :return:
        """
        if self.password_characteristic == characteristic:
            return True
        else:
            return False

    def generate_token(self, expires_in=2592000):
        # itsdangerous 2.1 removed the old JWS serializer. New tokens use its
        # supported timed format; verification below still accepts the legacy
        # JWS format so active sessions survive the dependency upgrade.
        serializer = URLSafeTimedSerializer(current_app.config["SECRET_KEY"])
        return serializer.dumps(
            {"id": str(self.id), "pc": self.password_characteristic}
        )

    @classmethod
    def verify_token(cls, token: str):
        # 检查是否时Bearer token
        if not token.startswith("Bearer "):
            raise BadTokenError(gettext("令牌格式错误，应形如 Bearer x.x.x"))
        # 去掉'Bearer ',获得token内容
        token = token[7:]
        secret_key = current_app.config["SECRET_KEY"]
        serializer = URLSafeTimedSerializer(secret_key)
        try:
            data = serializer.loads(token, max_age=2592000)
        except BadSignature as serializer_error:
            # Tokens issued before this migration used HS512 JWS with TimedJSONWebSignatureSerializer
            try:
                legacy_serializer = TimedJSONWebSignatureSerializer(secret_key)
                data = legacy_serializer.loads(token)
            except Exception:
                raise BadTokenError(
                    gettext("令牌错误，{error}").format(error=serializer_error)
                )
        # 获取用户
        user = User.objects(id=data.get("id")).first()
        # 没有此用户
        if user is None:
            raise BadTokenError(gettext("用户不存在"))
        # 检查密码是否修改
        if not user.verify_password_characteristic(data.get("pc")):
            raise BadTokenError(gettext("密码已修改，请重新登录"))
        return user

    @property
    def avatar(self):
        if self._avatar:
            return oss.sign_url(
                current_app.config["OSS_USER_AVATAR_PREFIX"], self._avatar
            )
        return current_app.config.get("DEFAULT_USER_AVATAR", None)

    @avatar.setter
    def avatar(self, value):
        self._avatar = value

    def has_avatar(self):
        return bool(self._avatar)

    def to_api(self):
        """
        @apiDefine UserInfoModel
        @apiSuccess {String} id ID
        @apiSuccess {String} email email
        @apiSuccess {String} name 昵称
        @apiSuccess {String} avatar 头像地址
        @apiSuccess {String} signature 签名
        @apiSuccess {String} locale 语言
        """
        data = {
            "id": str(self.id),
            "name": self.name,
            "signature": self.signature,
            "avatar": self.avatar,
            "has_avatar": self.has_avatar(),
            "locale": Locale.to_api(id=self.locale),
            "admin": self.admin,
            "aliases": list(self.aliases or []),
        }
        if g.get("current_user") and g.get("current_user").admin:
            data = {**data, **{"email": self.email}}
        return data

    # =====团队操作=====
    def teams(
        self,
        role=None,
        skip: Optional[int] = None,
        limit: Optional[int] = None,
        word: Optional[str] = None,
    ):
        """
        获取自己加入的团队

        :param role: 用户的角色，可以是一个列表
        :param skip: 跳过的数量
        :param limit: 限制的数量
        :param word: 模糊搜索词
        :return:
        """
        from app.models.team_member import TeamMember

        members = TeamMember.objects(user=self, status="active")
        if role:
            role_codes = {
                getattr(item, "system_code", item) for item in (role if isinstance(role, list) else [role])
            }
            # TeamMember intentionally stores only the normalized
            # creator/admin/member identities.  Keep the public legacy role
            # filters usable while translating their old member variants at
            # the query boundary.
            role_codes = {
                "member" if code in {"beginner", "senior"} else code
                for code in role_codes
            }
            members = members.filter(base_tag__in=role_codes)
        teams = Team.objects(id__in=[member.team.id for member in members])
        # 模糊搜索词
        if word:
            teams = teams.filter(name__icontains=word)
        # 处理分页
        teams = mongo_slice(teams, skip, limit)
        return teams

    def get_team_relation(self, team):
        """获取和某个团队的关系"""
        from app.models.team_member import TeamMember

        member = TeamMember.objects(user=self, team=team, status="active").first()
        if member is None:
            return None
        role = TeamRole.by_system_code(member.base_tag)
        return _IdentityRelation(member, role)

    def join_team(self, team, role=None):
        """加入团队"""
        from app.models.team_member import TeamMember
        from app.services.team_member import TeamMemberService

        member = TeamMember.objects(user=self, team=team).first()
        if member is not None and member.status == "active":
            requested = getattr(role, "system_code", None)
            if requested and requested in {"creator", "admin", "member"} and requested != member.base_tag:
                member.base_tag = requested
                member.version += 1
                member.save()
            return _IdentityRelation(member, TeamRole.by_system_code(member.base_tag))
        role_code = getattr(role or team.default_role, "system_code", "member")
        role_code = role_code if role_code in {"creator", "admin", "member"} else "member"
        needs_slot = member is None or member.status != "active"
        if needs_slot:
            TeamMemberService._reserve_capacity(team)
        try:
            if member is None:
                member = TeamMember(
                    team=team, user=self, base_tag=role_code, status="active"
                )
            else:
                member.base_tag = role_code
                member.status = "active"
                member.removed_time = None
                member.version += 1
            member.save()
        except Exception:
            if needs_slot:
                TeamMemberService._sync_count(team)
            raise
        TeamMemberService._sync_count(team)
        return _IdentityRelation(member, TeamRole.by_system_code(member.base_tag))

    # =====项目操作=====
    def projects(
        self,
        role=None,
        skip: int = None,
        limit: int = None,
        project_set=None,
        status=None,
        order_by=None,
        word: str = None,
    ):
        """
        查询自己的项目

        :param role: 用户的角色，可以是一个列表
        :param skip: 跳过的数量
        :param limit: 限制的数量
        :param project_set: 所属项目集
        :param status: 项目进度
        :param word: 模糊搜索词
        """
        from app.models.project_member import ProjectMember

        members = ProjectMember.objects(user=self, status="active")
        if role:
            role_codes = {
                getattr(item, "system_code", item) for item in (role if isinstance(role, list) else [role])
            }
            role_tags = {
                "creator": "creator", "admin": "admin", "coordinator": "proofreader",
                "proofreader": "proofreader", "translator": "translator",
                "picture_editor": "typesetter", "supporter": "translator",
            }
            tags = {role_tags.get(code, code) for code in role_codes}
            members = [member for member in members if tags.intersection(member.tags)]
        project_ids = {member.project.id for member in members}
        projects = Project.objects(id__in=list(project_ids))
        # 限制在某个项目集中
        if project_set:
            projects = projects.filter(project_set=project_set)
        # 查询何种进度的项目
        if isinstance(status, list):
            if len(status) > 0:
                projects = projects.filter(status__in=status)
        elif isinstance(status, int):
            projects = projects.filter(status=status)
        # 模糊搜索词
        if word:
            projects = projects.filter(name_search__icontains=word)
        # 排序处理
        projects = mongo_order(projects, order_by, ["-edit_time"])
        projects = mongo_slice(projects, skip, limit)
        return projects

    def get_project_relation(self, project):
        """获取与某个项目的关系"""
        from app.models.project_member import ProjectMember

        member = ProjectMember.objects(user=self, project=project, status="active").first()
        if member is None:
            return None
        tag_to_role = {
            "creator": "creator", "admin": "admin", "proofreader": "proofreader",
            "translator": "translator", "typesetter": "picture_editor",
        }
        role_code = next((tag_to_role[tag] for tag in member.tags if tag in tag_to_role), "translator")
        return _IdentityRelation(member, ProjectRole.by_system_code(role_code))

    def join_project(self, project, role=None):
        """加入项目"""
        from app.models.project_member import ProjectMember

        member = ProjectMember.objects(user=self, project=project).first()
        role_code = getattr(role or project.default_role, "system_code", "translator")
        # The project owner is represented by the creator identity tag.  Some
        # legacy application/invitation paths pass ``admin`` while repairing
        # a missing projection, so never let that compatibility argument
        # downgrade the owner's identity.
        is_owner = project.owner_user == self
        if is_owner:
            role_code = "creator"
        tag_map = {
            "creator": ["creator"], "admin": ["admin"], "coordinator": ["proofreader"],
            "proofreader": ["proofreader"], "translator": ["translator"],
            "picture_editor": ["typesetter"], "supporter": ["translator"],
        }
        if member is not None and member.status == "active":
            if is_owner and member.tags != tag_map["creator"]:
                member.tags = list(tag_map["creator"])
                member.version += 1
                member.save()
            return _IdentityRelation(member, ProjectRole.by_system_code(role_code))
        if member is None:
            from app.services.project_member import ProjectMemberService

            member = ProjectMember(
                project=project,
                user=self,
                display_name=ProjectMemberService.team_default_display_name(project, self),
                tags=tag_map.get(role_code, []),
                status="active",
            )
        else:
            member.tags = tag_map.get(role_code, [])
            member.status = "active"
            member.removed_time = None
            member.version += 1
        member.save()
        project.update(set__user_count=ProjectMember.objects(project=project, status="active").count())
        return _IdentityRelation(member, ProjectRole.by_system_code(role_code))

    # =====加入流程=====
    def invitations(self, group=None, status=None, skip=None, limit=None):
        """获取对于个人的所有的邀请"""
        invitations = Invitation.get(
            user=self, group=group, status=status, skip=skip, limit=limit
        )
        return invitations

    def applications(self, group=None, status=None, skip=None, limit=None):
        """获取自己发出的所有的申请"""
        applications = Application.get(
            user=self,
            group=group,
            status=status,
            skip=skip,
            limit=limit,
        )
        return applications

    def invite(self, user, group, role, message=""):
        """邀请某个用户加入某个团体"""
        # 判断团体是否已满员
        if group.is_full():
            raise TargetIsFullError
        # 判断自己有没有权限
        user_relation = user.get_relation(group)
        # 自己没有权限
        if not self.can(group, group.permission_cls.INVITE_USER):
            raise NoPermissionError
        # 处理用户没有加入项目，但是是项目所属团队的管理员
        self_role = self.get_role(group)
        # 设置的角色，等级大于当前角色
        if self_role.level <= role.level:
            raise NoPermissionError(gettext("邀请的角色等级需要比您低"))
        # 被邀请人已加入
        if user_relation:
            raise UserAlreadyJoinedError(group.name)
        # 已经有pending中的邀请，拒绝
        old_i: Invitation = user.invitations(
            group=group, status=InvitationStatus.PENDING
        ).first()
        if old_i:
            raise InvitationAlreadyExistError
        # 有pending中的申请，直接加入，并设置成当前选择的角色
        old_a: Application = user.applications(
            group=group, status=ApplicationStatus.PENDING
        ).first()
        if old_a:
            old_a.allow(operator=self, role=role)
            return {"message": gettext("此用户已有申请，已直接加入")}
        # ###对于项目类型团体的特殊流程###
        if isinstance(group, Project):
            # 用户也在这个项目所属团队中，则直接拉入
            if user.get_relation(group.team):
                # 直接加入
                user.join(group, role)
                return {"message": gettext("此用户是项目所在团队成员，已直接加入")}
        # 新建邀请
        invitation = Invitation(
            user=user, operator=self, group=group, role=role, message=message
        ).save()
        return {
            "message": gettext("邀请成功，请等待用户确认"),
            "invitation": invitation.to_api(),
        }

    def apply(self, group, message=""):
        """申请加入某个团体"""
        # 判断团体是否已满员
        if group.is_full():
            raise TargetIsFullError
        # 用户已加入
        relation = self.get_relation(group)
        if relation:
            raise UserAlreadyJoinedError(group.name)
        # 是项目，并且用户在所属团队有自动成为管理员权限，直接加入成管理员
        if isinstance(group, Project) and self.can(
            group, TeamPermission.AUTO_BECOME_PROJECT_ADMIN
        ):
            self.join(group, role=ProjectRole.by_system_code("admin"))
            # 删除旧的申请和邀请
            old_a = self.applications(
                group=group, status=InvitationStatus.PENDING
            ).first()
            if old_a:
                old_a.delete()
            old_i = self.invitations(
                group=group, status=InvitationStatus.PENDING
            ).first()
            if old_i:
                old_i.delete()
            group.reload()
            return {
                "message": gettext("加入成功，管理员继承自团队"),
                "group": group.to_api(user=self),
            }
        # 已有pending中的申请
        old_a = self.applications(group=group, status=InvitationStatus.PENDING).first()
        if old_a:
            raise ApplicationAlreadyExistError
        # 有pending中的邀请
        old_i = self.invitations(group=group, status=InvitationStatus.PENDING).first()
        if old_i:
            old_i.allow()
            group.reload()
            return {
                "message": gettext("管理员先前邀请了您，已直接加入"),
                "group": group.to_api(user=self),
            }
        # 是否允许此用户申请
        if group.is_allow_apply(self):
            application = Application.create(user=self, group=group, message=message)
        else:
            raise NoPermissionError(gettext("项目不允许申请加入") + "(2)")
        # 目标团体无需审核即可加入
        if not group.is_need_check_application():
            application.allow(operator=self)  # 无需审核时，自己就是操作者
            group.reload()
            return {
                "message": gettext("加入成功"),
                "group": group.to_api(user=self),
            }
        return {"message": gettext("申请成功，请等待管理员审核")}

    # =====自动鉴别部分=====
    def get_relation(self, group):
        """返回与目标的关系，返回关系对象或None，以此判断是否加入"""
        if isinstance(group, Team):
            return self.get_team_relation(group)
        elif isinstance(group, Project):
            return self.get_project_relation(group)

    def join(self, group, role=None):
        """加入某个group"""
        if isinstance(group, Team):
            return self.join_team(group, role)
        if isinstance(group, Project):
            return self.join_project(group, role)
        return None

    def leave(self, group):
        """离开某个group"""
        relation = self.get_relation(group)
        if isinstance(group, Project):
            if group.owner_user == self or (
                relation is not None
                and getattr(relation.role, "system_code", None) == "creator"
            ):
                raise MemberAlreadyOwnerError
        elif isinstance(group, Team):
            if relation is not None and getattr(
                relation.role, "system_code", None
            ) == "creator":
                raise CreatorCanNotLeaveError
            from app.models.team_member import TeamMember

            identity_member = TeamMember.objects(
                team=group, user=self, status="active"
            ).first()
            if identity_member is not None and identity_member.base_tag == "creator":
                raise CreatorCanNotLeaveError
        if relation:
            member = relation.member
            member.status = "removed"
            member.removed_time = datetime.datetime.utcnow()
            member.version += 1
            member.save()
            if isinstance(group, Team):
                from app.models.team_member import TeamMember
                group.update(set__user_count=TeamMember.objects(team=group, status="active").count())
            else:
                from app.models.project_member import ProjectMember
                group.update(set__user_count=ProjectMember.objects(project=group, status="active").count())

    def get_role(self, group):
        """获取在group中的角色"""
        relation = self.get_relation(group)
        if relation:
            return relation.role
        else:
            # 如果和项目没有关系，则尝试获取所属团队的转换角色
            if isinstance(group, Project):
                team_relation = self.get_relation(group.team)
                if team_relation:
                    converted_role = team_relation.role.convert_to_project_role()
                    if converted_role:
                        return converted_role

    def set_role(self, group, role):
        """设置在group中的角色"""
        relation = self.get_relation(group)
        if relation:
            role_code = getattr(role, "system_code", role)
            if isinstance(group, Team):
                relation.member.base_tag = role_code if role_code in {"creator", "admin", "member"} else "member"
            else:
                tag_map = {"creator": ["creator"], "admin": ["admin"], "coordinator": ["proofreader"], "proofreader": ["proofreader"], "translator": ["translator"], "picture_editor": ["typesetter"], "supporter": ["translator"]}
                relation.member.tags = tag_map.get(role_code, [])
            relation.member.version += 1
            relation.member.save()

    def is_superior(self, group, user):
        """在group中是否是另一个用户的上级"""
        self_role = self.get_role(group)
        user_role = user.get_role(group)
        if self_role and user_role:
            if self_role.level > user_role.level:
                return True
        return False

    def can(self, group, permission):
        """在group是否拥有某个权限"""
        from app.services.identity_permission import IdentityPermissionService

        if isinstance(group, Project):
            return IdentityPermissionService.can_project(self, group, permission)
        if isinstance(group, Team):
            if isinstance(permission, int):
                team_permissions = {
                    1: "ACCESS", 5: "DELETE", 10: "CHANGE", 1010: "AUTO_BECOME_PROJECT_ADMIN",
                    101: "CHECK_USER", 105: "INVITE_USER", 110: "DELETE_USER",
                    115: "CHANGE_USER_ROLE", 120: "CHANGE_USER_REMARK",
                    1020: "CREATE_TERM_BANK", 1030: "ACCESS_TERM_BANK", 1040: "CHANGE_TERM_BANK",
                    1050: "DELETE_TERM_BANK", 1060: "CREATE_TERM", 1070: "CHANGE_TERM",
                    1080: "DELETE_TERM", 1090: "CREATE_PROJECT", 1100: "CREATE_PROJECT_SET",
                    1110: "CHANGE_PROJECT_SET", 1120: "DELETE_PROJECT_SET", 1130: "USE_OCR_QUOTA",
                    1140: "USE_MT_QUOTA", 1150: "INSIGHT",
                }
                permission = f"team:{team_permissions.get(permission, permission)}"
            return IdentityPermissionService.team_snapshot(self, group).has(permission)
        return False

    def admin_can(self):
        """
        检测用户是否有(某项)管理员权限
        TODO: 增加对 PERMISSION 的检测
        """
        return True if self.admin is True else False


User.register_delete_rule(Invitation, "user", CASCADE)
User.register_delete_rule(Invitation, "operator", CASCADE)
User.register_delete_rule(Application, "user", CASCADE)
User.register_delete_rule(Application, "operator", CASCADE)
User.register_delete_rule(Output, "user", NULLIFY)
User.register_delete_rule(TeamUserRelation, "user", CASCADE)
User.register_delete_rule(ProjectUserRelation, "user", CASCADE)
User.register_delete_rule(Message, "sender", CASCADE)
User.register_delete_rule(Message, "receiver", CASCADE)
