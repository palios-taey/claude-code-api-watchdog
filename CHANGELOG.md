# Changelog

## v1.0.2 - 2026-05-27

- Fixed a missed transient pattern: `API Error: Overloaded` now classifies as recoverable and triggers the normal `Continue` path.
- Audit result: no additional raw pane literals were confirmed from the available 30-day watchdog logs; those logs primarily record recovery outcomes rather than the original rendered error text.

## v1.0.1 - 2026-05-27

- Added per-poll structured watchdog logs.
- Added cleared-error tracking and idle queued-message prompt recovery.
