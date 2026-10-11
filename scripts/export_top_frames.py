#!/usr/bin/env python3
"""Export the most used frames from GA4 into a bundled JSON file.

    python3 scripts/export_top_frames.py [--refresh]

Reads the Framify GA4 property through the Analytics Data API and writes
Marketing/Analytics/TopFrames.json with:

  weeks    one entry per completed Sunday-Saturday week (keyed by its Sunday,
           e.g. 2026-09-27), top 100 frames. Sunday-Saturday matches Google
           Analytics' own weeks, so numbers agree with the GA UI.
  months   one entry per completed calendar month (2026-09), top 30 frames
  overall  all stored weeks summed per frame, top 30 (a "user-weeks" score:
           distinct users cannot be added across periods, so a user who
           returns every week counts every week)
  current  what the app reads: trending (latest week vs the previous four),
           month (latest month), overall

Weeks with no frame data (the first ones) are skipped.

Frames are ranked by distinct users who triggered any event carrying frame_id,
ties broken by users who saved. Raw saves are easy for one person to inflate.

Per-frame data does not exist before late August 2026 (the frame_id dimension was
registered then and GA4 does not backfill), so the first run starts there.
Completed periods already in the file are kept; the latest week is always
re-fetched because GA revises recent days. --refresh re-fetches everything.

Setup (one time):
  pip3 install google-analytics-data
  Either   gcloud auth application-default login \\
               --scopes=https://www.googleapis.com/auth/analytics.readonly
  or       export GOOGLE_APPLICATION_CREDENTIALS=<service account key.json>
           with that service account added as a Viewer on the GA4 property.
"""

import argparse
import datetime as dt
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(ROOT, "Marketing", "Analytics", "TopFrames.json")
DEFAULT_PROPERTY = "153125601"
FIRST_DAY = dt.date(2026, 8, 9)  # a Sunday; frame_id data starts 2026-08-10 or later
WEEK_TOP = 100
MONTH_TOP = 30
OVERALL_TOP = 30
TREND_TOP = 12
TREND_MIN_USERS = 15
TREND_BASELINE_WEEKS = 4
DIMENSION = "customEvent:frame_id"


def completed_weeks(today):
    """(label, start, end) for every Sunday-Saturday week that has fully ended."""
    weeks = []
    start = FIRST_DAY
    while start + dt.timedelta(days=6) < today:
        weeks.append((start.isoformat(), start, start + dt.timedelta(days=6)))
        start += dt.timedelta(days=7)
    return weeks


