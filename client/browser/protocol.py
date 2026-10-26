"""Untrusted extension messages. All times are epoch seconds internally."""
import json
import math
import time
from uuid import UUID

from .sanitizer import normalize_host

BROWSERS = {"chrome": "chrome.exe", "edge": "msedge.exe",
            "brave": "brave.exe", "vivaldi": "vivaldi.exe"}


def parse_message(raw):
    if len(raw.encode("utf-8")) > 16384:
        raise ValueError("message_too_large")
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("type") not in {
            "hello", "heartbeat", "segment", "diagnostic"}:
        raise ValueError("invalid_message")
    return data


def segment(data, now=None):
    now = time.time() if now is None else now
    for key in ("event_id", "session_id", "profile_instance_id"):
        if str(UUID(str(data.get(key, "")))) != data[key]:
            raise ValueError("invalid_id")
    if data.get("protocol") != 1 or data.get("browser") not in BROWSERS:
        raise ValueError("invalid_protocol")
    start, end = float(data["started_at_ms"]) / 1000, float(data["ended_at_ms"]) / 1000
    if (not math.isfinite(start) or not math.isfinite(end) or not 0 <= end - start <= 120
            or start < now - 600 or end > now + 2):
        raise ValueError("invalid_time")
    scheme = data.get("scheme", "https")
    host = normalize_host(data.get("host"), scheme)
    return {"event_id": data["event_id"], "session_id": data["session_id"],
            "browser": data["browser"], "host": host, "start": start, "end": end}
