"""Privacy-preserving HTTP(S) origin normalization (no network access)."""
import ipaddress
import re
from urllib.parse import urlsplit


def normalize_host(value, scheme="https"):
    if scheme not in ("http", "https") or not isinstance(value, str):
        raise ValueError("invalid_scheme")
    if (not value or len(value) > 300 or any(c.isspace() for c in value)
            or any(c in value for c in "/\\@?#%")):
        raise ValueError("invalid_host")
    parsed = urlsplit(f"{scheme}://{value}")
    name = (parsed.hostname or "").rstrip(".").lower()
    try:
        ip = ipaddress.ip_address(name)
        name = f"[{ip.compressed}]" if ip.version == 6 else ip.compressed
    except ValueError:
        name = name.encode("idna").decode("ascii")
        if len(name) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", x)
                                      for x in name.split(".")):
            raise ValueError("invalid_host")
    port = parsed.port
    if port is not None and port != (443 if scheme == "https" else 80):
        name += f":{port}"
    return name
