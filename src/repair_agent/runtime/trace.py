"""Audit trace redaction without retaining model chain-of-thought."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ..domain import redact_text, to_primitive


def sanitize(value: Any) -> Any:
    return _sanitize_primitive(to_primitive(value))


def _sanitize_primitive(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key_text)
            lowered = re.sub(r"([A-Z])([A-Z][a-z])", r"\1_\2", lowered).lower().replace("-", "_")
            sensitive_parts = {"authorization", "password", "secret", "apikey", "token", "credential", "credentials"}
            parts = set(lowered.split("_"))
            if parts & sensitive_parts or lowered.endswith("_key") and ("api" in parts or "private" in parts):
                result[str(key)] = "<redacted>"
            else:
                result[str(key)] = _sanitize_primitive(item)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_sanitize_primitive(item) for item in value]
    return value
