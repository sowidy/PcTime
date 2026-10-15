"""接口的请求 / 响应数据结构（Pydantic 模型）。

单独放一个文件，是为了让各路由模块只关心「路径怎么走」——数据长什么样，
在这里一眼能看全。客户端那边要照着这些字段构造请求体。

字段长度上限和数据库里的列宽是对齐的（见 db/schema.sql）。这里挡一道，
好处是超长数据会被 FastAPI 直接拒掉并返回 422，而不是让 MySQL 在
写入时报错或者静默截断。
"""
from datetime import date
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

# 客户端生成的 UUID 就是 36 个字符（8-4-4-4-12）
USER_KEY_LEN = 36


# ── 请求体 ────────────────────────────────────────────────────

class AppUsage(BaseModel):
    """某一天里，某一个应用的使用情况。"""

    app_name: str = Field(..., max_length=255,
                          description="归一化后的进程名，如 pycharm64.exe")
    display_name: str = Field("", max_length=255,
                              description="中文显示名，如 PyCharm；可能为空")
    seconds: float = Field(0.0, ge=0, description="当天累计使用秒数")
    launches: int = Field(0, ge=0, description="当天启动次数")

    @field_validator("app_name")
    @classmethod
    def _app_name_not_blank(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("app_name 不能为空")
        return v


class DayUsage(BaseModel):
    """某一天的全部应用汇总。客户端传上来的就是「这一天」这一整块。"""

    day: date
    apps: List[AppUsage] = Field(default_factory=list)


class UploadRequest(BaseModel):
    """客户端上传的请求体。

    user_key 是这个接口唯一要认的东西 —— 它同时充当身份和凭据，
    所以客户端必须把它当密码一样保管（详见 app.py 顶部的说明）。
    """

    user_key: str = Field(..., min_length=USER_KEY_LEN, max_length=USER_KEY_LEN,
                          description="客户端首次运行生成的 UUID")
    hostname: str = Field("", max_length=255, description="机器名，方便认机器")
    platform: str = Field("", max_length=64)
    client_version: str = Field("", max_length=32)
    email: Optional[str] = Field(None, max_length=255,
                                 description="订阅邮箱，可不传")
    days: List[DayUsage] = Field(..., description="要写入的日期，可一次传多天")

    @field_validator("user_key")
    @classmethod
    def _user_key_is_uuid(cls, v):
        """必须是合法 UUID。

        为什么要卡这么死：user_key 是主键性质的东西，如果允许任意字符串，
        客户端一个笔误就会凭空造出一个新用户，而且以后想换成真账号体系时，
        历史数据里会混着一堆乱七八糟的键，没法收拾。
        """
        import uuid
        v = v.strip()
        try:
            uuid.UUID(v)
        except (ValueError, AttributeError, TypeError):
            raise ValueError("user_key 必须是合法的 UUID")
        return v

    @field_validator("email")
    @classmethod
    def _email_shape(cls, v):
        """只做最基本的形状检查，不写完整的邮箱正则。

        完整的邮箱正则是出了名的又长又容易误杀（合法的地址比大多数人
        想象的怪得多）。真要确认邮箱可用，唯一的办法是往它发一封信，
        那件事属于 mail/ 的职责，不在这里做。
        """
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if "@" not in v or v.startswith("@") or v.endswith("@"):
            raise ValueError("email 看起来不是一个邮箱地址")
        return v


class SubscriptionRequest(BaseModel):
    """设置某个用户的邮箱 / 订阅开关。"""

    email: Optional[str] = Field(None, max_length=255)
    subscribed: bool = True

    @field_validator("email")
    @classmethod
    def _email_shape(cls, v):
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if "@" not in v or v.startswith("@") or v.endswith("@"):
            raise ValueError("email 看起来不是一个邮箱地址")
        return v


# ── 响应体 ────────────────────────────────────────────────────

class UploadResponse(BaseModel):
    """上传结果。客户端只看 ok 就够了，其余是给人排查问题用的。"""

    ok: bool
    user_key: str
    days_written: int = Field(..., description="实际写入的天数")
    apps_written: int = Field(..., description="实际写入的应用行数")
    email_saved: bool = Field(..., description="这次有没有带邮箱过来")


class AppUsageOut(BaseModel):
    app_name: str
    display_name: str
    seconds: float
    launches: int


class UsageRecord(AppUsageOut):
    """usage_records 表里的一行：某天某应用的使用情况。

    比 AppUsageOut 多出 day（那天）和 uploaded_at（这行什么时候写的）。
    """

    day: str
    uploaded_at: str


class UsageResponse(BaseModel):
    """GET /api/v1/usage/{user_key} 的返回体。"""

    user_key: str
    start: str
    end: str
    days: int = Field(..., description="区间内实际有数据的天数")
    records: List[UsageRecord]


class DaySummary(BaseModel):
    """某一天的汇总，也是以后每日邮件正文的数据源。"""

    day: str
    total_seconds: float
    app_count: int
    apps: List[AppUsageOut]


class UserOut(BaseModel):
    user_key: str
    email: Optional[str] = None
    subscribed: bool = False
    hostname: str = ""
    platform: str = ""
    client_version: str = ""
    created_at: str
    last_seen_at: str


class HealthResponse(BaseModel):
    ok: bool
    database: bool
    db_name: str
    server_time: str
