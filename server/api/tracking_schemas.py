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


class SiteHour(StrictModel):
    hour: int = Field(ge=0, le=23, strict=True)
    host: str = Field(min_length=1, max_length=300)
    seconds: float = Field(gt=0, le=3600)

    @field_validator("host")
    @classmethod
    def host_only(cls, value):
        return SiteUsage.host_only(value)


class SiteInterval(StrictModel):
    host: str = Field(min_length=1, max_length=300)
    start_second: float = Field(ge=0, lt=86400)
    end_second: float = Field(gt=0, le=86400)

    @field_validator("host")
    @classmethod
    def host_only(cls, value):
        return SiteUsage.host_only(value)

    @model_validator(mode="after")
    def valid_range(self):
        if self.end_second <= self.start_second:
            raise ValueError("interval end must follow start")
        return self


class SiteDay(StrictModel):
    day: date
    sites: list[SiteUsage] = Field(max_length=1000)
    hourly: list[SiteHour] | None = Field(default=None, max_length=1000)
    intervals: list[SiteInterval] | None = Field(default=None, max_length=3000)
    complete: bool = False

    @model_validator(mode="after")
    def validate_total(self):
        if len({s.host for s in self.sites}) != len(self.sites) or sum(s.seconds for s in self.sites) > 86700:
            raise ValueError("duplicate host or invalid total")
        if self.hourly is not None:
            by_host = defaultdict(float)
            seen = set()
            for hour in self.hourly:
                key = (hour.hour, hour.host)
                if key in seen:
                    raise ValueError("duplicate host/hour")
                seen.add(key)
                by_host[hour.host] += hour.seconds
            expected = {s.host: s.seconds for s in self.sites if s.seconds > 0}
            if set(by_host) != set(expected) or any(
                    abs(by_host[host] - seconds) > max(2, seconds * .001)
                    for host, seconds in expected.items()):
                raise ValueError("hourly website usage must match daily totals")
        if self.intervals is not None:
            by_host = defaultdict(float)
            for interval in self.intervals:
                by_host[interval.host] += interval.end_second - interval.start_second
            expected = {s.host: s.seconds for s in self.sites if s.seconds > 0}
            if set(by_host) != set(expected) or any(
                    abs(by_host[host] - seconds) > max(2, seconds * .001)
                    for host, seconds in expected.items()):
                raise ValueError("website intervals must match daily totals")
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
