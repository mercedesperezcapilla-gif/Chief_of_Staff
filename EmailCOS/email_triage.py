#!/usr/bin/env python3
"""Gmail Email Triage Agent

Classifies unread Gmail emails into READ, SKIM, or SKIP categories using
a three-layer approach: rule-based auto-skip, rule-based auto-read, and
AI-powered classification for everything else.

Usage:
    python email_triage.py              # Process last 24 hours
    python email_triage.py --hours 48   # Process last 48 hours
    python email_triage.py --dry-run    # Classify but don't apply labels
    python email_triage.py --stats      # Show recent run statistics
    python email_triage.py --auth       # One-time: authorize Gmail in browser
"""

import argparse
import base64
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# CONFIGURATION — customize these for your setup
# ---------------------------------------------------------------------------

DIR = Path(__file__).parent
OAUTH_CREDENTIALS_FILE = DIR / "oauth_credentials.json"  # Google OAuth client JSON
TOKEN_FILE = DIR / "gmail_token.json"                     # Auto-generated after auth
STATS_FILE = DIR / "triage_stats.json"                    # Run history
CONFIG_FILE = DIR / "config.json"                         # Your personal config
TZ = ZoneInfo("America/Los_Angeles")                      # Change to your timezone

# Gmail API scopes — modify allows us to add labels and archive
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.readonly",
]

# Gmail label names we create/manage
LABEL_READ = "Triage/Read This"
LABEL_SKIM = "Triage/Skim"
LABEL_SKIP = "Triage/Skip"

# ---------------------------------------------------------------------------
# LAYER 1: AUTO-SKIP rules (no AI needed)
# ---------------------------------------------------------------------------

# Marketing / bulk sender domains — emails from these are almost never personal
SKIP_DOMAINS = {
    "mailchimp.com", "hubspot.com", "convertkit.com", "constantcontact.com",
    "sendgrid.net", "mailgun.com", "amazonses.com", "sendinblue.com",
    "klaviyo.com", "drip.com", "activecampaign.com", "aweber.com",
    "campaign-monitor.com", "mailerlite.com", "getresponse.com",
    "intercom.io", "customer.io", "braze.com", "iterable.com",
    "sailthru.com", "responsys.com", "exacttarget.com", "pardot.com",
    "marketo.com",
}

# Noreply-style sender prefixes — automated, no human on the other end
SKIP_SENDER_PREFIXES = {
    "noreply", "no-reply", "notification", "notifications", "alerts",
    "alert", "marketing", "promo", "deals", "offers", "mailer-daemon",
    "donotreply", "do-not-reply", "info", "support", "news", "newsletter",
    "updates", "billing", "receipts", "invoice",
    "calendar-notification", "calendar-noreply", "calendar-server",
}

# Full sender addresses to always skip
SKIP_SENDER_ADDRESSES = {
    "calendar-notification@google.com",
    "calendar-noreply@google.com",
}

# Subject patterns that indicate automated/transactional email
SKIP_SUBJECT_PATTERNS = [
    r"unsubscribe",
    r"order\s+(confirmation|confirmed|shipped|delivered)",
    r"shipping\s+(notification|update|confirmation)",
    r"delivery\s+(notification|update|confirmation|attempt)",
    r"your\s+statement",
    r"account\s+alert",
    r"password\s+reset",
    r"verify\s+your\s+(email|account)",
    r"confirm\s+your\s+(email|account|address)",
    r"reset\s+your\s+password",
    r"two.?factor",
    r"verification\s+code",
    r"security\s+code",
    r"sign.?in\s+(attempt|alert)",
    r"login\s+(attempt|alert|notification)",
    r"^(updated|canceled|cancelled)\s+(event|invitation)",
    r"^(accepted|declined|tentative):",
    r"^new\s+event:",
]

# Compile subject patterns for performance
_SKIP_SUBJECT_RE = re.compile(
    "|".join(SKIP_SUBJECT_PATTERNS), re.IGNORECASE
)

