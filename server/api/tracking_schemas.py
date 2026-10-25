"""Strict, bounded contracts for opt-in remote tracking data."""
from collections import defaultdict
from datetime import date
import ipaddress
import re
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class DeviceRequest(StrictModel):
    user_key: UUID
    hostname: str = Field(default="", max_length=255)
    platform: str = Field(default="", max_length=64)
    client_version: str = Field(default="", max_length=32)


class SiteUsage(StrictModel):
    host: str = Field(min_length=1, max_length=300)
    display_name: str = Field(default="", max_length=255)
    seconds: float = Field(ge=0, le=86400)

    @field_validator("host")
    @classmethod
    def host_only(cls, value):
        if any(c.isspace() for c in value) or any(c in value for c in "/\\@?#%"):
            raise ValueError("host only")
        parsed = urlsplit("https://" + value)
        name = (parsed.hostname or "").lower().rstrip(".")
        try:
            ip = ipaddress.ip_address(name)
            name = f"[{ip.compressed}]" if ip.version == 6 else ip.compressed
        except ValueError:
            name = name.encode("idna").decode("ascii")
            if len(name) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", x)
                                          for x in name.split(".")):
                raise ValueError("invalid host")
        port = parsed.port
        return name + (f":{port}" if port else "")


class SiteDay(StrictModel):
    day: date
    sites: list[SiteUsage] = Field(max_length=1000)
    complete: bool = False

    @model_validator(mode="after")
    def validate_total(self):
        if len({s.host for s in self.sites}) != len(self.sites) or sum(s.seconds for s in self.sites) > 86700:
            raise ValueError("duplicate host or invalid total")
        return self


class TimelineBin(StrictModel):
    bucket: int = Field(ge=0, le=95, strict=True)
    app_name: str = Field(min_length=1, max_length=255)
    seconds: float = Field(gt=0, le=900)

    @field_validator("app_name")
    @classmethod
    def app_only(cls, value):
        if any(c in value for c in "/\\\x00\r\n"):
            raise ValueError("application name only")
        return value.lower()


class TimelineDay(StrictModel):
    day: date
    bins: list[TimelineBin] = Field(max_length=1000)
    complete: bool = False

    @model_validator(mode="after")
    def validate_total(self):
        totals = defaultdict(float)
        seen = set()
        for b in self.bins:
            key = (b.bucket, b.app_name)
            if key in seen:
                raise ValueError("duplicate bin")
            seen.add(key)
            totals[b.bucket] += b.seconds
        if any(s > 905 for s in totals.values()) or sum(totals.values()) > 86700:
            raise ValueError("invalid total")
        return self


class WebUpload(DeviceRequest):
    days: list[SiteDay] = Field(min_length=1, max_length=31)


class TimelineUpload(DeviceRequest):
    bucket_minutes: int = Field(default=15, ge=15, le=15)
    days: list[TimelineDay] = Field(min_length=1, max_length=31)


class CompletedAppDay(StrictModel):
    day: date
    seconds: float = Field(ge=0, le=86700)


class CompleteApps(DeviceRequest):
    days: list[CompletedAppDay] = Field(min_length=1, max_length=31)
