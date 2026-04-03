# Gmail Email Triage Agent

An AI-powered email triage agent that classifies your unread Gmail into **READ**, **SKIM**, or **SKIP** using a three-layer approach. Built to help busy people reclaim their inbox without missing what matters.

## How It Works

The agent processes emails through three layers, in order:

### Layer 1: Auto-Skip (rule-based, no AI)
Instantly skips emails that are almost never worth reading:
- Has a `List-Unsubscribe` header (marketing/bulk mail)
- From known marketing sender domains (mailchimp, hubspot, etc.)
- From `noreply@`, `notifications@`, etc.
- Subject matches transactional patterns (order confirmations, password resets, etc.)
- Calendar RSVPs and event updates

**Exception:** Account cancellation/change confirmations are NOT skipped — those get passed to Layer 3 as SKIM candidates.

### Layer 2: Auto-Read (rule-based, no AI)
Flags emails that always deserve attention:
- From your important domains (your company, kids' school, etc.)
- Contains important keywords you define
- From someone you've emailed in the last 90 days
- In a thread you're already participating in
- New meeting invitations
- Document shares / Google Docs comments

### Layer 3: AI Classification (Claude Haiku)
Everything that survives Layers 1 and 2 goes to Claude Haiku with context about who you are. The AI returns one word: READ, SKIM, or SKIP.

## Setup

### 1. Install dependencies

```bash
pip install google-auth google-auth-oauthlib google-api-python-client anthropic
```

### 2. Google Cloud OAuth credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project (or use an existing one)
3. Enable the **Gmail API**
4. Go to **Credentials** > **Create Credentials** > **OAuth 2.0 Client ID**
5. Application type: **Desktop app**
6. Download the JSON and save it as `oauth_credentials.json` in this directory

### 3. Create your config

```bash
cp config.example.json config.json
```

Edit `config.json` with your details:

```json
{
  "user_context": "Jane Doe is a product manager at Acme Corp and freelance UX consultant. She cares about: SaaS, design systems, accessibility, and her kids' school updates.",
  "important_domains": [
    "acmecorp.com",
    "myfreelance.co",
    "lincolnelementary.org"
  ],
  "important_keywords": [
    "lincoln elementary"
  ],
  "skip_domains": [
    "retailer-you-hate.com"
  ],
  "skip_label_ids": [],
  "claude_api_key": "sk-ant-..."
}
```

| Field | What it does |
|-------|-------------|
| `user_context` | Tells the AI who you are so it can judge relevance. Be specific. |
| `important_domains` | Emails from these domains always get READ. Your company, school, etc. |
| `important_keywords` | Subject/sender containing these words always get READ. |
| `skip_domains` | Extra domains to auto-skip beyond the built-in marketing list. |
| `skip_label_ids` | Gmail label IDs to treat as auto-skip (e.g., Superhuman AI labels). |
| `claude_api_key` | Your Anthropic API key. Can also use `ANTHROPIC_API_KEY` env var. |
| `creds_file` | Optional: path to a JSON file containing `claude_api_key`. |

### 4. Authenticate Gmail

```bash
python email_triage.py --auth
```

This opens a browser for Google OAuth. You only need to do this once — the token is saved locally.

## Usage

```bash
# Triage unread emails from the last 24 hours
python email_triage.py

# Scan the last 48 hours
python email_triage.py --hours 48

# Dry run — see classifications without applying labels
python email_triage.py --dry-run

# View stats from recent runs
python email_triage.py --stats
```

## What It Does to Your Gmail

- Creates three labels: `Triage/Read This`, `Triage/Skim`, `Triage/Skip`
- Applies one label to each unread email it processes
- **Does NOT archive or delete anything** by default
- All emails stay in your inbox — labels are additive

To enable auto-archiving of SKIP emails once you trust the classification, uncomment the `removeLabelIds` line in `apply_classification()`.

## Cost

Layer 3 uses Claude Haiku, which is very cheap. A typical run processing 50 emails with ~20 hitting the AI layer costs less than $0.01.

## Tips

- **Start with `--dry-run`** to see how it classifies before applying labels
- **Be specific in `user_context`** — the more the AI knows about you, the better it triages
- **Add domains liberally** to `important_domains` — false negatives (missing a READ) are worse than false positives
- **Run it on a schedule** with cron for hands-free triage:
  ```
  0 7,12,18 * * * cd /path/to/email_triage && python email_triage.py --hours 8
  ```

## License

MIT
