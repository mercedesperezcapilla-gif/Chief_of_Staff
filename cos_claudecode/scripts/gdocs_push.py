#!/usr/bin/env python3
"""Push a markdown file to a Google Doc with formatting.

Two modes:

  1. UPDATE EXISTING DOC (default) — clears and replaces the contents of a
     specific Doc by ID. Useful for a "shared working draft" pattern where you
     iterate on content locally as markdown and re-push to the same Doc each
     time so a collaborator (or you on another device) reads/edits in Docs.

  2. CREATE NEW DOC — creates a fresh Doc in your Drive, pushes the markdown
     into it, and prints the URL. Useful for one-shot publishing (a meeting
     prep doc, a column draft, a project brief).

Markdown supported:
    # H1                    → HEADING_1
    ## H2                   → HEADING_2
    ### H3                  → HEADING_3
    **bold**                → bold text
    [text](url)             → linked text
    - bullet                → bulleted list item
    1. item                 → numbered list item
    ---                     → horizontal rule
    Regular text            → NORMAL_TEXT paragraphs

Usage:
    # One-time OAuth (opens browser)
    python gdocs_push.py --auth

    # Update an existing Doc by ID
    python gdocs_push.py draft.md --doc-id 1abc...xyz

    # Create a new Doc and push to it
    python gdocs_push.py draft.md --create --title "My Doc Title"

    # Create + share with a specific email as writer
    python gdocs_push.py draft.md --create --title "My Doc" --share you@example.com

    # Just clear an existing doc (no push)
    python gdocs_push.py --clear --doc-id 1abc...xyz

Files:
    Reads:  ./oauth_credentials.json   (your Google OAuth client credentials)
    Writes: ./gdocs_token.json         (cached refresh token — NEVER COMMIT)

Why this is here:
    Lets /cos write structured artifacts to Drive (drafts, briefs, prep docs)
    so they're available everywhere Google Docs is — including mobile, where
    Docs is the easiest review surface. Pair this with /cos skill instructions
    that auto-publish certain artifact types (e.g., "when I lock a meeting prep,
    push it to a Doc and share with attendees").
"""

import argparse
import re
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]

DIR = Path(__file__).parent
CREDS_FILE = DIR / "oauth_credentials.json"
TOKEN_FILE = DIR / "gdocs_token.json"


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


# ---------------------------------------------------------------------------
# Doc create + share (Drive API)
# ---------------------------------------------------------------------------

def create_doc(drive, title, folder_id=None, share_with=None):
    """Create an empty Google Doc, optionally inside a Drive folder, and
    optionally share with one or more email addresses (writer access)."""
    file_meta = {"name": title, "mimeType": "application/vnd.google-apps.document"}
    if folder_id:
        file_meta["parents"] = [folder_id]
    doc = drive.files().create(body=file_meta, fields="id,webViewLink").execute()
    doc_id = doc["id"]
    doc_url = doc["webViewLink"]

    if share_with:
        if isinstance(share_with, str):
            share_with = [share_with]
        for email in share_with:
            drive.permissions().create(
                fileId=doc_id,
                body={"type": "user", "role": "writer", "emailAddress": email},
                sendNotificationEmail=False,
            ).execute()

    return doc_id, doc_url


# ---------------------------------------------------------------------------
# Doc state helpers
# ---------------------------------------------------------------------------

def get_doc_length(docs, doc_id):
    doc = docs.documents().get(documentId=doc_id).execute()
    body = doc.get("body", {})
    content = body.get("content", [])
    if content:
        return content[-1].get("endIndex", 1)
    return 1


def clear_doc(docs, doc_id):
    end_index = get_doc_length(docs, doc_id)
    if end_index <= 2:
        return
    docs.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{"deleteContentRange": {
            "range": {"startIndex": 1, "endIndex": end_index - 1}
        }}]},
    ).execute()


# ---------------------------------------------------------------------------
# Markdown parser
# ---------------------------------------------------------------------------

