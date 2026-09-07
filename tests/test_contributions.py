"""
Tests for modules/contributions.py — the per-destination contribution toggle system
(CONTRIBUTION_TOGGLE_REDESIGN.md, 2026-08-16).
"""

from __future__ import annotations

import pytest

from modules.contributions import (
    fund_from_available_cash,
    resolve_401k_target,
    resolve_roth_ira_target,
    resolve_traditional_ira_target,
)
from modules.tax import compute_max_401k_contributions, load_bracket_table, roth_ira_phase_out_max


@pytest.fixture
def bracket_table():
    return load_bracket_table()


class TestResolve401kTarget:
    def test_max_pretax_targets_full_statutory_caps(self, bracket_table):
        maxed = compute_max_401k_contributions(2026, 35, 100000.0, 0.0, bracket_table)
        target = resolve_401k_target("max_pretax", 0.0, "pretax", False, 2026, 35, 100000.0, 0.0, bracket_table)
        assert target["w2_pretax_target"] == pytest.approx(maxed["max_w2_employee_deferral"])
        assert target["se_employee_pretax_target"] == pytest.approx(maxed["max_se_employee_deferral"])
        assert target["w2_roth_target"] == 0.0
        assert target["se_employee_roth_target"] == 0.0
        assert target["over_cap_warning"] is None

    def test_max_roth_targets_employee_side_as_roth(self, bracket_table):
        maxed = compute_max_401k_contributions(2026, 35, 100000.0, 0.0, bracket_table)
        target = resolve_401k_target("max_roth", 0.0, "pretax", False, 2026, 35, 100000.0, 0.0, bracket_table)
        assert target["w2_roth_target"] == pytest.approx(maxed["max_w2_employee_deferral"])
        assert target["se_employee_roth_target"] == pytest.approx(maxed["max_se_employee_deferral"])
        assert target["w2_pretax_target"] == 0.0
        assert target["se_employee_pretax_target"] == 0.0

    def test_se_employer_target_independent_of_employee_mode(self, bracket_table):
        # also_maximize_se_employer is its own toggle, checked with real SE income, applying
        # identically whether the employee side is maxed pretax or maxed Roth.
        maxed = compute_max_401k_contributions(2026, 35, 100000.0, 50000.0, bracket_table)
        pretax = resolve_401k_target("max_pretax", 0.0, "pretax", True, 2026, 35, 100000.0, 50000.0, bracket_table)
        roth = resolve_401k_target("max_roth", 0.0, "pretax", True, 2026, 35, 100000.0, 50000.0, bracket_table)
        assert pretax["se_employer_target"] == pytest.approx(maxed["max_se_employer_contribution"])
        assert roth["se_employer_target"] == pytest.approx(maxed["max_se_employer_contribution"])
        assert pretax["se_employer_target"] > 0

    def test_se_employer_target_zero_when_not_checked(self, bracket_table):
        target = resolve_401k_target("max_pretax", 0.0, "pretax", False, 2026, 35, 100000.0, 50000.0, bracket_table)
        assert target["se_employer_target"] == 0.0

    def test_se_employer_target_zero_with_no_se_income_even_if_checked(self, bracket_table):
        target = resolve_401k_target("max_pretax", 0.0, "pretax", True, 2026, 35, 100000.0, 0.0, bracket_table)
        assert target["se_employer_target"] == 0.0

    def test_custom_pretax_within_w2_capacity_stays_entirely_in_w2(self, bracket_table):
        # A small custom amount, well under the W-2 plan's own capacity -- none should roll over.
        target = resolve_401k_target("custom", 5000.0, "pretax", False, 2026, 35, 100000.0, 0.0, bracket_table)
        assert target["w2_pretax_target"] == pytest.approx(5000.0)
        assert target["se_employee_pretax_target"] == 0.0
        assert target["over_cap_warning"] is None

    def test_custom_over_w2_capacity_rolls_into_se_employee(self, bracket_table):
        maxed = compute_max_401k_contributions(2026, 35, 20000.0, 100000.0, bracket_table)
        w2_cap = maxed["max_w2_employee_deferral"]
        custom = w2_cap + 3000.0
        target = resolve_401k_target("custom", custom, "pretax", False, 2026, 35, 20000.0, 100000.0, bracket_table)
        assert target["w2_pretax_target"] == pytest.approx(w2_cap)
        assert target["se_employee_pretax_target"] == pytest.approx(3000.0)

    def test_custom_50000_pretax_clamped_to_statutory_ceiling(self, bracket_table):
        # From CONTRIBUTION_TOGGLE_REDESIGN.md §10's own testing checklist: custom $50,000 pretax
        # with a compensation/statutory ceiling of $24,500 -> w2_pretax_target == 24500.0, never
        # the entered amount, plus a warning.
        maxed = compute_max_401k_contributions(2026, 35, 100000.0, 0.0, bracket_table)
        assert maxed["max_w2_employee_deferral"] == pytest.approx(24500.0)  # sanity check the fixture
        target = resolve_401k_target("custom", 50000.0, "pretax", False, 2026, 35, 100000.0, 0.0, bracket_table)
        assert target["w2_pretax_target"] == pytest.approx(24500.0)
        assert target["se_employee_pretax_target"] == 0.0  # no SE income to roll into
        assert target["over_cap_warning"] is not None
        assert "24,500" in target["over_cap_warning"]

    def test_custom_roth_type_targets_roth_not_pretax(self, bracket_table):
        target = resolve_401k_target("custom", 5000.0, "roth", False, 2026, 35, 100000.0, 0.0, bracket_table)
        assert target["w2_roth_target"] == pytest.approx(5000.0)
        assert target["w2_pretax_target"] == 0.0

    def test_zero_w2_and_se_income_produces_zero_everywhere(self, bracket_table):
        target = resolve_401k_target("max_pretax", 0.0, "pretax", True, 2026, 35, 0.0, 0.0, bracket_table)
        assert target["w2_pretax_target"] == 0.0
        assert target["se_employee_pretax_target"] == 0.0
        assert target["se_employer_target"] == 0.0

    def test_invalid_mode_raises(self, bracket_table):
        with pytest.raises(ValueError):
            resolve_401k_target("bogus", 0.0, "pretax", False, 2026, 35, 100000.0, 0.0, bracket_table)

    def test_invalid_custom_type_raises(self, bracket_table):
        with pytest.raises(ValueError):
            resolve_401k_target("custom", 1000.0, "bogus", False, 2026, 35, 100000.0, 0.0, bracket_table)


