"""Small Flask API primitives used by MoeFlow.

This module contains the small response, validation, pagination, and query
helpers used by the application. Keeping them local makes the HTTP contract
explicit and allows Flask and Marshmallow to be upgraded independently.
"""

import math
from collections.abc import Callable, Mapping
from functools import wraps
from typing import Any, ClassVar

from flask import current_app, jsonify, make_response, request
from flask.views import MethodView
from marshmallow import Schema
from marshmallow.exceptions import ValidationError


class APIError(Exception):
    """Base exception rendered as the application's JSON error response."""

    status_code = 400
    code = 1
    message = "Undefined Error"
    headers = None

    def __init__(self, message=None, replace=False):
        if message:
            if replace:
                self.message = message
            else:
                self.message = f"{self.message}: {message}"

    def to_tuple(self):
        return (
            jsonify(
                {
                    "error": self.__class__.__name__,
                    "code": self.code,
                    "message": self.message,
                }
            ),
            self.status_code,
            self.headers,
        )


class ValidateError(APIError):
    """Request validation failed."""

    code = 2
    message = "Validate Error"


class QueryParseError(APIError):
    """A query parameter could not be converted to its requested type."""

    code = 3
    message = "Query Parse Error"


class APIResponse:
    """A JSON response with an optional status code and headers."""

    def __init__(self, data, status_code=200, headers=None):
        self.data = data
        self.status_code = status_code
        self.headers = headers

    def to_tuple(self):
        return jsonify(self.data), self.status_code, self.headers


class Pagination(APIResponse):
    """JSON response that carries pagination metadata in response headers."""

    def __init__(
        self,
        default_limit: int | None = None,
        max_limit: int | None = None,
        page_key: str | None = None,
        limit_key: str | None = None,
        status_code: int = 200,
        headers: Mapping[str, Any] | None = None,
        auto_expose_headers: bool = True,
    ):
        self.count = 0
        self._parse_query(
            default_limit=default_limit,
            max_limit=max_limit,
            page_key=page_key,
            limit_key=limit_key,
        )

        response_headers = dict(headers or {})
        if auto_expose_headers:
            existing = response_headers.get("Access-Control-Expose-Headers", "")
            if existing:
                existing += ", "
            exposed = [
                current_app.config["APIKIT_PAGINATION_HEADER_PAGE_KEY"],
                current_app.config["APIKIT_PAGINATION_HEADER_LIMIT_KEY"],
                current_app.config["APIKIT_PAGINATION_HEADER_COUNT_KEY"],
                current_app.config["APIKIT_PAGINATION_HEADER_PAGE_COUNT_KEY"],
            ]
            response_headers["Access-Control-Expose-Headers"] = existing + ", ".join(
                name.upper() for name in exposed
            )

        super().__init__([], status_code, response_headers)

    def set_data(self, data, count):
        self.data = data
        self.count = count
        self._set_pagination_headers()
        return self

    def _parse_query(
        self,
        default_limit: int | None = None,
        max_limit: int | None = None,
        page_key: str | None = None,
        limit_key: str | None = None,
    ):
        if default_limit is None:
            default_limit = current_app.config["APIKIT_PAGINATION_DEFAULT_LIMIT"]
        if max_limit is None:
            max_limit = current_app.config["APIKIT_PAGINATION_MAX_LIMIT"]
        if page_key is None:
            page_key = current_app.config["APIKIT_PAGINATION_PAGE_KEY"]
        if limit_key is None:
            limit_key = current_app.config["APIKIT_PAGINATION_LIMIT_KEY"]

        page = request.args.get(page_key, 1, int)
        page = max(page, 1)
        limit = request.args.get(limit_key, default_limit, int)
        if limit < 1:
            limit = default_limit
        if max_limit and limit > max_limit:
            limit = max_limit

        self.page = page
        self.limit = limit
        self.skip = (page - 1) * limit

    def _set_pagination_headers(self):
        self.headers[current_app.config["APIKIT_PAGINATION_HEADER_PAGE_KEY"]] = (
            self.page
        )
        self.headers[current_app.config["APIKIT_PAGINATION_HEADER_LIMIT_KEY"]] = (
            self.limit
        )
        self.headers[current_app.config["APIKIT_PAGINATION_HEADER_COUNT_KEY"]] = (
            self.count
        )
        self.headers[current_app.config["APIKIT_PAGINATION_HEADER_PAGE_COUNT_KEY"]] = (
            math.ceil(self.count / self.limit)
        )