def completed_months(today):
    months = []
    cursor = dt.date(FIRST_DAY.year, FIRST_DAY.month, 1)
    while True:
        following = (cursor.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        end = following - dt.timedelta(days=1)
        if end >= today:
            break
        months.append((cursor.strftime("%Y-%m"), max(cursor, FIRST_DAY), end))
        cursor = following
    return months


def fetch(client, prop, start, end, event_name=None):
    """frame_id -> (active users, event count) for the date range."""
    from google.analytics.data_v1beta.types import (
        DateRange, Dimension, Filter, FilterExpression, Metric, RunReportRequest,
    )

    event_filter = None
    if event_name:
        event_filter = FilterExpression(filter=Filter(
            field_name="eventName",
            string_filter=Filter.StringFilter(value=event_name)))
    result = {}
    offset = 0
    while True:
        response = client.run_report(RunReportRequest(
            property=f"properties/{prop}",
            dimensions=[Dimension(name=DIMENSION)],
            metrics=[Metric(name="activeUsers"), Metric(name="eventCount")],
            date_ranges=[DateRange(start_date=start.isoformat(), end_date=end.isoformat())],
            dimension_filter=event_filter,
            limit=10000,
            offset=offset,
        ))
        for row in response.rows:
            frame_id = row.dimension_values[0].value
            if not frame_id or frame_id == "(not set)":
                continue
            result[frame_id] = (int(row.metric_values[0].value), int(row.metric_values[1].value))
        offset += len(response.rows)
        if offset >= response.row_count or not response.rows:
            return result


def build_period(client, prop, start, end, top):
    used = fetch(client, prop, start, end)
    saved = fetch(client, prop, start, end, "frame_saved")
    frames = []
    for frame_id, (users, _) in used.items():
        save_users, saves = saved.get(frame_id, (0, 0))
        frames.append({"id": frame_id, "users": users, "save_users": save_users, "saves": saves})
    frames.sort(key=lambda f: (-f["users"], -f["save_users"], f["id"]))
    return {"window": f"{start}/{end}", "frames": frames[:top]}


def overall(weeks):
    totals = {}
    for entry in weeks.values():
        for frame in entry["frames"]:
            row = totals.setdefault(frame["id"], {"id": frame["id"], "users": 0, "save_users": 0, "saves": 0})
            for key in ("users", "save_users", "saves"):
                row[key] += frame.get(key, 0)
    ranked = sorted(totals.values(), key=lambda f: (-f["users"], -f["save_users"], f["id"]))
    labels = sorted(weeks)
    first = weeks[labels[0]]["window"].split("/")[0]
    last = weeks[labels[-1]]["window"].split("/")[1]
    return {"window": f"{first}/{last}", "frames": ranked[:OVERALL_TOP]}


def trending(weeks):
    labels = sorted(weeks)
    latest = labels[-1]
    before = labels[-1 - TREND_BASELINE_WEEKS:-1]
    baseline = {}
    for label in before:
        for frame in weeks[label]["frames"]:
            baseline[frame["id"]] = baseline.get(frame["id"], 0) + frame["users"]
    scored = []
    for frame in weeks[latest]["frames"]:
        if frame["users"] < TREND_MIN_USERS:
            continue
        average = baseline.get(frame["id"], 0) / max(len(before), 1)
        rise = frame["users"] - average
        if rise > 0:
            scored.append((rise, frame["users"], frame["id"]))
    scored.sort(key=lambda s: (-s[0], -s[1], s[2]))
    return [s[2] for s in scored[:TREND_TOP]]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--property", default=DEFAULT_PROPERTY, help="GA4 property id")
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--refresh", action="store_true", help="re-fetch every period")
    args = parser.parse_args()

    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
    except ImportError:
        sys.exit("pip3 install google-analytics-data  (see the setup notes at the top of this file)")

    data = {"weeks": {}, "months": {}}
    if os.path.exists(args.out) and not args.refresh:
        with open(args.out) as handle:
            data = json.load(handle)
    client = BetaAnalyticsDataClient()
    today = dt.date.today()

    weeks = completed_weeks(today)
    for index, (label, start, end) in enumerate(weeks):
        is_latest = index == len(weeks) - 1
        if label in data["weeks"] and not is_latest:
            continue
        entry = build_period(client, args.property, start, end, WEEK_TOP)
        if entry["frames"]:
            data["weeks"][label] = entry
            print(f"week  {label}  {len(entry['frames'])} frames")

    for label, start, end in completed_months(today):
        if label in data["months"]:
            continue
        entry = build_period(client, args.property, start, end, MONTH_TOP)
        if entry["frames"]:
            data["months"][label] = entry
            print(f"month {label}  {len(entry['frames'])} frames")

    if not data["weeks"]:
        sys.exit("No per-frame data returned. Check the property id and the account's access.")

    ids = lambda entry: [frame["id"] for frame in entry["frames"]]
    latest_month = data["months"][sorted(data["months"])[-1]] if data["months"] else None
    all_time = overall(data["weeks"])
    data["overall"] = all_time
    data["current"] = {
        "week": sorted(data["weeks"])[-1],
        "trending": trending(data["weeks"]),
        "month": ids(latest_month) if latest_month else [],
        "overall": ids(all_time),
    }
    data["generated"] = today.isoformat()
    with open(args.out, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    print(f"wrote {os.path.relpath(args.out, ROOT)}")


if __name__ == "__main__":
    main()
