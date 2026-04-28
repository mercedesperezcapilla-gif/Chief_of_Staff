#!/usr/bin/env python3
"""/cos Slack bot — Socket Mode (no public server, no ngrok).

Lets you trigger /cos from Slack on your phone. Pattern:

    iPhone (Slack DM)
        ↓
    Slack Socket Mode (long-poll, no inbound port)
        ↓
    This script (running on your laptop or a VM)
        ↓
    Reads session_log.yaml, runs tasks_sync.py + gcal_sync.py,
    calls Claude with the result, posts the answer back as a DM

Why this matters: it's the same /cos brain you use in Claude Code,
but from your phone, on the go, without needing a Mac in front of you.

See SLACK.md for the full setup walkthrough (Slack app creation, tokens,
permission scopes, install).

Modes (sent as message text or /cos slash command):
    cos                     → today briefing
    cos today               → today briefing
    cos focus               → "what should I work on right now"
    cos week                → week overview
    cos update <note>       → log a session-log entry
    <plain question>        → answered using session log + tasks + calendar
    <plain statement>       → logged as an "update" entry

Files:
    Reads:  ./creds.json          (Slack tokens + Anthropic API key)
            ./session_log.yaml    (the /cos memory)
            ./tasks_snapshot.json + gcal_snapshot.json (auto-refreshed)
    Writes: ./session_log.yaml    (appends new updates)

Run:
    python3 cos_slack_bot.py

To keep it running unattended, see SLACK.md → "Running as a daemon."
"""

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
import yaml
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# ── Config (paths and timezone) ──────────────────────────────────────────────
DIR = Path(__file__).parent
CREDS_FILE = DIR / "creds.json"
SESSION_LOG = DIR / "session_log.yaml"
TASKS_SNAPSHOT = DIR / "tasks_snapshot.json"
GCAL_SNAPSHOT = DIR / "gcal_snapshot.json"
TASKS_SYNC = DIR / "tasks_sync.py"
GCAL_SYNC = DIR / "gcal_sync.py"

# Edit to your local timezone
TZ = ZoneInfo("America/Los_Angeles")

# ── Credentials ──────────────────────────────────────────────────────────────
# creds.json must contain (see SLACK.md for how to obtain each):
#   {
#     "anthropic_api_key": "sk-ant-...",
#     "slack_cos_app_token": "xapp-...",   # App-Level Token, scope: connections:write
#     "slack_cos_bot_token": "xoxb-..."    # Bot User OAuth Token
#   }

if not CREDS_FILE.exists():
    sys.exit(
        f"Missing {CREDS_FILE}. Create it with anthropic_api_key, "
        "slack_cos_app_token, and slack_cos_bot_token. See SLACK.md."
    )

_creds = json.loads(CREDS_FILE.read_text())
ANTHROPIC_API_KEY = _creds.get("anthropic_api_key")
APP_TOKEN = _creds.get("slack_cos_app_token")  # xapp-...
BOT_TOKEN = _creds.get("slack_cos_bot_token")  # xoxb-...

if not (ANTHROPIC_API_KEY and APP_TOKEN and BOT_TOKEN):
    sys.exit(
        "Missing one or more required creds. Need: anthropic_api_key, "
        "slack_cos_app_token, slack_cos_bot_token. See SLACK.md."
    )

app = App(token=BOT_TOKEN)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── System prompt — EDIT for your context ────────────────────────────────────
# This is the persona Claude uses when answering. Personalize it the way you
# personalized skill.md — your role, what your day looks like, what energy
# windows you keep, and any rules you want enforced consistently.

SYSTEM = """You are a personal Chief of Staff assistant in Slack.

Your user is a working executive at a software company who also runs a
household. Replace this paragraph with your own one-line bio so Claude
has the right context (e.g. role, company stage, team size, life rhythms
that matter — kids' ages, evening work block, fencing meets, etc.).

Rules:
- Slack markdown only: *bold*, _italic_, • bullets. No headers, no tables.
- Be direct, no preamble, max 400 words.
- For an `update`: acknowledge briefly, note if it closes an open intent,
  say what's next.
- For a question: answer directly using the session log + tasks + calendar.
- For `cos today`: lead with unresolved intents → today's meetings →
  open work windows → recommended focus.
- For `cos focus`: one direct "Work on X because Y" recommendation.
- Never invent commitments not in the source data.
"""


# ── Session log helpers ──────────────────────────────────────────────────────

def read_log(n=12) -> list:
    if not SESSION_LOG.exists():
        return []
    try:
        entries = yaml.safe_load(SESSION_LOG.read_text()) or []
        return entries[-n:] if isinstance(entries, list) else []
    except Exception:
        return []


def append_log(entry_type: str, note: str):
    now = datetime.now(TZ)
    entry = {"ts": now.strftime("%Y-%m-%dT%H:%M"), "type": entry_type, "note": note}
    entries = read_log(500)
    entries.append(entry)
    SESSION_LOG.write_text(
        yaml.dump(entries, default_flow_style=False, allow_unicode=True)
    )
    print(f"  logged [{entry_type}]: {note[:60]}")


# ── Refresh + load context ──────────────────────────────────────────────────

# Add task list names you want the bot to IGNORE (e.g. delegation lists for
# people who aren't you). Empty list means show all lists.
SKIP_TASK_LISTS: list[str] = []  # e.g. ["someone_else", "team-only"]


