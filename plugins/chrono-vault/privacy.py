"""Write-time minimization for shared/memory-discipline.md's privacy baseline.

Screen whole inputs before lossy transforms, and screen their complete outputs
before bounding. Persistence and external payloads also check the final text;
an earlier screening pass is never evidence that transformed text is safe.
These format checks cover the named baseline, not arbitrary personal information
or secrets without a recognizable format. Never include matched values in logs.
"""

from __future__ import annotations

import json
import re
from typing import Any


_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    # URL credentials must be removed before individual token formats.
    r"(?<=[?&])(?:access_token|auth_token|api_key|apikey|token|bearer|authorization)=[^\s&#<>\"']+",
    r"(?<=://)[^\s/@:]+:[^\s/@]+@",
    r"\bBearer\s+[A-Za-z0-9._~+/%=-]+",
    r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9.-]*[A-Z0-9])?\.[A-Z]{2,}\b",
    r"\bsk-(?:ant-|proj-|svcacct-)?[A-Za-z0-9_-]{20,}",
    r"\b(?:xai-|pplx-)[A-Za-z0-9_-]{20,}",
    r"\bAIza[A-Za-z0-9_-]{35}\b",
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})",
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}",
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
    r"\bhf_[A-Za-z0-9]{20,}",
    r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}",
    r"\bapify_api_[A-Za-z0-9_-]{20,}",
))


def redact_text(text: str) -> str:
    # Match line cleanup's C0 removal before looking for identifiers. Keep
    # whitespace as separators so normalization never joins separate words.
    text = "".join(character for character in text if ord(character) >= 32 or character.isspace())
    for pattern in _PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def redact_fields(value: Any) -> Any:
    """Return a screened copy; preserve schema keys and non-text values."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: redact_fields(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_fields(item) for item in value]
    return value


def require_screened(text: str) -> str:
    """Fail closed if final serialization or formatting introduced a match.

    Do not repair serialized data here: replacing a match spanning structural
    delimiters could corrupt the format. Never put the offending text in errors.
    """
    if redact_text(text) != text:
        raise ValueError("unscreened output at privacy boundary")
    return text


def screened_json(value: Any, **options: Any) -> str:
    """Minimize values, serialize, and check the exact outgoing JSON text."""
    return require_screened(json.dumps(redact_json_fields(value), **options))


def redact_json_fields(value: Any) -> Any:
    """Also minimize matches introduced by JSON's whitespace escape spelling.

    A newline becomes two printable characters in JSON. If that spelling
    creates a match, preserve word separation with spaces and screen again.
    Other unsafe scalar encodings are withheld; schema keys remain unchanged
    and the final serialized-object guard rejects matches across structure.
    """
    if isinstance(value, str):
        cleaned = redact_text(value)
        encoded = json.dumps(cleaned, ensure_ascii=False)
        if redact_text(encoded) != encoded:
            cleaned = redact_text(" ".join(cleaned.split()))
            encoded = json.dumps(cleaned, ensure_ascii=False)
            if redact_text(encoded) != encoded:
                return "[REDACTED]"
        return cleaned
    if isinstance(value, dict):
        return {key: redact_json_fields(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_json_fields(item) for item in value]
    return value
