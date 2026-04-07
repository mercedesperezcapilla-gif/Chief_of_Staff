#!/usr/bin/env python3
"""Google Calendar sync for Chief of Staff.

Pulls events from all calendars listed in config.yaml into a local snapshot.
Classifies events as meetings, work blocks, or commitments based on attendee
count, organizer, and presence of video conferencing links.

Usage:
    python gcal_sync.py --auth          # One-time: authorize in browser
    python gcal_sync.py                 # Pull events for today → gcal_snapshot.json
    python gcal_sync.py --days 7        # Pull events for next 7 days
    python gcal_sync.py --list          # Print today's events summary
"""

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
DIR = Path(__file__).parent
CREDS_FILE = DIR / "oauth_credentials.json"
TOKEN_FILE = DIR / "gcal_token.json"
SNAPSHOT_FILE = DIR / "gcal_snapshot.json"
CONFIG_FILE = DIR / "config.yaml"


def load_config():
    """Load calendar list and timezone from config.yaml."""
    if not CONFIG_FILE.exists():
        print(f"ERROR: {CONFIG_FILE} not found.")
        print("Copy config.example.yaml to config.yaml and fill in your calendars.")
        exit(1)
    with open(CONFIG_FILE) as f:
        return yaml.safe_load(f)


def get_credentials():
    """Load or refresh credentials, or run auth flow."""
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDS_FILE.exists():
                print(f"ERROR: {CREDS_FILE} not found.")
                print("Download your OAuth client JSON from Google Cloud Console")
                print("and save it as 'oauth_credentials.json' in this directory.")
                exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return creds


def fetch_events(service, calendar_id, time_min, time_max, timezone):
    """Fetch events from a single calendar."""
    events = []
    page_token = None
    while True:
        result = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                maxResults=50,
                singleEvents=True,
                orderBy="startTime",
                timeZone=timezone,
                pageToken=page_token,
            )
            .execute()
        )
        for evt in result.get("items", []):
            start = evt.get("start", {})
            end = evt.get("end", {})
            attendees = evt.get("attendees", [])
            organizer = evt.get("organizer", {})
            conf_data = evt.get("conferenceData", {})

            # Extract video link if present
            video_link = None
            for ep in conf_data.get("entryPoints", []):
                if ep.get("entryPointType") == "video":
                    video_link = ep.get("uri")
                    break
            # Also check description/location for zoom/meet/teams links
            desc = evt.get("description", "") or ""
            location = evt.get("location", "") or ""
            has_video = bool(video_link) or any(
                x in (desc + location).lower()
                for x in ["zoom.us", "meet.google.com", "teams.microsoft.com"]
            )

            events.append(
                {
                    "id": evt.get("id"),
                    "summary": evt.get("summary", "(no title)"),
                    "start": start.get("dateTime", start.get("date")),
                    "end": end.get("dateTime", end.get("date")),
                    "all_day": "date" in start and "dateTime" not in start,
                    "location": location,
                    "description": desc[:200] if desc else "",
                    "attendee_count": len(attendees),
                    "attendees": [
                        a.get("email", "") for a in attendees[:10]
                    ],
                    "organizer": organizer.get("email", ""),
                    "self_organized": organizer.get("self", False),
                    "has_video_link": has_video,
                    "video_link": video_link,
                    "status": evt.get("status", "confirmed"),
                }
            )
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return events


def classify_event(evt):
    """Classify event as meeting, work_block, or commitment."""
    if evt["all_day"]:
        return "all_day"
    # Self-created, no other attendees (or just self), no video = work block
    if (
        evt["self_organized"]
        and evt["attendee_count"] <= 1
        and not evt["has_video_link"]
    ):
        return "work_block"
    # Has video link or multiple attendees = real meeting
    if evt["has_video_link"] or evt["attendee_count"] > 1:
        return "meeting"
    return "commitment"


def main():
    parser = argparse.ArgumentParser(description="Google Calendar sync")
    parser.add_argument("--auth", action="store_true", help="Run auth flow only")
    parser.add_argument(
        "--days", type=int, default=1, help="Number of days to fetch (default: 1)"
    )
    parser.add_argument("--list", action="store_true", help="Print events summary")
    args = parser.parse_args()

    config = load_config()
    timezone = config.get("timezone", "America/Los_Angeles")
    calendars = config.get("calendars", [])
    tz = ZoneInfo(timezone)

    creds = get_credentials()

    if args.auth:
        print(f"Authenticated. Token saved to {TOKEN_FILE}")
        return

    service = build("calendar", "v3", credentials=creds)

    now = datetime.now(tz)
    time_min = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    time_max = (
        now.replace(hour=0, minute=0, second=0, microsecond=0)
        + timedelta(days=args.days)
    ).isoformat()

    all_events = []
    for cal in calendars:
        try:
            events = fetch_events(service, cal["id"], time_min, time_max, timezone)
            for evt in events:
                evt["calendar"] = cal["label"]
                evt["calendar_id"] = cal["id"]
                evt["type"] = classify_event(evt)
            all_events.extend(events)
        except Exception as e:
            print(f"  Warning: could not fetch {cal['label']}: {e}")

    # Sort all events by start time
    all_events.sort(key=lambda e: e["start"])

    snapshot = {
        "synced_at": datetime.now(tz).isoformat(),
        "range": {"from": time_min, "to": time_max, "days": args.days},
        "events": all_events,
    }
    SNAPSHOT_FILE.write_text(json.dumps(snapshot, indent=2))

    total = len(all_events)
    meetings = sum(1 for e in all_events if e["type"] == "meeting")
    print(
        f"Synced {len(calendars)} calendars, {total} events ({meetings} meetings) → {SNAPSHOT_FILE}"
    )

    if args.list:
        current_date = None
        for evt in all_events:
            evt_date = evt["start"][:10]
            if evt_date != current_date:
                current_date = evt_date
                print(f"\n  {current_date}")

            if evt["all_day"]:
                tag = "ALL DAY"
                time_str = "         "
            else:
                time_str = evt["start"][11:16]
                tag = evt["type"].upper()

            print(f"    {time_str} [{tag:10s}] {evt['summary']} ({evt['calendar']})")


if __name__ == "__main__":
    main()
