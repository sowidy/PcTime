"""Cross-device merge for mail: group by mailbox, merge totals, measure overlap.

Pure computation, no database or network: sender.py fetches per-device data
and this module decides how it combines. Rules (sync-design.md §6):

    * Grouping key is users.email lowercased and stripped; every user_key
      under one mailbox counts as one person.
    * Merged screen time = phone + PC - overlap, where overlap is the number
      of seconds in which two or more devices both have an app interval.
      Overlap is reported on its own line in the mail, never silently cut.
    * usage_records stay the authority for totals and per-app ranking;
      timeline intervals/bins are merged by per-app union (see below).

Two different "overlap" notions, kept apart on purpose:

    * ``compute_overlap_seconds`` counts any second covered by >= 2 devices,
      whatever app each device ran. That is the number subtracted from the
      headline merged total.
    * ``union_intervals_by_app`` / ``union_bins_from_intervals`` merge each
      app's intervals across devices (same app, overlapping spans collapse
      to one). Charts built from those show real recorded spans, never
      fabricated numbers. Their total equals phone + PC - same-app overlap,
      which can exceed the headline total by the seconds both devices ran
      *different* apps at once; that gap is bounded by the overlap seconds
      printed in the mail, and in practice ~0 (one person foregrounds one
      device at a time).
"""
from collections import defaultdict


def normalize_email(email):
    """Mailbox grouping key: lowercase, whitespace stripped. Empty stays empty."""
    return (email or "").strip().lower()


def group_devices_by_email(rows):
    """Group subscriber rows by normalized email.

    Input rows are dicts with at least ``user_key`` and ``email``; they may
    also carry ``hostname``, ``platform`` and ``created_at``. Devices inside
    a group are ordered earliest-created first, so ``group[0]`` is the
    anchor whose user_key mail_log uses. Groups come back ordered by their
    anchor's creation order (then email) for deterministic sending.
    Rows without an email never form a group.
    """
    groups = {}
    order = []
    for index, row in enumerate(rows):
        email = normalize_email(row.get("email"))
        if not email:
            continue
        key = email
        if key not in groups:
            groups[key] = {"email": email, "devices": []}
            order.append(key)
        device = dict(row)
        device["email"] = email
        device["_order"] = index
        groups[key]["devices"].append(device)
    result = []
    for key in order:
        devices = sorted(
            groups[key]["devices"],
            key=lambda d: (str(d.get("created_at") or ""), d["_order"]),
        )
        for device in devices:
            device.pop("_order", None)
        result.append({"email": key, "devices": devices})
    result.sort(key=lambda g: (str(g["devices"][0].get("created_at") or ""),
                               g["email"]))
    return result


def device_label(device):
    """Short human label for a device in the per-device breakdown."""
    return device.get("hostname") or device.get("platform") or device.get("user_key") or "未知设备"


def merge_app_summaries(named_summaries):
    """Merge per-device usage_records summaries into one.

    ``named_summaries`` is a list of (label, summary-or-None). Totals and
    launches add up per app_name; display_name keeps the first non-empty one
    seen. ``daily_apps`` (weekly stacked chart input) is summed per day/app
    when present; the caller may replace it with the union-bin-derived one
    (see ``daily_apps_from_bins``) so charts agree with merged timeline bins.
    Returns None when every summary is None.
    """
    present = [(label, s) for label, s in named_summaries if s is not None]
    if not present:
        return None
    by_app = {}
    order = []
    daily = defaultdict(float)
    days_with_data = set()
    devices = []
    for label, summary in named_summaries:
        total = float(summary.get("total_seconds") or 0) if summary else 0.0
        apps = summary.get("apps") or [] if summary else []
        devices.append({"label": label, "total_seconds": round(total, 1),
                        "app_count": len(apps)})
        if not summary:
            continue
        for app in apps:
            name = app.get("app_name") or ""
            if name not in by_app:
                by_app[name] = {"app_name": name,
                                "display_name": app.get("display_name") or "",
                                "seconds": 0.0, "launches": 0}
                order.append(name)
            entry = by_app[name]
            entry["seconds"] += float(app.get("seconds") or 0)
            entry["launches"] += int(app.get("launches") or 0)
            if not entry["display_name"] and app.get("display_name"):
                entry["display_name"] = app["display_name"]
        for row in summary.get("daily_apps") or []:
            day = str(row.get("day"))
            daily[(day, row.get("app_name"))] += float(row.get("seconds") or 0)
            days_with_data.add(day)
    merged_apps = [by_app[name] for name in order]
    for entry in merged_apps:
        entry["seconds"] = round(entry["seconds"], 1)
    merged_apps.sort(key=lambda a: -a["seconds"])
    total = round(sum(a["seconds"] for a in merged_apps), 1)
    out = {"total_seconds": total,
           "app_count": len(merged_apps),
           "apps": merged_apps,
           "_devices": devices}
    if daily:
        out["daily_apps"] = [{"day": day, "app_name": app, "seconds": round(seconds, 1)}
                             for (day, app), seconds in sorted(daily.items())]
        out["days_with_data"] = len(days_with_data)
    else:
        days = {str(s.get("day")) for _, s in present if s.get("day")}
        if days:
            out["days_with_data"] = len(days)
    return out


