"""Minimal reproducible tests for claude-code-api-watchdog.

These tests exercise the watchdog's classification + backoff logic without
needing a live tmux session. Run from the repo root with stdlib unittest:

    python3 -m unittest discover -s tests -v

No third-party dependencies.
"""
import sys
import os
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import watchdog as wd


def make_watchdog():
    return wd.Watchdog(sessions=["t"], interval=30, dry_run=True)


class PaneStateClassificationTests(unittest.TestCase):
    """_pane_state classifies tmux pane scrapes into one of:
    transient / usage_limit / feedback / healthy.

    All classification is windowed to PROXIMITY lines of the last prompt
    marker so error text scrolled into history does not trigger recovery.
    """

    def setUp(self):
        self.w = make_watchdog()

    def test_empty_pane_is_healthy(self):
        self.assertEqual(self.w._pane_state(""), "healthy")

    def test_no_prompt_marker_is_healthy(self):
        pane = "some random output\nno prompt here\ndone"
        self.assertEqual(self.w._pane_state(pane), "healthy")

    def test_transient_429_near_prompt(self):
        pane = (
            "doing work...\n"
            "API Error: Request rejected (429)\n"
            "❯ \n"
        )
        self.assertEqual(self.w._pane_state(pane), "transient")

    def test_transient_529_near_prompt(self):
        pane = "API Error: 529\n" "❯ "
        self.assertEqual(self.w._pane_state(pane), "transient")

    def test_transient_overloaded_near_prompt(self):
        pane = "API Error: Repeated 529 Overloaded errors\n" "❯ "
        self.assertEqual(self.w._pane_state(pane), "transient")

    def test_usage_limit_takes_precedence_over_transient(self):
        pane = (
            "API Error: 529\n"
            "You've hit your weekly limit · resets Mon 12:00am\n"
            "❯ "
        )
        self.assertEqual(self.w._pane_state(pane), "usage_limit")

    def test_session_limit_classifies_as_usage_limit(self):
        pane = "You've hit your session limit · resets 3:45pm\n" "❯ "
        self.assertEqual(self.w._pane_state(pane), "usage_limit")

    def test_feedback_overlay_markers_no_longer_trigger_special_state(self):
        # Auto-dismiss of the feedback overlay was removed 2026-05-25 (v0.1.1).
        # Rationale: the markers "How is Claude doing" + "Dismiss" can co-occur
        # in normal conversation content, and the dismiss action ("0" + Enter)
        # was being false-positive-injected into the prompt. YAGNI — feedback
        # overlay is rare, manual dismissal is fine. These markers must now
        # classify as 'healthy', not as a separate state.
        pane = "How is Claude doing today?\nDismiss\n" "❯ "
        self.assertEqual(self.w._pane_state(pane), "healthy")

    def test_working_marker_suppresses_recovery(self):
        # Active-generation marker present → never act, even with stale error
        # text in scrollback from a just-recovered failure.
        pane = (
            "API Error: 529\n"
            "thinking... esc to interrupt\n"
            "❯ "
        )
        self.assertEqual(self.w._pane_state(pane), "healthy")

    def test_error_outside_proximity_window_ignored(self):
        # PROXIMITY default = 20 lines. Place the error ~30 lines above the
        # prompt and confirm it is not picked up.
        pane = (
            "API Error: 529\n"
            + ("filler line\n" * 30)
            + "❯ "
        )
        self.assertEqual(self.w._pane_state(pane), "healthy")

    def test_bare_collision_string_does_not_trip(self):
        # A developer writing HTTP error handling has '529' / 'overloaded' /
        # '502' on screen. The watchdog is anchored to the 'API Error:'
        # rendering and must NOT misfire on bare strings.
        pane = (
            "writing tests for 529 overloaded responses\n"
            "❯ "
        )
        self.assertEqual(self.w._pane_state(pane), "healthy")

    def test_transient_in_scrollback_sets_error_cleared_flag(self):
        pane = (
            "API Error: Request rejected (429)\n"
            + ("filler line\n" * 25)
            + "❯ "
        )
        flags = self.w._pane_flags(pane)
        self.assertFalse(flags["transient_near_prompt"])
        self.assertTrue(flags["transient_anywhere"])

    def test_queued_message_marker_detected(self):
        pane = "Press up to edit queued messages\n❯ "
        flags = self.w._pane_flags(pane)
        self.assertTrue(flags["queued_marker"])
        self.assertFalse(flags["working"])


