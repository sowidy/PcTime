"""用户管理：列表、单查、改订阅。

前两个是管理接口（要 X-Admin-Token），第三个**不是** —— 见下面
update_subscription 上的说明。
"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import require_admin
from api.schemas import SubscriptionRequest, UserOut
from db import users as db_users

router = APIRouter()


@router.get("/api/v1/users", response_model=List[UserOut], dependencies=[Depends(require_admin)])
def read_users(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """列出所有用户，最近活跃的排前面。"""
    return db_users.list_users(limit=limit, offset=offset)


@router.get("/api/v1/users/{user_key}", response_model=UserOut,
            dependencies=[Depends(require_admin)])
def read_user(user_key: str):
    """看单个用户的信息。"""
    user = db_users.get_user(user_key)
    if user is None:
        raise HTTPException(status_code=404, detail="没有这个 user_key")
    return user


# ⚠️ 这一条**故意**不加 dependencies=[Depends(require_admin)]。
# 它是客户端唯一能自己调的管理类接口：用户在设置页改邮箱 / 退订时就会打它，
# 而客户端手里只有自己的 user_key，没有管理口令。加了口令校验，
# 客户端就再也改不了自己的订阅了。
# 安全性靠 user_key 本身当凭据（122 位随机 UUID，见 app.py 顶部说明）。
@router.post("/api/v1/subscription/{user_key}")
def update_subscription(user_key: str, req: SubscriptionRequest):
    """设置某个用户的邮箱和订阅开关。

    走这个接口才能退订：上传接口只会在「这次带了邮箱」时更新邮箱，
    绝不碰 subscribed，所以每天例行的上传不会把退订状态又改回订阅。
    """
    if req.subscribed and not req.email:
        raise HTTPException(
            status_code=400,
            detail="订阅需要同时提供 email；只想退订的话传 subscribed=false",
        )
    if not db_users.set_subscription(user_key, req.email, req.subscribed):
        raise HTTPException(status_code=404, detail="没有这个 user_key")
    return {"ok": True, "user_key": user_key,
            "email": req.email, "subscribed": req.subscribed}
