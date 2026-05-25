#!/usr/bin/env python3
"""
Fake Claude Code TUI for demo purposes — claude-code-api-watchdog v0.1.0.

This is a SIMULATION, not real Claude Code. It renders the same visual state
the real TUI shows when stuck on a 529 API error, accepts the 'Continue'
keystroke the watchdog injects, then renders a recovered state. Used only
for the asciinema demo at the top of the README.

Run: python3 fake_claude.py
"""
import ctypes
import sys
import time

# Rename our own process to "claude" so pstree shows it the way the watchdog's
# liveness check expects (PR_SET_NAME = 15). This is a demo-harness shim;
# real Claude Code already has "claude" as its process name.
try:
    ctypes.CDLL("libc.so.6").prctl(15, b"claude\0", 0, 0, 0)
except Exception:
    pass


def clear():
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def clear_with_scrollback():
    # \033[3J clears the scrollback buffer too, not just the visible screen.
    # Needed so the watchdog's next poll doesn't see the prior "API Error: 529"
    # text in scrollback and try to inject again into the recovered pane.
    sys.stdout.write("\033[2J\033[3J\033[H")
    sys.stdout.flush()


def render_stuck():
    clear()
    print("\033[1m● Refactoring authentication module...\033[0m")
    print()
    print("  ✓ Identified 3 callers of validate_token")
    print("  ✓ Updated auth.py imports")
    print("  ⠋ Running test suite...")
    print()
    print("\033[31mAPI Error: 529\033[0m")
    print("  (this is not your usage limit; the model is temporarily overloaded.)")
    print()
    sys.stdout.write("\033[36m❯ \033[0m")
    sys.stdout.flush()


def render_recovered():
    print("Continue")
    time.sleep(0.4)
    clear_with_scrollback()
    print("\033[1m● Refactoring authentication module...\033[0m")
    print()
    print("  ✓ Identified 3 callers of validate_token")
    print("  ✓ Updated auth.py imports")
    print("  ✓ Test suite passed (47 tests, 0.8s)")
    print("  ✓ Refactor complete")
    print()
    print("\033[32m  Resumed after 1 API-error recovery.\033[0m")
    print()
    sys.stdout.write("\033[36m❯ \033[0m")
    sys.stdout.flush()
    # Stay alive — the demo orchestrator kills the tmux session when done.
    # If we exit early, the bash shell underneath would receive any further
    # keystrokes the watchdog might send, producing noise in the recording.
    time.sleep(60)


def main():
    render_stuck()
    try:
        line = input()
    except (EOFError, KeyboardInterrupt):
        return
    if line.strip().lower() == "continue":
        render_recovered()
    else:
        print(f"\n(received: {line!r}, expected 'Continue')")
        time.sleep(2)


if __name__ == "__main__":
    main()