def get_context(days=1) -> dict:
    # Refresh snapshots before reading them
    for script, snapshot in [(TASKS_SYNC, TASKS_SNAPSHOT), (GCAL_SYNC, GCAL_SNAPSHOT)]:
        try:
            args = [sys.executable, str(script)]
            if script == GCAL_SYNC:
                args += ["--days", str(days)]
            subprocess.run(args, capture_output=True, timeout=60, cwd=str(DIR))
        except Exception:
            pass

    tasks_text = "unavailable"
    cal_text = "unavailable"
    now = datetime.now(TZ)

    if TASKS_SNAPSHOT.exists():
        try:
            snap = json.loads(TASKS_SNAPSHOT.read_text())
            lines = []
            for tl in snap.get("task_lists", []):
                name = tl.get("title", "")
                if any(s.lower() in name.lower() for s in SKIP_TASK_LISTS):
                    continue
                tasks = [t for t in tl.get("tasks", []) if t.get("title", "").strip()]
                if not tasks:
                    continue
                lines.append(f"\n{name} ({len(tasks)}):")
                for t in tasks[:4]:
                    flag = ""
                    due = t.get("due", "")
                    if due:
                        try:
                            d = datetime.fromisoformat(due.replace("Z", "+00:00"))
                            if d.date() < now.date():
                                flag = " ⚠️ OVERDUE"
                            elif d.date() == now.date():
                                flag = " 📅 TODAY"
                        except Exception:
                            pass
                    lines.append(f"  - {t['title']}{flag}")
            tasks_text = "\n".join(lines) or "none"
        except Exception:
            pass

    if GCAL_SNAPSHOT.exists():
        try:
            snap = json.loads(GCAL_SNAPSHOT.read_text())
            lines = []
            for evt in snap.get("events", []):
                if evt.get("all_day"):
                    lines.append(f"  ALL DAY: {evt['summary']}")
                else:
                    t = (evt.get("start") or "")[11:16]
                    tag = {"meeting": "MTG", "work_block": "BLOCK"}.get(
                        evt.get("type", ""), ""
                    )
                    lines.append(
                        f"  {t} [{tag}] {evt['summary']} ({evt.get('calendar', '')})"
                    )
            cal_text = "\n".join(lines) or "none"
        except Exception:
            pass

    log_text = "\n".join(
        f"[{e.get('ts', '')}] {e.get('type', '')}: {e.get('note', '')}"
        for e in read_log(10)
    ) or "empty"

    return {"log": log_text, "tasks": tasks_text, "calendar": cal_text}


# ── Claude call ──────────────────────────────────────────────────────────────

def ask_claude(mode: str, user_message: str, context: dict) -> str:
    now = datetime.now(TZ)
    prompt = f"""Now: {now.strftime('%A %B %d %Y — %I:%M %p %Z')}
Mode: {mode}
Message: {user_message}

SESSION LOG:
{context['log']}

CALENDAR:
{context['calendar']}

TASKS:
{context['tasks']}"""

    msg = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


# ── Message handling ─────────────────────────────────────────────────────────

def is_question(text: str) -> bool:
    text = text.strip().lower()
    return (
        text.endswith("?")
        or text.startswith((
            "what", "when", "how", "should", "which", "why", "where",
            "who", "can you", "give me", "show me",
        ))
    )


def handle_message(text: str, say) -> None:
    text = text.strip()
    if not text:
        return

    # Parse /cos commands sent as plain text (e.g. "cos focus" or "cos update X")
    lower = text.lower()
    cos_match = re.match(
        r"^/?cos\s*(today|focus|week|update|review)?\s*(.*)?$",
        lower,
        re.IGNORECASE,
    )

    if cos_match:
        mode = cos_match.group(1) or "today"
        note = cos_match.group(2) or ""
        if mode == "update" and note:
            append_log("update", note)
        say("_Thinking..._")
        context = get_context(days=7 if mode == "week" else 1)
        reply = ask_claude(mode, note or mode, context)
        say(reply)

    elif is_question(text):
        say("_On it..._")
        context = get_context()
        reply = ask_claude("question", text, context)
        say(reply)

    else:
        # Plain statement → log as update + quick ack
        append_log("update", text)
        say(f"✓ Logged: _{text[:80]}_")


# ── Slack event handlers ─────────────────────────────────────────────────────

@app.event("message")
def on_message(event, say):
    # Only respond to DMs (channel type "im"); ignore bot messages
    if event.get("bot_id") or event.get("subtype"):
        return
    handle_message(event.get("text", ""), say)


@app.command("/cos")
def on_slash_cos(ack, command, respond):
    ack()  # acknowledge to Slack immediately (3s deadline)
    handle_message(f"cos {command.get('text', '')}".strip(), respond)


# ── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"Anthropic API: {'✓' if ANTHROPIC_API_KEY else '✗ MISSING'}")
    print(f"Bot token:     {'✓' if BOT_TOKEN else '✗ MISSING'}")
    print(f"App token:     {'✓' if APP_TOKEN else '✗ MISSING'}")
    print("\nStarting /cos Slack bot (Socket Mode)...")
    print("DM the bot or use /cos in any channel where it's installed.\n")
    handler = SocketModeHandler(app, APP_TOKEN)
    handler.start()
