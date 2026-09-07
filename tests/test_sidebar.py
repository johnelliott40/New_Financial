"""Tests for ui/sidebar.py's pure helper functions (no Streamlit context needed)."""

from __future__ import annotations

import pytest

from ui.sidebar import _migrate_contribution_entry


class TestMigrateContributionEntry:
    """CONTRIBUTION_TOGGLE_REDESIGN.md §7 (2026-08-16) — migrating a pre-redesign
    `contribution_by_year` entry (six raw dollar fields) into the current mode-based shape."""

    def test_already_new_shape_is_a_no_op(self):
        entry = {
            "contribution_401k_mode": "max_pretax",
            "contribution_401k_custom_amount": 0.0,
            "contribution_401k_custom_type": "pretax",
            "maximize_se_employer_401k": True,
            "roth_ira_mode": "maximize",
            "roth_ira_custom_amount": 0.0,
            "traditional_ira_mode": "maximize",
            "traditional_ira_custom_amount": 0.0,
        }
        assert _migrate_contribution_entry(entry) == entry

    def test_pretax_401k_only_migrates_to_custom_pretax(self):
        entry = {"w2_401k_contribution": 10000.0}
        migrated = _migrate_contribution_entry(entry)
        assert migrated["contribution_401k_mode"] == "custom"
        assert migrated["contribution_401k_custom_type"] == "pretax"
        assert migrated["contribution_401k_custom_amount"] == 10000.0
        assert migrated["maximize_se_employer_401k"] is False

    def test_roth_401k_only_migrates_to_custom_roth(self):
        entry = {"roth_401k_contribution": 8000.0}
        migrated = _migrate_contribution_entry(entry)
        assert migrated["contribution_401k_custom_type"] == "roth"
        assert migrated["contribution_401k_custom_amount"] == 8000.0

    def test_se_employee_deferral_folds_into_the_combined_custom_amount(self):
        entry = {"w2_401k_contribution": 10000.0, "se_401k_employee_contribution": 5000.0}
        migrated = _migrate_contribution_entry(entry)
        assert migrated["contribution_401k_custom_type"] == "pretax"
        assert migrated["contribution_401k_custom_amount"] == pytest.approx(15000.0)

    def test_se_employer_contribution_migrates_to_the_maximize_checkbox(self):
        entry = {"se_401k_employer_contribution": 3000.0}
        migrated = _migrate_contribution_entry(entry)
        assert migrated["maximize_se_employer_401k"] is True

    def test_larger_of_pretax_or_roth_wins_the_custom_type(self):
        entry = {"w2_401k_contribution": 2000.0, "roth_401k_contribution": 9000.0}
        migrated = _migrate_contribution_entry(entry)
        assert migrated["contribution_401k_custom_type"] == "roth"
        assert migrated["contribution_401k_custom_amount"] == pytest.approx(11000.0)

    def test_ira_fields_migrate_exactly(self):
        entry = {"roth_ira_contribution": 7500.0, "traditional_ira_contribution": 1200.0}
        migrated = _migrate_contribution_entry(entry)
        assert migrated["roth_ira_mode"] == "custom"
        assert migrated["roth_ira_custom_amount"] == 7500.0
        assert migrated["traditional_ira_mode"] == "custom"
        assert migrated["traditional_ira_custom_amount"] == 1200.0

    def test_empty_entry_migrates_to_all_zero_custom(self):
        migrated = _migrate_contribution_entry({})
        assert migrated["contribution_401k_custom_amount"] == 0.0
        assert migrated["roth_ira_custom_amount"] == 0.0
        assert migrated["traditional_ira_custom_amount"] == 0.0
        assert migrated["maximize_se_employer_401k"] is False
