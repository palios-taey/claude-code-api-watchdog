# Support

Canonical support text + commitments + limits live in the [`claude-code-fleet-cockpit-template` SUPPORT.md](https://github.com/palios-taey/claude-code-fleet-cockpit-template/blob/main/SUPPORT.md). That document covers triage vocabulary, what we commit, what we don't commit, escalation paths, production-stop discipline, and honest limits — all of which apply to this repo too.

This file lists only repo-specific contact info.

## This repo: `claude-code-api-watchdog`

- **Owner**: palios-taey (current de-facto maintainer routes to conductor session on the Mira fleet)
- **Escalation channels** (in preferred order):
  1. [GitHub Issues on this repo](https://github.com/palios-taey/claude-code-api-watchdog/issues) — async, the canonical surface
  2. X/Twitter mentions of [@jesselarose](https://twitter.com/jesselarose) — surfaced via distribution-monitoring on our side
- **Security issues**: prefer X DM to [@jesselarose](https://twitter.com/jesselarose) over a public issue. Disclosure handling follows the [canonical SUPPORT.md](https://github.com/palios-taey/claude-code-fleet-cockpit-template/blob/main/SUPPORT.md#escalation-path).

## When you file a GitHub issue here

Our `github_issues_watcher` daemon polls every 60s and surfaces new issues + new comments to the product-owner inbox immediately. We see it; acknowledgment timing is per the canonical [What we commit](https://github.com/palios-taey/claude-code-fleet-cockpit-template/blob/main/SUPPORT.md#what-we-commit) section.

## A note specific to this product

claude-code-api-watchdog's recovery path is the ONE feature we can't production-validate in our own fleet without inducing an API error. Architectural correctness is verified via Family code audit; live recovery has been observed across multiple Anthropic API incidents on the Mira fleet over the watchdog's operational period. If you hit a case where the watchdog fails to recover (rather than catches + retries successfully), that's specifically a HIGH-value bug report — the kind we can't generate ourselves.