def compute_overlap_seconds(intervals_by_device):
    """Seconds covered by intervals from >= 2 distinct devices.

    ``intervals_by_device`` is a list of per-device lists of
    (start_second, end_second) with end > start, already clipped to
    [0, 86400). Sweep-line over endpoints; a segment counts when at least
    two devices are active. Adjacent (touching) intervals do not overlap.
    """
    events = []
    for index, intervals in enumerate(intervals_by_device):
        for start, end in intervals:
            start, end = float(start), float(end)
            if end <= start:
                continue
            events.append((start, index, 1))
            events.append((end, index, -1))
    if not events:
        return 0.0
    # Ends before starts at the same timestamp: touching spans share no second.
    events.sort(key=lambda e: (e[0], e[2]))
    active = set()
    overlap = 0.0
    previous = None
    for timestamp, index, delta in events:
        if previous is not None and timestamp > previous and len(active) >= 2:
            overlap += timestamp - previous
        if delta > 0:
            active.add(index)
        else:
            active.discard(index)
        previous = timestamp
    return round(overlap, 3)


def union_intervals_by_app(rows):
    """Collapse intervals per (day, app) across devices into union spans.

    Input rows are dicts with ``day``, ``app_name``, ``start_second``,
    ``end_second`` (as returned by tracking.read_range("app_intervals")).
    Overlapping or touching spans of the same app merge; different apps and
    different days never merge. Output rows keep the same shape.
    """
    grouped = defaultdict(list)
    for row in rows:
        try:
            start, end = float(row["start_second"]), float(row["end_second"])
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        grouped[(str(row.get("day")), row.get("app_name"))].append((start, end))
    out = []
    for (day, app), spans in sorted(grouped.items()):
        spans.sort()
        cur_start, cur_end = spans[0]
        for start, end in spans[1:]:
            if start <= cur_end:
                cur_end = max(cur_end, end)
            else:
                out.append({"day": day, "app_name": app,
                            "start_second": cur_start, "end_second": cur_end})
                cur_start, cur_end = start, end
        out.append({"day": day, "app_name": app,
                    "start_second": cur_start, "end_second": cur_end})
    return out


def per_app_overlap_seconds(rows):
    """Same-app cross-device overlap per app_name, in seconds.

    Input is the flat list of interval rows from every device (same shape
    as union_intervals_by_app takes). For each app, overlap is the summed
    span durations minus the union span durations: the seconds two or more
    devices ran the same app at the same time. Apps with no overlap are
    left out. Different days are added together; the daily Gantt only ever
    feeds it one day's rows.

    This is what the daily Gantt chart's completeness check must allow for
    merged mail: the merged per-app totals are sums across devices, but the
    chart draws union spans, so the two differ by exactly this overlap.
    Without the allowance the chart refuses to draw for any merged day
    where one app ran on both devices at once.
    """
    summed = defaultdict(float)
    for row in rows:
        try:
            start, end = float(row["start_second"]), float(row["end_second"])
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        summed[row.get("app_name")] += end - start
    union = defaultdict(float)
    for row in union_intervals_by_app(rows):
        union[row.get("app_name")] += (float(row["end_second"])
                                       - float(row["start_second"]))
    return {app: round(total - union.get(app, 0.0), 1)
            for app, total in summed.items()
            if total - union.get(app, 0.0) > 0}


def device_kind(platform):
    """'P' for a phone (Android), 'C' for anything else (a computer)."""
    return "P" if str(platform or "").strip().lower().startswith("android") else "C"


