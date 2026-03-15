# Triage Autonomy

HiveScanner can optionally auto-post triage responses to your oncall channels. This is governed by a **6-gate safety system** — every gate must pass before any content is posted.

## The 6 Gates

| Gate | Check | What It Prevents |
|------|-------|-------------------|
| 1. Global Kill Switch | `autonomy.enabled` must be `true` | Accidental posts when autonomy is off |
| 2. Draft Exists | Pollen must have a `triage_draft` in metadata | Posting without prepared content |
| 3. Group Allowlist | Target group must be in `oncall_groups` | Posts to unauthorized channels |
| 4. Rate Limiting | Max 3 posts per hour per group | Spam and runaway loops |
| 5. Content Safety | Draft must not contain remediation language | Dangerous automated advice |
| 6. Attribution Prefix | Draft must start with `[Posted by HiveScanner]` | Unattributed automated posts |

If **any single gate fails**, the post is blocked and logged.

## Template-Based Drafts

Triage responses are generated from **fixed templates** — not LLMs, not AI-generated text. Templates contain structured prompts like "Can you share the crash ID?" or "What's the impact scope?" — safe, predictable, and auditable.

## Content Safety Checks

Gate 5 uses regex patterns to block content containing:

- Remediation instructions ("try running...", "to fix this...")
- Code blocks (triple backticks)
- Suggestions or recommendations ("you should...", "I recommend...")
- Operational commands ("rollback", "revert", "hotfix")

If any pattern matches, the post is blocked.

## Audit Logging

Every triage action — posted or blocked — is logged to `~/.hivescanner/audit.json` with timestamps, gate results, and content previews.

## Kill Switch

If anything goes wrong:

```
/hive autonomy off
```

This immediately sets `autonomy.enabled = false`. All 6 gates will fail at Gate 1 until you explicitly re-enable it. The toggle is logged to the audit trail.
