#!/usr/bin/env python3
"""Sync session_log.yaml between a local /cos directory and a GitHub repo.

This is the glue that makes /cos work across multiple devices. Pattern:

    Mac (~/path/to/cos/session_log.yaml)
              ↕
    This script (merge + dedup + age-out)
              ↕
    GitHub (private repo containing session_log.yaml)
              ↕
    Other devices (iPad, second Mac, mobile via SSH, etc.)

The script does five things in order:
  1. git pull from the GitHub repo (latest entries from other devices)
  2. Load both the local and the repo session_log.yaml
  3. Merge + deduplicate by (timestamp + first 80 chars of note)
  4. Split into "active" (last 14 days) and "archive" (older), saving each
     to the correct file in BOTH locations
  5. (Optional) Refresh gcal_snapshot.json + tasks_snapshot.json so other
     devices have current data, then git push everything

Usage:
    python3 sync_session_log.py            # full merge + push
    python3 sync_session_log.py --dry-run  # show what would change

Setup:
    1. Create a PRIVATE GitHub repo (named anything — e.g. "myCOS")
    2. Clone it to your laptop
    3. Place this script in that cloned repo's root
    4. Edit LOCAL_LOG_CANDIDATES below to point at where your /cos session_log
       lives on this machine. Add other paths if you sync between multiple
       Macs / devices.
    5. Run it once to verify pull + push work

Optional (Step 8 of SETUP.md): wire this into a launchd / cron job so it
runs every 30 min during work hours.
"""

import argparse
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yaml

# Script dir = the repo dir (so this script just works wherever you place it)
REPO_DIR = Path(__file__).parent.resolve()
REPO_LOG = REPO_DIR / "session_log.yaml"
REPO_ARCHIVE = REPO_DIR / "session_log_archive.yaml"

# Local /cos session log location(s) — edit for your setup.
# The script picks the FIRST candidate that exists. On a device that doesn't
# have a local /cos directory (e.g. a remote VM that only consumes the synced
# data), leave the list empty or pointing at a non-existent path; the script
# will still pull/push the GitHub copy.
LOCAL_LOG_CANDIDATES = [
    Path.home() / "Desktop" / "python" / "cos" / "session_log.yaml",
    # Add more candidates if you sync across multiple machines:
    # Path.home() / "Documents" / "cos" / "session_log.yaml",
]
LOCAL_LOG = next((p for p in LOCAL_LOG_CANDIDATES if p.exists()), None)

# Entries older than this go to the archive file (still preserved, just
# rotated out of the actively-loaded log so /cos stays fast).
MAX_AGE_DAYS = 14


def load_yaml(path):
    if not path.exists():
        return []
    with open(path) as f:
        return yaml.safe_load(f) or []


def save_yaml(path, entries):
    with open(path, "w") as f:
        yaml.dump(entries, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def entry_key(e):
    """Dedup key: timestamp + first 80 chars of note."""
    ts = str(e.get("ts", ""))
    note = str(e.get("note", ""))[:80]
    return f"{ts}|{note}"


def merge_logs(local_entries, repo_entries):
    """Merge two lists, dedupe, sort by timestamp.

    Returns (active, archived) — active is the last MAX_AGE_DAYS of entries,
    archived is everything older.
    """
    seen = set()
    merged = []

    for e in local_entries + repo_entries:
        key = entry_key(e)
        if key not in seen:
            seen.add(key)
            merged.append(e)

    def sort_key(e):
        try:
            return datetime.fromisoformat(str(e.get("ts", "2000-01-01")))
        except (ValueError, TypeError):
            return datetime.min

    merged.sort(key=sort_key)

    cutoff = datetime.now() - timedelta(days=MAX_AGE_DAYS)
    active = []
    archived = []
    for e in merged:
        try:
            dt = datetime.fromisoformat(str(e.get("ts", "")))
            if dt >= cutoff:
                active.append(e)
            else:
                archived.append(e)
        except (ValueError, TypeError):
            # Entries with malformed timestamps default to active (don't lose them)
            active.append(e)

    return active, archived


def git_pull(repo_dir):
    subprocess.run(
        ["git", "pull", "--rebase", "origin", "main"],
        cwd=repo_dir, capture_output=True, text=True, timeout=30,
    )


def refresh_snapshots(repo_dir):
    """Run gcal_sync.py and tasks_sync.py if they exist next to this script.

    These produce the JSON snapshots that other devices read when they don't
    have direct API access. Failure is non-fatal — the script logs and moves on.
    """
    for script, label, args in [
        ("gcal_sync.py", "Calendar", ["--days", "7"]),
        ("tasks_sync.py", "Tasks", []),
    ]:
        script_path = repo_dir / script
        if not script_path.exists():
            print(f"  {label}: script not found, skipping")
            continue
        result = subprocess.run(
            [sys.executable, str(script_path)] + args,
            cwd=repo_dir, capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            print(f"  {label}: snapshot refreshed")
        else:
            print(f"  {label}: failed — {result.stderr[:100]}")


def git_push(repo_dir):
    """Stage all sync artifacts, commit, push. Skip if nothing changed."""
    subprocess.run(
        ["git", "add",
         "session_log.yaml", "session_log_archive.yaml",
         "gcal_snapshot.json", "tasks_snapshot.json"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    if not status.stdout.strip():
        print("  No changes to push.")
        return
    subprocess.run(
        ["git", "commit", "-m",
         f"sync {datetime.now().strftime('%Y-%m-%d %H:%M')}: session log + snapshots"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    push = subprocess.run(
        ["git", "push", "origin", "main"],
        cwd=repo_dir, capture_output=True, text=True, timeout=30,
    )
    if push.returncode == 0:
        print("  Pushed to GitHub.")
    else:
        print(f"  Push failed: {push.stderr[:200]}")


def main():
    parser = argparse.ArgumentParser(
        description="Sync /cos session_log between local and GitHub"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing or pushing")
    args = parser.parse_args()

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"Session log sync — {now}")

    print("  Pulling from GitHub...")
    git_pull(REPO_DIR)

    local = load_yaml(LOCAL_LOG) if LOCAL_LOG else []
    repo = load_yaml(REPO_LOG)
    if LOCAL_LOG:
        print(f"  Local: {len(local)} entries | Repo: {len(repo)} entries")
    else:
        print(f"  Local: not found (sync-only device) | Repo: {len(repo)} entries")

    active, newly_archived = merge_logs(local, repo)
    print(f"  Active: {len(active)} entries | Newly archived: {len(newly_archived)}")

    existing_archive = load_yaml(REPO_ARCHIVE)
    archive_seen = {entry_key(e) for e in existing_archive}
    for e in newly_archived:
        if entry_key(e) not in archive_seen:
            existing_archive.append(e)
            archive_seen.add(entry_key(e))
    existing_archive.sort(key=lambda e: str(e.get("ts", "")))
    print(f"  Total archive: {len(existing_archive)} entries")

    if args.dry_run:
        print(f"  [dry-run] Would write {len(active)} active, {len(existing_archive)} archive")
        return

    save_yaml(REPO_LOG, active)
    save_yaml(REPO_ARCHIVE, existing_archive)
    if LOCAL_LOG:
        save_yaml(LOCAL_LOG, active)
        local_archive = LOCAL_LOG.parent / "session_log_archive.yaml"
        save_yaml(local_archive, existing_archive)
        print("  Written to both locations.")
    else:
        print("  Written to repo.")

    print("  Refreshing snapshots...")
    refresh_snapshots(REPO_DIR)

    git_push(REPO_DIR)
    print("Done.")


if __name__ == "__main__":
    main()
