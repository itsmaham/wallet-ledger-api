"""Request parsing and the input rules from the brief."""

import re
from typing import Any

import orjson

from .errors import invalid

USER_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
MIN_AMOUNT = 1
MAX_AMOUNT = 1_000_000_000
MAX_BODY_BYTES = 16 * 1024
IDEMPOTENCY_KEY_RE = re.compile(r"[\x21-\x7e]{1,128}")  # printable ASCII, no spaces


def parse_json_object(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_BODY_BYTES:
        raise invalid("request body is too large")
    try:
        body = orjson.loads(raw)
    except orjson.JSONDecodeError:
        raise invalid("request body must be valid JSON") from None
    if not isinstance(body, dict):
        raise invalid("request body must be a JSON object")
    return body


def validate_user_id(value: Any) -> str:
    if value is None:
        raise invalid("user_id is required")
    # fullmatch (not match + $) so a trailing newline cannot slip through.
    if not isinstance(value, str) or USER_ID_RE.fullmatch(value) is None:
        raise invalid("user_id must be 1-64 characters of A-Z, a-z, 0-9, _ or -")
    return value


def validate_amount(value: Any) -> int:
    if value is None:
        raise invalid("amount is required")
    # `type(...) is int` rather than isinstance: bool is a subclass of int in
    # Python, and `true` must be rejected. JSON 1.0 / 1e3 decode to float.
    if type(value) is not int or not MIN_AMOUNT <= value <= MAX_AMOUNT:
        raise invalid(f"amount must be an integer between {MIN_AMOUNT} and {MAX_AMOUNT}")
    return value


def validate_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    if IDEMPOTENCY_KEY_RE.fullmatch(value) is None:
        raise invalid("Idempotency-Key must be 1-128 printable ASCII characters")
    return value