# ---------------------------------------------------------------------------
# LAYER 2: AUTO-READ rules (no AI needed)
# Loaded from config.json — see README for setup
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Load user config from config.json."""
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}

# ---------------------------------------------------------------------------
# LAYER 3: AI classification prompt
# ---------------------------------------------------------------------------

def build_ai_prompt(user_context: str) -> str:
    """Build the AI system prompt using the user's context."""
    return f"""You are an email triage assistant. Classify emails into exactly one category.

Context about the recipient:
{user_context}

Categories:
- READ: Personally relevant, requires action or response, from a real person who expects a reply, important business or personal matter.
- SKIM: Informational, potentially useful but not urgent. Newsletters they opted into, industry updates, event invites they might care about.
- SKIP: Marketing, automated notifications, irrelevant promotions, spam-like content, mass emails with no personal relevance.

Respond with ONLY one word: READ, SKIM, or SKIP. Nothing else."""


# ---------------------------------------------------------------------------
# GMAIL AUTH
# ---------------------------------------------------------------------------

def get_credentials(force_auth: bool = False) -> Credentials:
    """Load, refresh, or create Gmail OAuth2 credentials."""
    creds = None

    if TOKEN_FILE.exists() and not force_auth:
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json())
            return creds
        except Exception as e:
            print(f"[warn] Token refresh failed ({e}), re-authenticating...")

    # Run the full OAuth flow
    if not OAUTH_CREDENTIALS_FILE.exists():
        print(f"[error] OAuth credentials not found at {OAUTH_CREDENTIALS_FILE}")
        print("Download your OAuth client JSON from Google Cloud Console and save it as oauth_credentials.json")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(
        str(OAUTH_CREDENTIALS_FILE), SCOPES
    )
    creds = flow.run_local_server(port=8099)
    TOKEN_FILE.write_text(creds.to_json())
    print(f"[ok] Gmail token saved to {TOKEN_FILE}")
    return creds


def get_gmail_service(force_auth: bool = False):
    """Build and return a Gmail API service object."""
    creds = get_credentials(force_auth)
    return build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------------------
# LABEL MANAGEMENT
# ---------------------------------------------------------------------------

def ensure_labels(service) -> dict:
    """Create triage labels if they don't exist. Returns {name: label_id}."""
    existing = service.users().labels().list(userId="me").execute()
    label_map = {lbl["name"]: lbl["id"] for lbl in existing.get("labels", [])}

    result = {}
    for label_name in [LABEL_READ, LABEL_SKIM, LABEL_SKIP]:
        if label_name in label_map:
            result[label_name] = label_map[label_name]
        else:
            body = {
                "name": label_name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            }
            created = service.users().labels().create(
                userId="me", body=body
            ).execute()
            result[label_name] = created["id"]
            print(f"[ok] Created label '{label_name}' ({created['id']})")

    return result


# ---------------------------------------------------------------------------
# FETCH EMAILS
# ---------------------------------------------------------------------------

