from marshmallow import fields, post_load, validates_schema

from app.exceptions import ProjectSetNotExistError, LanguageNotExistError
from app.models.project import Project
from app.models.language import Language
from app.constants.role import RoleType
from app.constants.output import OutputTypes
from app.validators.custom_message import required_message
from app.validators.custom_validate import (
    ProjectSetValidate,
    ProjectValidate,
    need_in,
    not_zero,
    object_id,
)
from app.validators.custom_schema import DefaultSchema
from marshmallow.exceptions import ValidationError


class ProjectSetsSchema(DefaultSchema):
    name = fields.Str(
        required=True,
        validate=[ProjectSetValidate.name_length],
        error_messages={**required_message},
    )


class CreateProjectSchema(DefaultSchema):
    """创建项目验证器"""

    name = fields.Str(
        required=True,
        validate=[ProjectValidate.name_length],
        error_messages={**required_message},
    )
    intro = fields.Str(
        required=True,
        validate=[ProjectValidate.intro_length],
        error_messages={**required_message},
    )
    allow_apply_type = fields.Int(
        required=True,
        validate=[need_in(Project.allow_apply_type_cls.ids())],
        error_messages={**required_message},
    )
    application_check_type = fields.Int(
        required=True,
        validate=[need_in(Project.application_check_type_cls.ids())],
        error_messages={**required_message},
    )
    default_role = fields.Str(
        required=True,
        validate=[object_id],
        error_messages={**required_message},
    )
    project_set = fields.Str(
        required=True,
        validate=[object_id],
        error_messages={**required_message},
    )
    source_language = fields.Str(
        required=True,
        validate=[need_in(Language.codes)],
        error_messages={**required_message},
    )
    target_languages = fields.List(
        fields.Str(
            required=True,
            validate=[need_in(Language.codes)],
            error_messages={**required_message},
        ),
        required=True,
    )
    labelplus_txt = fields.Str(load_default=None)

    @validates_schema
    def verify_default_role(self, data, **kwargs):
        # 角色必须在系统团队的角色中
        need_in(
            [
                str(role.id)
                for role in Project.role_cls.system_roles(without_creator=True)
            ]
        )(data["default_role"], field_name="default_role")

    @post_load
    def to_model(self, in_data, **kwargs):
        """通过id获取模型，以供直接使用"""
        # 获取默认角色
        in_data["default_role"] = Project.role_cls.by_id(in_data["default_role"])
        # 必须是项目所在团队的项目集
        project_set = (
            self.context["team"]
            .project_sets()
            .filter(id=in_data["project_set"])
            .first()
        )
        if project_set is None:
            raise ProjectSetNotExistError
        in_data["project_set"] = project_set
        # 获取源语言
        try:
            in_data["source_language"] = Language.by_code(in_data["source_language"])
        except LanguageNotExistError as e:
            raise ValidationError(e.message, field_name="source_language")
        # 获取目标语言
        try:
            in_data["target_languages"] = Language.by_codes(in_data["target_languages"])
        except LanguageNotExistError as e:
            raise ValidationError(e.message, field_name="target_languages")
        return in_data


