# Advanced: Multi-Skill Chains

`/cos` works on its own from day one. But once you have other Claude Code skills you use regularly — a standup prep skill, a 1:1 doc generator, a release-notes drafter — you can wire `/cos` to **trigger them automatically** so the right thing happens at the right time without you remembering.

This doc covers three patterns for cross-skill chains, with worked examples for a working executive at a software company who also runs a household. Adapt the example skill names to the ones you actually use.

---

## The pattern

Claude Code skills are independent. `/cos` doesn't have privileged access to call them — but it can:

1. **Detect a trigger condition** (today is Monday, a specific draft file exists, a particular update message was logged)
2. **Run the other skill BEFORE its own brief** (so the chained skill's output appears in context)
3. **Surface the chained output as part of the brief** (so you read both together)

You author this in `skill.md` as plain instructions to Claude. The harness doesn't run skills automatically; the LLM reads your rules and follows them.

---

## Flavor 1 — Time-triggered chains

Run another skill on a specific day or date pattern.

### Worked example: `/cos` auto-runs `/weekly-pulse` on Monday mornings

You're a VP of Engineering at a software company. Every Monday you want a snapshot of last week's PR throughput, on-call incident count, and team velocity before your standup. You've built `/weekly-pulse` for this. You want it to fire automatically on Monday so you don't have to remember.

In your `cos/skill.md`, add this rule:

```markdown
- **Monday auto-run cadence**: When `/cos` is invoked on a Monday (`date +%u` == 1) AND
  `/cos today`, `/cos focus`, or `/cos week` is the requested mode, automatically run
  `/weekly-pulse` BEFORE presenting the /cos brief. After /weekly-pulse completes,
  present its summary first, THEN proceed with the normal /cos brief. If
  `/weekly-pulse` has already been logged in `session_log.yaml` within the last 6 days
  (search for tag `[weekly-pulse]`), skip the auto-run to avoid duplication.
  Log completion to session_log with timestamp + week reviewed.
```

Other useful triggers in this flavor:
- **First-of-month** → run a budget reconciliation skill
- **Day-of-month <= 7 AND Monday** → first-Monday-of-month rituals (board prep, monthly review)
- **Friday afternoon** → run a "clear the queue" skill before the weekend

### Pattern template

```markdown
- **<TRIGGER NAME> cadence**: When `/cos` runs <TRIGGER CONDITION>, automatically
  invoke `/<OTHER SKILL>` BEFORE presenting the brief. Detection: <DATE/STATE CHECK>.
  After it completes, present its output first, THEN proceed normally. If
  `/<OTHER SKILL>` has already run within <DEDUP WINDOW>, skip the auto-run.
  Log completion to session_log with `[<OTHER SKILL>]` tag + relevant period.
```

---

## Flavor 2 — State-triggered chains

Run another skill (or surface a reminder) when a specific file, draft, or condition exists.

### Worked example: 1:1 prep skill triggers when a 1:1 is on tomorrow's calendar

You're a director with five direct reports. You have a `/oneonone-prep` skill that pulls recent commits, open feedback, and prior 1:1 notes. You want `/cos` to flag tomorrow's 1:1s and offer to run prep automatically the night before.

```markdown
- **1:1 prep trigger**: When `/cos today` runs and tomorrow's calendar contains
  any event with title matching `/1:1|one.on.one|1on1/i`, surface this in the
  Suggested Focus block: "Tomorrow's 1:1 with [name] — want me to run /oneonone-prep?"
  Do NOT auto-execute (preserves Annie's choice + lets her batch). Track in
  session_log if she accepts so we don't re-prompt.
```

### Worked example: release-prep skill triggers when a release branch exists

You're a tech lead. Whenever a `release/*` branch exists in the team's repo, you want `/cos` to surface release-prep tasks (changelog, release notes, regression test prompt).

```markdown
- **Release-branch trigger**: On every `/cos today` run, check if any branch matching
  `release/*` exists in `~/code/main-repo` via `git -C ~/code/main-repo branch -a`.
  If yes, surface under Interact category: "Release branch detected — `/release-notes`
  ready to draft when you are." Skip if a session_log update notes the release was
  already shipped in the last 3 days.
```

### Pattern template

```markdown
- **<TRIGGER NAME> trigger**: On every `/cos <MODE>` run, check <STATE CONDITION>
  (file existence, calendar event match, git state, snapshot value). If true, surface
  under <CATEGORY> as "<HUMAN-READABLE PROMPT>" with the option to run `/<SKILL>`.
  [Auto-execute or just-surface — your call.] Suppress if <DEDUP CONDITION>.
```

---

## Flavor 3 — Update-triggered auto-actions

When you log an `update` to the session log via `/cos update`, the skill can pattern-match the content and run a follow-up action automatically.

### Worked example: pasting a published-doc URL auto-archives the doc

You write a weekly internal memo and publish to your team's Notion. Whenever you tell /cos "the memo is published at <URL>", you want it to auto-save a PDF copy to your archive directory so you have an offline trail.

```markdown
**Auto-actions on specific update types:**

- **Memo published with URL**: If a `/cos update` mentions "memo published" / "memo
  is up" / "shipped the memo" AND contains a URL, automatically archive the URL as
  a PDF using headless Chrome:
  ```
  /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --headless \
    --disable-gpu \
    --print-to-pdf="$HOME/Documents/memos/Memo_$(date +%Y-%m-%d).pdf" \
    "<URL>"
  ```
  Confirm the PDF was saved with file size; log success/failure to session_log.
```

### Worked example: pasting a PR number triggers PR review prep

```markdown
- **PR review request via update**: If a `/cos update` matches "review PR #N" or
  "asked to review #N", look up the PR via `gh pr view N --json title,body,files`,
  draft a review checklist (security review, test coverage, naming consistency)
  and append to the brief as a follow-up prompt. Don't auto-comment on the PR.
```

### Pattern template

```markdown
**Auto-actions on specific update types:**

- **<TRIGGER PHRASE / TYPE>**: If a `/cos update` <PATTERN MATCH>, automatically:
  1. <CONCRETE ACTION>
  2. <VERIFY / CONFIRM>
  3. Log to session_log
```

---

## How to test a new chain

After authoring the rule in `skill.md`:

1. Make the trigger condition true (set system date, create the file, paste the matching update text)
2. Run `/cos today` (or whatever mode you wired the trigger to)
3. Confirm the chained skill ran AND its output is in your brief
4. Confirm the dedup logic prevents double-running on the second invocation that day

If the chain doesn't fire, the most common reasons are:
- The trigger phrase in `skill.md` is too vague — Claude doesn't pattern-match it reliably. Tighten the language with concrete examples.
- The dedup check is too aggressive — surface the dedup tag and verify it's actually in the log
- The chained skill isn't installed in `~/.claude/skills/<skill-name>/` — confirm with `ls ~/.claude/skills/`

---

## Common chain inventory for an executive mom at a software company

This is a starting menu. Pick what you actually use.

| Skill | Trigger | Notes |
|---|---|---|
| `/weekly-pulse` | Monday auto-run | Last week's eng metrics in your brief before standup |
| `/oneonone-prep <name>` | Day before each 1:1 | Recent commits, prior notes, open feedback |
| `/board-prep` | First Monday before quarterly board meeting | Pulls KPIs, last quarter's deck, open board questions |
| `/release-notes` | Release branch exists in repo | Auto-drafts changelog from PR titles |
| `/oncall-handoff` | Friday afternoon during your on-call rotation | Generates handoff doc for the weekend rotation |
| `/family-week` | Sunday evening | Pulls family calendar + meal plan + kid activities, surfaces gaps |
| `/school-followup` | After any teacher email lands in inbox | Drafts response from prior thread context |
| `/travel-plan <trip>` | Calendar event with "travel" in title | Itinerary, packing list, OOO schedule |
| `/budget-check` | First of month | Pulls bank/credit balances, flags variance from prior month |
| `/birthday-plan <kid>` | 6 weeks before each kid's birthday | Surfaces venue, invite list, dependencies |

**Note:** These are illustrative names. You'll author your own skills with your own naming. The point is the wiring pattern, not the specific skills.

---

## Anti-patterns to avoid

- **Don't chain a skill that takes >10 seconds to run on a triggered every-/cos basis.** /cos should stay snappy. If a skill is slow, run it on a real timed cadence (cron / launchd) and have /cos READ its output, not RE-RUN it.
- **Don't fire the same chained skill multiple times per day** without a dedup check. Add a session_log tag and check for it.
- **Don't auto-execute destructive actions.** Auto-run reads and drafts; surface (don't auto-execute) anything that sends, posts, or commits.
- **Don't bury the chained output inside the brief.** If you triggered /weekly-pulse before /cos today, present its output AT THE TOP, not at the bottom — otherwise you'll skim past it.
