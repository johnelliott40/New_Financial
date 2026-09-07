"""
Tests for modules/expense_ratio_overrides.py — now a read-only legacy migration seed (Module 1
amendment, 2026-08-08). No more save_override(): nothing writes to this file anymore, since expense
ratio now lives in each ticker's own ticker_universe entry, which already round-trips through named
Save/Load. See the module's own docstring and ui/sidebar.py's _migrate_ticker_universe.
"""

import json

from modules.expense_ratio_overrides import load_overrides


def test_load_overrides_empty_when_file_missing(tmp_path):
    assert load_overrides(path=tmp_path / "does_not_exist.json") == {}


def test_load_overrides_reads_existing_file(tmp_path):
    path = tmp_path / "expense_ratio_overrides.json"
    path.write_text(json.dumps({"FXAIX": 0.00015, "FSKAX": 0.00015}))

    assert load_overrides(path=path) == {"FXAIX": 0.00015, "FSKAX": 0.00015}
