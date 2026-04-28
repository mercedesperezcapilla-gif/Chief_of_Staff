#!/usr/bin/env python3
"""Add a task to a Google Tasks list.

Use this when /cos needs to write or complete tasks (not just read them).
The companion `tasks_sync.py` script reads tasks; this one writes.

Usage:
    python tasks_add.py --list "Work" --title "Cybersecurity renewal" --due 2026-04-17
    python tasks_add.py --list "Personal" --title "Pick up dry cleaning"

Auth notes:
    - This script uses a SEPARATE OAuth token from tasks_sync.py because the
      WRITE scope is broader than the read-only scope. That keeps the read
      token least-privilege.
    - First run opens a browser window for OAuth consent. The token is then
      cached in tasks_write_token.json for subsequent runs.
    - Both scripts share the same oauth_credentials.json (your OAuth 2.0
      client ID from Google Cloud Console).

Files:
    Reads:  ./oauth_credentials.json  (your Google OAuth 2.0 client credentials)
    Writes: ./tasks_write_token.json  (cached refresh token — never commit)
"""
import argparse
from datetime import datetime, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/tasks"]
DIR = Path(__file__).parent
CREDS_FILE = DIR / "oauth_credentials.json"
TOKEN_FILE = DIR / "tasks_write_token.json"


def get_credentials():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDS_FILE.exists():
                raise SystemExit(
                    f"Missing {CREDS_FILE}. See SETUP.md → Step 7b for OAuth setup."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return creds


def main():
    parser = argparse.ArgumentParser(description="Add a task to a Google Tasks list")
    parser.add_argument("--list", required=True, help="Task list name (case-insensitive)")
    parser.add_argument("--title", required=True, help="Task title")
    parser.add_argument("--notes", default="", help="Optional task notes")
    parser.add_argument("--due", default="", help="Due date as YYYY-MM-DD (optional)")
    args = parser.parse_args()

    service = build("tasks", "v1", credentials=get_credentials())
    lists = service.tasklists().list(maxResults=100).execute().get("items", [])
    match = next(
        (l for l in lists if l["title"].strip().lower() == args.list.strip().lower()),
        None,
    )
    if not match:
        print(f"List '{args.list}' not found. Available lists:")
        for l in lists:
            print(f"  - {l['title']}")
        raise SystemExit(1)

    body = {"title": args.title}
    if args.notes:
        body["notes"] = args.notes
    if args.due:
        dt = datetime.strptime(args.due, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        body["due"] = dt.strftime("%Y-%m-%dT00:00:00.000Z")

    result = service.tasks().insert(tasklist=match["id"], body=body).execute()
    print(f"Added to '{match['title']}': {result['title']} (due {args.due or 'n/a'})")
    print(f"  id: {result['id']}")


if __name__ == "__main__":
    main()
