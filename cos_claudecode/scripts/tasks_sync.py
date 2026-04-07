#!/usr/bin/env python3
"""Google Tasks sync for Chief of Staff.

Pulls all open tasks from your Google Tasks lists into a local JSON snapshot
that the /cos skill reads on every run.

Usage:
    python tasks_sync.py --auth          # One-time: authorize in browser
    python tasks_sync.py                 # Pull all tasks → tasks_snapshot.json
    python tasks_sync.py --list          # Print task lists summary
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/tasks.readonly"]
DIR = Path(__file__).parent
CREDS_FILE = DIR / "oauth_credentials.json"
TOKEN_FILE = DIR / "tasks_token.json"
SNAPSHOT_FILE = DIR / "tasks_snapshot.json"


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
                print("See README.md for setup instructions.")
                exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return creds


def fetch_all_tasks(service):
    """Fetch all task lists and their tasks."""
    task_lists = []
    page_token = None

    # Get all task lists
    while True:
        result = service.tasklists().list(
            maxResults=100, pageToken=page_token
        ).execute()
        task_lists.extend(result.get("items", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break

    output = []
    for tl in task_lists:
        tl_data = {
            "id": tl["id"],
            "title": tl["title"],
            "updated": tl.get("updated"),
            "tasks": [],
        }

        # Get all tasks in this list
        page_token = None
        while True:
            result = service.tasks().list(
                tasklist=tl["id"],
                maxResults=100,
                showCompleted=False,
                showHidden=False,
                pageToken=page_token,
            ).execute()
            for task in result.get("items", []):
                tl_data["tasks"].append({
                    "id": task["id"],
                    "title": task.get("title", ""),
                    "notes": task.get("notes", ""),
                    "status": task.get("status", ""),
                    "due": task.get("due"),
                    "parent": task.get("parent"),
                    "position": task.get("position"),
                    "updated": task.get("updated"),
                })
            page_token = result.get("nextPageToken")
            if not page_token:
                break

        output.append(tl_data)

    return output


def main():
    parser = argparse.ArgumentParser(description="Google Tasks sync")
    parser.add_argument("--auth", action="store_true", help="Run auth flow only")
    parser.add_argument("--list", action="store_true", help="Print summary")
    args = parser.parse_args()

    creds = get_credentials()

    if args.auth:
        print(f"Authenticated. Token saved to {TOKEN_FILE}")
        return

    service = build("tasks", "v1", credentials=creds)
    data = fetch_all_tasks(service)

    snapshot = {
        "synced_at": datetime.now().isoformat(),
        "task_lists": data,
    }
    SNAPSHOT_FILE.write_text(json.dumps(snapshot, indent=2))

    total_tasks = sum(len(tl["tasks"]) for tl in data)
    print(f"Synced {len(data)} task lists, {total_tasks} open tasks → {SNAPSHOT_FILE}")

    if args.list:
        for tl in data:
            print(f"\n  {tl['title']} ({len(tl['tasks'])} tasks)")
            for t in tl["tasks"][:5]:
                due = f" [due {t['due'][:10]}]" if t.get("due") else ""
                print(f"    - {t['title']}{due}")
            if len(tl["tasks"]) > 5:
                print(f"    ... and {len(tl['tasks']) - 5} more")


if __name__ == "__main__":
    main()
