#!/usr/bin/env python3
"""claude-code-watchdog — auto-recover Claude Code sessions stuck on API errors.

Claude Code running in automation stops on two things it shouldn't:

  1. Transient API errors (not usage limits) — random 429 / 500 / 502 / 503 /
     529-overloaded / connection resets. Your overnight loop dies because the
     API hiccupped for a few seconds.
  2. Process death / hang.

This watchdog watches your Claude Code tmux sessions and nudges them back to
life: when a session is stuck on a transient API-error state at the prompt, it
injects `Continue` with exponential backoff and a retry cap. It distinguishes
transient errors (recover) from real usage limits (wait, don't hammer).

IMPORTANT — point this at UNATTENDED automation sessions, not the interactive
session you're working in. Detection is a heuristic (see "How detection works"
in the README). To be safe it requires the error state to persist across two
consecutive polls before acting, and you can run it with --dry-run first to see
exactly what it WOULD do without sending a single keystroke.

If a pane scores healthy but stops changing, the watchdog writes a rate-limited
forensic snapshot to CCW_FORENSIC_LOG. Add new transient patterns only from a
real captured string in that log, not from guesses.

Requirements:
  - Your Claude Code sessions run inside named tmux sessions.
  - Python 3.10+. No third-party dependencies at all. Escalation, if you want
    it, is just an external command you supply (`--escalate-cmd`).

Quick start (recommended: dry-run first):
    python3 watchdog.py --sessions mybot,worker1 --dry-run   # watch, send nothing
    python3 watchdog.py --sessions mybot,worker1             # live

This file is standalone — copy it anywhere and run it. Apache-2.0.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import shlex
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [claude-code-watchdog] %(message)s",
)
log = logging.getLogger(__name__)

# --- Tunables (env-overridable; CLI flags take final precedence) -------------

DEAD_THRESHOLD = int(os.environ.get("CCW_DEAD_THRESHOLD", "300"))
MAX_RESTART_ATTEMPTS = int(os.environ.get("CCW_MAX_RESTART_ATTEMPTS", "10"))
DEAD_CONFIRM_POLLS = int(os.environ.get("CCW_DEAD_CONFIRM_POLLS", "2"))
RESTART_LOAD_MULTIPLIER = float(os.environ.get("CCW_RESTART_LOAD_MULTIPLIER", "4"))

RL_BACKOFF_BASE = float(os.environ.get("CCW_BACKOFF_BASE", "2"))
RL_BACKOFF_CAP = float(os.environ.get("CCW_BACKOFF_CAP", "120"))
RL_BACKOFF_JITTER = 0.2
RL_MAX_ATTEMPTS = int(os.environ.get("CCW_MAX_ATTEMPTS", "10"))

# Require the transient-error state to be seen on this many CONSECUTIVE polls
# before injecting anything. Debounce against single-frame redraws and against
# error text that's merely being displayed/discussed rather than blocking.
CONFIRM_POLLS = int(os.environ.get("CCW_CONFIRM_POLLS", "2"))

# Default resume command is EMPTY → escalate-only, no auto-restart, by default.
# This is the fail-closed default for a public tool. Opt in to auto-restart by
# setting --resume-cmd / CCW_RESUME_CMD with the {session_id} placeholder (e.g.
#   "claude --resume {session_id} --dangerously-skip-permissions").
DEFAULT_RESUME_CMD = os.environ.get("CCW_RESUME_CMD", "")
RESUME_SESSION_PLACEHOLDER = "{session_id}"

# Transient API-error patterns. Anchored to Claude Code's "API Error:" /
# "Unable to connect" rendering wherever possible so we don't false-match a
# developer who merely has "529" / "overloaded" / "502" on screen while writing
# HTTP error-handling code — the single most common false-positive for this
# audience. Strings verified against the official Claude Code error reference
# (code.claude.com/docs/en/errors) and real screenshots, 2026-05-22.
TRANSIENT_PATTERNS = (
    "API Error: 529",                                 # raw-JSON 529 form
    "API Error: Repeated 529",                        # "Repeated 529 Overloaded errors"
    "API Error: Overloaded",                          # plain overloaded banner
    "Overloaded errors",                              # the capital-O rendered form
    "overloaded_error",                               # raw JSON body type
    "API Error: Request rejected (429)",              # verbatim official string
    "API Error: Server is temporarily limiting requests",  # verbatim
    "not your usage limit",                           # transient self-label
    "API Error: 500",
    "API Error: 502",
    "API Error: 503",
    "Unable to connect to API",                       # connectivity; covers the
                                                      # "Unable to connect to API (ECONNRESET)"
                                                      # line. Bare "ECONNRESET" is intentionally
                                                      # NOT matched — devs writing connection
                                                      # handling have it on screen (false positive).
    "Request timed out",                              # verbatim official string
)
# Real usage limits — wait them out, never spam Continue. Verified against the
# official error reference: current TUI says "You've hit your <session|weekly|
# Opus> limit · resets <time>", NOT "usage limit reached".
USAGE_LIMIT_PATTERNS = (
    "You've hit your session limit",
    "You've hit your weekly limit",
    "You've hit your Opus limit",
    "Rate limit reached",
    "resets ",                                        # "resets 3:45pm" / "resets Mon 12:00am"
)
PROMPT_MARKERS = ("❯", "bypass permissions")
# Active-generation indicators. When present, Claude is busy working — NOT
# idle-stuck on an error. Never inject in this state, even if stale error text
# lingers in scrollback from a just-recovered failure. "esc to interrupt" shows
# only while Claude is generating; absent at an idle prompt (verified 2026-05-22).
WORKING_MARKERS = ("esc to interrupt", "to interrupt")
QUEUED_MARKERS = ("Press up to edit queued messages",)

PROXIMITY = int(os.environ.get("CCW_PROXIMITY", "20"))
RECENT_ERROR_POLLS = int(os.environ.get("CCW_RECENT_ERROR_POLLS", "4"))
STUCK_AFTER_ERROR_POLLS = int(os.environ.get("CCW_STUCK_AFTER_ERROR_POLLS", "1"))
FORENSIC_LOG = os.path.expanduser(
    os.environ.get("CCW_FORENSIC_LOG", "~/.cache/claude-code-watchdog/forensic.log")
)
FORENSIC_MAX_BYTES = int(os.environ.get("CCW_FORENSIC_MAX_BYTES", "1048576"))
FORENSIC_CAPTURE_LINES = int(os.environ.get("CCW_FORENSIC_CAPTURE_LINES", "20"))
FORENSIC_STAGNANT_POLLS = int(
    os.environ.get("CCW_FORENSIC_STAGNANT_POLLS", str(STUCK_AFTER_ERROR_POLLS))
)


class Watchdog:
    def __init__(
        self,
        sessions: List[str],
        interval: int,
        no_restart: Optional[List[str]] = None,
        resume_cmd: str = DEFAULT_RESUME_CMD,
        escalate_cmd: Optional[str] = None,
        dry_run: bool = False,
    ):
        self.sessions = sessions
        self.interval = interval
        self.no_restart = set(no_restart or [])
        self.resume_cmd = resume_cmd
        self.escalate_cmd = escalate_cmd
        self.dry_run = dry_run
        self.last_alive: Dict[str, float] = {}
        self.rl_cooldown: Dict[str, float] = {}
        self.rl_attempts: Dict[str, int] = {}
        self.rl_escalated: Dict[str, bool] = {}
        self.pending_transient: Dict[str, int] = {}   # consecutive transient polls
        self.restart_count: Dict[str, int] = {}
        self.definitive_dead_polls: Dict[str, int] = {}
        self.last_fingerprint: Dict[str, str] = {}
        self.stagnant_polls: Dict[str, int] = {}
        self.recent_error_polls: Dict[str, int] = {}
        self.forensic_dumped: Dict[str, bool] = {}

    # --- tmux helpers --------------------------------------------------------

    def _capture(self, session: str, lines: int = 50) -> str:
        try:
            r = subprocess.run(
                ["tmux", "capture-pane", "-t", session, "-p", "-S", f"-{lines}"],
                capture_output=True, text=True, timeout=5,
            )
            return r.stdout if r.returncode == 0 else ""
        except Exception:
            return ""

    def _session_exists(self, session: str) -> bool:
        try:
            return subprocess.run(
                ["tmux", "has-session", "-t", session],
                capture_output=True, timeout=5,
            ).returncode == 0
        except Exception:
            return False

    def _pane_pid(self, session: str) -> Optional[str]:
        try:
            pane = subprocess.run(
                ["tmux", "list-panes", "-t", session, "-F", "#{pane_pid}"],
                capture_output=True, text=True, timeout=5,
            )
            if pane.returncode != 0:
                log.warning("%s: tmux pane probe failed: %s", session, (pane.stderr or "").strip())
                return None
            return pane.stdout.strip().split("\n")[0] if pane.stdout.strip() else None
        except subprocess.TimeoutExpired:
            log.warning("%s: tmux pane probe timed out", session)
            return None
        except Exception as e:
            log.warning("%s: tmux pane probe errored: %s", session, e)
            return None

    def _claude_running(self, session: str) -> Optional[bool]:
        pid = self._pane_pid(session)
        if not pid:
            return None
        try:
            tree = subprocess.run(
                ["pstree", "-p", pid], capture_output=True, text=True, timeout=5,
            )
            if tree.returncode != 0:
                log.warning("%s: process-tree probe failed for pane pid %s: %s",
                            session, pid, (tree.stderr or "").strip())
                return None
            return "claude" in tree.stdout.lower()
        except subprocess.TimeoutExpired:
            log.warning("%s: process-tree probe timed out for pane pid %s", session, pid)
            return None
        except Exception as e:
            log.warning("%s: process-tree probe errored for pane pid %s: %s", session, pid, e)
            return None

    def _send(self, session: str, *args: str, desc: str = "") -> None:
        if self.dry_run:
            log.info(f"[dry-run] would send to {session}: {list(args)}"
                     + (f"  ({desc})" if desc else ""))
            return
        try:
            subprocess.run(
                ["tmux", "send-keys", "-t", session, *args],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass

    # --- detection -----------------------------------------------------------

    def _pane_state(self, pane: str) -> str:
        """Return 'transient' / 'usage_limit' / 'healthy'.

        All checks operate within PROXIMITY lines of the last prompt marker, so
        error text that has scrolled into history does not trigger recovery.
        """
        if not pane:
            return "healthy"
        # Busy-guard: if Claude is actively generating, it is not idle-stuck —
        # never act, even if stale error text lingers in scrollback from a
        # just-recovered failure.
        if any(m in pane for m in WORKING_MARKERS):
            return "healthy"
        window = self._pane_scored_window(pane)
        if not window:
            return "healthy"

        # Usage-limit check first — more specific than transient.
        if any(p in window for p in USAGE_LIMIT_PATTERNS):
            return "usage_limit"
        if any(p in window for p in TRANSIENT_PATTERNS):
            return "transient"
        return "healthy"

    def _pane_flags(self, pane: str) -> Dict[str, bool]:
        state = self._pane_state(pane)
        return {
            "working": any(m in pane for m in WORKING_MARKERS) if pane else False,
            "transient_near_prompt": state == "transient",
            "usage_limit": state == "usage_limit",
            "transient_anywhere": any(p in pane for p in TRANSIENT_PATTERNS) if pane else False,
            "queued_marker": any(m in pane for m in QUEUED_MARKERS) if pane else False,
        }

    def _pane_scored_window(self, pane: str) -> str:
        if not pane:
            return ""
        lines = pane.split("\n")
        last_prompt = -1
        for i in range(len(lines) - 1, -1, -1):
            if any(m in lines[i] for m in PROMPT_MARKERS):
                last_prompt = i
                break
        if last_prompt < 0:
            return ""
        return "\n".join(lines[max(0, last_prompt - PROXIMITY): last_prompt + 1])

    def _pane_fingerprint(self, pane: str) -> str:
        if not pane:
            return ""
        return "\n".join(line.rstrip() for line in pane.splitlines()[-12:]).strip()

    def _note_progress(self, session: str, pane: str) -> bool:
        fingerprint = self._pane_fingerprint(pane)
        previous = self.last_fingerprint.get(session)
        self.last_fingerprint[session] = fingerprint
        if previous is None or fingerprint != previous:
            self.stagnant_polls[session] = 0
            self.forensic_dumped[session] = False
            return True
        self.stagnant_polls[session] = self.stagnant_polls.get(session, 0) + 1
        return False

    def _poll_log(self, session: str, state: str, action: str) -> None:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        log.info(f"[{ts}] [watchdog] poll session={session} state={state} action={action}")

    def _captured_tail(self, pane: str) -> str:
        if not pane:
            return ""
        return "\n".join(pane.splitlines()[-FORENSIC_CAPTURE_LINES:])

    def _rotate_forensic_log(self, path: str) -> None:
        if FORENSIC_MAX_BYTES <= 0 or not os.path.exists(path):
            return
        if os.path.getsize(path) < FORENSIC_MAX_BYTES:
            return
        rotated = f"{path}.1"
        if os.path.exists(rotated):
            os.remove(rotated)
        os.replace(path, rotated)

    def _write_forensic_record(self, record: Dict[str, object]) -> None:
        path = FORENSIC_LOG
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._rotate_forensic_log(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")

    def _maybe_dump_forensic(self, session: str, pane: str, flags: Dict[str, bool], action: str) -> None:
        stagnant = self.stagnant_polls.get(session, 0)
        if stagnant <= 0 or stagnant < FORENSIC_STAGNANT_POLLS:
            return
        if self.forensic_dumped.get(session):
            return

        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "session": session,
            "state": "healthy",
            "action": action,
            "stagnant_polls": stagnant,
            "pane_flags": flags,
            "scored_window": self._pane_scored_window(pane),
            "captured_tail": self._captured_tail(pane),
        }
        try:
            self._write_forensic_record(record)
            log.debug(
                "%s: healthy pane is stagnant for %s poll(s); forensic snapshot written to %s",
                session,
                stagnant,
                FORENSIC_LOG,
            )
        except Exception as e:
            log.error("%s: forensic snapshot failed: %s", session, e)
        self.forensic_dumped[session] = True

    # --- recovery actions ----------------------------------------------------

    def _backoff(self, attempts: int) -> float:
        base = min(RL_BACKOFF_BASE * (2 ** attempts), RL_BACKOFF_CAP)
        return base * (1 + random.uniform(-RL_BACKOFF_JITTER, RL_BACKOFF_JITTER))

    def _send_continue(self, session: str) -> None:
        # Pre-clear the input line first (Ctrl-U kills the line) so we never
        # append "Continue" onto a half-typed human instruction sitting at the
        # prompt. Then type Continue and submit. Claude Code's Ink TUI needs the
        # Kitty-protocol Enter (CSI-u) on many modal states; legacy Enter alone
        # is unreliable, so we send the chain.
        self._send(session, "C-u", desc="pre-clear input line")
        time.sleep(0.15)
        self._send(session, "-l", "Continue", desc="type Continue")
        time.sleep(0.3)
        self._send(session, "Enter", desc="submit (legacy)")
        time.sleep(0.1)
        self._send(session, "-H", "1b", "5b", "31", "33", "75", desc="submit (CSI-u Enter)")

    def _handle_transient(self, session: str, reason: str) -> str:
        now = time.time()
        attempts = self.rl_attempts.get(session, 0)
        last = self.rl_cooldown.get(session, 0.0)

        if attempts >= RL_MAX_ATTEMPTS:
            if not self.rl_escalated.get(session):
                msg = (f"{session}: API error persists after {RL_MAX_ATTEMPTS} "
                       f"Continue attempts — auto-retry halted")
                log.error(msg)
                self._escalate(msg)
                self.rl_escalated[session] = True
            return "wait"

        if now - last < self._backoff(attempts):
            return "wait"
        log.info(f"{session}: {reason} — Continue {attempts + 1}/{RL_MAX_ATTEMPTS}")
        self._send_continue(session)
        self.rl_cooldown[session] = now
        self.rl_attempts[session] = attempts + 1
        self.recent_error_polls[session] = RECENT_ERROR_POLLS
        return "continue"

    def _dismiss_feedback(self, session: str) -> None:
        log.info(f"{session}: dismissing feedback overlay")
        self._send(session, "0", "Enter", desc="dismiss feedback")

    def _reset_rl(self, session: str) -> None:
        if self.rl_attempts.get(session):
            log.info(f"{session}: error cleared after {self.rl_attempts[session]} attempt(s) — reset")
        self.rl_attempts[session] = 0
        self.rl_cooldown[session] = 0.0
        self.rl_escalated[session] = False
        self.recent_error_polls[session] = 0

    def _restart_load_too_high(self) -> bool:
        if RESTART_LOAD_MULTIPLIER <= 0:
            return False
        try:
            load1, _, _ = os.getloadavg()
            cpu_count = os.cpu_count() or 1
        except (AttributeError, OSError):
            return False
        return load1 > (cpu_count * RESTART_LOAD_MULTIPLIER)

    def _claude_project_dir_for_cwd(self, cwd: str) -> Path:
        project_name = cwd.replace(os.sep, "-")
        return Path.home() / ".claude" / "projects" / project_name

    def _session_id_from_jsonl(self, path: Path) -> Optional[str]:
        try:
            with path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    session_id = str(payload.get("sessionId") or "").strip()
                    if session_id:
                        return session_id
        except OSError:
            return None
        return path.stem or None

    def _claude_session_id_for_pane(self, session: str) -> Optional[str]:
        pid = self._pane_pid(session)
        if not pid:
            return None
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError as e:
            log.warning("%s: cannot read pane cwd for pid %s: %s", session, pid, e)
            return None
        project_dir = self._claude_project_dir_for_cwd(cwd)
        try:
            candidates = sorted(
                project_dir.glob("*.jsonl"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError as e:
            log.warning("%s: cannot inspect Claude project dir %s: %s", session, project_dir, e)
            return None
        for candidate in candidates:
            session_id = self._session_id_from_jsonl(candidate)
            if session_id:
                return session_id
        log.warning("%s: no Claude session jsonl found in %s", session, project_dir)
        return None

    def _resume_command_for_session(self, session: str) -> Optional[str]:
        if not self.resume_cmd:
            return None
        try:
            tokens = shlex.split(self.resume_cmd)
        except ValueError as e:
            log.error("%s: invalid resume-cmd %r: %s", session, self.resume_cmd, e)
            return None
        if any(token == "latest" or token.endswith("=latest") for token in tokens):
            log.error(
                "%s: refusing global resume target 'latest'; use %s so this session resumes its own conversation",
                session,
                RESUME_SESSION_PLACEHOLDER,
            )
            return None
        if RESUME_SESSION_PLACEHOLDER not in self.resume_cmd:
            log.error(
                "%s: resume-cmd must include %s to avoid resuming the wrong conversation",
                session,
                RESUME_SESSION_PLACEHOLDER,
            )
            return None
        session_id = self._claude_session_id_for_pane(session)
        if not session_id:
            return None
        return self.resume_cmd.replace(RESUME_SESSION_PLACEHOLDER, shlex.quote(session_id))

    def _restart(self, session: str) -> None:
        if session in self.no_restart:
            reason = "no-restart set"
            log.warning(f"{session}: dead but {reason} — escalating instead of restarting")
            self._escalate(f"{session}: Claude process gone; not auto-restarting ({reason})")
            return
        resume_cmd = self._resume_command_for_session(session)
        if not resume_cmd:
            reason = "no safe resume-cmd resolved"
            log.warning(f"{session}: dead but {reason} — escalating instead of restarting")
            self._escalate(f"{session}: Claude process gone; not auto-restarting ({reason})")
            return
        n = self.restart_count.get(session, 0) + 1
        self.restart_count[session] = n
        if n > MAX_RESTART_ATTEMPTS:
            msg = f"{session}: exceeded {MAX_RESTART_ATTEMPTS} restart attempts — giving up"
            log.error(msg)
            self._escalate(msg)
            return
        log.warning(f"{session}: restart {n}/{MAX_RESTART_ATTEMPTS}: {resume_cmd}")
        self._send(session, resume_cmd, "Enter", desc="restart")

    def _escalate(self, body: str) -> None:
        if not self.escalate_cmd:
            return
        if self.dry_run:
            log.info(f"[dry-run] would escalate: {body}")
            return
        try:
            subprocess.run(shlex.split(self.escalate_cmd) + [body],
                           capture_output=True, timeout=10)
        except Exception as e:
            log.error(f"escalate command failed: {e}")

    # --- main loop -----------------------------------------------------------

    def check(self, session: str) -> None:
        if not self._session_exists(session):
            return

        # Liveness FIRST. Never inject Continue/Escape/0 into a pane unless a
        # Claude process is actually running in it — otherwise a crashed
        # session whose pane still shows the prompt + a stale error in
        # scrollback would receive keystrokes typed straight into the bare
        # shell. If Claude is gone, the only action is restart/escalate.
        claude_running = self._claude_running(session)
        if claude_running is None:
            self.pending_transient[session] = 0
            self.definitive_dead_polls[session] = 0
            self._poll_log(session, "probe_unknown", "wait")
            return

        if not claude_running:
            self.pending_transient[session] = 0   # don't carry error state across a death
            dead_polls = self.definitive_dead_polls.get(session, 0) + 1
            self.definitive_dead_polls[session] = dead_polls
            idle = time.time() - self.last_alive.get(session, time.time())
            if idle > DEAD_THRESHOLD and dead_polls >= DEAD_CONFIRM_POLLS:
                if self._restart_load_too_high():
                    log.warning(
                        "%s: no Claude process for %.0fs, but system load is above restart guard — skip restart",
                        session,
                        idle,
                    )
                    self._poll_log(session, "no_progress", "wait_high_load")
                    return
                log.warning(
                    "%s: no Claude process for %.0fs (%s/%s definitive polls)",
                    session,
                    idle,
                    dead_polls,
                    DEAD_CONFIRM_POLLS,
                )
                self._restart(session)
                self._poll_log(session, "no_progress", "reset")
            else:
                log.info(
                    "%s: Claude process absent (%s/%s definitive polls, %.0fs idle) — waiting",
                    session,
                    dead_polls,
                    DEAD_CONFIRM_POLLS,
                    idle,
                )
                self._poll_log(session, "no_progress", "wait")
            return

        # Claude is alive — safe to do pane-state recovery.
        self.definitive_dead_polls[session] = 0
        self.last_alive[session] = time.time()
        self.restart_count[session] = 0

        pane = self._capture(session)
        flags = self._pane_flags(pane)
        progress = self._note_progress(session, pane)
        state = "healthy"
        action = "none"

        if flags["usage_limit"]:
            self.pending_transient[session] = 0
            if self.rl_attempts.get(session):
                action = "reset"
                self._reset_rl(session)
            self._poll_log(session, state, action)
            return

        if state == "feedback":
            self.pending_transient[session] = 0
            self._dismiss_feedback(session)
            self._poll_log(session, state, "none")
            return

        if flags["transient_near_prompt"]:
            # Debounce: require CONFIRM_POLLS consecutive transient detections
            # before acting. Single-frame redraws / momentarily-displayed error
            # text won't reach the threshold.
            pending = self.pending_transient.get(session, 0) + 1
            self.pending_transient[session] = pending
            self.recent_error_polls[session] = RECENT_ERROR_POLLS
            state = "error_visible"
            if pending >= CONFIRM_POLLS:
                action = self._handle_transient(
                    session,
                    f"transient API error (confirmed across {CONFIRM_POLLS} polls)",
                )
            else:
                log.info(f"{session}: transient error seen ({pending}/{CONFIRM_POLLS}) "
                         f"— waiting for confirmation before acting")
                action = "wait"
            self._poll_log(session, state, action)
            return

        self.pending_transient[session] = 0

        if (
            flags["queued_marker"]
            and not flags["working"]
            and self.stagnant_polls.get(session, 0) >= STUCK_AFTER_ERROR_POLLS
        ):
            state = "stuck_after_error"
            action = self._handle_transient(session, "idle queued-message prompt stalled")
            self._poll_log(session, state, action)
            return

        recent_error = (
            self.recent_error_polls.get(session, 0) > 0
            or flags["transient_anywhere"]
            or bool(self.rl_attempts.get(session))
        )
        if recent_error:
            self.recent_error_polls[session] = max(self.recent_error_polls.get(session, 0) - 1, 0)
            if flags["queued_marker"] and self.stagnant_polls.get(session, 0) >= STUCK_AFTER_ERROR_POLLS:
                state = "stuck_after_error"
                action = self._handle_transient(session, "stuck after error cleared into queued-message prompt")
            elif flags["transient_anywhere"]:
                state = "error_cleared"
                action = "wait"
                if progress and not flags["queued_marker"] and self.rl_attempts.get(session):
                    action = "reset"
                    self._reset_rl(session)
            elif self.stagnant_polls.get(session, 0) >= STUCK_AFTER_ERROR_POLLS and self.rl_attempts.get(session):
                state = "no_progress"
                action = "wait"
            else:
                if progress and self.rl_attempts.get(session):
                    action = "reset"
                    self._reset_rl(session)
                state = "healthy"
            self._poll_log(session, state, action)
            return

        if self.rl_attempts.get(session):
            action = "reset"
            self._reset_rl(session)
        self._maybe_dump_forensic(session, pane, flags, action)
        self._poll_log(session, state, action)

    def run(self) -> None:
        log.info(f"started{' [DRY-RUN]' if self.dry_run else ''}: sessions={self.sessions} "
                 f"interval={self.interval}s confirm_polls={CONFIRM_POLLS} "
                 f"dead_confirm_polls={DEAD_CONFIRM_POLLS} "
                 f"max_attempts={RL_MAX_ATTEMPTS} "
                 f"auto_restart={'on' if self.resume_cmd else 'off (escalate-only)'} "
                 f"no_restart={sorted(self.no_restart)}")
        now = time.time()
        for s in self.sessions:
            self.last_alive[s] = now
        while True:
            try:
                for s in self.sessions:
                    self.check(s)
            except Exception as e:
                log.error(f"check loop error: {e}", exc_info=True)
            time.sleep(self.interval)


def main() -> None:
    p = argparse.ArgumentParser(description="claude-code-watchdog")
    p.add_argument("--sessions", default=os.environ.get("CCW_SESSIONS", ""),
                   help="comma-separated tmux session names (or CCW_SESSIONS env)")
    p.add_argument("--interval", type=int,
                   default=int(os.environ.get("CCW_INTERVAL", "30")))
    p.add_argument("--no-restart", default=os.environ.get("CCW_NO_RESTART", ""),
                   help="comma-separated sessions to nudge-only, never restart")
    p.add_argument("--resume-cmd", default=DEFAULT_RESUME_CMD,
                   help="command to relaunch a dead session. EMPTY (default) = "
                        "escalate-only, never auto-restart. Opt in explicitly, "
                        "e.g. 'claude --resume {session_id} --dangerously-skip-permissions'. "
                        "Commands using 'latest' are refused.")
    p.add_argument("--escalate-cmd", default=os.environ.get("CCW_ESCALATE_CMD", ""))
    p.add_argument("--dry-run", action="store_true",
                   default=os.environ.get("CCW_DRY_RUN", "").lower() in ("1", "true", "yes"),
                   help="log what it WOULD do; send no keystrokes. Run this first.")
    args = p.parse_args()

    sessions = [s.strip() for s in args.sessions.split(",") if s.strip()]
    if not sessions:
        p.error("no sessions given (use --sessions or CCW_SESSIONS)")
    no_restart = [s.strip() for s in args.no_restart.split(",") if s.strip()]

    Watchdog(
        sessions=sessions,
        interval=args.interval,
        no_restart=no_restart,
        resume_cmd=args.resume_cmd,
        escalate_cmd=args.escalate_cmd or None,
        dry_run=args.dry_run,
    ).run()


if __name__ == "__main__":
    main()
