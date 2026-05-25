#!/bin/bash
# claude-code-api-watchdog v0.1.0 — asciinema demo (single-pane narrative).
#
# Run via: asciinema rec -c "./run_demo.sh" demo.cast
#
# SIMULATED demo per cannot-lie provenance. fake_claude (renamed to "claude"
# for pstree liveness) is a stand-in for real Claude Code; the watchdog is
# the actual shipped binary.

set -e

unset TMUX TMUX_PANE
SESSION=ccaw-demo
tmux kill-session -t $SESSION 2>/dev/null || true
sleep 0.3

C_DIM='\033[2m'
C_BOLD='\033[1m'
C_CYAN='\033[36m'
C_GREEN='\033[32m'
C_RESET='\033[0m'

intro() {
  echo -e "${C_BOLD}claude-code-api-watchdog v0.1.0${C_RESET} — simulated demo"
  echo -e "${C_DIM}stuck-state rendering is a stand-in script; the watchdog is the real shipped binary${C_RESET}"
  echo
}

step() {
  echo
  echo -e "${C_CYAN}\$ $1${C_RESET}"
}

clear
intro
sleep 1

# --- Setup: spin up a tmux session with the fake stuck Claude in it ---
step "tmux new-session -d -s ccaw-demo  # start a session running Claude Code"
tmux new-session -d -s $SESSION -x 100 -y 18
tmux send-keys -t $SESSION:0 "/tmp/ccaw-demo/claude" Enter
sleep 1.5

# --- Show the stuck pane ---
step "tmux capture-pane -t ccaw-demo -p  # what the session currently shows"
echo -e "${C_DIM}┌── ccaw-demo ────────────────────────────────────────────────────────${C_RESET}"
tmux capture-pane -t $SESSION:0 -p | sed '/^[[:space:]]*$/d' | sed "s/^/  /"
echo -e "${C_DIM}└─────────────────────────────────────────────────────────────────────${C_RESET}"
echo -e "${C_DIM}# stuck on a transient API error — Continue prompt waiting for a human${C_RESET}"
sleep 2

# --- Run the watchdog (foreground; demo-tuned intervals) ---
step "CCW_INTERVAL=3 CCW_CONFIRM_POLLS=2 python3 watchdog.py --sessions ccaw-demo"
CCW_INTERVAL=3 CCW_CONFIRM_POLLS=2 CCW_BACKOFF_BASE=1 timeout 12 \
    python3 /tmp/ccaw-demo/watchdog.py --sessions $SESSION 2>&1 || true

# --- Show the recovered pane ---
step "tmux capture-pane -t ccaw-demo -p  # what the session shows now"
echo -e "${C_DIM}┌── ccaw-demo ────────────────────────────────────────────────────────${C_RESET}"
tmux capture-pane -t $SESSION:0 -p | sed '/^[[:space:]]*$/d' | sed "s/^/  /"
echo -e "${C_DIM}└─────────────────────────────────────────────────────────────────────${C_RESET}"
echo
echo -e "${C_GREEN}# recovered. session continued from where it was stuck.${C_RESET}"
echo

# --- Outro ---
sleep 2
echo -e "${C_DIM}github.com/palios-taey/claude-code-api-watchdog${C_RESET}"
sleep 2

tmux kill-session -t $SESSION 2>/dev/null || true
