# Demo

`demo.gif` (and the source asciinema `demo.cast`) at the top of the project
README. **This is a simulated demo.** No real Anthropic API was hit; no real
Claude Code session was stalled. `fake_claude.py` is a stand-in script that
renders the same visual state the real Claude Code TUI shows when stuck on a
`API Error: 529`, then renders a recovered state after receiving the `Continue`
keystroke that the watchdog injects.

The watchdog binary in the demo is the actual shipped `watchdog.py` from the
repo root — same code, same heuristics, same injection chain. Only the
*observed* program is mocked.

Why a simulated demo: capturing a real overnight stall under stock Anthropic
load is not deterministic and not on a useful schedule. Per cannot-lie
provenance, the substitution is labeled in the demo itself, in this README,
and in the announce thread. A real-traffic capture will follow as a quote-tweet
follow-up when the watchdog catches an organic stall.

## How to rebuild

```bash
# Requirements: tmux, pstree, python3 ≥3.10, asciinema, agg (for cast → gif)
cd demo
./run_demo.sh                                       # smoke-test (no recording)
asciinema rec --cols 100 --rows 30 \
    -c ./run_demo.sh \
    -t "claude-code-api-watchdog v0.1.0 demo (simulated)" \
    --overwrite demo.cast
agg --font-size 14 --speed 1.0 --theme monokai demo.cast demo.gif
```

## What `run_demo.sh` does

1. Starts a tmux session `ccaw-demo`
2. Runs `claude` (a copy of `fake_claude.py`) in that session — it renders the
   stuck state with `API Error: 529` at the prompt
3. Snapshots the stuck pane with `tmux capture-pane`
4. Runs the watchdog against the session with demo-tuned intervals
   (`CCW_INTERVAL=3 CCW_CONFIRM_POLLS=2 CCW_BACKOFF_BASE=1`)
5. The watchdog detects the transient state across 2 polls, injects `Continue`,
   `fake_claude` reads it, renders the recovered state, clears scrollback
6. Snapshots the recovered pane

The `fake_claude` script renames its own process to `claude` via
`prctl(PR_SET_NAME)` so the watchdog's `pstree`-based liveness check passes —
real Claude Code already has `claude` as its process name.

## Production tunables vs demo tunables

The demo uses short intervals so the asciinema clip fits in ~20 seconds.
Production defaults are calmer:

| Setting             | Demo | Production default |
|---------------------|------|--------------------|
| `CCW_INTERVAL`      | 3s   | 30s                |
| `CCW_CONFIRM_POLLS` | 2    | 2                  |
| `CCW_BACKOFF_BASE`  | 1s   | 2s                 |
| `CCW_BACKOFF_CAP`   | 120s | 120s               |
| `CCW_MAX_ATTEMPTS`  | 10   | 10                 |

The classification logic, injection chain, and fail-closed defaults are
identical between demo and production.
