"""Native attempt correlation and bounded, body-free send diagnostics."""
import json
import math
import re
import sys

EVENT_TYPE = "muse_delivery_attempt"
DIAGNOSTIC_PREFIX = "SEND_DIAGNOSTIC "


def attempt_metadata(attempt_id):
    return {"event_type": EVENT_TYPE,
            "event_payload": {"attempt_id": attempt_id}}


def correlated(message, attempt_id):
    if not attempt_id:
        return False
    metadata = message.get("metadata")
    payload = metadata.get("event_payload") if isinstance(metadata, dict) else None
    return (message.get("client_msg_id") == attempt_id or
            (isinstance(metadata, dict) and metadata.get("event_type") == EVENT_TYPE
             and isinstance(payload, dict) and payload.get("attempt_id") == attempt_id))


def safe_diagnostic(data):
    """Only protocol scalars; never copy exception text, response bodies or URLs."""
    result = {}
    for key in ("elapsed_ms", "post_elapsed_ms", "exit_code", "http_status", "stdout_bytes", "stderr_bytes"):
        value = data.get(key)
        if type(value) in (int, float) and math.isfinite(value):
            result[key] = value
    for key in ("metadata_echo", "client_id_echo", "timeout"):
        if type(data.get(key)) is bool:
            result[key] = data[key]
    patterns = {"result": r"(?:ok|fail|uncertain|retry_wait|not_sent)",
                "exception_type": r"[A-Za-z][A-Za-z0-9_]{0,79}",
                "slack_error": r"[a-z][a-z0-9_]{0,63}",
                "request_id": r"[a-fA-F0-9-]{1,80}",
                "ts": r"[0-9]+\.[0-9]+|[0-9]+"}
    for key, pattern in patterns.items():
        value = data.get(key)
        if isinstance(value, str) and re.fullmatch(pattern, value):
            result[key] = value
    warnings = data.get("warnings")
    if isinstance(warnings, list):
        result["warnings"] = [v for v in warnings[:10] if isinstance(v, str)
                              and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", v)]
    return result


def emit_diagnostic(data):
    safe = safe_diagnostic(data)
    print(DIAGNOSTIC_PREFIX + json.dumps(safe, sort_keys=True), file=sys.stderr)
    return safe
