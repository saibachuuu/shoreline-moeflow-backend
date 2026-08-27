from marshmallow import EXCLUDE, Schema


class DefaultSchema(Schema):
    # marshmallow的默认配置
    class Meta:
        unknown = EXCLUDE
