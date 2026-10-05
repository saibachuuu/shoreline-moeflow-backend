from marshmallow import fields

from app.validators.custom_schema import DefaultSchema
from app.validators.custom_validate import object_id


class PartnerSearchSettingsSchema(DefaultSchema):
    partner_search_enabled = fields.Boolean()
    partner_search_team_ids = fields.List(
        fields.Str(validate=[object_id]),
    )
    partner_search_rate_limit_seconds = fields.Int(
        validate=lambda v: v is not None and v >= 0
    )
    partner_search_max_limit = fields.Int(validate=lambda v: v is not None and v >= 1)
