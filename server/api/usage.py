"""读取使用记录的两个接口：区间明细、单日汇总。

两个都是只读的，而且都按 user_key 取数 —— 也就是「知道 key 就能读」，
和上传接口同一个信任模型（见 app.py 顶部那段鉴权说明）。
"""
from datetime import date

from fastapi import APIRouter, HTTPException, Query

from api.schemas import DaySummary, UsageResponse
from db import usage_records as db_usage_records

router = APIRouter()


@router.get("/api/v1/usage/{user_key}", response_model=UsageResponse)
def read_usage(
    user_key: str,
    start: date | None = Query(None, description="起始日，默认与 end 相同"),
    end: date | None = Query(None, description="结束日，默认今天"),
):
    """读某个用户在 [start, end] 区间内的每日明细。"""
    end = end or date.today()
    start = start or end
    if start > end:
        raise HTTPException(status_code=400, detail="start 不能晚于 end")
    if (end - start).days > 366:
        raise HTTPException(status_code=400, detail="区间最长一年，请分段查询")

    records = db_usage_records.get_usage(user_key, start, end)
    return UsageResponse(
        user_key=user_key,
        start=start.isoformat(),
        end=end.isoformat(),
        days=len({r["day"] for r in records}),
        records=records,
    )


@router.get("/api/v1/summary/{user_key}", response_model=DaySummary)
def read_summary(
    user_key: str,
    day: date | None = Query(None, description="哪一天，默认今天"),
):
    """读某个用户某天的汇总。找不到数据返回 404。

    这个接口就是以后每日邮件正文的数据源，所以单独留一个 ——
    免得邮件逻辑自己去拼 SQL，两边对「总时长怎么算」理解不一致。
    """
    day = day or date.today()
    summary = db_usage_records.get_daily_summary(user_key, day)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"{day} 没有该用户的记录")
    return summary
