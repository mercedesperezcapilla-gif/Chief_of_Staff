#!/usr/bin/env python3
"""Gmail API helper — search, read, and send messages from one Google account.

When to use this:
    Use the Gmail MCP if you have it; it's simpler. This OAuth helper is the
    fallback when you don't have a Gmail MCP installed, or when /cos needs
    to programmatically send emails (current Gmail MCP coverage is read-only
    in many setups).

Usage:
    python3 gmail_helper.py --auth                  # One-time browser OAuth
    python3 gmail_helper.py --profile               # Confirm connected account
    python3 gmail_helper.py --search "<query>"      # Gmail search syntax
    python3 gmail_helper.py --read <MSG_ID>         # Print one message body
    python3 gmail_helper.py --send \
        --to user@example.com \
        --subject "..." \
        --body "..."                                # Send (or pass --dry-run)

Multi-account note:
    To use this with more than one Gmail account, copy the script and rename
    the TOKEN_FILE constant per account (e.g. gmail_personal.py uses
    "personal_token.json", gmail_work.py uses "work_token.json"). Each
    account needs its own OAuth consent flow.

Files:
    Reads:  ./oauth_credentials.json   (your Google OAuth client credentials)
    Writes: ./gmail_token.json         (cached refresh token — NEVER COMMIT)
"""

import argparse
import base64
import sys
from email.mime.text import MIMEText
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# These three scopes cover read, send, and modify (label / archive / mark-read).
# Trim to .readonly if you don't need send / modify.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]

DIR = Path(__file__).parent
CREDS_FILE = DIR / "oauth_credentials.json"
TOKEN_FILE = DIR / "gmail_token.json"


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


def get_service():
    return build("gmail", "v1", credentials=get_credentials())


def cmd_profile():
    service = get_service()
    profile = service.users().getProfile(userId="me").execute()
    print(f"Connected as: {profile['emailAddress']}")
    print(f"Messages total: {profile.get('messagesTotal', '?')}")
    print(f"Threads total: {profile.get('threadsTotal', '?')}")


def cmd_search(query, max_results=10):
    service = get_service()
    results = service.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute()
    messages = results.get("messages", [])
    if not messages:
        print("No messages found.")
        return []

    print(f"Found {len(messages)} messages:\n")
    msg_list = []
    for msg in messages:
        detail = service.users().messages().get(
            userId="me", id=msg["id"], format="metadata",
            metadataHeaders=["From", "To", "Subject", "Date"]
        ).execute()
        headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
        snippet = detail.get("snippet", "")[:100]
        print(f"  ID: {msg['id']}")
        print(f"  From: {headers.get('From', '?')}")
        print(f"  Subject: {headers.get('Subject', '?')}")
        print(f"  Date: {headers.get('Date', '?')}")
        print(f"  Preview: {snippet}")
        print()
        msg_list.append({"id": msg["id"], **headers, "snippet": snippet})
    return msg_list


def cmd_read(msg_id):
    service = get_service()
    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}

    print(f"From: {headers.get('From', '?')}")
    print(f"To: {headers.get('To', '?')}")
    print(f"Subject: {headers.get('Subject', '?')}")
    print(f"Date: {headers.get('Date', '?')}")
    print("---")

    body = _extract_body(msg.get("payload", {}))
    print(body)
    return body


def _extract_body(payload):
    """Recursively extract a text body from a Gmail message payload."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")

    parts = payload.get("parts", [])
    for part in parts:
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
        if part.get("parts"):
            result = _extract_body(part)
            if result:
                return result

    for part in parts:
        if part.get("mimeType") == "text/html" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")

    return "(no text body found)"


def cmd_send(to, subject, body, dry_run=False):
    if dry_run:
        print("DRY RUN — would send:")
        print(f"  To: {to}")
        print(f"  Subject: {subject}")
        print(f"  Body: {body[:200]}...")
        return

    service = get_service()
    message = MIMEText(body)
    message["to"] = to
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"Sent! Message ID: {sent['id']}")
    return sent


def main():
    parser = argparse.ArgumentParser(description="Gmail API helper for /cos")
    parser.add_argument("--auth", action="store_true", help="Run OAuth flow now")
    parser.add_argument("--profile", action="store_true", help="Show connected account")
    parser.add_argument("--search", type=str, help="Gmail search query")
    parser.add_argument("--read", type=str, help="Read message by ID")
    parser.add_argument("--send", action="store_true", help="Send an email")
    parser.add_argument("--to", type=str, help="Recipient (with --send)")
    parser.add_argument("--subject", type=str, help="Subject (with --send)")
    parser.add_argument("--body", type=str, help="Body (or pipe via stdin)")
    parser.add_argument("--dry-run", action="store_true", help="Preview send without actually sending")
    parser.add_argument("--max", type=int, default=10, help="Max search results")
    args = parser.parse_args()

    if args.auth:
        get_credentials()
        print(f"Authenticated. Token saved to {TOKEN_FILE}")
        cmd_profile()
        return

    if args.profile:
        cmd_profile()
    elif args.search:
        cmd_search(args.search, args.max)
    elif args.read:
        cmd_read(args.read)
    elif args.send:
        if not args.to or not args.subject:
            print("Error: --to and --subject required for --send")
            sys.exit(1)
        body = args.body if args.body else sys.stdin.read()
        cmd_send(args.to, args.subject, body, dry_run=args.dry_run)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
