"""Additional datasets: transactional daily replacement and readiness markers."""
from datetime import datetime

from db import conn as db_conn

TABLES = {"web": ("website_usage_records", "host,display_name,seconds"),
          "timeline": ("app_timeline_bins", "bucket,app_name,seconds")}


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