class QueryParser:
    """Converters for common query-string values."""

    @classmethod
    def int(cls, data: str) -> int:
        try:
            return int(data)
        except (TypeError, ValueError) as exc:
            raise QueryParseError(f'value "{data}" can not parse to int') from exc

    @classmethod
    def float(cls, data: str) -> float:
        try:
            return float(data)
        except (TypeError, ValueError) as exc:
            raise QueryParseError(f'value "{data}" can not parse to float') from exc

    @classmethod
    def bool(cls, data: str) -> bool:
        return data.lower() == "true" or data == "1"


def _deduplicate_messages(value):
    if isinstance(value, dict):
        return {key: _deduplicate_messages(item) for key, item in value.items()}
    if isinstance(value, list):
        result = []
        for item in value:
            item = _deduplicate_messages(item)
            if item not in result:
                result.append(item)
        return result
    return value


def api_response(func: Callable):
    """Convert view return values to Flask responses."""

    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            response = func(*args, **kwargs)
        except APIError as error:
            return error.to_tuple()

        if response is None:
            return "", 204
        if isinstance(response, (dict, list)):
            return jsonify(response)
        if (
            isinstance(response, tuple)
            and len(response) > 1
            and isinstance(response[0], (dict, list))
        ):
            return jsonify(response[0]), *response[1:]
        if isinstance(response, APIResponse):
            return response.to_tuple()
        return response

    return wrapper


def api_cors(func: Callable):
    """Apply the application's CORS response policy."""

    @wraps(func)
    def wrapper(*args, **kwargs):
        origin = request.headers.get("Origin")
        if not origin:
            return make_response(func(*args, **kwargs))

        if request.method == "OPTIONS":
            response = current_app.make_default_options_response()
            headers = response.headers
            headers["Access-Control-Allow-Methods"] = headers.get("allow")
            allowed_headers = current_app.config.get(
                "APIKIT_ACCESS_CONTROL_ALLOW_HEADERS", []
            )
            if allowed_headers:
                headers["Access-Control-Allow-Headers"] = ", ".join(
                    item.upper() for item in allowed_headers
                )
            max_age = current_app.config.get("APIKIT_ACCESS_CONTROL_MAX_AGE")
            if max_age:
                headers["Access-Control-Max-Age"] = max_age
        else:
            response = make_response(func(*args, **kwargs))
            headers = response.headers
            exposed_headers = current_app.config.get(
                "APIKIT_ACCESS_CONTROL_EXPOSE_HEADERS", []
            )
            if exposed_headers:
                existing = headers.get("Access-Control-Expose-Headers", "")
                if existing:
                    existing += ", "
                headers["Access-Control-Expose-Headers"] = existing + ", ".join(
                    item.upper() for item in exposed_headers
                )

        if current_app.config.get("APIKIT_ACCESS_CONTROL_ALLOW_CREDENTIALS") is True:
            headers["Access-Control-Allow-Credentials"] = "true"

        allowed_origin = current_app.config.get(
            "APIKIT_ACCESS_CONTROL_ALLOW_ORIGIN", "*"
        )
        if allowed_origin == "*":
            if (
                current_app.config.get("APIKIT_ACCESS_CONTROL_ALLOW_CREDENTIALS")
                is True
            ):
                headers["Access-Control-Allow-Origin"] = origin
            else:
                headers["Access-Control-Allow-Origin"] = "*"
        elif isinstance(allowed_origin, str):
            if origin.lower() == allowed_origin.lower():
                headers["Access-Control-Allow-Origin"] = origin
        elif isinstance(allowed_origin, list) and origin.lower() in [
            item.lower() for item in allowed_origin
        ]:
            headers["Access-Control-Allow-Origin"] = origin

        return response

    return wrapper


