from app.exceptions.project import LabelplusParseFailedError
import datetime
import logging
from typing import List, Optional, Union, BinaryIO, TYPE_CHECKING

from bson import ObjectId
from io import BufferedReader
from flask import current_app
from flask_babel import gettext, lazy_gettext
from app.tasks.import_from_labelplus import import_from_labelplus
from mongoengine import (
    CASCADE,
    DENY,
    PULL,
    BooleanField,
    DateTimeField,
    Document,
    IntField,
    ListField,
    LongField,
    ReferenceField,
    StringField,
)
from app.utils.labelplus import load_from_labelplus
from app.core.rbac import (
    AllowApplyType,
    GroupMixin,
    PermissionMixin,
    RelationMixin,
    RoleMixin,
)
from app.exceptions import (
    FilenameDuplicateError,
    FilenameIllegalError,
    FolderNotExistError,
    LanguageNotExistError,
    NoPermissionError,
    ProjectNotExistError,
    ProjectSetNotExistError,
    TargetNotExistError,
    TargetAndSourceLanguageSameError,
)
from app.models.application import Application
from app.models.file import File, Filename
from app.models.invitation import Invitation
from app.models.language import Language
from app.models.target import Target
from app.models.term import TermBank

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.team import Team
from app.models.output import Output
from app.tasks.file_parse import find_terms
from app.constants.file import (
    FileNotExistReason,
    FileType,
    FindTermsStatus,
)
from app.constants.project import (
    ImportFromLabelplusErrorType,
    ImportFromLabelplusStatus,
    ProjectStatus,
    STAFF_LIST_DEFAULT_PAGE,
)
from app.utils.mongo import mongo_order, mongo_slice
from app.utils.search import normalize_search_text

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class ProjectAllowApplyType(AllowApplyType):
    """
    允许谁申请加入
    """

    TEAM_USER = 3

    # 合并复写details
    details = {
        **AllowApplyType.details,
        **{"TEAM_USER": {"name": lazy_gettext("仅团队成员")}},
    }


class ProjectPermission(PermissionMixin):
    FINISH = 1010
    ADD_FILE = 1020
    MOVE_FILE = 1030
    RENAME_FILE = 1040
    DELETE_FILE = 1050
    OUTPUT_TRA = 1060
    # 1070 空余，原为导出源图
    ADD_LABEL = 1080
    MOVE_LABEL = 1090
    DELETE_LABEL = 1100
    ADD_TRA = 1110
    DELETE_TRA = 1120
    PROOFREAD_TRA = 1130
    CHECK_TRA = 1140
    ADD_TARGET = 1150
    CHANGE_TARGET = 1160
    DELETE_TARGET = 1170

    details = {
        # RBAC默认权限介绍
        **PermissionMixin.details,
        # RBAC默认权限介绍（覆盖）
        "ACCESS": {"name": lazy_gettext("访问项目")},
        "DELETE": {"name": lazy_gettext("删除项目")},
        "CHANGE": {"name": lazy_gettext("设置项目")},
        # 项目级权限介绍
        "FINISH": {"name": lazy_gettext("完结项目")},
        "ADD_FILE": {"name": lazy_gettext("上传图片")},
        "MOVE_FILE": {"name": lazy_gettext("移动文件")},
        "RENAME_FILE": {"name": lazy_gettext("修改图片名称")},
        "DELETE_FILE": {"name": lazy_gettext("删除图片")},
        "OUTPUT_TRA": {"name": lazy_gettext("导出翻译")},
        "ADD_LABEL": {"name": lazy_gettext("新建图片标记")},
        "MOVE_LABEL": {"name": lazy_gettext("移动图片标记")},
        "DELETE_LABEL": {"name": lazy_gettext("删除图片标记")},
        "ADD_TRA": {"name": lazy_gettext("新增翻译")},
        "DELETE_TRA": {"name": lazy_gettext("删除他人翻译")},
        "PROOFREAD_TRA": {"name": lazy_gettext("校对翻译")},
        "CHECK_TRA": {
            "name": lazy_gettext("选定翻译"),
            "intro": lazy_gettext("将某条翻译指定导出项"),
        },
        "ADD_TARGET": {"name": lazy_gettext("新增目标语言")},
        "CHANGE_TARGET": {"name": lazy_gettext("修改目标语言")},
        "DELETE_TARGET": {"name": lazy_gettext("删除目标语言")},
    }


