"""存储池接口的入参校验。"""

from marshmallow import fields, validate

from app.validators.custom_schema import DefaultSchema

#: 子路径只允许「目录样式」的字符：不能有空白、反斜杠、``..``。
_SUB_PATH_PATTERN = r"^[A-Za-z0-9._\-/]*$"


class SourceCreateSchema(DefaultSchema):
    name = fields.Str(required=True, validate=validate.Length(min=1, max=64))
    endpoint = fields.Str(required=True, validate=validate.Length(min=1, max=255))
    bucket = fields.Str(required=True, validate=validate.Length(min=1, max=128))
    sub_path = fields.Str(
        load_default="storage/",
        validate=validate.Regexp(_SUB_PATH_PATTERN, error="子路径含非法字符"),
    )
    region = fields.Str(load_default="auto")
    addressing_style = fields.Str(
        load_default="path", validate=validate.OneOf(["path", "virtual"])
    )
    verify_ssl = fields.Boolean(load_default=True)
    access_key_id = fields.Str(required=True, validate=validate.Length(min=1))
    secret_access_key = fields.Str(required=True, validate=validate.Length(min=1))
    public_domain = fields.Str(load_default="")
    remark = fields.Str(load_default="", validate=validate.Length(max=255))
    inactive_days = fields.Int(
        load_default=90, validate=validate.Range(min=1, max=3650)
    )
    max_objects_per_run = fields.Int(
        load_default=2000, validate=validate.Range(min=1, max=100000)
    )
    enabled = fields.Boolean(load_default=True)


class SourceUpdateSchema(DefaultSchema):
    """更新。凭据留空表示「不修改」。"""

    name = fields.Str(validate=validate.Length(min=1, max=64))
    endpoint = fields.Str(validate=validate.Length(min=1, max=255))
    bucket = fields.Str(validate=validate.Length(min=1, max=128))
    sub_path = fields.Str(
        validate=validate.Regexp(_SUB_PATH_PATTERN, error="子路径含非法字符")
    )
    region = fields.Str()
    addressing_style = fields.Str(validate=validate.OneOf(["path", "virtual"]))
    verify_ssl = fields.Boolean()
    access_key_id = fields.Str()
    secret_access_key = fields.Str()
    public_domain = fields.Str()
    remark = fields.Str(validate=validate.Length(max=255))
    inactive_days = fields.Int(validate=validate.Range(min=1, max=3650))
    max_objects_per_run = fields.Int(validate=validate.Range(min=1, max=100000))
    enabled = fields.Boolean()


class MigrateSchema(DefaultSchema):
    source_id = fields.Str(required=True)


class StoragePoolSettingSchema(DefaultSchema):
    """开关与配额的局部更新。字段都可选，只改传了的那个。

    刻意不做全量覆盖：前端只提交被改的那一项，其它不该被顺手改掉。
    """

    scan_enabled = fields.Boolean()
    run_enabled = fields.Boolean()
    # 单轮字节预算。下限 1 MB 防止手抽填 0 把所有项目都挡在门外。
    run_max_bytes = fields.Int(validate=validate.Range(min=1024 * 1024))
    run_max_objects = fields.Int(validate=validate.Range(min=1, max=100000))
    run_max_projects = fields.Int(validate=validate.Range(min=1, max=1000))
