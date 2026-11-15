"""Usage validation shared by response accounting; never estimate missing tokens."""

from typing import Any


def parse_usage(value: object, missing_reason: str = "upstream_not_provided") -> dict[str, Any]:
    result: dict[str, Any] = {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "usage_status": "missing",
        "usage_source": "none",
        "usage_missing_reason": missing_reason,
        "usage_validation_error": None,
    }
    if value is None:
        return result
    result.update(usage_source="upstream_json", usage_missing_reason=None)
    if not isinstance(value, dict):
        result.update(usage_status="invalid", usage_validation_error="not_an_object")
        return result
    invalid = False
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if name in value:
            token = value[name]
            if type(token) is int and 0 <= token <= 9223372036854775807:
                result[name] = token
            else:
                invalid = True
    if invalid:
        result.update(usage_status="invalid", usage_validation_error="invalid_token_type_or_range")
    elif all(
        result[name] is None for name in ("prompt_tokens", "completion_tokens", "total_tokens")
    ):
        result.update(
            usage_status="missing", usage_source="none", usage_missing_reason=missing_reason
        )
    elif any(
        result[name] is None for name in ("prompt_tokens", "completion_tokens", "total_tokens")
    ):
        result.update(usage_status="partial", usage_validation_error="missing_fields")
    elif result["total_tokens"] != result["prompt_tokens"] + result["completion_tokens"]:
        result.update(usage_status="invalid", usage_validation_error="inconsistent_total")
    else:
        result["usage_status"] = "reported"
    return result
