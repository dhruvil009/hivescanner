---
layout: home
hero:
  name: HiveScanner
  text: Your IDE is your command center
  tagline: A Claude Code plugin that brings your notifications to you — GitHub, Slack, Calendar, and more — right where you code.
  actions:
    - theme: brand
      text: Get Started
      link: /quickstart
    - theme: alt
      text: View on GitHub
      link: https://github.com/dhruvil009/hivescanner

features:
  - title: Zero-Token Idle Polling
    details: Python workers handle all background polling. The LLM is never invoked during idle cycles — zero cost when nothing's happening.
  - title: 21 Scanners
    details: 7 built-in scanners and 14 community scanners covering GitHub, Slack, Jira, Linear, PagerDuty, Sentry, and more.
  - title: Community Ecosystem
    details: Extend HiveScanner with community-built scanners. Hire and fire them with /hive hire and /hive fire.
  - title: Plugin-Native
    details: Not another dashboard or Slack bot. HiveScanner runs inside Claude Code — no separate servers, no Docker, no port binding.
  - title: Smart Notifications
    details: Bootstrap silence, watermark-based incremental polling, smart batch grouping, and a full pollen lifecycle.
  - title: Security First
    details: Process isolation for third-party scanners, atomic file writes, no secrets in pollen, and a 6-gate safety system for triage autonomy.
---