def parse_markdown(md_text):
    """Parse markdown into a list of block elements.

    Each block is a dict:
        {"type": "heading1"|"heading2"|"heading3"|"paragraph"|"bullet"|"numbered"|"hr",
         "runs": [{"text": str, "bold": bool, "link": str|None}, ...]}
    """
    blocks = []
    lines = md_text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]

        if re.match(r"^---+\s*$", line):
            blocks.append({"type": "hr", "runs": []})
            i += 1
            continue

        m = re.match(r"^#\s+(.+)$", line)
        if m:
            blocks.append({"type": "heading1", "runs": parse_inline(m.group(1))})
            i += 1
            continue

        m = re.match(r"^##\s+(.+)$", line)
        if m:
            blocks.append({"type": "heading2", "runs": parse_inline(m.group(1))})
            i += 1
            continue

        m = re.match(r"^###\s+(.+)$", line)
        if m:
            blocks.append({"type": "heading3", "runs": parse_inline(m.group(1))})
            i += 1
            continue

        m = re.match(r"^\d+[.)]\s+(.+)$", line)
        if m:
            num_text = m.group(1)
            while i + 1 < len(lines):
                nxt = lines[i + 1]
                if (nxt.strip() == "" or re.match(r"^\d+[.)]\s+", nxt) or
                        re.match(r"^[-*]\s+", nxt) or re.match(r"^#{1,3}\s+", nxt) or
                        re.match(r"^---+\s*$", nxt)):
                    break
                num_text += " " + nxt.strip()
                i += 1
            blocks.append({"type": "numbered", "runs": parse_inline(num_text)})
            i += 1
            continue

        m = re.match(r"^[-*]\s+(.+)$", line)
        if m:
            bullet_text = m.group(1)
            while i + 1 < len(lines):
                nxt = lines[i + 1]
                if (nxt.strip() == "" or re.match(r"^[-*]\s+", nxt) or
                        re.match(r"^\d+[.)]\s+", nxt) or re.match(r"^#{1,3}\s+", nxt) or
                        re.match(r"^---+\s*$", nxt)):
                    break
                bullet_text += " " + nxt.strip()
                i += 1
            blocks.append({"type": "bullet", "runs": parse_inline(bullet_text)})
            i += 1
            continue

        if line.strip() == "":
            i += 1
            continue

        para_text = line
        while i + 1 < len(lines):
            nxt = lines[i + 1]
            if (nxt.strip() == "" or re.match(r"^[-*]\s+", nxt) or
                    re.match(r"^\d+[.)]\s+", nxt) or re.match(r"^#{1,3}\s+", nxt) or
                    re.match(r"^---+\s*$", nxt)):
                break
            para_text += " " + nxt.strip()
            i += 1
        blocks.append({"type": "paragraph", "runs": parse_inline(para_text)})
        i += 1

    return blocks


def parse_inline(text):
    """Parse inline markdown (bold, links) into runs."""
    runs = []
    pattern = r"(\*\*(.+?)\*\*)|(\[([^\]]+)\]\(([^)]+)\))"
    last_end = 0

    for m in re.finditer(pattern, text):
        if m.start() > last_end:
            plain = text[last_end:m.start()]
            if plain:
                runs.append({"text": plain, "bold": False, "link": None})

        if m.group(2):  # **bold**
            bold_text = m.group(2)
            link_in_bold = re.search(r"\[([^\]]+)\]\(([^)]+)\)", bold_text)
            if link_in_bold:
                before = bold_text[:link_in_bold.start()]
                if before:
                    runs.append({"text": before, "bold": True, "link": None})
                runs.append({"text": link_in_bold.group(1), "bold": True, "link": link_in_bold.group(2)})
                after = bold_text[link_in_bold.end():]
                if after:
                    runs.append({"text": after, "bold": True, "link": None})
            else:
                runs.append({"text": bold_text, "bold": True, "link": None})
        elif m.group(4):  # [text](url)
            runs.append({"text": m.group(4), "bold": False, "link": m.group(5)})

        last_end = m.end()

    if last_end < len(text):
        remaining = text[last_end:]
        if remaining:
            runs.append({"text": remaining, "bold": False, "link": None})

    if not runs:
        runs.append({"text": text, "bold": False, "link": None})

    return runs


# ---------------------------------------------------------------------------
# Block → Docs API request mapping
# ---------------------------------------------------------------------------

