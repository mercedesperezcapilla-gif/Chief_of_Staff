#!/usr/bin/env python3
"""Gmail API helper — search, read, and send across one OR MORE accounts.

When to use this:
    Use the Gmail MCP if your skill only needs read access on a single
    account; it's simpler. Use this OAuth helper when you need to:
      - Access multiple Gmail accounts (work, personal, side-project, etc.)
      - Send mail programmatically (current Gmail MCP coverage is read-only
        in many setups)
      - Run from /cos as a scriptable action (not just an MCP-aware chat)

Multi-account pattern:
    The same script handles any number of accounts. Pass `--account <name>`
    and the script uses a per-account token file (e.g. `gmail_work_token.json`,
    `gmail_personal_token.json`, `gmail_mints_token.json`). Each account does
    its own one-time OAuth flow on first use. Account names are arbitrary —
    pick whatever makes sense to you.

Usage:
    # First-time auth for an account (opens browser)
    python3 gmail_helper.py --account work --auth
    python3 gmail_helper.py --account personal --auth
    python3 gmail_helper.py --account side-project --auth

    # Confirm which account is connected
    python3 gmail_helper.py --account work --profile

    # Search a specific account
    python3 gmail_helper.py --account work --search "from:boss subject:Q3 review"
    python3 gmail_helper.py --account personal --search "from:school"

    # Read one message
    python3 gmail_helper.py --account work --read <MSG_ID>

    # Send from a specific account
    python3 gmail_helper.py --account work --send \
        --to colleague@company.com \
        --subject "..." \
        --body "..."

    # Single-account convenience: omit --account to use 'default'
    python3 gmail_helper.py --search "is:unread"   # uses gmail_default_token.json

How /cos uses this:
    The /cos skill can shell out to this script when you ask it to scan or
    send across your accounts. Example skill instruction:

        When the user asks "any unread from <person>", run:
          python3 scripts/gmail_helper.py --account <best-guess-account> \
              --search "from:<person> is:unread"
        and synthesize the top 3 results.

    For multi-account scans, loop over accounts the user has authorized.

Files:
    Reads:  ./oauth_credentials.json          (your Google OAuth client)
    Writes: ./gmail_<account>_token.json      (per-account token; NEVER COMMIT)
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
# Trim to .readonly if you don't need send / modify for an account.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]

DIR = Path(__file__).parent
CREDS_FILE = DIR / "oauth_credentials.json"


def token_file_for(account):
    """Return the per-account token filename. Sanitize account name for filesystem."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in account)
    return DIR / f"gmail_{safe}_token.json"


def get_credentials(account):
    token_file = token_file_for(account)
    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDS_FILE.exists():
                raise SystemExit(
                    f"Missing {CREDS_FILE}. See SETUP.md → Step 7b for OAuth setup."
                )
            print(f"Starting OAuth flow for account '{account}'...")
            print(f"  Browser will open. SIGN IN AS THE GMAIL ACCOUNT YOU WANT TO LINK.")
            print(f"  (NOT necessarily the same account that owns the OAuth client.)")
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        token_file.write_text(creds.to_json())
        print(f"  Token saved to {token_file.name}")
    return creds


def get_service(account):
    return build("gmail", "v1", credentials=get_credentials(account))


def cmd_profile(account):
    service = get_service(account)
    profile = service.users().getProfile(userId="me").execute()
    print(f"Account label:    {account}")
    print(f"Connected as:     {profile['emailAddress']}")
    print(f"Messages total:   {profile.get('messagesTotal', '?')}")
    print(f"Threads total:    {profile.get('threadsTotal', '?')}")


def cmd_search(account, query, max_results=10):
    service = get_service(account)
    results = service.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute()
    messages = results.get("messages", [])
    if not messages:
        print(f"[{account}] No messages found.")
        return []

    print(f"[{account}] Found {len(messages)} messages:\n")
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
        msg_list.append({"id": msg["id"], "account": account, **headers, "snippet": snippet})
    return msg_list


def cmd_read(account, msg_id):
    service = get_service(account)
    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}

    print(f"[Account: {account}]")
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


def cmd_send(account, to, subject, body, dry_run=False):
    if dry_run:
        print(f"[{account}] DRY RUN — would send:")
        print(f"  To: {to}")
        print(f"  Subject: {subject}")
        print(f"  Body: {body[:200]}...")
        return

    service = get_service(account)
    message = MIMEText(body)
    message["to"] = to
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"[{account}] Sent! Message ID: {sent['id']}")
    return sent


def cmd_list_accounts():
    """List all accounts that have a token file in this directory."""
    tokens = sorted(DIR.glob("gmail_*_token.json"))
    if not tokens:
        print("No accounts authorized yet. Run with --account <name> --auth to add one.")
        return
    print(f"Found {len(tokens)} authorized account(s):")
    for t in tokens:
        # Extract name from gmail_<name>_token.json
        name = t.stem.replace("gmail_", "").replace("_token", "")
        print(f"  - {name}  (token: {t.name})")


def main():
    parser = argparse.ArgumentParser(description="Gmail API helper for /cos (multi-account)")
    parser.add_argument("--account", default="default",
                        help="Account label (e.g. work, personal, mints). Default: 'default'")
    parser.add_argument("--auth", action="store_true", help="Run OAuth flow for this account")
    parser.add_argument("--profile", action="store_true", help="Show account info")
    parser.add_argument("--list-accounts", action="store_true",
                        help="List all authorized accounts in this directory")
    parser.add_argument("--search", type=str, help="Gmail search query")
    parser.add_argument("--read", type=str, help="Read message by ID")
    parser.add_argument("--send", action="store_true", help="Send an email")
    parser.add_argument("--to", type=str, help="Recipient (with --send)")
    parser.add_argument("--subject", type=str, help="Subject (with --send)")
    parser.add_argument("--body", type=str, help="Body (or pipe via stdin)")
    parser.add_argument("--dry-run", action="store_true", help="Preview send without sending")
    parser.add_argument("--max", type=int, default=10, help="Max search results")
    args = parser.parse_args()

    if args.list_accounts:
        cmd_list_accounts()
        return

    if args.auth:
        get_credentials(args.account)
        cmd_profile(args.account)
        return

    if args.profile:
        cmd_profile(args.account)
    elif args.search:
        cmd_search(args.account, args.search, args.max)
    elif args.read:
        cmd_read(args.account, args.read)
    elif args.send:
        if not args.to or not args.subject:
            print("Error: --to and --subject required for --send")
            sys.exit(1)
        body = args.body if args.body else sys.stdin.read()
        cmd_send(args.account, args.to, args.subject, body, dry_run=args.dry_run)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
