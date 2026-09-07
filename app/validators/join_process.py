from marshmallow import ValidationError, fields, post_load, validates_schema

from flask_babel import lazy_gettext

from app.exceptions import RoleNotExistError, UserNotExistError
from app.models.user import User
from app.validators.custom_schema import DefaultSchema
from app.validators.custom_message import required_message
from app.validators.custom_validate import JoinValidate, object_id


class CreateInvitationSchema(DefaultSchema):
    user_id = fields.Str(
        required=True,
        validate=[object_id],
        error_messages={**required_message},
    )
    # Legacy team-role invitation argument.  Project invitations may instead
    # (or additionally) carry the current identity position tags; role_id is
    # then optional and only used as the compatibility role of the legacy
    # invitation record.
    role_id = fields.Str(
        missing=None,
        validate=[object_id],
        error_messages={**required_message},
    )
    message = fields.Str(
        required=True,
        validate=[JoinValidate.message_length],
        error_messages={**required_message},
    )
    tags = fields.List(fields.Str(), missing=None)

    @validates_schema
    def verify_identity(self, data, **kwargs):
        group = self.context["group"]
        if group.group_type == "team":
            if not data.get("role_id"):
                raise ValidationError({"role_id": [lazy_gettext("必填")]})
        elif not data.get("role_id") and not data.get("tags"):
            raise ValidationError({"tags": [lazy_gettext("必填")]})

    @post_load
    def to_model(self, in_data, **kwargs):
        # 获取role和User
        role_id = in_data.pop("role_id", None)
        in_data["role"] = (
            self.context["group"].role_cls.objects(id=role_id).first()
            if role_id is not None
            else None
        )
        in_data["user"] = User.by_id(in_data["user_id"])
        # 如果缺少则抛出错误
        if in_data["role"] is None and role_id is not None:
            raise RoleNotExistError
        if in_data["user"] is None:
            raise UserNotExistError
        return in_data


class SearchInvitationSchema(DefaultSchema):
    status = fields.List(fields.Int(), missing=None)


class SearchRelatedApplicationSchema(DefaultSchema):
    status = fields.List(fields.Int(), missing=None)


class ChangeInvitationSchema(DefaultSchema):
    role_id = fields.Str(missing=None, error_messages={**required_message})
    tags = fields.List(fields.Str(), missing=None)


class CheckInvitationSchema(DefaultSchema):
    allow = fields.Boolean(required=True, error_messages={**required_message})


class SearchApplicationSchema(DefaultSchema):
    status = fields.List(fields.Int(), missing=None)


class CreateApplicationSchema(DefaultSchema):
    message = fields.Str(required=True, validate=[JoinValidate.message_length])


class CheckApplicationSchema(DefaultSchema):
    allow = fields.Boolean(required=True, error_messages={**required_message})