class TestResolveRothIraTarget:
    def test_maximize_returns_full_phase_out_max_when_earned_income_gate_open(self, bracket_table):
        magi = 50000.0
        phase_out_max = roth_ira_phase_out_max(2026, "single", 35, magi, bracket_table)
        result = resolve_roth_ira_target("maximize", 0.0, 2026, "single", 35, magi, phase_out_max, bracket_table)
        assert result == pytest.approx(phase_out_max)

    def test_custom_5000_clamped_to_3000_legal_limit(self, bracket_table):
        # The spec's own worked example: enter $5,000, real legal limit (via a MAGI constructed to
        # force partial phase-out) is $3,000 -> roth_ira_used == 3000.0.
        # Find a MAGI that phases the statutory limit down to exactly $3,000 for a single filer.
        statutory = roth_ira_phase_out_max(2026, "single", 35, 0.0, bracket_table)
        phase_out = bracket_table[2026]["ira_limits"]["roth_phase_out"]["single"]
        lower, upper = phase_out["lower"], phase_out["upper"]
        # Solve for magi where raw = statutory * (1 - (magi-lower)/(upper-lower)) == 3000, then
        # nudge to the nearest value whose rounded-up-to-$10 result is exactly 3000.
        target_raw = 3000.0
        magi = lower + (1 - target_raw / statutory) * (upper - lower)
        actual_max = roth_ira_phase_out_max(2026, "single", 35, magi, bracket_table)
        assert actual_max == pytest.approx(3000.0)  # confirms the constructed MAGI is right
        result = resolve_roth_ira_target("custom", 5000.0, 2026, "single", 35, magi, actual_max, bracket_table)
        assert result == pytest.approx(3000.0)

    def test_earned_income_gate_caps_below_magi_phase_out_math(self, bracket_table):
        # IRC §219(f)(1): combined_ira_limit=0.0 (no earned income) must cap Roth at $0 regardless
        # of how much MAGI phase-out room exists on paper.
        magi = 0.0  # well under the phase-out floor -- full statutory room on MAGI grounds alone
        result = resolve_roth_ira_target("maximize", 0.0, 2026, "single", 35, magi, 0.0, bracket_table)
        assert result == 0.0

    def test_mfs_filing_status_treated_as_zero_room_not_a_crash(self, bracket_table):
        # roth_ira_phase_out_max itself raises for "mfs" -- this function pre-checks defensively.
        result = resolve_roth_ira_target("maximize", 0.0, 2026, "mfs", 35, 50000.0, 10000.0, bracket_table)
        assert result == 0.0

    def test_invalid_mode_raises(self, bracket_table):
        with pytest.raises(ValueError):
            resolve_roth_ira_target("bogus", 0.0, 2026, "single", 35, 50000.0, 7000.0, bracket_table)


