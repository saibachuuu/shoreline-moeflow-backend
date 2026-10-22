from marshmallow import fields

from app.validators.custom_message import required_message
from app.validators.custom_schema import DefaultSchema
from app.validators.custom_validate import object_id


class CreateTipSchema(DefaultSchema):
    content = fields.Str(required=True, error_messages={**required_message})
    target_id = fields.Str(
        required=True,
        validate=[object_id],
        error_messages={**required_message},
    )


class EditTipSchema(DefaultSchema):
    content = fields.Str(required=True, error_messages={**required_message})
