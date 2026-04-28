# Optional: /cos in Slack (mobile access)

Run /cos from Slack on your phone. Same brain as the Claude Code skill — just reachable from anywhere you have Slack.

This is optional. Skip this whole doc if you only use /cos at your desk.

---

## What you get

After this is set up, you can DM your bot from your phone:

```
You:  cos today
Bot:  *Today: Tue Apr 28 — 3 meetings + evening block*
      • 10:30 AM Standup ...
      ...

You:  cos update finished the proposal draft
Bot:  ✓ Logged: finished the proposal draft

You:  what's left on the book this week?
Bot:  Three things still in flight:
      • Mark Tavani follow-up (waiting on him until 5/11)
      ...
```

The bot reads the same `session_log.yaml`, the same `tasks_snapshot.json`, and the same `gcal_snapshot.json` your laptop /cos uses. Updates you log from Slack appear in your laptop /cos session log on the next sync.

---

## Architecture (Socket Mode — no public server needed)

```
iPhone Slack DM
     ↓
Slack server (long-polls your bot connection)
     ↓
Your laptop running cos_slack_bot.py
     ↓
Loads session_log.yaml + snapshots, calls Claude API,
posts answer back as a DM
```

**Socket Mode** is the easy path. You don't need ngrok, you don't deploy anything to a cloud, you don't open ports. The bot maintains a persistent outbound websocket to Slack, and Slack pushes events to it. It just runs as a Python process on your laptop.

Trade-offs:
- ✅ No public URL needed, no deployment
- ✅ Free (no infra cost)
- ✅ Tokens stay on your machine
- ❌ **Bot only works while your laptop is awake and the script is running.** This is the real annoyance — when your laptop sleeps, your phone messages get queued by Slack but nothing responds until you wake the Mac. Plan around this.
- ❌ Solo / single-user pattern (multi-user setups want HTTP mode + a deployed server)

> **If laptop-must-be-on is a dealbreaker, use [MOBILE.md](MOBILE.md) instead.** That pattern uses a GitHub-synced sync repo so your phone runs /cos directly with zero dependency on your desktop being awake. The Slack bot is best for people who want a *chat-style interface* on top of the mobile pattern, or who already keep a Mac awake all day; it's not the right pattern if your Mac sleeps when you leave the house.
>
> If you want 24/7 Slack access without keeping your laptop on, see "Advanced: HTTP mode" at the bottom — that requires deploying the bot to a small always-on host (Cloud Run / Fly.io / Railway / a Raspberry Pi). More setup, but it does solve the sleep problem.

---

## Step 1: Create the Slack app

1. Go to **https://api.slack.com/apps**
2. Click **Create New App** → **From scratch**
3. App Name: `cos-bot` (or whatever you want)
4. Pick your workspace
5. Click **Create App**

You're now on the app's settings page. Keep this tab open — you'll come back to several sections.

---

## Step 2: Enable Socket Mode + create the App-Level Token

1. Left sidebar → **Settings** → **Socket Mode**
2. Toggle **Enable Socket Mode** to ON
3. A modal asks you to create an **App-Level Token**:
   - Token Name: `cos-socket`
   - Scopes: add `connections:write`
4. Click **Generate**
5. **Copy the token** that starts with `xapp-...` — this is your `slack_cos_app_token`. You'll only see it once.

---

## Step 3: Add bot scopes (OAuth & Permissions)

1. Left sidebar → **Features** → **OAuth & Permissions**
2. Scroll to **Scopes** → **Bot Token Scopes**
3. Add each of these scopes one at a time:
   - `chat:write` — bot can send messages
   - `commands` — bot can register slash commands
   - `im:history` — bot can read DM history
   - `im:write` — bot can send DMs
   - (Optional) `app_mentions:read` — if you want to @-mention the bot in channels

---

## Step 4: Enable the Messages tab

1. Left sidebar → **Features** → **App Home**
2. Scroll to **Show Tabs** → toggle **Messages Tab** ON
3. Check the box for **Allow users to send Slash commands and messages from the messages tab**

This is what lets you DM the bot from any device.

---

## Step 5: Create the slash command

