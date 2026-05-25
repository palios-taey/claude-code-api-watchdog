# Security Policy

## Scope

`claude-code-api-watchdog` is a local automation tool. It:

- Reads your own tmux panes (`tmux capture-pane`)
- Sends keystrokes to your own tmux sessions (`tmux send-keys`)
- Optionally runs a relaunch command and an escalation command you configure

It does **not** make network calls, store credentials, or transmit any data off
the machine. The only external commands it runs are the ones you pass via
`--resume-cmd` and `--escalate-cmd`.

## Things to know before you run it

- **Auto-restart of dead sessions is OFF by default.** `--resume-cmd` is empty
  by default → if a Claude process dies, the watchdog escalates and does NOT
  relaunch. Opt in to auto-restart by setting `--resume-cmd` explicitly (e.g.
  `claude --resume latest --dangerously-skip-permissions`). The opt-in flag
  bypasses Claude Code's permission prompts on the relaunched session — that
  is appropriate for unattended automation but is a deliberate trust decision.
- Use `--no-restart` for any session where re-running the last action on resume
  would be harmful (e.g. anything that posts, sends, or pays). The watchdog will
  nudge such sessions with `Continue` but never relaunch them.
- The watchdog injects `Continue` into whatever session you name. Point it only
  at sessions you control.

## Reporting a vulnerability

If you find a security issue in the watchdog itself (e.g. a way for pane content
to trigger an unintended command), please open an issue without exploit details
and request a private channel, or email the maintainer listed in the repo.
