"""
Legacy expense-ratio data, kept as a read-only migration seed.

Before the Module 1 amendment on 2026-08-08 (expense ratio and dividend rate are now entered
directly when a ticker is added — see ui/portfolio_tab.py — never fetched from Yahoo Finance),
this file was a live, growing store of manual corrections to Yahoo's often-wrong expense-ratio
data, written to on every edit. It no longer gets written to: expense ratio is now part of each
ticker's own entry in ticker_universe, which already round-trips through named Save/Load, so a
second persistent store for the same value would just be a second source of truth for no reason.

This module is kept, read-only, purely so `ui/sidebar.py`'s migration of pre-amendment saves (whose
ticker_universe only has a bare asset-class string per ticker, no expense ratio at all) has
somewhere better than 0.0 to seed a ticker's expense ratio from, when that ticker's old
manual_quotes entry doesn't have one either. See CLAUDE.md's standing rule against discarding a
user's existing data without a clear reason.
"""

from __future__ import annotations

import json
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "data" / "expense_ratio_overrides.json"


def load_overrides(path: Path = _PATH) -> dict[str, float]:
    """Returns {ticker: expense_ratio}. Empty dict if the file doesn't exist yet."""
    if not path.exists():
        return {}
    with open(path, "r") as f:
        return json.load(f)
