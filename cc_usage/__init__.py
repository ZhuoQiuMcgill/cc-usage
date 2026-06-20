"""ccusage — a keyboard-first interactive TUI of Claude Code usage across all sessions.

Token counts + API-equivalent cost parsed from local transcripts, a compact usage
heartbeat, rolling spend windows (1h/5h/24h/7d/all-time), plus the official 5-hour /
7-day subscription limits captured (reversibly) from the statusline. Everything —
viewing, switching views, and all configuration — is driven by arrow keys + Enter.
"""

__version__ = "2.0.0"
