from marshmallow import fields

from app.models.file import File
from app.validators.custom_validate import indexes_in, object_id
from app.validators.custom_schema import DefaultSchema


class FileSearchSchema(DefaultSchema):
    word = fields.Str(load_default=None)
    parent_id = fields.Str(load_default=None, validate=[object_id])
    only_folder = fields.Bool(load_default=False)
    only_file = fields.Bool(load_default=False)
    order_by = fields.List(fields.Str(), load_default=None, validate=[indexes_in(File)])
    target = fields.Str(load_default=None, validate=[object_id])


class FileGetSchema(DefaultSchema):
    target = fields.Str(load_default=None, validate=[object_id])


class FileUploadSchema(DefaultSchema):
    parent_id = fields.Str(load_default=None, validate=[object_id])
