"""Additional datasets: transactional daily replacement and readiness markers."""
from datetime import datetime

from db import conn as db_conn

TABLES = {"web": ("website_usage_records", "host,display_name,seconds"),
          "web_hourly": ("website_hourly_usage", "hour,host,seconds"),
          "web_intervals": ("website_intervals", "host,start_second,end_second"),
          "timeline": ("app_timeline_bins", "bucket,app_name,seconds"),
          "app_intervals": ("app_timeline_intervals", "app_name,start_second,end_second")}


def replace_day(cur, kind, user_key, day, records, complete=False):
    table, columns = TABLES[kind]
    now = datetime.now()
    cur.execute(f"DELETE FROM {table} WHERE user_key=%s AND day=%s", (user_key, day))
    rows = [(user_key, day, *(r[c] for c in columns.split(",")), now) for r in records]
    if rows:
        cur.executemany(f"INSERT INTO {table} (user_key,day,{columns},uploaded_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s)", rows)
    cur.execute("INSERT INTO tracking_uploads (user_key,day,kind,complete,uploaded_at) VALUES (%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE complete=VALUES(complete),uploaded_at=VALUES(uploaded_at)",
                (user_key, day, kind, bool(complete), now))
    return len(rows)


def clear_day(cur, kind, user_key, day):
    """Remove a stale optional snapshot when an older client cannot supply it."""
    table = TABLES[kind][0]
    cur.execute(f"DELETE FROM {table} WHERE user_key=%s AND day=%s", (user_key, day))
    cur.execute("DELETE FROM tracking_uploads WHERE user_key=%s AND day=%s AND kind=%s",
                (user_key, day, kind))


def read_range(kind, user_key, start, end):
    table, columns = TABLES[kind]
    with db_conn.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT day,{columns},uploaded_at FROM {table} WHERE user_key=%s "
                        "AND day BETWEEN %s AND %s ORDER BY day", (user_key, start, end))
            return [db_conn.row_to_json(r) for r in cur.fetchall()]


def readiness(user_key, start, end):
    with db_conn.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT day,kind,complete,uploaded_at FROM tracking_uploads WHERE user_key=%s "
                        "AND day BETWEEN %s AND %s", (user_key, start, end))
            return [db_conn.row_to_json(r) for r in cur.fetchall()]


def save_unlocks(cur, user_key, day, unlocks):
    """Write one day of Android unlock counts.

    Same replace-not-accumulate rule as the other daily tables: a re-upload
    overwrites the row instead of adding to it, so retries never double-count.
    Takes the caller's cursor so it lands in the same transaction as the
    timeline bins/intervals of the same upload. ``unlocks`` is a device-wide
    daily count from the Android keyguard, not per-app launches: the legacy
    usage_records.launches stays "starts per app per day" and is untouched.
    """
    now = datetime.now()
    cur.execute("INSERT INTO android_daily_stats (user_key,day,unlocks,uploaded_at) "
                "VALUES (%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                "unlocks=VALUES(unlocks),uploaded_at=VALUES(uploaded_at)",
                (user_key, day, int(unlocks), now))


def read_unlocks(user_key, start, end):
    """Read unlock counts for [start, end]; rows are JSON-shaped dicts."""
    with db_conn.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT day,unlocks,uploaded_at FROM android_daily_stats "
                        "WHERE user_key=%s AND day BETWEEN %s AND %s ORDER BY day",
                        (user_key, start, end))
            return [db_conn.row_to_json(r) for r in cur.fetchall()]