def sources_by_key(per_device):
    """Merge per-device key sets into {key: 'P'/'C'/'M'}.

    ``per_device`` is an iterable of (kind, keys) pairs, kind 'P' or 'C'.
    A key seen on both kinds is 'M': the app or site ran on both devices.
    Used for the Gantt chart markers.
    """
    kinds = {}
    for kind, keys in per_device:
        for key in keys or []:
            kinds.setdefault(key, set()).add(kind)
    return {key: "M" if len(seen) > 1 else next(iter(seen))
            for key, seen in kinds.items()}


def dedupe_devices_by_label(devices, app_intervals_by_device, device_rows):
    """Collapse the same physical device registered under several user_keys.

    A fresh install used to mint a random user_key, so reinstalling the app
    registered the same phone again under the same hostname. The merged mail
    then listed it twice with identical totals and subtracted the duplicate
    span as overlap. Devices are grouped by (hostname, platform); each group
    becomes one breakdown row.

    ``devices`` is the merged ``_devices`` list in device order, each entry
    with ``label``, ``total_seconds`` and ``app_count``. ``app_intervals_by_device``
    is the per-device app_intervals rows in the same order, and ``device_rows``
    the original device dicts (hostname, platform) in the same order.

    Returns ``(deduped_devices, label_day_spans)``. A group's total is its
    devices' summed totals minus the same-app overlap inside the group, so
    the headline total does not move when a duplicate collapses. Groups
    with no data at all are dropped from the breakdown. ``label_day_spans``
    is one ``{day: [(start, end)]}`` per label, built from that label's union
    spans, for measuring overlap only across different labels.
    """
    groups = {}
    order = []
    for index, row in enumerate(device_rows):
        key = (row.get("hostname") or "", row.get("platform") or "")
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(index)
    deduped = []
    label_day_spans = []
    for key in order:
        indices = groups[key]
        rows = []
        for index in indices:
            rows.extend(app_intervals_by_device[index] or [])
        total = round(
            sum(float(devices[index].get("total_seconds") or 0) for index in indices)
            - sum(per_app_overlap_seconds(rows).values()), 1)
        apps = {row.get("app_name") for row in rows if row.get("app_name")}
        if total <= 0 and not rows:
            continue
        label = devices[indices[0]].get("label") or key[0] or "未知设备"
        deduped.append({"label": label, "total_seconds": total,
                        "app_count": len(apps)})
        spans = {}
        for row in union_intervals_by_app(rows):
            spans.setdefault(str(row.get("day")), []).append(
                (float(row["start_second"]), float(row["end_second"])))
        label_day_spans.append(spans)
    return deduped, label_day_spans


def union_bins_from_intervals(union_intervals):
    """Split union intervals into 15-minute bins (bucket = second // 900).

    Output rows match app_timeline_bins shape: ``day``, ``bucket`` (0-95),
    ``app_name``, ``seconds``. Seconds are rounded to 3 decimals like the
    PC client snapshot; a bin never exceeds 900 seconds by construction.
    """
    totals = defaultdict(float)
    for row in union_intervals:
        try:
            start, end = float(row["start_second"]), float(row["end_second"])
        except (KeyError, TypeError, ValueError):
            continue
        day, app = str(row.get("day")), row.get("app_name")
        cursor = max(0.0, start)
        end = min(86400.0, end)
        while cursor < end:
            bucket = min(95, int(cursor // 900))
            stop = min(end, (bucket + 1) * 900)
            totals[(day, bucket, app)] += stop - cursor
            cursor = stop
    return [{"day": day, "bucket": bucket, "app_name": app,
             "seconds": round(seconds, 3)}
            for (day, bucket, app), seconds in sorted(totals.items())
            if seconds > 0]


def daily_apps_from_bins(bins):
    """Per (day, app) totals from merged bins, for the weekly stacked chart.

    Lets the chart validate against the same bins the heatmap uses, so a
    merged week still renders instead of showing "missing" whenever the
    devices overlapped for a second.
    """
    totals = defaultdict(float)
    for row in bins:
        try:
            totals[(str(row.get("day")), row.get("app_name"))] += float(row.get("seconds") or 0)
        except (TypeError, ValueError):
            continue
    return [{"day": day, "app_name": app, "seconds": round(seconds, 1)}
            for (day, app), seconds in sorted(totals.items())]
