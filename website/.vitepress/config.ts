import { defineConfig } from 'vitepress'

export default defineConfig({
  title: 'HiveScanner',
  description: 'Your IDE is your command center. Documentation for the HiveScanner Claude Code plugin.',

  head: [
    ['meta', { name: 'theme-color', content: '#F59E0B' }],
    ['meta', { property: 'og:type', content: 'website' }],
    ['meta', { property: 'og:title', content: 'HiveScanner Docs' }],
    ['meta', { property: 'og:description', content: 'Documentation for the HiveScanner Claude Code plugin' }],
    ['meta', { property: 'og:url', content: 'https://docs.hivescanner.com' }],
  ],

  themeConfig: {
    logo: '/logo.svg',
    siteTitle: 'HiveScanner',

    nav: [
      { text: 'Guide', link: '/quickstart' },
      { text: 'Scanners', link: '/built-in-scanners/' },
      { text: 'Community', link: '/community-scanners/' },
      { text: 'GitHub', link: 'https://github.com/dhruvil009/hivescanner' },
    ],

    sidebar: [
      {
        text: 'Getting Started',
        items: [
          { text: 'Introduction', link: '/' },
          { text: 'Quick Start', link: '/quickstart' },
          { text: 'Installation', link: '/getting-started/installation' },
          { text: 'Configuration', link: '/getting-started/configuration' },
          { text: 'Commands', link: '/getting-started/commands' },
        ],
      },
      {
        text: 'Built-in Scanners',
        items: [
          { text: 'Overview', link: '/built-in-scanners/' },
          { text: 'GitHub', link: '/built-in-scanners/github' },
          { text: 'Git Status', link: '/built-in-scanners/git-status' },
          { text: 'Calendar', link: '/built-in-scanners/calendar' },
          { text: 'Google Chat', link: '/built-in-scanners/gchat' },
          { text: 'Email', link: '/built-in-scanners/email' },
          { text: 'WhatsApp', link: '/built-in-scanners/whatsapp' },
          { text: 'Weather', link: '/built-in-scanners/weather' },
        ],
      },
      {
        text: 'Community Scanners',
        items: [
          { text: 'Overview', link: '/community-scanners/' },
          { text: 'Linear', link: '/community-scanners/linear' },
          { text: 'Slack', link: '/community-scanners/slack' },
          { text: 'Discord', link: '/community-scanners/discord' },
          { text: 'Telegram', link: '/community-scanners/telegram' },
          { text: 'Jira', link: '/community-scanners/jira' },
          { text: 'GitLab', link: '/community-scanners/gitlab' },
          { text: 'PagerDuty', link: '/community-scanners/pagerduty' },
          { text: 'Sentry', link: '/community-scanners/sentry' },
          { text: 'Notion', link: '/community-scanners/notion' },
          { text: 'Twitter / X', link: '/community-scanners/twitter' },
          { text: 'Facebook', link: '/community-scanners/facebook' },
          { text: 'RSS', link: '/community-scanners/rss' },
          { text: 'Hacker News', link: '/community-scanners/hackernews' },
          { text: 'Package Tracking', link: '/community-scanners/package-tracking' },
        ],
      },
      {
        text: 'Concepts',
        items: [
          { text: 'Architecture', link: '/concepts/architecture' },
          { text: 'Pollen Lifecycle', link: '/concepts/pollen-lifecycle' },
          { text: 'Security Model', link: '/concepts/security' },
          { text: 'Triage Autonomy', link: '/concepts/triage-autonomy' },
        ],
      },
      {
        text: 'Build Your Own Scanner',
        items: [
          { text: 'Scanner Interface', link: '/build-your-own/scanner-interface' },
          { text: 'Manifest (teammate.json)', link: '/build-your-own/manifest' },
          { text: 'Sandboxed Execution', link: '/build-your-own/sandboxed-execution' },
          { text: 'Testing Locally', link: '/build-your-own/testing' },
        ],
      },
      {
        text: 'Contributing',
        items: [
          { text: 'Contribution Guide', link: '/contributing' },
        ],
      },
    ],

    socialLinks: [
      { icon: 'github', link: 'https://github.com/dhruvil009/hivescanner' },
    ],

    search: {
      provider: 'local',
    },

    editLink: {
      pattern: 'https://github.com/dhruvil009/hivescanner/edit/main/website/:path',
      text: 'Edit this page on GitHub',
    },

    footer: {
      message: 'Released under the Apache 2.0 + Commons Clause License.',
      copyright: 'Copyright 2025 HiveScanner Contributors',
    },
  },
})