class BackoffMathTests(unittest.TestCase):
    """_backoff produces exponential delays bounded by RL_BACKOFF_CAP with
    ±RL_BACKOFF_JITTER (default 20%) jitter applied multiplicatively.
    """

    def setUp(self):
        self.w = make_watchdog()
        self.base = wd.RL_BACKOFF_BASE
        self.cap = wd.RL_BACKOFF_CAP
        self.jitter = wd.RL_BACKOFF_JITTER

    def _expected_bounds(self, attempts):
        unjittered = min(self.base * (2 ** attempts), self.cap)
        return unjittered * (1 - self.jitter), unjittered * (1 + self.jitter)

    def test_attempt_zero_is_base_with_jitter(self):
        for _ in range(50):
            lo, hi = self._expected_bounds(0)
            v = self.w._backoff(0)
            self.assertGreaterEqual(v, lo)
            self.assertLessEqual(v, hi)

    def test_backoff_capped(self):
        # 2^15 * 2 = 65536, way above cap=120. All samples must be within
        # cap * (1 ± jitter).
        lo = self.cap * (1 - self.jitter)
        hi = self.cap * (1 + self.jitter)
        for _ in range(50):
            v = self.w._backoff(15)
            self.assertGreaterEqual(v, lo)
            self.assertLessEqual(v, hi)

    def test_backoff_monotone_in_expectation(self):
        # Sample many times; lower attempts have lower mean delays (until cap).
        samples = lambda n: sum(self.w._backoff(n) for _ in range(50)) / 50
        self.assertLess(samples(0), samples(2))
        self.assertLess(samples(2), samples(4))


class DryRunInvariantTests(unittest.TestCase):
    """In dry-run mode, _send must never actually invoke subprocess."""

    def test_dry_run_skips_subprocess(self):
        w = wd.Watchdog(sessions=["t"], interval=30, dry_run=True)
        original_run = wd.subprocess.run
        called = {"count": 0}

        def fake_run(*args, **kwargs):
            called["count"] += 1
            return original_run(["true"], capture_output=True)

        wd.subprocess.run = fake_run
        try:
            w._send("t", "C-u", desc="test")
            w._send_continue("t")
        finally:
            wd.subprocess.run = original_run
        self.assertEqual(called["count"], 0)


class RecentErrorRecoveryTests(unittest.TestCase):
    def test_stuck_after_error_retries_continue(self):
        w = wd.Watchdog(sessions=["t"], interval=30, dry_run=True)
        sends = []
        w._session_exists = lambda session: True
        w._claude_running = lambda session: True
        panes = iter([
            "API Error: 529\n❯ ",
            "Press up to edit queued messages\n❯ ",
            "Press up to edit queued messages\n❯ ",
        ])
        w._capture = lambda session, lines=50: next(panes)
        w._send_continue = lambda session: sends.append(session)

        w.check("t")
        w.check("t")
        w.check("t")

        self.assertEqual(sends, ["t"])

    def test_idle_queued_message_prompt_retries_without_recent_error(self):
        w = wd.Watchdog(sessions=["t"], interval=30, dry_run=True)
        sends = []
        w._session_exists = lambda session: True
        w._claude_running = lambda session: True
        panes = iter([
            "Press up to edit queued messages\n❯ ",
            "Press up to edit queued messages\n❯ ",
        ])
        w._capture = lambda session, lines=50: next(panes)
        w._send_continue = lambda session: sends.append(session)

        w.check("t")
        w.check("t")

        self.assertEqual(sends, ["t"])


class PatternListSanityTests(unittest.TestCase):
    """Pattern-list sanity. These are the load-bearing strings the watchdog
    classifies on; verify they are non-empty and disjoint enough that the
    audit-flagged false-positives don't sneak back in."""

    def test_no_bare_ECONNRESET_in_transient(self):
        # Bare ECONNRESET was deliberately removed per Gaia 2026-05-22
        # because developers writing connection handling have it on screen.
        self.assertNotIn("ECONNRESET", wd.TRANSIENT_PATTERNS)

    def test_no_bare_529_in_transient(self):
        # Bare '529' (without 'API Error:' anchor) was removed for the same
        # reason — devs writing HTTP handlers have it on screen.
        self.assertNotIn("529", wd.TRANSIENT_PATTERNS)
        self.assertNotIn("overloaded", wd.TRANSIENT_PATTERNS)

    def test_api_error_anchored_strings_present(self):
        self.assertIn("API Error: 529", wd.TRANSIENT_PATTERNS)
        self.assertIn("API Error: Request rejected (429)", wd.TRANSIENT_PATTERNS)
        self.assertIn("Unable to connect to API", wd.TRANSIENT_PATTERNS)

    def test_usage_limit_strings_match_current_tui(self):
        # Per Clarity audit 2026-05-22: current TUI renders
        # "You've hit your <session|weekly|Opus> limit · resets <time>".
        self.assertIn("You've hit your session limit", wd.USAGE_LIMIT_PATTERNS)
        self.assertIn("You've hit your weekly limit", wd.USAGE_LIMIT_PATTERNS)
        self.assertIn("You've hit your Opus limit", wd.USAGE_LIMIT_PATTERNS)
        self.assertIn("resets ", wd.USAGE_LIMIT_PATTERNS)

    def test_default_resume_cmd_is_empty(self):
        # Fail-closed: empty default = escalate-only on dead processes.
        # This is the safety-critical default; do not regress.
        self.assertEqual(wd.DEFAULT_RESUME_CMD, "")


if __name__ == "__main__":
    unittest.main()