def fetch_unread_emails(service, hours: int = 24) -> list:
    """Fetch unread emails from the last `hours` hours."""
    after_ts = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp())
    query = f"is:unread after:{after_ts}"

    messages = []
    page_token = None

    while True:
        resp = service.users().messages().list(
            userId="me", q=query, pageToken=page_token, maxResults=200
        ).execute()
        messages.extend(resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    # Fetch full message details
    detailed = []
    for msg_stub in messages:
        msg = service.users().messages().get(
            userId="me", id=msg_stub["id"], format="full"
        ).execute()
        detailed.append(msg)

    return detailed


def extract_header(msg: dict, name: str) -> str:
    """Get a header value from a Gmail message, case-insensitive."""
    headers = msg.get("payload", {}).get("headers", [])
    name_lower = name.lower()
    for h in headers:
        if h["name"].lower() == name_lower:
            return h["value"]
    return ""


def extract_sender_email(msg: dict) -> str:
    """Extract the sender's email address from From header."""
    from_header = extract_header(msg, "From")
    _, email_addr = parseaddr(from_header)
    return email_addr.lower()


def extract_sender_domain(email_addr: str) -> str:
    """Extract domain from an email address."""
    if "@" in email_addr:
        return email_addr.split("@", 1)[1].lower()
    return ""


def extract_sender_local(email_addr: str) -> str:
    """Extract local part (before @) from an email address."""
    if "@" in email_addr:
        return email_addr.split("@", 1)[0].lower()
    return ""


def extract_body_text(msg: dict, max_chars: int = 300) -> str:
    """Extract plain-text body from a Gmail message, truncated."""
    payload = msg.get("payload", {})

    def _find_text_parts(part):
        """Recursively find text/plain parts."""
        texts = []
        mime = part.get("mimeType", "")
        if mime == "text/plain" and "data" in part.get("body", {}):
            raw = part["body"]["data"]
            decoded = base64.urlsafe_b64decode(raw).decode("utf-8", errors="replace")
            texts.append(decoded)
        for sub in part.get("parts", []):
            texts.extend(_find_text_parts(sub))
        return texts

    parts = _find_text_parts(payload)
    full_text = "\n".join(parts).strip()

    if not full_text:
        full_text = msg.get("snippet", "")

    text = full_text[:max_chars].strip()
    text = re.sub(r"\s+", " ", text)
    return text


# ---------------------------------------------------------------------------
# SENT-MAIL CACHE (for Layer 2 "replied-to" check)
# ---------------------------------------------------------------------------

def build_sent_contacts(service, days: int = 90) -> set:
    """Get set of email addresses you've sent to in the last N days."""
    after_ts = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    query = f"in:sent after:{after_ts}"

    contacts = set()
    page_token = None

    while True:
        resp = service.users().messages().list(
            userId="me", q=query, pageToken=page_token, maxResults=500
        ).execute()

        for msg_stub in resp.get("messages", []):
            msg = service.users().messages().get(
                userId="me", id=msg_stub["id"], format="metadata",
                metadataHeaders=["To", "Cc"]
            ).execute()
            for hdr_name in ["To", "Cc"]:
                val = extract_header(msg, hdr_name)
                if val:
                    for part in val.split(","):
                        _, addr = parseaddr(part.strip())
                        if addr:
                            contacts.add(addr.lower())

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return contacts


def check_thread_has_sent(service, thread_id: str) -> bool:
    """Check if you've sent a message in this thread (are a participant)."""
    try:
        thread = service.users().threads().get(
            userId="me", id=thread_id, format="minimal"
        ).execute()
        for msg in thread.get("messages", []):
            labels = msg.get("labelIds", [])
            if "SENT" in labels:
                return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# LAYER 1: AUTO-SKIP
# ---------------------------------------------------------------------------

def check_auto_skip(msg: dict, config: dict) -> tuple[bool, str]:
    """Check if email should be auto-skipped. Returns (should_skip, reason)."""
    # Load extra skip domains from config
    extra_skip_domains = set(config.get("skip_domains", []))
    all_skip_domains = SKIP_DOMAINS | extra_skip_domains

    # Check for Superhuman AI labels if configured
    label_ids = msg.get("labelIds", [])
    for skip_label in config.get("skip_label_ids", []):
        if skip_label in label_ids:
            return True, f"Skip label: {skip_label}"

    # Account/subscription change confirmations — SKIM, not SKIP
    subject = extract_header(msg, "Subject")
    _SKIM_SUBJECT_RE = re.compile(
        r"(has been (canceled|cancelled|paused|updated|changed|modified))"
        r"|(subscription (canceled|cancelled|changed|updated|confirmed))"
        r"|(plan (canceled|cancelled|changed|updated|downgraded|upgraded))"
        r"|(account (canceled|cancelled|closed|deactivated|updated|changed))"
        r"|(membership (canceled|cancelled|paused|ended))"
        r"|(cancellation confirm)"
        r"|(successfully (canceled|cancelled|unsubscribed))"
        r"|(your .{0,20} has been (removed|deleted|canceled|cancelled))",
        re.IGNORECASE
    )
    if _SKIM_SUBJECT_RE.search(subject):
        return False, ""  # Don't skip — will be classified as SKIM by Layer 3

    # List-Unsubscribe header (bulk/marketing mail)
    if extract_header(msg, "List-Unsubscribe"):
        return True, "Has List-Unsubscribe header"

    # Google collaboration notifications — NOT skip
    sender = extract_sender_email(msg)
    sender_lower = sender.lower()
    if sender_lower.startswith("drive-shares") or sender_lower.startswith("comments-noreply@docs") or "sharepoint" in sender_lower:
        return False, ""

    # Sender address exact match
    if sender_lower in SKIP_SENDER_ADDRESSES:
        return True, f"Skip sender: {sender}"

    # Sender domain match
    domain = extract_sender_domain(sender)
    if domain in all_skip_domains:
        return True, f"Skip domain: {domain}"

    # Noreply-style sender prefix
    local = extract_sender_local(sender)
    if local in SKIP_SENDER_PREFIXES:
        return True, f"Automated sender: {local}@"

    # Subject pattern match
    if _SKIP_SUBJECT_RE.search(subject):
        return True, "Skip subject pattern matched"

    return False, ""


# ---------------------------------------------------------------------------
# LAYER 2: AUTO-READ
# ---------------------------------------------------------------------------

def check_auto_read(
    msg: dict,
    service,
    sent_contacts: set,
    config: dict,
) -> tuple[bool, str]:
    """Check if email should be auto-read. Returns (should_read, reason)."""
    # Load from config
    important_domains = set(config.get("important_domains", []))
    important_keywords = set(config.get("important_keywords", []))

    sender = extract_sender_email(msg)
    domain = extract_sender_domain(sender)

    # New meeting invitation
    subject = extract_header(msg, "Subject")
    if re.match(r"^invitation:", subject, re.IGNORECASE):
        return True, "New meeting invitation"

    # Document/file shared with you
    if re.search(r"(shared with you|commented on|mentioned you in)", subject, re.IGNORECASE):
        return True, "Document shared/commented"

    # From important domain
    if domain in important_domains:
        return True, f"Important domain: {domain}"

    # Subject or sender contains important keyword
    subject_lower = subject.lower()
    sender_full = extract_header(msg, "From").lower()
    for kw in important_keywords:
        if kw in subject_lower or kw in sender_full:
            return True, f"Important keyword: {kw}"

    # Sender is someone you've emailed recently
    if sender in sent_contacts:
        return True, "Recent sent contact"

    # Thread you're already participating in
    thread_id = msg.get("threadId", "")
    if thread_id and check_thread_has_sent(service, thread_id):
        return True, "Active thread participant"

    # Direct reply (In-Reply-To present + you're in thread)
    in_reply_to = extract_header(msg, "In-Reply-To")
    if in_reply_to and thread_id:
        if check_thread_has_sent(service, thread_id):
            return True, "Reply to your email"

    return False, ""


# ---------------------------------------------------------------------------
# LAYER 3: AI CLASSIFICATION
# ---------------------------------------------------------------------------

def load_claude_api_key(config: dict) -> str:
    """Load Claude API key from config or environment."""
    import os

    # Check config first
    key = config.get("claude_api_key", "")
    if key:
        return key

    # Check environment variable
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return key

    # Check a creds file if specified
    creds_file = config.get("creds_file")
    if creds_file:
        creds_path = Path(creds_file).expanduser()
        if creds_path.exists():
            creds = json.loads(creds_path.read_text())
            key = creds.get("claude_api_key", "")
            if key:
                return key

    print("[error] Claude API key not found. Set it in config.json, ANTHROPIC_API_KEY env var, or a creds file.")
    sys.exit(1)


def classify_with_ai(sender: str, subject: str, body_snippet: str, api_key: str, system_prompt: str) -> str:
    """Use Claude Haiku to classify an email. Returns READ, SKIM, or SKIP."""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)

    user_message = (
        f"From: {sender}\n"
        f"Subject: {subject}\n"
        f"Body preview: {body_snippet}\n\n"
        "Classify this email."
    )

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        result = response.content[0].text.strip().upper()
        if result in ("READ", "SKIM", "SKIP"):
            return result
        for cat in ("READ", "SKIM", "SKIP"):
            if cat in result:
                return cat
        return "SKIM"
    except Exception as e:
        print(f"  [warn] AI classification failed: {e}")
        return "SKIM"


