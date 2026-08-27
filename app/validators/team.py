from marshmallow import fields, post_load, validates_schema

from app.models.team import Team
from app.constants.role import RoleType
from app.validators.custom_message import required_message
from app.validators.custom_schema import DefaultSchema
from app.validators.custom_validate import TeamValidate, need_in, not_zero, object_id


class CreateTeamSchema(DefaultSchema):
    """创建团队验证器"""

    name = fields.Str(
        required=True,
        validate=[TeamValidate.valid_new_name],
        error_messages={**required_message},
    )
    intro = fields.Str(
        required=True,
        validate=[TeamValidate.intro_length],
        error_messages={**required_message},
    )
    allow_apply_type = fields.Int(
        required=True, validate=[need_in(Team.allow_apply_type_cls.ids())]
    )
    application_check_type = fields.Int(
        required=True,
        validate=[need_in(Team.application_check_type_cls.ids())],
    )
    default_role = fields.Str(required=True, validate=[object_id])

    @validates_schema
    def verify_default_role(self, data, **kwargs):
        # 角色必须在系统团队的角色中
        need_in(
            [str(role.id) for role in Team.role_cls.system_roles(without_creator=True)]
        )(data["default_role"], field_name="default_role")

    @post_load
    def to_model(self, in_data, **kwargs):
        """通过id获取模型，以供直接使用"""
        # 获取默认角色
        in_data["default_role"] = Team.role_cls.by_id(in_data["default_role"])
        return in_data


class EditTeamSchema(DefaultSchema):
    """修改团队验证器"""

    name = fields.Str(error_messages={**required_message})
    intro = fields.Str(
        validate=[TeamValidate.intro_length],
        error_messages={**required_message},
    )
    allow_apply_type = fields.Int(validate=[need_in(Team.allow_apply_type_cls.ids())])
    application_check_type = fields.Int(
        validate=[need_in(Team.application_check_type_cls.ids())]
    )
    default_role = fields.Str(validate=[object_id])
    # 工作人员资格校验模式：qualified 校验、open 不校验（全员可加入任意职位）。
    # 仅团队创建者可以修改（在 API 层强制）。
    worker_qualification_mode = fields.Str(validate=[need_in(["qualified", "open"])])
    # 团队内项目默认的名单植入页序号（同项目设置，非零整数；null 表示未设置）。
    staff_list_page = fields.Int(allow_none=True, validate=[not_zero])
    # 画廊归档导入的第三方档案 API key 列表（每项 {id?, key?, remark?, enabled?}）。
    # 含 id 的项按 id 更新/保留；不含 id 且含 key 的项为新增；未引用的旧 id 被删除。
    archive_api_keys = fields.List(fields.Dict())
    # 画廊归档导入的第三方档案 API 基址（可空字符串 = 使用系统默认）。
    archive_api_url = fields.Str(allow_none=True, validate=[lambda v: len(v or "") <= 512])

    @validates_schema
    def verify_name(self, data, **kwargs):
        # 如果新名字和旧名字不同,检查是否合法
        if "name" in data and data["name"] != self.context["team"].name:
            TeamValidate.valid_new_name(data["name"], field_name="name")

    @validates_schema
    def verify_default_role(self, data, **kwargs):
        # 角色必须在团队的角色中
        if "default_role" in data:
            need_in(
                [
                    str(role.id)
                    for role in self.context["team"].roles(
                        type=RoleType.ALL, without_creator=True
                    )
                ]
            )(data["default_role"], field_name="default_role")

    @post_load
    def to_model(self, in_data, **kwargs):
        """通过id获取模型，以供直接使用"""
        # 获取默认角色
        if "default_role" in in_data:
            in_data["default_role"] = Team.role_cls.by_id(in_data["default_role"])
        return in_data