class ProjectRole(RoleMixin, Document):
    permission_cls = ProjectPermission
    group = ReferenceField("Project", db_field="g")
    system_role_data: List[dict] = [
        {
            "name": "创建人",
            "permissions": [
                ProjectPermission.ACCESS,
                ProjectPermission.CHANGE,
                ProjectPermission.FINISH,
                ProjectPermission.DELETE,
                ProjectPermission.ADD_FILE,
                ProjectPermission.MOVE_FILE,
                ProjectPermission.RENAME_FILE,
                ProjectPermission.DELETE_FILE,
                ProjectPermission.OUTPUT_TRA,
                ProjectPermission.ADD_LABEL,
                ProjectPermission.MOVE_LABEL,
                ProjectPermission.DELETE_LABEL,
                ProjectPermission.ADD_TRA,
                ProjectPermission.DELETE_TRA,
                ProjectPermission.PROOFREAD_TRA,
                ProjectPermission.CHECK_TRA,
                ProjectPermission.CHECK_USER,
                ProjectPermission.INVITE_USER,
                ProjectPermission.CHANGE_USER_REMARK,
                ProjectPermission.CHANGE_USER_ROLE,
                ProjectPermission.DELETE_USER,
                ProjectPermission.ADD_TARGET,
                ProjectPermission.CHANGE_TARGET,
                ProjectPermission.DELETE_TARGET,
            ],
            "level": 500,
            "system_code": "creator",
        },
        {
            "name": "管理员",
            "permissions": [
                ProjectPermission.ACCESS,
                ProjectPermission.CHANGE,
                ProjectPermission.FINISH,
                ProjectPermission.DELETE,
                ProjectPermission.ADD_FILE,
                ProjectPermission.MOVE_FILE,
                ProjectPermission.RENAME_FILE,
                ProjectPermission.DELETE_FILE,
                ProjectPermission.OUTPUT_TRA,
                ProjectPermission.ADD_LABEL,
                ProjectPermission.MOVE_LABEL,
                ProjectPermission.DELETE_LABEL,
                ProjectPermission.ADD_TRA,
                ProjectPermission.DELETE_TRA,
                ProjectPermission.PROOFREAD_TRA,
                ProjectPermission.CHECK_TRA,
                ProjectPermission.INVITE_USER,
                ProjectPermission.CHECK_USER,
                ProjectPermission.CHANGE_USER_REMARK,
                ProjectPermission.CHANGE_USER_ROLE,
                ProjectPermission.DELETE_USER,
                ProjectPermission.ADD_TARGET,
                ProjectPermission.CHANGE_TARGET,
                ProjectPermission.DELETE_TARGET,
            ],
            "level": 400,
            "system_code": "admin",
        },
        {
            "name": "监理",
            "permissions": [
                ProjectPermission.ACCESS,
                ProjectPermission.ADD_FILE,
                ProjectPermission.MOVE_FILE,
                ProjectPermission.RENAME_FILE,
                ProjectPermission.DELETE_FILE,
                ProjectPermission.OUTPUT_TRA,
                ProjectPermission.ADD_LABEL,
                ProjectPermission.MOVE_LABEL,
                ProjectPermission.DELETE_LABEL,
                ProjectPermission.ADD_TRA,
                ProjectPermission.DELETE_TRA,
                ProjectPermission.PROOFREAD_TRA,
                ProjectPermission.CHECK_TRA,
                ProjectPermission.INVITE_USER,
                ProjectPermission.CHECK_USER,
                ProjectPermission.DELETE_USER,
                ProjectPermission.CHANGE_USER_REMARK,
                ProjectPermission.CHANGE_USER_ROLE,
            ],
            "level": 300,
            "system_code": "coordinator",
        },
        {
            "name": "校对",
            "permissions": [
                ProjectPermission.ACCESS,
                ProjectPermission.OUTPUT_TRA,
                ProjectPermission.ADD_LABEL,
                ProjectPermission.MOVE_LABEL,
                ProjectPermission.DELETE_LABEL,
                ProjectPermission.ADD_TRA,
                ProjectPermission.PROOFREAD_TRA,
                ProjectPermission.CHECK_TRA,
            ],
            "level": 200,
            "system_code": "proofreader",
        },
        {
            "name": "翻译",
            "permissions": [
                ProjectPermission.ACCESS,
                ProjectPermission.OUTPUT_TRA,
                ProjectPermission.ADD_LABEL,
                ProjectPermission.MOVE_LABEL,
                ProjectPermission.DELETE_LABEL,
                ProjectPermission.ADD_TRA,
            ],
            "level": 200,
            "system_code": "translator",
        },
        {
            "name": "嵌字",
            "permissions": [
                ProjectPermission.ACCESS,
                ProjectPermission.OUTPUT_TRA,
                ProjectPermission.ADD_LABEL,
                ProjectPermission.MOVE_LABEL,
                ProjectPermission.DELETE_LABEL,
            ],
            "level": 200,
            "system_code": "picture_editor",
        },
        {
            "name": "见习翻译",
            "permissions": [ProjectPermission.ACCESS, ProjectPermission.ADD_TRA],
            "level": 100,
            "system_code": "supporter",
        },
    ]

    @classmethod
    def unused_provide_babel_names(cls):
        gettext("创建人")
        gettext("管理员")
        gettext("监理")
        gettext("校对")
        gettext("翻译")
        gettext("嵌字")
        gettext("见习翻译")


class ProjectSet(Document):
    """项目集"""

    name = StringField(db_field="n", required=True)  # 集合名
    intro = StringField(db_field="i", default="")  # 项目集介绍
    team: "Team" = ReferenceField("Team", db_field="t", required=True)
    create_time = DateTimeField(db_field="ct", default=datetime.datetime.utcnow)
    edit_time = DateTimeField(
        db_field="et", required=True, default=datetime.datetime.utcnow
    )
    default = BooleanField(
        db_field="d", required=True, default=False
    )  # 是否是默认项目集

    @classmethod
    def create(cls, name, team, default=False):
        return cls(name=name, team=team, default=default).save()

    def clear(self):
        # TODO: 将项目集内项目移动到默认项目集
        self.delete()

    @classmethod
    def by_id(cls, id: ObjectId):
        project_set = cls.objects(id=id).first()
        if project_set is None:
            raise ProjectSetNotExistError()
        return project_set

    def to_api(self):
        """
        @apiDefine TeamProjectSetInfoModel
        @apiSuccess {String} id ID
        @apiSuccess {String} name 名称
        @apiSuccess {String} intro 介绍
        @apiSuccess {Number} type 项目集类型（暂时没有用）
        @apiSuccess {String} create_time 创建时间
        @apiSuccess {String} edit_time 修改时间

        """
        return {
            "id": str(self.id),
            "name": self.name,
            "intro": self.intro,
            "default": self.default,
            "create_time": self.create_time.isoformat(),
            "edit_time": self.edit_time.isoformat(),
        }

    def to_list_api(self):
        """Serialize the fields used by project-set navigation lists only."""
        return {
            "id": str(self.id),
            "name": self.name,
            "default": self.default,
        }


