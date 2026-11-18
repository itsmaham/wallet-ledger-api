"""JSON responses and the API's error type."""

from typing import Any

import orjson
from starlette.responses import Response


def json_response(status: int, payload: Any) -> Response:
    return Response(orjson.dumps(payload), status_code=status, media_type="application/json")


def error_body(code: str, message: str) -> dict[str, str]:
    return {"error": code, "message": message}


def error_response(status: int, code: str, message: str) -> Response:
    return json_response(status, error_body(code, message))


class ApiError(Exception):
    """An expected client-facing error, rendered as {"error", "message"}."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def invalid(message: str) -> ApiError:
    return ApiError(400, "invalid_request", message)