1. Left sidebar → **Features** → **Slash Commands**
2. Click **Create New Command**
3. Fill in:
   - **Command:** `/cos`
   - **Request URL:** leave EMPTY (Socket Mode doesn't need this)
   - **Short Description:** `Talk to your Chief of Staff`
   - **Usage Hint:** `today | focus | week | update <note>`
4. Click **Save**

---

## Step 6: Install the app to your workspace

1. Left sidebar → **Settings** → **Install App** → **Install to Workspace**
2. Review the requested scopes → **Allow**
3. **Copy the Bot User OAuth Token** that starts with `xoxb-...` — this is your `slack_cos_bot_token`

You should now have two tokens saved somewhere safe:
- `xapp-...` → `slack_cos_app_token` (App-Level, from Step 2)
- `xoxb-...` → `slack_cos_bot_token` (Bot User OAuth, from this step)

---

## Step 7: Add tokens + Anthropic key to creds.json

In your `<your_cos_dir>/` (the same place your `oauth_credentials.json` lives), edit or create `creds.json`:

```json
{
  "anthropic_api_key": "sk-ant-api03-...",
  "slack_cos_app_token": "xapp-1-A0...",
  "slack_cos_bot_token": "xoxb-1234..."
}
```

Get the Anthropic API key from https://console.anthropic.com → API Keys.

⚠️ **Add `creds.json` to your `.gitignore`** — never commit this file.

---

## Step 8: Install Python dependencies

```bash
pip install slack-bolt anthropic pyyaml
```

(These are also in `requirements.txt` if you set that up during the main /cos setup.)

---

## Step 9: Personalize the bot's persona

Open `scripts/cos_slack_bot.py` and find the `SYSTEM` constant near the top. Edit the placeholder paragraph to describe yourself the way you personalized `skill.md` — role, company, life rhythms that matter for context.

This is what Claude uses to phrase responses correctly. The default is a generic "working executive at a software company" — replace it with you.

Optional: edit `SKIP_TASK_LISTS` to filter out task lists you don't want surfaced (e.g. delegation lists for other people).

---

## Step 10: Run it

```bash
cd <your_cos_dir>
python3 scripts/cos_slack_bot.py
```

You should see:

```
Anthropic API: ✓
Bot token:     ✓
App token:     ✓

Starting /cos Slack bot (Socket Mode)...
DM the bot or use /cos in any channel where it's installed.
```

Now in Slack: find your bot in the sidebar (under **Apps** or **Direct Messages**), click it, and message it `cos today`. You should get a brief back within a few seconds.

---

## Step 11: (Recommended) Keep it running as a daemon

If you want the bot to stay reachable when you close your terminal, run it as a background service.

### macOS (launchd)

Create `~/Library/LaunchAgents/com.you.cos-slack-bot.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.you.cos-slack-bot</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/python3</string>
        <string>/Users/YOU/path/to/your_cos_dir/scripts/cos_slack_bot.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/YOU/path/to/your_cos_dir</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key>
    <string>/tmp/cos-slack-bot.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/cos-slack-bot.err</string>
</dict>
</plist>
```

Load it:
```bash
launchctl load ~/Library/LaunchAgents/com.you.cos-slack-bot.plist
```

The bot now starts at login and restarts if it crashes. Check `/tmp/cos-slack-bot.log` for output.

### Linux (systemd user service)

Create `~/.config/systemd/user/cos-slack-bot.service`:

```ini
[Unit]
Description=cos Slack bot (Socket Mode)
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/YOU/path/to/your_cos_dir
ExecStart=/usr/bin/python3 /home/YOU/path/to/your_cos_dir/scripts/cos_slack_bot.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

Enable + start:
```bash
systemctl --user daemon-reload
systemctl --user enable --now cos-slack-bot.service
journalctl --user -u cos-slack-bot.service -f   # tail logs
```

---

## Troubleshooting

**Bot doesn't respond at all** → Confirm the script is running and shows all three `✓` on startup. Confirm the workspace install completed (Step 6 — you should see the bot in your Slack sidebar).

**`invalid_auth` error** → Token typo in creds.json, or the bot was uninstalled from the workspace. Reinstall from Step 6.

**Bot responds in DM but not to `/cos` slash command** → Slash command may not be saved properly. Re-check Step 5 — the Request URL must be EMPTY for Socket Mode.

**Bot logs say "missing scope"** → A scope you're using requires reinstall. Add the scope in Step 3, then reinstall in Step 6.

**Bot can read tasks but not write them** → That's expected if you only have `tasks_token.json` (read scope) but not `tasks_write_token.json` (write scope). Run `python3 scripts/tasks_add.py --list "Test" --title "x"` once to trigger the write-scope OAuth flow.

---

## Advanced: HTTP mode (deployed server, multi-user)

If you outgrow the single-laptop pattern (e.g. you want the bot reachable when your laptop is asleep, or you want to share access with a small team), the alternative is to run a FastAPI server that handles Slack's HTTP webhooks instead of Socket Mode.

That pattern uses:
- A FastAPI app (instead of `slack-bolt` Socket Mode)
- A public HTTPS URL (Cloud Run, Fly.io, ngrok for testing)
- Slack slash command Request URL pointing at that public endpoint
- The same session log + snapshots, but accessed via API or a shared cloud bucket

This is out of scope for this template, but the conversion is roughly: replace the Socket Mode handler with `fastapi.FastAPI()` + `@app.post("/slack/commands")`, validate the Slack signing secret on each request, and respond within Slack's 3-second deadline by acking immediately and posting the real reply via `response_url` from a background task.

For most solo users, Socket Mode (this doc) is the right answer. Don't add infra you don't need.