def blocks_to_requests(blocks):
    """Convert parsed blocks into Google Docs API batchUpdate requests."""
    requests = []
    formatting = []
    idx = 1

    for block in blocks:
        if block["type"] == "hr":
            requests.append({"insertText": {"location": {"index": idx}, "text": "\n"}})
            formatting.append({
                "updateParagraphStyle": {
                    "range": {"startIndex": idx, "endIndex": idx + 1},
                    "paragraphStyle": {
                        "borderBottom": {
                            "color": {"color": {"rgbColor": {"red": 0.8, "green": 0.8, "blue": 0.8}}},
                            "width": {"magnitude": 1, "unit": "PT"},
                            "padding": {"magnitude": 8, "unit": "PT"},
                            "dashStyle": "SOLID",
                        }
                    },
                    "fields": "borderBottom",
                }
            })
            idx += 1
            continue

        full_text = "".join(run["text"] for run in block["runs"]) + "\n"
        # Visual paragraph spacing (skip for bullets — tight list spacing — and
        # headings, which have their own built-in spacing).
        if block["type"] == "paragraph":
            full_text += "\n"

        requests.append({"insertText": {"location": {"index": idx}, "text": full_text}})

        run_start = idx
        for run in block["runs"]:
            run_end = run_start + len(run["text"])
            if run["bold"]:
                formatting.append({
                    "updateTextStyle": {
                        "range": {"startIndex": run_start, "endIndex": run_end},
                        "textStyle": {"bold": True},
                        "fields": "bold",
                    }
                })
            if run["link"]:
                formatting.append({
                    "updateTextStyle": {
                        "range": {"startIndex": run_start, "endIndex": run_end},
                        "textStyle": {
                            "link": {"url": run["link"]},
                            "foregroundColor": {"color": {"rgbColor": {"red": 0.067, "green": 0.333, "blue": 0.8}}},
                            "underline": True,
                        },
                        "fields": "link,foregroundColor,underline",
                    }
                })
            run_start = run_end

        para_end = idx + len(full_text)
        style_map = {
            "heading1": "HEADING_1",
            "heading2": "HEADING_2",
            "heading3": "HEADING_3",
        }
        if block["type"] in style_map:
            formatting.append({
                "updateParagraphStyle": {
                    "range": {"startIndex": idx, "endIndex": para_end},
                    "paragraphStyle": {"namedStyleType": style_map[block["type"]]},
                    "fields": "namedStyleType",
                }
            })
        elif block["type"] == "numbered":
            formatting.append({
                "createParagraphBullets": {
                    "range": {"startIndex": idx, "endIndex": para_end},
                    "bulletPreset": "NUMBERED_DECIMAL_ALPHA_ROMAN",
                }
            })
        elif block["type"] == "bullet":
            formatting.append({
                "createParagraphBullets": {
                    "range": {"startIndex": idx, "endIndex": para_end},
                    "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                }
            })

        idx = para_end

    return requests + formatting


def push_to_doc(docs, doc_id, md_text):
    clear_doc(docs, doc_id)

    blocks = parse_markdown(md_text)
    if not blocks:
        print("No content to push.")
        return

    requests = blocks_to_requests(blocks)
    if not requests:
        print("No requests generated.")
        return

    # Batch in 100s to stay under per-request limits.
    BATCH = 100
    for i in range(0, len(requests), BATCH):
        docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": requests[i:i + BATCH]}
        ).execute()

    print(f"Pushed {len(blocks)} blocks ({len(requests)} API requests).")
    print(f"https://docs.google.com/document/d/{doc_id}/edit")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Push markdown to Google Docs")
    parser.add_argument("file", nargs="?", help="Markdown file to push")
    parser.add_argument("--auth", action="store_true", help="Run OAuth flow now")
    parser.add_argument("--clear", action="store_true", help="Clear an existing doc (with --doc-id)")
    parser.add_argument("--doc-id", help="Update an existing doc with this ID")
    parser.add_argument("--create", action="store_true", help="Create a new doc instead of updating")
    parser.add_argument("--title", help="Title for new doc (with --create)")
    parser.add_argument("--folder-id", help="Drive folder ID to put new doc in (with --create)")
    parser.add_argument("--share", action="append",
                        help="Email to share new doc with (writer); repeat for multiple")
    args = parser.parse_args()

    if args.auth:
        get_credentials()
        print(f"Authenticated. Token saved to {TOKEN_FILE}")
        return

    creds = get_credentials()
    docs = build("docs", "v1", credentials=creds)
    drive = build("drive", "v3", credentials=creds)

    # Clear-only path
    if args.clear:
        if not args.doc_id:
            parser.error("--clear requires --doc-id")
        clear_doc(docs, args.doc_id)
        print("Doc cleared.")
        return

    # Need a markdown file from here on
    if not args.file:
        parser.error("Provide a markdown file (or use --auth / --clear)")

    md_path = Path(args.file)
    if not md_path.exists():
        print(f"File not found: {md_path}", file=sys.stderr)
        sys.exit(1)
    md_text = md_path.read_text()

    # Create-new-doc path
    if args.create:
        title = args.title or md_path.stem
        doc_id, doc_url = create_doc(drive, title, folder_id=args.folder_id, share_with=args.share)
        print(f"Created: {doc_url}")
        push_to_doc(docs, doc_id, md_text)
        return

    # Update-existing-doc path
    if not args.doc_id:
        parser.error("Provide --doc-id (to update existing) or --create (to make new)")
    push_to_doc(docs, args.doc_id, md_text)


if __name__ == "__main__":
    main()