class APIView(MethodView):
    """MethodView with the MoeFlow response and CORS policies."""

    decorators: ClassVar[list[Callable]] = [api_response, api_cors]
    provide_automatic_options = False

    def verify_data(
        self, data: dict, schema: Schema, context: dict | None = None
    ) -> dict:
        if context:
            schema.context = context
        try:
            return schema.load(data)
        except ValidationError as error:
            raise ValidateError(
                _deduplicate_messages(error.messages), replace=True
            ) from error

    def get_json(
        self,
        schema: Schema | None = None,
        context: dict | None = None,
        additional_data: dict | None = None,
        *args,
        **kwargs,
    ) -> dict:
        json_data = request.get_json(*args, **kwargs)
        if json_data is None:
            json_data = {}
        if additional_data:
            json_data = {**json_data, **additional_data}
        if isinstance(schema, Schema):
            return self.verify_data(json_data, schema, context)
        return json_data

    def get_query(
        self,
        parsers: dict | None = None,
        schema: Schema | None = None,
        context: dict | None = None,
        additional_data: dict | None = None,
    ) -> dict:
        query_data = request.args.to_dict(flat=False)
        for key in query_data:
            parser = parsers.get(key) if parsers else None
            if isinstance(parser, list):
                if parser:
                    query_data[key] = [parser[0](item) for item in query_data[key]]
            elif parser is not None:
                query_data[key] = parser(query_data[key][0])
            else:
                query_data[key] = query_data[key][0]

        if additional_data:
            query_data = {**query_data, **additional_data}
        if isinstance(schema, Schema):
            return self.verify_data(query_data, schema, context)
        return query_data


def init_api(app):
    """Install defaults used by the local API helpers."""

    app.config.setdefault("APIKIT_PAGINATION_DEFAULT_LIMIT", 10)
    app.config.setdefault("APIKIT_PAGINATION_MAX_LIMIT", 100)
    app.config.setdefault("APIKIT_PAGINATION_PAGE_KEY", "page")
    app.config.setdefault("APIKIT_PAGINATION_LIMIT_KEY", "limit")
    app.config.setdefault("APIKIT_PAGINATION_HEADER_PAGE_KEY", "X-Pagination-Page")
    app.config.setdefault("APIKIT_PAGINATION_HEADER_LIMIT_KEY", "X-Pagination-Limit")
    app.config.setdefault("APIKIT_PAGINATION_HEADER_COUNT_KEY", "X-Pagination-Count")
    app.config.setdefault(
        "APIKIT_PAGINATION_HEADER_PAGE_COUNT_KEY", "X-Pagination-Page-Count"
    )
    app.config.setdefault("APIKIT_ACCESS_CONTROL_MAX_AGE", 600)
    app.config.setdefault("APIKIT_ACCESS_CONTROL_ALLOW_ORIGIN", "*")
    app.config.setdefault(
        "APIKIT_ACCESS_CONTROL_ALLOW_HEADERS", ["Authorization", "Content-Type"]
    )
    app.config.setdefault("APIKIT_ACCESS_CONTROL_ALLOW_CREDENTIALS", False)
    app.config.setdefault("APIKIT_ACCESS_CONTROL_EXPOSE_HEADERS", [])


__all__ = [
    "APIError",
    "APIResponse",
    "APIView",
    "Pagination",
    "QueryParseError",
    "QueryParser",
    "ValidateError",
    "api_cors",
    "api_response",
    "init_api",
]
