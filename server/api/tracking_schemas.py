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


class AppInterval(StrictModel):
    app_name: str = Field(min_length=1, max_length=255)
    start_second: float = Field(ge=0, lt=86400)
    end_second: float = Field(gt=0, le=86400)

    @field_validator("app_name")
    @classmethod
    def app_only(cls, value):
        return TimelineBin.app_only(value)

    @model_validator(mode="after")
    def valid_range(self):
        if self.end_second <= self.start_second:
            raise ValueError("interval end must follow start")
        return self


class TimelineDay(StrictModel):
    day: date
    bins: list[TimelineBin] = Field(max_length=1000)
    intervals: list[AppInterval] | None = Field(default=None, max_length=10000)
    # Android daily unlock count (keyguard unlocks for the whole day).
    # Optional so old clients keep working; when present it is stored in
    # android_daily_stats by api/tracking.py. It is NOT launches: the legacy
    # usage_records.launches remains "starts per app per day".
    unlocks: int | None = Field(default=None, ge=0, le=100000, strict=True)
    complete: bool = False

    @model_validator(mode="after")
    def validate_total(self):
        totals = defaultdict(float)
        by_app = defaultdict(float)
        seen = set()
        for b in self.bins:
            key = (b.bucket, b.app_name)
            if key in seen:
                raise ValueError("duplicate bin")
            seen.add(key)
            totals[b.bucket] += b.seconds
            by_app[b.app_name] += b.seconds
        if any(s > 905 for s in totals.values()) or sum(totals.values()) > 86700:
            raise ValueError("invalid total")
        if self.intervals is not None:
            interval_totals = defaultdict(float)
            for interval in self.intervals:
                interval_totals[interval.app_name] += interval.end_second - interval.start_second
            if set(interval_totals) != set(by_app) or any(
                    abs(interval_totals[app] - seconds) > max(2, seconds * .001)
                    for app, seconds in by_app.items()):
                raise ValueError("application intervals must match timeline bins")
        return self


class WebUpload(DeviceRequest):
    days: list[SiteDay] = Field(min_length=1, max_length=31)


# ── 应用名归一：PC 规则与安卓映射对照（供安卓同步工人对照，不作服务端校验）──
# PC 端规则见 client/core/storage.py 的 canonical_app_name：先把名字转小写，
# 再沿用户合并规则链一路追到最终父名（带循环检测），特殊处理是
# chatgpt.exe 且 exe 路径含 openai.codex_ 时记为 codex.exe。服务端 timeline
# 的 app_name 本就强制小写（见上 TimelineBin.app_only），不收 display_name。
# 安卓端在上传前把包名映射为下面的 exe 风格归一键，与 PC 键完全一致，
# 中文显示名只走旧汇总接口 POST /api/v1/upload 的 display_name，
# timeline 不收 display_name。有电脑版的应用取 PC 的 exe 键
# （微信 -> weixin.exe，QQ -> qq.exe，钉钉 -> dingtalk.exe）；没有电脑版的
# 应用用 "<plain>.exe"（如抖音 -> douyin.exe）。企业微信在 PC 表中无对应
# 项（飞书 feishu.exe 是另一个应用），按无电脑版处理为 wework.exe。
# 回落规则：包名最后一段小写、去非字母数字，加 ".exe" 后缀；unknown 保持
# unknown。被排除的应用不上传（只发 countedOnly=true 的会话），服务端
# 不设映射表、不改名。
ANDROID_PACKAGE_TO_APP_KEY = {
    "com.tencent.mm": ("weixin.exe", "微信"),
    "com.tencent.mobileqq": ("qq.exe", "QQ"),
    "com.ss.android.ugc.aweme": ("douyin.exe", "抖音"),
    "com.xingin.xhs": ("xiaohongshu.exe", "小红书"),
    "com.bilibili.app.in": ("bilibili.exe", "哔哩哔哩"),
    "com.taobao.taobao": ("taobao.exe", "淘宝"),
    "com.alibaba.android.rimet": ("dingtalk.exe", "钉钉"),
    "com.tencent.wework": ("wework.exe", "企业微信"),
    "com.smile.gifmaker": ("kuaishou.exe", "快手"),
    "com.zhihu.android": ("zhihu.exe", "知乎"),
    "com.sina.weibo": ("weibo.exe", "微博"),
}


class TimelineUpload(DeviceRequest):
    bucket_minutes: int = Field(default=15, ge=15, le=15)
    days: list[TimelineDay] = Field(min_length=1, max_length=31)


class CompletedAppDay(StrictModel):
    day: date
    seconds: float = Field(ge=0, le=86700)


class CompleteApps(DeviceRequest):
    days: list[CompletedAppDay] = Field(min_length=1, max_length=31)