class TestResolveTraditionalIraTarget:
    def test_maximize_gets_combined_limit_minus_roth_used(self, bracket_table):
        result = resolve_traditional_ira_target("maximize", 0.0, combined_ira_limit=7000.0, roth_ira_used=3000.0)
        assert result == pytest.approx(4000.0)

    def test_custom_clamped_to_remaining_room_after_roth(self, bracket_table):
        result = resolve_traditional_ira_target("custom", 10000.0, combined_ira_limit=7000.0, roth_ira_used=3000.0)
        assert result == pytest.approx(4000.0)

    def test_custom_within_room_uses_entered_amount(self, bracket_table):
        result = resolve_traditional_ira_target("custom", 1000.0, combined_ira_limit=7000.0, roth_ira_used=3000.0)
        assert result == pytest.approx(1000.0)

    def test_roth_used_at_full_combined_limit_leaves_zero_room(self, bracket_table):
        result = resolve_traditional_ira_target("maximize", 0.0, combined_ira_limit=7000.0, roth_ira_used=7000.0)
        assert result == 0.0

    def test_combined_never_exceeds_limit_across_mode_combinations(self, bracket_table):
        combined_limit = 7000.0
        for roth_mode in ("maximize", "custom"):
            for trad_mode in ("maximize", "custom"):
                roth_custom = 20000.0 if roth_mode == "custom" else 0.0
                roth_used = min(roth_custom, combined_limit) if roth_mode == "custom" else combined_limit
                trad_custom = 20000.0 if trad_mode == "custom" else 0.0
                trad_used = resolve_traditional_ira_target(trad_mode, trad_custom, combined_limit, roth_used)
                assert roth_used + trad_used <= combined_limit + 1e-9

    def test_invalid_mode_raises(self, bracket_table):
        with pytest.raises(ValueError):
            resolve_traditional_ira_target("bogus", 0.0, combined_ira_limit=7000.0, roth_ira_used=0.0)


class TestFundFromAvailableCash:
    def test_ample_cash_funds_both_ira_targets_and_leaves_rest_for_taxable(self):
        result = fund_from_available_cash(roth_ira_target=3000.0, traditional_ira_target=2000.0, available_cash=10000.0)
        assert result["roth_ira_used"] == pytest.approx(3000.0)
        assert result["traditional_ira_used"] == pytest.approx(2000.0)
        assert result["taxable_used"] == pytest.approx(5000.0)

    def test_cash_runs_out_partway_through_roth_ira(self):
        result = fund_from_available_cash(roth_ira_target=5000.0, traditional_ira_target=2000.0, available_cash=3000.0)
        assert result["roth_ira_used"] == pytest.approx(3000.0)
        assert result["traditional_ira_used"] == 0.0
        assert result["taxable_used"] == 0.0

    def test_cash_covers_roth_but_runs_out_partway_through_traditional(self):
        result = fund_from_available_cash(roth_ira_target=3000.0, traditional_ira_target=5000.0, available_cash=4000.0)
        assert result["roth_ira_used"] == pytest.approx(3000.0)
        assert result["traditional_ira_used"] == pytest.approx(1000.0)
        assert result["taxable_used"] == 0.0

    def test_zero_cash_funds_nothing(self):
        result = fund_from_available_cash(roth_ira_target=3000.0, traditional_ira_target=2000.0, available_cash=0.0)
        assert result == {"roth_ira_used": 0.0, "traditional_ira_used": 0.0, "taxable_used": 0.0}

    def test_zero_ira_targets_routes_everything_to_taxable(self):
        result = fund_from_available_cash(roth_ira_target=0.0, traditional_ira_target=0.0, available_cash=10000.0)
        assert result["taxable_used"] == pytest.approx(10000.0)

    def test_always_sums_to_available_cash_when_targets_exceed_it(self):
        result = fund_from_available_cash(roth_ira_target=100000.0, traditional_ira_target=100000.0, available_cash=15000.0)
        total = result["roth_ira_used"] + result["traditional_ira_used"] + result["taxable_used"]
        assert total == pytest.approx(15000.0)