class Project(GroupMixin, Document):
    name = StringField(db_field="n", required=True)  # 项目名
    # NFC + casefold projection used by project-name search.
    name_search = StringField(default="", db_field="ns")
    intro = StringField(db_field="i", default="")  # 项目介绍
    team = ReferenceField("Team", db_field="t", required=True)  # 所属团队
    project_set = ReferenceField("ProjectSet", db_field="ps", required=True)  # 所属集合
    default_role = ReferenceField(
        "ProjectRole", db_field="dr", reverse_delete_rule=DENY
    )
    max_user = IntField(db_field="u", required=True, default=100000)  # 最大用户数
    source_name = StringField(db_field="sn")  # 作品原名
    target_name = StringField(db_field="tn")  # 作品译名
    source_language = ReferenceField(  # 源语言
        Language, db_field="ol", required=True, reverse_delete_rule=DENY
    )
    tags = ListField(StringField(), db_field="ta", default=list)  # 项目标签
    status = IntField(db_field="st", default=ProjectStatus.WORKING)  # 项目状态
    status_version = IntField(db_field="stv", required=True, default=0)
    owner_user = ReferenceField("User", db_field="ou")
    owner_version = IntField(db_field="ouv", required=True, default=0)
    completed_time = DateTimeField(db_field="ctm")
    clear_in_progress = BooleanField(db_field="cip", default=False)
    # == 导出设置 ==
    # 导出 translations.txt 时人员名单的植入页序号：
    # 正数从前往后（1 = 第一页），负数从后往前（-1 = 最后一页）；
    # 未设置（None）时跟随团队设置，团队也未设置时使用默认第一页。
    staff_list_page = IntField(db_field="slp", null=True)
    # == 术语库 ==
    _term_banks = ListField(
        ReferenceField(TermBank, reverse_delete_rule=PULL),
        db_field="tb",
        default=list,
    )
    need_find_terms = BooleanField(db_field="nft", default=False)

    # == 解析原文 ==
    ocring = BooleanField(db_field="oc", default=False)  # 是否正在进行项目级解析

    # == 缓存 ==
    target_count = IntField(db_field="tc", required=True, default=0)  # 目标语言数量缓存
    folder_count = IntField(db_field="fo", required=True, default=0)  # 文件数量
    file_count = IntField(db_field="fc", required=True, default=0)  # 文件数量
    file_size = LongField(db_field="fs", required=True, default=0)  # 文件大小，单位KB
    source_count = IntField(db_field="sc", required=True, default=0)  # 原文数量
    # targets汇总的缓存
    translated_source_count = IntField(db_field="tsc", default=0)  # 已翻译的原文数量
    checked_source_count = IntField(db_field="csc", default=0)  # 已校对的原文数量

    # == 从 LP 导入 ==
    import_from_labelplus_status = IntField(
        db_field="is", default=ImportFromLabelplusStatus.SUCCEEDED
    )
    import_from_labelplus_percent = IntField(db_field="ip", default=0)
    import_from_labelplus_error_type = IntField(
        db_field="ie", default=ImportFromLabelplusErrorType.UNKNOWN
    )
    import_from_labelplus_txt = StringField(db_field="it", default="")

    # == GroupMixin ==
    default_role_system_code = "translator"
    role_cls = ProjectRole
    permission_cls = ProjectPermission
    allow_apply_type_cls = ProjectAllowApplyType

    def clean(self):
        # Project overrides GroupMixin.clean(), so explicitly preserve the
        # enum validation for application_check_type/allow_apply_type.
        super().clean()
        self.name_search = normalize_search_text(self.name)

    def update(self, **kwargs):
        # Project settings are updated with atomic queryset writes; keep the
        # search projection synchronized without reloading and saving the doc.
        for name_key in ("name", "set__name"):
            if name_key in kwargs:
                kwargs[name_key.replace("name", "name_search")] = normalize_search_text(
                    kwargs[name_key]
                )
        return super().update(**kwargs)

    @classmethod
    def create(
        cls,
        name: str,
        team: "Team",
        project_set: ProjectSet = None,
        default_role=None,
        allow_apply_type=None,
        application_check_type=None,
        creator=None,
        source_language=None,
        target_languages=None,
        intro="",
        labelplus_txt=None,
    ) -> "Project":
        """创建一个项目"""
        # 尝试解析 labelplus 文本
        if labelplus_txt:
            try:
                load_from_labelplus(labelplus_txt)
            except Exception:
                raise LabelplusParseFailedError
        # 语言默认值
        if source_language is None:
            source_language = Language.by_code("ja")
        if target_languages is None:
            target_languages = [Language.by_code("zh-CN")]
        elif isinstance(target_languages, list) and len(target_languages) == 0:
            target_languages = [Language.by_code("zh-CN")]
        # 将目标语言转换为列表
        if isinstance(target_languages, Language):
            target_languages = [target_languages]
        else:
            # 目标语言去重
            target_languages = list(set(target_languages))
        # 检查目标语言是否都存在
        for target_language in target_languages:
            if not isinstance(target_language, Language):
                raise LanguageNotExistError(gettext("必须是Language对象"))
        # 检查目标语言和源语言是否重复
        if source_language in target_languages:
            raise TargetAndSourceLanguageSameError
        # 检查项目集
        if project_set is None:
            project_set = team.default_project_set
        # 项目集的团队和项目的团队不一致
        if project_set.team != team:
            raise ProjectSetNotExistError
        # 创建项目
        project = cls(
            name=name,
            team=team,
            project_set=project_set,
            source_language=source_language,
            owner_user=creator,
        )
        # 设置默认角色
        if default_role:
            project.default_role = default_role
        else:
            project.default_role = cls.default_system_role()
        # 设置其他选项
        if allow_apply_type:
            project.allow_apply_type = allow_apply_type
        if application_check_type:
            project.application_check_type = application_check_type
        project.intro = intro
        # 保存团队
        project.save()
        # 创建项目目标语言对象
        targets = []
        for target_language in target_languages:
            target = Target.create(project=project, language=target_language)
            targets.append(target)
        # 添加创建人
        if creator:
            creator.join(project, role=cls.role_cls.by_system_code("creator"))
        # 通过 Labelplus 数据创建文件及翻译
        if labelplus_txt and creator and len(targets) == 1:
            project.update(
                import_from_labelplus_txt=labelplus_txt,
                import_from_labelplus_status=ImportFromLabelplusStatus.PENDING,
                import_from_labelplus_percent=0,
            )
            import_from_labelplus(str(project.id))
        project.reload()
        return project

    def targets(self, language=None):
        """获取所有目标语言"""
        targets = Target.objects(project=self)
        if language:
            targets = targets.filter(language=language)
        return targets

    def target_by_id(self, id) -> Target:
        """通过id获取目标语言"""
        target = Target.objects(id=id, project=self).first()
        if target is None:
            raise TargetNotExistError
        return target

    @property
    def relation_cls(self):
        return ProjectUserRelation

    @property
    def term_banks(self):
        """项目所用的术语库"""
        return self._term_banks

    @term_banks.setter
    def term_banks(self, value: list):
        """设置项目所用的术语库"""
        self._term_banks = value
        self.need_find_terms = True

    def find_terms(self):
        """异步刷新所有文件的可能术语"""
        # 如果有设置术语库，进行寻找术语任务
        if len(self._term_banks) > 0:
            for file in self.files(type_exclude=FileType.FOLDER):
                # celery任务
                run_sync = current_app.config.get(
                    "TESTING", False
                )  # 如果测试则同步执行
                result = find_terms(str(file.id), run_sync=run_sync)
                # 设置文件状态
                file.update(
                    find_terms_task_id=result.task_id,
                    find_terms_status=FindTermsStatus.QUEUING,
                )
        # 关闭提示
        self.need_find_terms = False
        self.save()

    def inc_cache(self, cache_name, step, target=None):
        # 0则不请求数据库
        if step == 0:
            return
        # 翻译缓存
        if cache_name in ["translated_source_count", "checked_source_count"]:
            if target is None:
                raise ValueError("更新翻译缓存数据必须指定target")
            target.update(**{"inc__" + cache_name: step})
            self.update(**{"inc__" + cache_name: step})
        # 其他缓存
        else:
            # 没有此属性跳过
            if not hasattr(self, cache_name):
                return
            # 更新项目缓存
            self.update(**{"inc__" + cache_name: step})

    def update_cache(self, cache_name, value):
        """更新某个缓存字段"""
        # 没有此属性跳过
        if not hasattr(self, cache_name):
            return
        # 更新自身缓存
        self.update(**{cache_name: value})
        # edit_time 需要同步修改 ProjectSet 和 Team 的
        if cache_name == "edit_time":
            self.project_set.update(**{cache_name: value})
            self.team.update(**{cache_name: value})

    def is_allow_apply(self, user) -> bool:
        """是否允许此用户申请加入"""
        # 只允许团队成员申请加入
        if self.allow_apply_type == ProjectAllowApplyType.TEAM_USER:
            # 是团队成员
            if user.get_relation(self.team):
                return True
            else:
                raise NoPermissionError(gettext("只允许项目所属团队成员申请加入"))
        return super().is_allow_apply(user)

    def move_to_project_set(self, project_set):
        """
        将项目添加到本项目集

        :param project_set: 如果为None，则移出项目集
        :return:
        """
        if project_set.team != self.team:
            raise ProjectSetNotExistError
        self.project_set = project_set
        self.save()

    def create_folder(self, name, parent=None):
        """创建文件夹"""
        # 确认父级文件夹是否存在于本项目
        if parent is not None:
            parent = self.get_folder(parent)
        # 检查是否有同名文件
        folder_name = Filename(name, folder=True)
        old_folder = self.get_files(name=folder_name.name, parent=parent).first()
        # 有同名文件/文件夹，报错
        if old_folder:
            raise FilenameDuplicateError
        # 新建文件夹
        folder = File(
            name=folder_name.name,
            project=self,
            parent=parent,
            type=folder_name.file_type,
            sort_name=folder_name.sort_name,
        ).save()
        # 创建FileTargetCache
        for target in self.targets():
            folder.create_target_cache(target)
        folder.inc_cache("folder_count", 1, update_self=False)
        return folder

    def create_file(self, name: str, parent: File = None) -> File:
        """
        创建文件

        [对于存在同名文件的策略]
        图片：返回同名文件对象
        文本：返回一个新建的未激活修订版
        其他类型：报错

        :param name: 文件名
        :param parent: 所属文件夹，顶层则为None
        :return:
        """
        logging.debug(
            f"Project(id={self.id}).create_file(name={name}, parent={parent})"
        )
        # 确认父级是文件夹且存在于本项目
        if parent is not None:
            parent = self.get_folder(parent)
        filename = Filename(name)
        # 不支持的后缀
        supported_types = (
            FileType.TEST_SUPPORTED
            if current_app.config.get("TESTING", False)
            else FileType.SUPPORTED
        )
        if filename.file_type not in supported_types:
            raise FilenameIllegalError(gettext("暂不支持的文件格式"))
        # 检查是否有同名文件
        file: File = self.get_files(name=filename.name, parent=parent).first()
        # 有同名文件
        if file:
            # 图片，什么都不做
            if file.type == FileType.IMAGE:
                pass
            # 文本，创建新修订版
            elif file.type == FileType.TEXT:
                file = file.create_revision()  # 创建新修订版
                file.activate_revision()  # 激活此修订版
            # 文本夹，与文件夹重名报错
            elif file.type == FileType.FOLDER:
                raise FilenameDuplicateError
            else:
                raise FilenameDuplicateError
        # 没有同名文件
        else:
            # 新建文件对象
            file = File(
                name=filename.name,
                project=self,
                parent=parent,
                type=filename.file_type,
                sort_name=filename.sort_name,
                file_not_exist_reason=FileNotExistReason.NOT_UPLOAD,
            ).save()
            # 创建FileTargetCache
            for target in self.targets():
                file.create_target_cache(target)
            # 更新文件个数
            file.inc_cache("file_count", 1, update_self=False)
        return file

    def upload(
        self, filename: str, real_file: Union[BufferedReader, BinaryIO], parent=None
    ) -> File:
        """
        上传文件

        :param filename: 文件名
        :param real_file: 文件实体
        :param parent: 所属文件夹，顶层则为None
        :return:
        """
        # 创建文件对象
        file = self.create_file(filename, parent)
        file.upload_real_file(real_file)
        return file

    @classmethod
    def by_id(cls, id_: str) -> "Project":
        project = cls.objects(id=id_).first()
        if project is None:
            raise ProjectNotExistError()
        return project

    def get_files(
        self, name, parent: Union[str, ObjectId, File] = "all", activated=True
    ):
        """通过文件名获取文件或文件夹(大小写不敏感)，默认仅获取激活的修订版"""
        file = File.objects(name__iexact=name, project=self)
        # 限制文件夹
        if parent != "all":
            if parent is not None:
                parent = self.get_folder(parent)
            file = file.filter(parent=parent)
        # 限制修订版，将activated设为'all'以搜索所有修订版
        if isinstance(activated, bool):
            file = file.filter(activated=activated)
        return file

    def get_folder(self, folder: Union[str, ObjectId, File]) -> File:
        """
        尝试获取本项目下的文件夹，检查文件夹是否存在于本项目
        如没有则raise FolderNotExistError

        :param folder: 支持 字符串、ObjectId或File对象
        :return:
        """
        # 是字符串、ObjectId，则尝试获取File对象
        if isinstance(folder, (str, ObjectId)):
            folder = File.objects(id=folder, project=self, type=FileType.FOLDER).first()
            # 文件夹不存在报错
            if folder is None:
                raise FolderNotExistError
        # 是File对象，则检查是否属于本项目
        elif isinstance(folder, File):
            # 不属于本项目或不是文件夹，报错
            if folder.project != self or folder.type != FileType.FOLDER:
                raise FolderNotExistError
        # 其他类型直接报错
        else:
            raise ValueError(
                "folder参数 需要是 字符串、ObjectId或File对象，所给值为"
                + f"[{type(folder)}]{folder}"
            )
        return folder

    def files(
        self,
        skip=None,
        limit=None,
        order_by: list = None,
        parent="all",
        type_only=None,
        type_exclude=None,
        word: str = None,
        file_ids_include: List[str] = None,
        file_ids_exclude: List[str] = None,
    ) -> List[File]:
        """
        获取所有文件

        :param skip: 跳过的数量
        :param limit: 限制的数量
        :param order_by: 排序
        :param parent: 父文件夹，默认'all'查询所有files
        :param type_only: 只显示某些类型
        :param type_exclude: 排除某些类型
        :return:
        """
        files = File.objects(project=self, activated=True)
        if word and word.strip():
            files = files.filter(name__icontains=word.strip())
        # 父文件夹
        if parent != "all":
            # 确认父级文件夹是否存在于本项目
            if parent is not None:
                parent = self.get_folder(parent)
            files = files.filter(parent=parent)
        # 只显示文件夹
        if type_only is not None:
            if isinstance(type_only, list):
                files = files.filter(type__in=type_only)
            else:
                files = files.filter(type=type_only)
        # 只显示文件
        if type_exclude is not None:
            if isinstance(type_exclude, list):
                files = files.filter(type__nin=type_exclude)
            else:
                files = files.filter(type__ne=type_exclude)
        # 限制 ids
        if file_ids_include:
            files = files.filter(id__in=file_ids_include)
        if file_ids_exclude:
            files = files.filter(id__nin=file_ids_exclude)
        # 排序处理
        files = mongo_order(files, order_by, ["dir_sort_name", "type", "sort_name"])
        # 分页处理
        files = mongo_slice(files, skip, limit)
        return files

    def outputs(self):
        """所有导出"""
        outputs = Output.objects(project=self).order_by("-create_time")
        return outputs

    @staticmethod
    def batch_to_api(
        projects: list["Project"],
        user: "User",
        /,
        *,
        inherit_admin_team=None,
        with_team=True,
        with_project_set=True,
        include_member_summary=False,
    ):
        """
        批量转换 projects 到 api 格式

        :param inherit_admin_team 从某个项目继承权限，此时需要所有 projects 都在一个 team 内
        """
        from app.models.team import TeamPermission
        from app.models.team_member import TeamMember
        from app.models.project_member import ProjectMember
        from app.models.identity_tag import IdentityTagPolicy
        from app.services.identity_permission import IdentityPermissionService

        projects = list(projects)
        snapshots = {}
        project_roles_data = {}
        auto_become_project_ids = set()

        if user and projects:
            members = ProjectMember.objects(
                user=user,
                project__in=[project.pk for project in projects],
            ).only("id", "project", "tags", "status")
            team_ids = list({project.team.pk for project in projects})
            team_relations = TeamMember.objects(
                user=user,
                team__in=team_ids,
                status="active",
            ).only("id", "team", "base_tag")
            policies = IdentityTagPolicy.objects(team__in=team_ids)
            snapshots = IdentityPermissionService.project_snapshots(
                user,
                projects,
                project_members=members,
                team_members=team_relations,
                policies=policies,
            )

            active_members = {
                str(member.project.id): member
                for member in members
                if member.status == "active"
            }
            tag_to_role = {
                "creator": "creator",
                "admin": "admin",
                "proofreader": "proofreader",
                "translator": "translator",
                "typesetter": "picture_editor",
            }
            role_codes = {
                next(
                    (tag_to_role[tag] for tag in member.tags if tag in tag_to_role),
                    "translator",
                )
                for member in active_members.values()
            }

            inherited_admin_team_ids = set()
            converted_roles = {}
            from app.models.team import TeamRole

            for relation in team_relations:
                base_tag = relation.base_tag
                if base_tag not in converted_roles:
                    converted_roles[base_tag] = TeamRole.by_system_code(
                        base_tag
                    ).convert_to_project_role()
                if converted_roles[base_tag] is not None:
                    inherited_admin_team_ids.add(str(relation.team.id))
            if any(value is not None for value in converted_roles.values()):
                role_codes.add("admin")

            roles_by_code = {
                role.system_code: role
                for role in ProjectRole.objects(system_code__in=list(role_codes))
                if role.system_code is not None
            }
            for project_id, member in active_members.items():
                role_code = next(
                    (tag_to_role[tag] for tag in member.tags if tag in tag_to_role),
                    "translator",
                )
                role = roles_by_code.get(role_code)
                if role is not None:
                    project_roles_data[project_id] = role.to_api()

            admin_role = roles_by_code.get("admin")
            for project in projects:
                project_id = str(project.pk)
                if (
                    project_id not in active_members
                    and str(project.team.pk) in inherited_admin_team_ids
                    and admin_role is not None
                ):
                    project_roles_data[project_id] = admin_role.to_api()
                    auto_become_project_ids.add(project_id)
        # 检查是否自动成为项目管理员权限
        role_from_team_data = None
        if inherit_admin_team and user.can(
            inherit_admin_team, TeamPermission.AUTO_BECOME_PROJECT_ADMIN
        ):
            role_from_team_data = ProjectRole.by_system_code("admin").to_api()
        member_summaries = {}
        if include_member_summary:
            from app.services.project_member import ProjectMemberService

            member_summaries = ProjectMemberService.member_summaries(projects)
        # 构建数据
        data = []
        for project in projects:
            project_data = project.to_api(
                user=user,
                with_team=with_team,
                with_project_set=with_project_set,
                _batch_context={
                    "snapshots": snapshots,
                    "auto_become_project_ids": auto_become_project_ids,
                    "roles": project_roles_data,
                    "teams": {},
                    "sets": {},
                },
            )
            project_data["role"] = None
            project_role_data = project_roles_data.get(str(project.id))
            if project_role_data:
                project_data["role"] = project_role_data
            else:
                if (
                    role_from_team_data
                    and str(project.id) not in auto_become_project_ids
                ):
                    project_data["role"] = role_from_team_data
                    project_data["auto_become_project_admin"] = True
            if include_member_summary:
                project_data["member_summary"] = member_summaries.get(
                    str(project.id), []
                )
            data.append(project_data)
        return data

    def clear(self):
        """物理删除项目"""
        for file in self.files(type_exclude=FileType.FOLDER):
            file.delete_real_file(init_obj=False)
        Output.delete_real_files(self.outputs())
        self.delete()

    def to_labelplus(
        self,
        /,
        *,
        target,
        file_ids_include: List[str] = None,
        file_ids_exclude: List[str] = None,
    ):
        """将图片文件的翻译导出成Labelplus格式"""
        # Labelplus翻译文件头格式
        data = (
            "1,0\r\n"  # 版本
            + "-\r\n"
            + gettext("框内")
            + "\r\n"  # 标签分组
            + gettext("框外")
            + "\r\n"
            + "-\r\n"
            + gettext("可使用 LabelPlus Photoshop 脚本导入 psd 中")
            + "\r\n"  # 注释
        )
        files = self.files(
            type_only=FileType.IMAGE,
            file_ids_include=file_ids_include,
            file_ids_exclude=file_ids_exclude,
        )
        # 遍历所有图片，按设定在指定页植入人员名单
        staff_block = self._staff_list_block()
        if staff_block is not None:
            file_count = len(files)
            staff_index = self._staff_list_target_index(
                file_count, self._staff_list_page_resolved()
            )
        else:
            staff_index = -1
        for index, file in enumerate(files):
            if index == staff_index:
                data += file.to_labelplus(
                    target=target,
                    staff_block=staff_block,
                    staff_block_first=self._staff_list_page_resolved() > 0,
                )
            else:
                data += file.to_labelplus(target=target)
        return data

    def _staff_list_page_resolved(self) -> int:
        """名单植入页解析：项目设置 > 团队设置 > 内置默认（第一页）"""
        if self.staff_list_page is not None:
            return self.staff_list_page
        team = self.team
        if team is not None and team.staff_list_page is not None:
            return team.staff_list_page
        return STAFF_LIST_DEFAULT_PAGE

    @staticmethod
    def _staff_list_target_index(file_count: int, page: int) -> int:
        """将页序号转换为文件索引；超出范围时自动回退到最近的一页。"""
        if file_count <= 0:
            return -1
        if page > 0:
            index = page - 1
            if index >= file_count:
                index = file_count - 1
        else:
            index = file_count + page
            if index < 0:
                index = 0
        return index

    def _staff_list_block(self) -> Optional[str]:
        """渲染人员名单文本块（框外居中）。

        Labelplus 标签行 + 多行文本；只列有活跃成员的 worker 职位。
        没有任何活跃工作人员时返回 None（不植入）。
        """
        from app.models.identity_tag import WORKER_TAG_LABELS, WORKER_TAG_ORDER
        from app.models.project_member import ProjectMember

        members = ProjectMember.objects(project=self, status="active")
        lines = []
        for tag in WORKER_TAG_ORDER:
            names = []
            for member in members:
                if tag in member.tags:
                    # display_name 是用户输入，防止换行破坏 Labelplus 块结构
                    names.append(
                        str(member.display_name).replace("\r", " ").replace("\n", " ")
                    )
            if not names:
                continue
            lines.append(f"{WORKER_TAG_LABELS[tag]}：{'、'.join(names)}")
        if not lines:
            return None
        # x=0.5, y=0.5 页面正中心；position_type=2 框外
        return (
            "----------------[0]----------------[0.5,0.5,2]\r\n"
            + "\r\n".join(lines)
            + "\r\n"
        )

    def to_output_json(self):
        data = {
            "name": self.name,
            "intro": self.intro,
            "default_role": self.default_role.system_code,
            "allow_apply_type": self.allow_apply_type,
            "application_check_type": self.application_check_type,
            "is_need_check_application": self.is_need_check_application(),
            "create_time": self.create_time.isoformat(),
            "edit_time": self.edit_time.isoformat(),
            "source_language": self.source_language.code,
            "target_languages": [target.language.code for target in self.targets()],
            "staff_list_page": self.staff_list_page,
        }
        return data

    def to_api(
        self,
        /,
        *,
        user=None,
        with_team=True,
        with_project_set=True,
        _batch_context=None,
    ):
        """
        @apiDefine ProjectPublicInfoModel
        @apiSuccess {String} group_type 团体类型
        @apiSuccess {String} id ID
        @apiSuccess {String} name 名称
        @apiSuccess {String} intro 介绍
        @apiSuccess {Number} max_user 最大用户数
        @apiSuccess {Number} status 项目状态
            WORKING = 0  # 进行中
            FINISHED = 1  # 已完结
            PLAN_FINISH = 2  # 处于完结计划
            PLAN_DELETE = 3  # 处于销毁计划
        @apiSuccess {String} default_role 默认角色 ID
        @apiSuccess {String} allow_apply_type 允许申请的类型
        @apiSuccess {String} application_check_type 如何处理申请
        @apiSuccess {String} is_need_check_application 是否需要确认申请
        @apiSuccess {String} role 用户在团体中的角色
        @apiSuccess {Boolean} auto_become_project_admin admin权限是否是继承自团队
        @apiSuccess {String} create_time 创建时间
        @apiSuccess {String} edit_time 修改时间
        """
        # 如果给予 user 则获取用户相关信息（角色等）
        auto_become_project_admin = False
        role = None
        effective_permissions = []
        permission_sources = {}
        snapshot = None
        if user:
            project_id = str(self.id)
            if _batch_context is not None:
                role = _batch_context.get("roles", {}).get(project_id)
                auto_become_project_admin = project_id in _batch_context.get(
                    "auto_become_project_ids", ()
                )
                snapshot = _batch_context.get("snapshots", {}).get(project_id)
            else:
                role_object = user.get_role(self)
                if role_object:
                    role = role_object.to_api()
                    relation = user.get_relation(self)
                    # 有 role 但是没有关系，则说明是继承自团队
                    if relation is None:
                        auto_become_project_admin = True
                try:
                    from app.services.identity_permission import (
                        IdentityPermissionService,
                    )

                    snapshot = IdentityPermissionService.project_snapshot(user, self)
                except (ImportError, AttributeError):
                    # Keep public project serialization usable during migration startup.
                    pass
            if snapshot is not None:
                effective_permissions = sorted(snapshot.effective_permissions)
                permission_sources = {
                    key: list(value)
                    for key, value in snapshot.permission_sources.items()
                }
        data = {
            "group_type": "project",
            "id": str(self.id),
            "name": self.name,
            "intro": self.intro,
            "max_user": self.max_user,
            "status": self.status,
            "identity_status": {
                ProjectStatus.WORKING: "NORMAL",
                ProjectStatus.CLEARED: "CLEARED",
                ProjectStatus.COMPLETED: "COMPLETED",
            }.get(self.status),
            "status_name": {
                ProjectStatus.WORKING: "NORMAL",
                ProjectStatus.CLEARED: "CLEARED",
                ProjectStatus.COMPLETED: "COMPLETED",
            }.get(self.status),
            "status_version": self.status_version,
            "owner_user_id": str(self.owner_user.id) if self.owner_user else None,
            "owner_version": self.owner_version,
            "user_count": self.user_count,
            "default_role": str(self.default_role.id),
            "allow_apply_type": self.allow_apply_type,
            "application_check_type": self.application_check_type,
            "is_need_check_application": self.is_need_check_application(),
            "role": role,
            "auto_become_project_admin": auto_become_project_admin,
            "effective_permissions": effective_permissions,
            "permission_sources": permission_sources,
            "staff_list_page": self.staff_list_page,
            "create_time": self.create_time.isoformat(),
            "edit_time": self.edit_time.isoformat(),
            "source_language": self.source_language.to_api(),
            "target_count": self.target_count,
            "source_count": self.source_count,
            "translated_source_count": self.translated_source_count,
            "checked_source_count": self.checked_source_count,
            "import_from_labelplus_status": self.import_from_labelplus_status,
            "import_from_labelplus_percent": self.import_from_labelplus_percent,
            "import_from_labelplus_error_type": self.import_from_labelplus_error_type,
            "import_from_labelplus_error_type_name": ImportFromLabelplusErrorType.get_detail_by_value(
                self.import_from_labelplus_error_type, "name"
            ),
        }
        # Worker identities are exposed through the project members endpoint.
        if with_team:
            team_cache = (
                _batch_context.setdefault("teams", {})
                if _batch_context is not None
                else {}
            )
            if not isinstance(team_cache, dict):
                team_cache = {}
            if str(self.team.pk) in team_cache:
                data["team"] = team_cache[str(self.team.pk)]
            else:
                team_data = self.team.to_api(user=user)
                if _batch_context is not None:
                    team_cache[str(self.team.pk)] = team_data
                data["team"] = team_data
        if with_project_set:
            set_cache = (
                _batch_context.setdefault("sets", {})
                if _batch_context is not None
                else {}
            )
            if not isinstance(set_cache, dict):
                set_cache = {}
            if str(self.project_set.pk) in set_cache:
                data["project_set"] = set_cache[str(self.project_set.pk)]
            else:
                project_set_data = self.project_set.to_api()
                if _batch_context is not None:
                    set_cache[str(self.project_set.pk)] = project_set_data
                data["project_set"] = project_set_data
        return data

    @staticmethod
    def batch_to_list_api(
        projects: list["Project"],
        user: "User",
        /,
        *,
        team=None,
    ):
        """Build the compact project-card response in a bounded number of queries.

        The regular ``to_api`` response is intentionally detail-oriented: it
        contains settings, legacy roles, permission sources and import metadata.
        Calling it for every project card makes a list request pay the detail
        page's cost.  This path only prepares the three permissions used by the
        card and reuses one permission context for the whole page.
        """
        from app.models.identity_tag import IdentityTagPolicy
        from app.models.project_member import ProjectMember
        from app.models.team_member import TeamMember
        from app.services.identity_permission import IdentityPermissionService

        projects = list(projects)
        if not projects:
            return []

        snapshots = {}
        if user:
            members = ProjectMember.objects(
                user=user,
                project__in=[project.pk for project in projects],
            ).only("id", "project", "tags", "status")
            team_ids = list({project.team.pk for project in projects})
            team_relations = TeamMember.objects(
                user=user,
                team__in=team_ids,
                status="active",
            ).only("id", "team", "base_tag", "tags")
            policies = IdentityTagPolicy.objects(team__in=team_ids)
            snapshots = IdentityPermissionService.project_snapshots(
                user,
                projects,
                project_members=members,
                team_members=team_relations,
                policies=policies,
            )

        team_data = team.to_project_list_api() if team is not None else None
        set_cache = {}
        data = []
        for project in projects:
            project_data = project.to_list_api(
                user=user,
                snapshot=snapshots.get(str(project.pk)),
                team_data=team_data,
                set_cache=set_cache,
            )
            data.append(project_data)
        return data

    def to_list_api(
        self,
        /,
        *,
        user=None,
        snapshot=None,
        team_data=None,
        set_cache=None,
    ):
        """Serialize only data required by the project list card."""
        list_permissions = {
            "project:ACCESS",
            "project:MANAGE_MEMBERS",
            "project:COMPLETE_PROJECT",
        }
        effective_permissions = (
            sorted(set(snapshot.effective_permissions).intersection(list_permissions))
            if snapshot is not None
            else []
        )
        if team_data is None:
            team_data = self.team.to_project_list_api()
        if set_cache is None:
            set_cache = {}
        project_set_id = str(self.project_set.pk)
        if project_set_id not in set_cache:
            set_cache[project_set_id] = self.project_set.to_list_api()
        return {
            "group_type": "project",
            "id": str(self.id),
            "name": self.name,
            "status": self.status,
            "effective_permissions": effective_permissions,
            "source_count": self.source_count,
            "target_count": self.target_count,
            "translated_source_count": self.translated_source_count,
            "checked_source_count": self.checked_source_count,
            "team": team_data,
            "project_set": set_cache[project_set_id],
        }


Project.register_delete_rule(ProjectRole, "group", CASCADE)
Project.register_delete_rule(Application, "group", CASCADE)
Project.register_delete_rule(Invitation, "group", CASCADE)
Project.register_delete_rule(File, "project", CASCADE)
Project.register_delete_rule(Target, "project", CASCADE)
Project.register_delete_rule(Output, "project", CASCADE)

ProjectSet.register_delete_rule(Project, "project_set", DENY)


class ProjectUserRelation(RelationMixin, Document):
    user = ReferenceField("User", db_field="u", required=True)
    group = ReferenceField(
        "Project", db_field="g", required=True, reverse_delete_rule=CASCADE
    )
    role = ReferenceField("ProjectRole", db_field="r", required=True)
