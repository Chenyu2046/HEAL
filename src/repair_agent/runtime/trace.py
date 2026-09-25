"""Audit trace redaction without retaining model chain-of-thought."""

from __future__ import annotations

from typing import Any

from ..domain import redact_text, to_primitive


def sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {"authorization", "api_key", "apikey", "password", "token", "secret"}:
                result[str(key)] = "<redacted>"
            else:
                result[str(key)] = sanitize(item)
        return result
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    return to_primitive(value)
