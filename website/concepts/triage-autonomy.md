# Confirmed Triage

HiveScanner can prepare a fixed-template Slack triage draft for eligible pending pollen. Despite the legacy `autonomy` configuration name, the normal `/hive` flow always requires a direct user confirmation before posting.

## Flow

1. A pending item must match exactly one trusted `group_policies` entry by source/group rules.
2. HiveScanner creates a local, 60-minute ticket that binds the source-qualified pollen ID, fixed draft, destination, and policy fingerprint.
3. The exact draft and destination are shown to the user.
4. Only an explicit `post #N` confirmation submits that local ticket.

Scanner text, metadata, URLs, and model-generated prose cannot select a destination, edit a draft, provide a ticket ID, or authorize a post.

## Posting gates

Posting fails closed unless all checks pass:

- `autonomy.enabled` is exactly `true`.
- The pollen item still exists uniquely and is pending.
- The ticket is valid, unexpired, and its group policy is unchanged.
- The destination is in `oncall_groups` and maps to exactly one enabled triage policy.
- A real Slack transport and bounded credential environment variable are configured.
- The fixed draft has the required HiveScanner attribution and contains no remediation instructions, code blocks, operational commands, or recommendations.
- The group stays below three successful posts and six attempts per hour.
- The configured per-thread cooldown has elapsed.
- The deterministic client message ID has no prior success or ambiguous attempt.

Slack link expansion, mentions, Markdown, and unfurling are disabled. Redirects cannot leave `slack.com`, and an ambiguous timeout is never retried automatically.

## Kill switch and audit

`/hive autonomy off` disables posting immediately. Attempts, successes, failures, blocks, and toggles are retained in the bounded local `~/.hivescanner/audit.json` log.
