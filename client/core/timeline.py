"""Build privacy-reduced 15-minute bins from local fragments."""
from collections import defaultdict
from datetime import date, datetime, timedelta
import heapq

from core import storage


def build_bins(day):
    day = date.fromisoformat(str(day))
    midnight = datetime.combine(day, datetime.min.time())
    with storage._get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id,app_name,exe_path,start_time,end_time FROM usage_fragments "
            "WHERE date=? AND is_idle=0 ORDER BY id", (str(day),))]
    rules = storage.get_app_rules()
    events = defaultdict(list)
    names = {}
    for r in rows:
        try:
            start = max(0.0, (datetime.fromisoformat(r["start_time"]) - midnight).total_seconds())
            end = min(86400.0, (datetime.fromisoformat(r["end_time"]) - midnight).total_seconds())
            name = storage.canonical_app_name(r["app_name"], rules, r["exe_path"] or "")
            if end - start < .5:
                continue
            names[r["id"]] = name
            events[start].append((r["id"], True))
            events[end].append((r["id"], False))
        except (TypeError, ValueError):
            continue
    active, priority, totals = set(), [], defaultdict(float)
    previous = 0.0
    for point in sorted(events):
        while priority and -priority[0] not in active:
            heapq.heappop(priority)
        if priority:
            app = names[-priority[0]]
            cursor = previous
            while cursor < point:
                bucket = min(95, int(cursor // 900))
                stop = min(point, (bucket + 1) * 900)
                totals[bucket, app] += stop - cursor
                cursor = stop
        for fragment_id, entering in events[point]:
            if entering:
                active.add(fragment_id)
                heapq.heappush(priority, -fragment_id)
            else:
                active.discard(fragment_id)
        previous = point
    bins = [{"bucket": b, "app_name": app, "seconds": round(seconds, 3)}
            for (b, app), seconds in sorted(totals.items()) if seconds > 0]
    summary = storage.get_daily_total(str(day))
    if abs(sum(r["seconds"] for r in bins) - summary) > max(60, summary * .01):
        raise ValueError("时间线与应用汇总不一致")
    if len(bins) > 1000:
        raise ValueError("时间线超过单日限额")
    return bins