# ---------------------------------------------------------------------------
# APPLY LABELS
# ---------------------------------------------------------------------------

def apply_classification(
    service, msg_id: str, category: str, label_map: dict, dry_run: bool = False
) -> None:
    """Apply the triage label. Optionally archive SKIP emails (disabled by default)."""
    label_name = {
        "READ": LABEL_READ,
        "SKIM": LABEL_SKIM,
        "SKIP": LABEL_SKIP,
    }[category]

    label_id = label_map[label_name]

    if dry_run:
        return

    body = {"addLabelIds": [label_id]}

    # Uncomment to auto-archive SKIP emails once you trust the classification:
    # if category == "SKIP":
    #     body["removeLabelIds"] = ["INBOX"]

    service.users().messages().modify(
        userId="me", id=msg_id, body=body
    ).execute()


# ---------------------------------------------------------------------------
# STATS
# ---------------------------------------------------------------------------

def save_stats(stats: dict) -> None:
    """Append run stats to the stats file."""
    history = []
    if STATS_FILE.exists():
        try:
            history = json.loads(STATS_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            history = []

    history.append(stats)
    history = history[-100:]
    STATS_FILE.write_text(json.dumps(history, indent=2))


def show_stats() -> None:
    """Display recent triage run statistics."""
    if not STATS_FILE.exists():
        print("No triage stats found. Run the triage agent first.")
        return

    history = json.loads(STATS_FILE.read_text())
    if not history:
        print("No triage runs recorded.")
        return

    print(f"\n{'='*60}")
    print(f"  EMAIL TRIAGE STATS — Last {len(history)} runs")
    print(f"{'='*60}\n")

    total_processed = sum(r.get("total", 0) for r in history)
    total_read = sum(r.get("READ", 0) for r in history)
    total_skim = sum(r.get("SKIM", 0) for r in history)
    total_skip = sum(r.get("SKIP", 0) for r in history)

    print(f"  Total emails processed:  {total_processed}")
    print(f"  READ:  {total_read:>5}  ({total_read/max(total_processed,1)*100:.1f}%)")
    print(f"  SKIM:  {total_skim:>5}  ({total_skim/max(total_processed,1)*100:.1f}%)")
    print(f"  SKIP:  {total_skip:>5}  ({total_skip/max(total_processed,1)*100:.1f}%)")

    print(f"\n  Recent runs:")
    print(f"  {'Date':<22} {'Total':>6} {'READ':>6} {'SKIM':>6} {'SKIP':>6}")
    print(f"  {'-'*52}")
    for run in history[-5:]:
        dt = run.get("timestamp", "?")
        print(
            f"  {dt:<22} {run.get('total',0):>6} "
            f"{run.get('READ',0):>6} {run.get('SKIM',0):>6} {run.get('SKIP',0):>6}"
        )

    print()


# ---------------------------------------------------------------------------
# MAIN TRIAGE PIPELINE
# ---------------------------------------------------------------------------

def run_triage(hours: int = 24, dry_run: bool = False) -> None:
    """Main triage pipeline."""
    config = load_config()

    print(f"\n{'='*60}")
    print(f"  EMAIL TRIAGE AGENT")
    print(f"  Processing unread emails from last {hours} hours")
    if dry_run:
        print(f"  ** DRY RUN — no labels will be applied **")
    print(f"{'='*60}\n")

    # Connect to Gmail
    print("[1/5] Connecting to Gmail API...")
    service = get_gmail_service()

    # Ensure labels exist
    if not dry_run:
        print("[2/5] Ensuring triage labels exist...")
        label_map = ensure_labels(service)
    else:
        label_map = {LABEL_READ: "dry", LABEL_SKIM: "dry", LABEL_SKIP: "dry"}
        print("[2/5] Skipping label setup (dry run)...")

    # Fetch unread emails
    print(f"[3/5] Fetching unread emails...")
    emails = fetch_unread_emails(service, hours)
    print(f"       Found {len(emails)} unread emails")

    if not emails:
        print("\nNo unread emails to process. Done!")
        return

    # Build sent contacts cache for Layer 2
    print("[4/5] Building sent contacts cache (last 90 days)...")
    sent_contacts = build_sent_contacts(service, days=90)
    print(f"       Found {len(sent_contacts)} unique sent contacts")

    # Load AI key and build prompt
    claude_key = load_claude_api_key(config)
    user_context = config.get("user_context", "A busy professional who receives a mix of personal and work emails.")
    system_prompt = build_ai_prompt(user_context)

    # Process each email
    print(f"[5/5] Classifying {len(emails)} emails...\n")
    results = {"READ": [], "SKIM": [], "SKIP": []}
    counters = Counter()

    for i, msg in enumerate(emails, 1):
        msg_id = msg["id"]
        sender = extract_sender_email(msg)
        subject = extract_header(msg, "Subject") or "(no subject)"
        short_subject = subject[:60] + ("..." if len(subject) > 60 else "")

        # Layer 1: Auto-Skip
        should_skip, skip_reason = check_auto_skip(msg, config)
        if should_skip:
            category = "SKIP"
            method = f"auto-skip: {skip_reason}"
        else:
            # Layer 2: Auto-Read
            should_read, read_reason = check_auto_read(msg, service, sent_contacts, config)
            if should_read:
                category = "READ"
                method = f"auto-read: {read_reason}"
            else:
                # Layer 3: AI Classification
                body_snippet = extract_body_text(msg, max_chars=300)
                category = classify_with_ai(sender, subject, body_snippet, claude_key, system_prompt)
                method = "AI"

        # Apply label
        apply_classification(service, msg_id, category, label_map, dry_run)

        # Record result
        results[category].append({
            "sender": sender,
            "subject": subject,
            "method": method,
        })
        counters[category] += 1

        icon = {"READ": ">>", "SKIM": "~ ", "SKIP": "  "}[category]
        print(f"  {icon} [{category:4}] {sender[:30]:<30}  {short_subject}")

    # Summary
    total = len(emails)
    print(f"\n{'='*60}")
    print(f"  TRIAGE SUMMARY")
    print(f"{'='*60}")
    print(f"  Total processed:  {total}")
    print(f"  READ:  {counters['READ']:>4}  — needs attention")
    print(f"  SKIM:  {counters['SKIM']:>4}  — informational")
    print(f"  SKIP:  {counters['SKIP']:>4}  — labeled (stays in inbox for review)")

    if results["READ"]:
        print(f"\n  --- READ THIS ({len(results['READ'])}) ---")
        for item in results["READ"]:
            subj = item['subject'][:55] + ("..." if len(item['subject']) > 55 else "")
            print(f"    From: {item['sender']}")
            print(f"    Subj: {subj}")
            print(f"    Why:  {item['method']}")
            print()

    if dry_run:
        print("  ** DRY RUN — no changes were made **")

    print()

    stats = {
        "timestamp": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "hours_scanned": hours,
        "total": total,
        "READ": counters["READ"],
        "SKIM": counters["SKIM"],
        "SKIP": counters["SKIP"],
        "dry_run": dry_run,
    }
    save_stats(stats)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Gmail Email Triage Agent — classify emails as READ, SKIM, or SKIP"
    )
    parser.add_argument(
        "--hours", type=int, default=24,
        help="How many hours back to scan (default: 24)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Classify emails but don't apply labels or archive"
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Show classification stats from recent runs"
    )
    parser.add_argument(
        "--auth", action="store_true",
        help="Run Gmail OAuth flow (one-time setup)"
    )
    args = parser.parse_args()

    if args.auth:
        get_credentials(force_auth=True)
        print("Gmail authentication complete.")
        return

    if args.stats:
        show_stats()
        return

    run_triage(hours=args.hours, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