class ImportProjectSchema(DefaultSchema):
    """创建项目验证器"""

    name = fields.Str(
        required=True,
        validate=[ProjectValidate.name_length],
        error_messages={**required_message},
    )
    intro = fields.Str(
        required=True,
        validate=[ProjectValidate.intro_length],
        error_messages={**required_message},
    )
    allow_apply_type = fields.Int(
        required=True,
        validate=[need_in(Project.allow_apply_type_cls.ids())],
        error_messages={**required_message},
    )
    application_check_type = fields.Int(
        required=True,
        validate=[need_in(Project.application_check_type_cls.ids())],
        error_messages={**required_message},
    )
    default_role = fields.Str(
        required=True,
        validate=[object_id],
        error_messages={**required_message},
    )
    project_set = fields.Str(
        required=True,
        validate=[object_id],
        error_messages={**required_message},
    )
    source_language = fields.Str(
        required=True,
        validate=[need_in(Language.codes)],
        error_messages={**required_message},
    )
    output_language = fields.Str(
        required=True,
        validate=[need_in(Language.codes)],
        error_messages={**required_message},
    )
    labelplus_txt = fields.Str(load_default=None)

    @validates_schema
    def verify_default_role(self, data, **kwargs):
        # 角色必须在系统团队的角色中
        need_in(
            [
                str(role.id)
                for role in Project.role_cls.system_roles(without_creator=True)
            ]
        )(data["default_role"], field_name="default_role")

    @post_load
    def to_model(self, in_data, **kwargs):
        """通过id获取模型，以供直接使用"""
        # 获取默认角色
        in_data["default_role"] = Project.role_cls.by_id(in_data["default_role"])
        # 必须是项目所在团队的项目集
        project_set = (
            self.context["team"]
            .project_sets()
            .filter(id=in_data["project_set"])
            .first()
        )
        if project_set is None:
            raise ProjectSetNotExistError
        in_data["project_set"] = project_set
        # 获取源语言
        try:
            in_data["source_language"] = Language.by_code(in_data["source_language"])
        except LanguageNotExistError as e:
            raise ValidationError(e.message, field_name="source_language")
        # 获取目标语言
        try:
            in_data["output_language"] = Language.by_code(in_data["output_language"])
        except LanguageNotExistError as e:
            raise ValidationError(e.message, field_name="output_language")
        return in_data


class EditProjectSchema(DefaultSchema):
    """修改项目验证器"""

    name = fields.Str(
        validate=[ProjectValidate.name_length],
        error_messages={**required_message},
    )
    intro = fields.Str(
        validate=[ProjectValidate.intro_length],
        error_messages={**required_message},
    )
    allow_apply_type = fields.Int(
        validate=[need_in(Project.allow_apply_type_cls.ids())]
    )
    application_check_type = fields.Int(
        validate=[need_in(Project.application_check_type_cls.ids())]
    )
    default_role = fields.Str(validate=[object_id])
    project_set = fields.Str(validate=[object_id])
    # 人员名单植入页序号：正数从前往后（1=第一页），负数从后往前（-1=最后一页），
    # 非零整数；null 表示未设置（跟随团队/默认）；不校验是否超出实际页数。
    staff_list_page = fields.Int(allow_none=True, validate=[not_zero])

    @validates_schema
    def verify_default_role(self, data, **kwargs):
        # 角色必须在团队的角色中
        if "default_role" in data:
            need_in(
                [
                    str(role.id)
                    for role in self.context["project"].roles(
                        type=RoleType.ALL, without_creator=True
                    )
                ]
            )(data["default_role"], field_name="default_role")

    @post_load
    def to_model(self, in_data, **kwargs):
        """通过id获取模型，以供直接使用"""
        # 获取默认角色
        if "default_role" in in_data:
            in_data["default_role"] = Project.role_cls.by_id(in_data["default_role"])
        if "project_set" in in_data:
            # 必须是项目所在团队的项目集
            project_set = (
                self.context["project"]
                .team.project_sets()
                .filter(id=in_data["project_set"])
                .first()
            )
            if project_set is None:
                raise ProjectSetNotExistError
            in_data["project_set"] = project_set
        return in_data


class ChangeProjectUserSchema(DefaultSchema):
    """修改项目用户验证器"""

    role = fields.Str(
        required=True,
        validate=[object_id],
        error_messages={**required_message},
    )


class CreateProjectTargetSchema(DefaultSchema):
    """创建项目目标验证器"""

    language = fields.Str(
        required=True,
        validate=[need_in(Language.codes)],
        error_messages={**required_message},
    )

    @post_load
    def to_model(self, in_data, **kwargs):
        """通过id获取模型，以供直接使用"""
        in_data["language"] = Language.by_code(in_data["language"])
        return in_data


class CreateOutputSchema(DefaultSchema):
    """创建导出内容验证器"""

    type = fields.Int(
        required=True,
        validate=[need_in(OutputTypes.ids())],
        error_messages={**required_message},
    )
    file_ids_include = fields.List(fields.Str(validate=[object_id]), load_default=None)
    file_ids_exclude = fields.List(fields.Str(validate=[object_id]), load_default=None)


class TeamInsightUserListSchema(DefaultSchema):
    word = fields.Str(load_default=None)


class TeamInsightProjectListSchema(DefaultSchema):
    word = fields.Str(load_default=None)
