"""
Tests for modules/tax.py.

The seven integration fixtures in TestComputeTaxesFixtures mirror the original tax build spec's
§6 scenarios plus the addendum's fixture 7 (added when `taxable_retirement_withdrawal` was
introduced to close the NIIT leak in the original fixture 5). Expected values were computed with a
second, independently-written implementation (different structure, no shared code with
modules/tax.py — see scratchpad/verify_tax.py from the session that built the original module)
rather than hand-derived. This project has no access to a live third-party calculator
(SmartAsset/NerdWallet) to cross-check against — flagged here rather than silently omitted.

TestMaxCombinedEmployeeDeferral / TestAllocateEmployeeDeferral / TestMaxSeEmployerContribution
cover 401k_contribution_module_spec.md's own §6 required test cases (catch-up tiers, employer
contribution not touching W-2 deferral room, SE-vs-W2 deferral competing for the shared pool,
combined deferral never exceeding the pool, §415(c) room after an SE employee deferral with
catch-up added on top, comp caps binding, and missing-year errors). Expected dollar figures use the
2026 bracket_table fixture: elective_deferral_limit=24,500, catchup_50_to_59=8,000,
catchup_60_to_63=11,250, annual_additions_limit=72,000 (data/tax_brackets.json).
"""

from __future__ import annotations

import pytest

from modules.tax import (
    DEFERRAL_PRIORITIES,
    FILING_STATUSES,
    _marginal_rate,
    _progressive_tax,
    _qbi_deduction,
    _ss_taxability,
    _stacked_bracket_tax,
    allocate_employee_deferral,
    check_402g_limit,
    check_415c_limit,
    check_ira_combined_limit,
    check_roth_ira_limit,
    compute_max_401k_contributions,
    compute_taxes,
    employer_401k_match,
    load_bracket_table,
    load_rmd_table,
    max_combined_employee_deferral,
    max_ira_contribution_limit,
    max_se_employer_contribution,
    net_se_earnings_for_retirement,
    ordinary_bracket_ceiling,
    rmd_divisor,
    rmd_start_age,
    roth_ira_magi,
    roth_ira_phase_out_max,
)


@pytest.fixture
def bracket_table():
    return load_bracket_table()


# ---- load_bracket_table ----


class TestLoadBracketTable:
    def test_years_present_as_int_keys(self, bracket_table):
        assert 2025 in bracket_table
        assert 2026 in bracket_table
        assert all(isinstance(y, int) for y in bracket_table)

    def test_no_note_key_leaks_into_years(self, bracket_table):
        assert "_note" not in bracket_table

    def test_known_values_spot_check(self, bracket_table):
        # Spot-check a few numbers straight from the build spec / sourced research, so a typo in
        # the JSON gets caught even though nothing else in this file happens to exercise it.
        assert bracket_table[2026]["standard_deduction"]["mfj"] == 32200
        assert bracket_table[2025]["fica"]["ss_wage_base"] == 176100
        assert bracket_table[2026]["fica"]["ss_wage_base"] == 184500
        assert bracket_table[2025]["niit_threshold"]["mfj"] == 250000
        assert bracket_table[2026]["ca_mhst_threshold"] == 1000000

    def test_k401_limits_spot_check(self, bracket_table):
        assert bracket_table[2025]["k401_limits"]["annual_additions_limit"] == 70000
        assert bracket_table[2026]["k401_limits"]["annual_additions_limit"] == 72000
        assert bracket_table[2026]["k401_limits"]["elective_deferral_limit"] == 24500
        assert bracket_table[2026]["k401_limits"]["catchup_50_to_59"] == 8000
        assert bracket_table[2026]["k401_limits"]["catchup_60_to_63"] == 11250


# ---- pure helper functions (existing, unrelated to 401(k)) ----


class TestProgressiveTax:
    def test_single_bracket_flat_rate(self):
        # All income in the first bracket: simple flat-rate check.
        brackets = [[0, 0.10], [1000, 0.20]]
        assert _progressive_tax(500, brackets) == pytest.approx(50.0)

    def test_spans_two_brackets(self):
        brackets = [[0, 0.10], [1000, 0.20]]
        # 1000 at 10% + 500 at 20% = 100 + 100 = 200
        assert _progressive_tax(1500, brackets) == pytest.approx(200.0)

    def test_negative_income_clamped_to_zero(self):
        brackets = [[0, 0.10], [1000, 0.20]]
        assert _progressive_tax(-500, brackets) == pytest.approx(0.0)

    def test_zero_income(self):
        brackets = [[0, 0.10], [1000, 0.20]]
        assert _progressive_tax(0, brackets) == pytest.approx(0.0)


class TestStackedBracketTax:
    def test_positive_floor_stacks_correctly(self):
        # Floor at 1000, 500 of amount stacked on top, all within the second bracket.
        brackets = [[0, 0.10], [1000, 0.20], [2000, 0.30]]
        assert _stacked_bracket_tax(1000, 500, brackets) == pytest.approx(100.0)

    def test_stack_spans_bracket_boundary(self):
        brackets = [[0, 0.10], [1000, 0.20], [2000, 0.30]]
        # Floor 1500, amount 1000 -> top 2500. 500 at 20% (1500-2000) + 500 at 30% (2000-2500).
        assert _stacked_bracket_tax(1500, 1000, brackets) == pytest.approx(100.0 + 150.0)

    def test_negative_floor_absorbs_low_end_at_zero_bracket(self):
        # Negative floor (deductions exceeded ordinary income): the negative portion is absorbed
        # for free rather than pushing the stack below the brackets' own lower bound of 0.
        brackets = [[0, 0.00], [1000, 0.15]]
        # floor=-500, amount=1500 -> top=1000. Segment [0,1000) is 0%, nothing reaches the 1000
        # lower bound of the 15% tier, so tax is 0.
        assert _stacked_bracket_tax(-500, 1500, brackets) == pytest.approx(0.0)
        # floor=-500, amount=2000 -> top=1500. [0,1000) at 0% + [1000,1500) at 15% = 500*0.15=75.
        assert _stacked_bracket_tax(-500, 2000, brackets) == pytest.approx(75.0)


class TestMarginalRate:
    def test_at_bracket_boundary(self):
        brackets = [[0, 0.10], [1000, 0.20], [2000, 0.30]]
        assert _marginal_rate(1000, brackets) == pytest.approx(0.20)

    def test_below_first_threshold(self):
        brackets = [[0, 0.10], [1000, 0.20]]
        assert _marginal_rate(500, brackets) == pytest.approx(0.10)

    def test_above_top_bracket(self):
        brackets = [[0, 0.10], [1000, 0.20], [2000, 0.30]]
        assert _marginal_rate(5000, brackets) == pytest.approx(0.30)

    def test_zero_or_negative_uses_first_bracket(self):
        brackets = [[0, 0.10], [1000, 0.20]]
        assert _marginal_rate(0, brackets) == pytest.approx(0.10)
        assert _marginal_rate(-100, brackets) == pytest.approx(0.10)


class TestQbiDeduction:
    THRESHOLDS = {"single": {"full_deduction_ceiling": 200000, "phaseout_ceiling": 250000}}

    def test_zero_or_negative_base(self):
        assert _qbi_deduction(0, "single", self.THRESHOLDS) == 0.0
        assert _qbi_deduction(-1000, "single", self.THRESHOLDS) == 0.0

    def test_full_deduction_below_ceiling(self):
        assert _qbi_deduction(100000, "single", self.THRESHOLDS) == pytest.approx(20000.0)

    def test_zero_at_or_above_phaseout_ceiling(self):
        assert _qbi_deduction(250000, "single", self.THRESHOLDS) == pytest.approx(0.0)
        assert _qbi_deduction(300000, "single", self.THRESHOLDS) == pytest.approx(0.0)

    def test_midpoint_of_phase_in_is_half_the_full_deduction(self):
        # Exactly halfway between 200,000 and 250,000 -> 50% of the full 20% deduction.
        base = 225000
        expected = 0.5 * (0.20 * base)
        assert _qbi_deduction(base, "single", self.THRESHOLDS) == pytest.approx(expected)


class TestSsTaxability:
    def test_below_tier1_not_taxable(self):
        assert _ss_taxability(20000, 20000, 25000, 34000) == pytest.approx(0.0)

    def test_between_tiers_fifty_percent_formula(self):
        # provisional=30000, tier1=25000: 0.5*(30000-25000)=2500, vs 0.5*benefit=10000 -> min=2500
        assert _ss_taxability(30000, 20000, 25000, 34000) == pytest.approx(2500.0)

    def test_above_tier2_eighty_five_percent_formula(self):
        # tier1=25000, tier2=34000, benefit=20000
        # tier1_amount = min(0.5*9000, 0.5*20000) = min(4500, 10000) = 4500
        # taxable = 4500 + 0.85*(40000-34000) = 4500 + 5100 = 9600, capped at 0.85*20000=17000
        assert _ss_taxability(40000, 20000, 25000, 34000) == pytest.approx(9600.0)

    def test_capped_at_eighty_five_percent_of_benefit(self):
        # Very high provisional income should still cap at 85% of the benefit itself.
        assert _ss_taxability(1_000_000, 20000, 25000, 34000) == pytest.approx(0.85 * 20000)


# ---- 401(k) / solo 401(k) contribution-cap functions ----


class TestMaxCombinedEmployeeDeferral:
    def test_catch_up_tiers(self, bracket_table):
        # 2026: base 24,500; ages 50-59 & 64+ get standard +8,000; ages 60-63 get enhanced +11,250.
        assert max_combined_employee_deferral(2026, 49, bracket_table) == pytest.approx(24500.0)
        assert max_combined_employee_deferral(2026, 50, bracket_table) == pytest.approx(32500.0)
        assert max_combined_employee_deferral(2026, 59, bracket_table) == pytest.approx(32500.0)
        assert max_combined_employee_deferral(2026, 60, bracket_table) == pytest.approx(35750.0)
        assert max_combined_employee_deferral(2026, 63, bracket_table) == pytest.approx(35750.0)
        assert max_combined_employee_deferral(2026, 64, bracket_table) == pytest.approx(32500.0)

    def test_pool_is_a_flat_limit_not_prorated_by_comp(self, bracket_table):
        # The pool is a pure function of (year, age) — no comp/partial-year concept exists here at
        # all; only allocate_employee_deferral's per-source caps respond to how much was earned.
        assert max_combined_employee_deferral(2026, 35, bracket_table) == pytest.approx(24500.0)

    def test_missing_year_raises(self, bracket_table):
        with pytest.raises(ValueError, match="1999"):
            max_combined_employee_deferral(1999, 35, bracket_table)


class TestAllocateEmployeeDeferral:
    def test_w2_first_priority_fills_w2_before_se(self, bracket_table):
        # Both sources individually exceed the pool -> w2-first exhausts the pool on W-2, leaving
        # nothing for SE, even though SE comp alone could have absorbed the whole pool too.
        result = allocate_employee_deferral(2026, 35, 100000, 100000, "w2-first", bracket_table)
        assert result["max_w2_deferral"] == pytest.approx(24500.0)
        assert result["max_se_employee_deferral"] == pytest.approx(0.0)

    def test_se_first_priority_fills_se_before_w2(self, bracket_table):
        result = allocate_employee_deferral(2026, 35, 100000, 100000, "se-first", bracket_table)
        assert result["max_se_employee_deferral"] == pytest.approx(24500.0)
        assert result["max_w2_deferral"] == pytest.approx(0.0)

    def test_comp_caps_bind_below_pool(self, bracket_table):
        # Low comp on both sides -> each capped by its own earnings, not by the (much larger) pool.
        result = allocate_employee_deferral(2026, 35, 10000, 5000, "w2-first", bracket_table)
        assert result["max_w2_deferral"] == pytest.approx(10000.0)
        assert result["max_se_employee_deferral"] == pytest.approx(5000.0)

    def test_combined_never_exceeds_pool(self, bracket_table):
        pool = max_combined_employee_deferral(2026, 35, bracket_table)
        for w2_comp in (0, 5000, 24500, 50000, 200000):
            for se_comp in (0, 5000, 24500, 50000, 200000):
                for priority in DEFERRAL_PRIORITIES:
                    result = allocate_employee_deferral(2026, 35, w2_comp, se_comp, priority, bracket_table)
                    assert result["max_w2_deferral"] + result["max_se_employee_deferral"] <= pool + 1e-9

    def test_invalid_priority_raises(self, bracket_table):
        with pytest.raises(ValueError, match="priority"):
            allocate_employee_deferral(2026, 35, 100000, 100000, "random", bracket_table)

    def test_missing_year_raises(self, bracket_table):
        with pytest.raises(ValueError, match="1999"):
            allocate_employee_deferral(1999, 35, 100000, 100000, "w2-first", bracket_table)


class TestMaxSeEmployerContribution:
    # Signature is (tax_year, age_at_year_end, net_se_earnings_for_retirement,
    # se_employee_deferral_used, bracket_table) — age_at_year_end is the 2nd positional argument,
    # per the finalized build spec (a first draft omitted it from the declared signature while
    # still requiring catch-up-dependent behavior in prose; this position is the resolved version).

    def test_profit_sharing_formula_when_415c_room_plentiful(self, bracket_table):
        # Net SE earnings low enough that the 20% formula binds, not §415(c).
        assert max_se_employer_contribution(2026, 35, 50000, 0, bracket_table) == pytest.approx(10000.0)

    def test_415c_room_binds_over_large_profit_sharing_formula(self, bracket_table):
        # 20% of 500,000 = 100,000, far above the 72,000 - 10,000 = 62,000 remaining §415(c) room.
        assert max_se_employer_contribution(2026, 35, 500000, 10000, bracket_table) == pytest.approx(62000.0)

    def test_catch_up_added_to_415c_ceiling_at_standard_tier(self, bracket_table):
        # Spec's own test case 5 calls out running this at BOTH the standard (50-59) and enhanced
        # (60-63) catch-up tiers, not just one — confirms age_at_year_end actually widens the
        # §415(c) ceiling before remaining room is computed, not just accepted and ignored.
        no_catchup = max_se_employer_contribution(2026, 35, 500000, 10000, bracket_table)
        with_standard_catchup = max_se_employer_contribution(2026, 50, 500000, 10000, bracket_table)
        assert with_standard_catchup == pytest.approx(no_catchup + 8000.0)

    def test_catch_up_added_to_415c_ceiling_at_enhanced_tier(self, bracket_table):
        no_catchup = max_se_employer_contribution(2026, 35, 500000, 10000, bracket_table)
        with_enhanced_catchup = max_se_employer_contribution(2026, 60, 500000, 10000, bracket_table)
        # Ages 60-63 get the enhanced +11,250 catch-up added to the §415(c) ceiling itself, not to
        # the (separate) §402(g) pool.
        assert with_enhanced_catchup == pytest.approx(no_catchup + 11250.0)

    def test_employee_deferral_used_reduces_remaining_room(self, bracket_table):
        low_deferral = max_se_employer_contribution(2026, 35, 500000, 0, bracket_table)
        high_deferral = max_se_employer_contribution(2026, 35, 500000, 24500, bracket_table)
        assert high_deferral == pytest.approx(low_deferral - 24500.0)

    def test_reported_16000_net_se_profit_scenario_returns_zero(self, bracket_table):
        """
        Spec test case 6: reproduces the exact reported bug. $16,000 net SE profit -> net SE
        earnings for retirement ≈ $14,869.64 (16000 * 0.9235, minus half the resulting SE tax —
        same formula compute_taxes itself uses). With employee deferral maxed at that full
        $14,869.64, there is zero compensation room left for an employer contribution — the
        20%-formula (~$2,973.93) and §415(c) constraints alone would both wrongly allow a nonzero
        figure, which is exactly the bug this fixes: the missing "100% of compensation" cap
        (remaining_comp_room) alongside the profit-sharing formula and §415(c).
        """
        se_tax_base = 16000 * 0.9235
        se_tax = se_tax_base * 0.153  # SE wages here are far under the SS wage base, so no capping
        net_se_earnings = 16000 - se_tax / 2
        assert net_se_earnings == pytest.approx(14869.64, abs=0.01)

        result = max_se_employer_contribution(2026, 35, net_se_earnings, net_se_earnings, bracket_table)
        assert result == pytest.approx(0.0)
        # The naive two-constraint (formula, §415(c)) answer would have been ~2,973.93 — confirm
        # this isn't silently still returning that.
        assert result != pytest.approx(0.20 * net_se_earnings, abs=0.01)

    def test_employer_plus_employee_never_exceeds_net_se_earnings(self, bracket_table):
        # General property behind the reported case: across a range of net SE earnings and
        # employee-deferral levels, employer room must never let the combined total exceed net SE
        # earnings itself, regardless of what the 20%-formula or §415(c) alone would allow.
        for net_se_earnings in (0, 5000, 14869.64, 50000, 100000, 500000):
            for deferral_fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
                deferral_used = net_se_earnings * deferral_fraction
                employer_max = max_se_employer_contribution(2026, 35, net_se_earnings, deferral_used, bracket_table)
                assert deferral_used + employer_max <= net_se_earnings + 1e-6

    def test_never_negative(self, bracket_table):
        # An SE employee deferral larger than the §415(c) ceiling itself must floor the employer
        # room at 0, not go negative.
        assert max_se_employer_contribution(2026, 35, 500000, 999999, bracket_table) == 0.0

    def test_missing_year_raises(self, bracket_table):
        with pytest.raises(ValueError, match="1999"):
            max_se_employer_contribution(1999, 35, 50000, 0, bracket_table)


# ---- net_se_earnings_for_retirement (standalone) ----


class TestNetSeEarningsForRetirementStandalone:
    def test_matches_compute_taxes_own_internal_value(self, bracket_table):
        # The standalone version must never drift from what compute_taxes computes internally, or a
        # caller that clamps against it (see modules/projection.py) could still trigger
        # compute_taxes's own stricter raise.
        result = compute_taxes(
            tax_year=2026, filing_status="single", age_at_year_end=35, w2_gross=40000,
            pretax_401k=0.0, roth_401k=0.0, pretax_health_dental=0.0, se_net_profit=16000,
            se_solo_employee_deferral=0.0, se_solo_employer_contribution=0.0,
            taxable_retirement_withdrawal=0.0, ltcg=0.0, qualified_dividends=0.0,
            ordinary_dividends=0.0, interest_income=0.0, ss_benefit_gross=0.0,
            bracket_table=bracket_table,
        )
        standalone = net_se_earnings_for_retirement(2026, 40000, 16000, bracket_table)
        assert standalone == pytest.approx(result["net_se_earnings_for_retirement"])

    def test_missing_year_raises(self, bracket_table):
        with pytest.raises(ValueError, match="1999"):
            net_se_earnings_for_retirement(1999, 40000, 16000, bracket_table)


# ---- compute_max_401k_contributions ----
# Built for an optimization spec's "Use Computed Maximum" checkbox + Tax tab max-contribution
# display (a Downloads-folder doc, not checked into this repo — see NEXT.md); also the shared
# capacity source `modules.contributions.resolve_401k_target` (CONTRIBUTION_TOGGLE_REDESIGN.md,
# 2026-08-16) builds every 401(k) mode's target from.


class TestComputeMax401kContributions:
    def test_w2_fills_pool_first_leaving_zero_se_employee_room(self, bracket_table):
        # $50,000 W-2 (well above the $24,500 pool) means the W-2 side alone consumes the entire
        # shared §402(g) pool, leaving $0 of room for the SE employee deferral — this is the "W-2
        # first" priority actually binding, not just accepted as a label.
        result = compute_max_401k_contributions(2026, 30, 50000, 20000, bracket_table)
        assert result["combined_employee_deferral_pool"] == pytest.approx(24500.0)
        assert result["max_w2_employee_deferral"] == pytest.approx(24500.0)
        assert result["max_se_employee_deferral"] == pytest.approx(0.0)
        # SE employer contribution is computed WITH the (zero) SE employee deferral already known —
        # min(20% of 20,000 = 4,000, remaining §415(c) room 72,000, remaining comp room 20,000).
        assert result["max_se_employer_contribution"] == pytest.approx(4000.0)

    def test_never_recreates_the_already_fixed_comp_room_bug(self, bracket_table):
        # The exact reported scenario from max_se_employer_contribution's own fixed bug (see
        # TestMaxSeEmployerContribution.test_reported_16000_net_se_profit_scenario_returns_zero),
        # run through the full priority chain this time. If this function computed the SE employer
        # contribution BEFORE the SE employee deferral (the optimization spec's literal stated
        # order), it would wrongly return ~$2,973.93 here instead of $0 — reproducing the exact bug
        # already fixed once. No W-2 income, so nothing competes with the SE side for the pool.
        se_tax_base = 16000 * 0.9235
        se_tax = se_tax_base * 0.153
        net_se_earnings = 16000 - se_tax / 2
        result = compute_max_401k_contributions(2026, 30, 0.0, net_se_earnings, bracket_table)
        assert result["max_se_employee_deferral"] == pytest.approx(net_se_earnings, abs=0.01)
        assert result["max_se_employer_contribution"] == pytest.approx(0.0)
        assert (
            result["max_se_employee_deferral"] + result["max_se_employer_contribution"]
            <= net_se_earnings + 1e-6
        )

    def test_property_never_exceeds_net_se_earnings_across_combinations(self, bracket_table):
        for w2 in (0, 20000, 60000, 200000):
            for se_earnings in (0, 5000, 30000, 100000):
                result = compute_max_401k_contributions(2026, 40, w2, se_earnings, bracket_table)
                assert (
                    result["max_se_employee_deferral"] + result["max_se_employer_contribution"]
                    <= se_earnings + 1e-6
                )


# ---- employer_401k_match (MODEL_WIRING.md §3.1/§3.2, 2026-08-10) ----


class TestEmployer401kMatch:
    def test_inert_when_both_inputs_are_zero(self):
        # The UI default (rate=0.0, cap=0.0) must provably produce $0 match for any income/deferral
        # — a user with no employer match sees no invented money.
        for w2_gross in (0.0, 50000.0, 250000.0):
            for deferral in (0.0, 5000.0, 24500.0):
                assert employer_401k_match(w2_gross, deferral, 0.0, 0.0) == 0.0

    def test_standard_50_percent_match_on_first_6_percent(self):
        # $100,000 W-2, deferring $10,000 (10%), 50% match capped at 6% of pay ($6,000 eligible).
        result = employer_401k_match(100000.0, 10000.0, 0.5, 0.06)
        assert result == pytest.approx(3000.0)  # 50% of min(10000, 6000)

    def test_deferral_below_the_cap_matches_the_full_deferral(self):
        # Deferring only 4% of pay, cap is 6% — the full deferral is matched, not the cap.
        result = employer_401k_match(100000.0, 4000.0, 0.5, 0.06)
        assert result == pytest.approx(2000.0)  # 50% of 4000

    def test_negative_inputs_never_produce_a_negative_match(self):
        assert employer_401k_match(-100.0, -100.0, 0.5, 0.06) == 0.0


# ---- IRA limits / Roth phase-out ----


class TestMaxIraContributionLimit:
    def test_2026_no_catchup(self, bracket_table):
        assert max_ira_contribution_limit(2026, 30, bracket_table) == pytest.approx(7500.0)

    def test_2026_with_catchup(self, bracket_table):
        assert max_ira_contribution_limit(2026, 55, bracket_table) == pytest.approx(8600.0)

    def test_2025_limits_differ_from_2026(self, bracket_table):
        assert max_ira_contribution_limit(2025, 30, bracket_table) == pytest.approx(7000.0)
        assert max_ira_contribution_limit(2025, 55, bracket_table) == pytest.approx(8000.0)

    def test_missing_year_raises(self, bracket_table):
        with pytest.raises(ValueError, match="1999"):
            max_ira_contribution_limit(1999, 30, bracket_table)


class TestRothIraMagi:
    def test_passthrough_of_federal_agi(self):
        assert roth_ira_magi(123456.78) == pytest.approx(123456.78)


class TestRothIraPhaseOutMax:
    def test_below_phase_out_gets_full_statutory_limit(self, bracket_table):
        assert roth_ira_phase_out_max(2026, "single", 30, 100000, bracket_table) == pytest.approx(7500.0)

    def test_at_or_above_ceiling_gets_zero(self, bracket_table):
        assert roth_ira_phase_out_max(2026, "single", 30, 168000, bracket_table) == pytest.approx(0.0)
        assert roth_ira_phase_out_max(2026, "single", 30, 200000, bracket_table) == pytest.approx(0.0)

    def test_inside_phase_out_prorated_and_rounded_to_nearest_10(self, bracket_table):
        # single/hoh range is $153,000-$168,000; at $160,000 the raw reduction is exactly $4,000 —
        # already a multiple of $10, so rounding is a no-op here (see the $200-floor case below for
        # rounding/flooring actually biting).
        assert roth_ira_phase_out_max(2026, "single", 30, 160000, bracket_table) == pytest.approx(4000.0)

    def test_near_ceiling_hits_the_200_dollar_floor(self, bracket_table):
        # $167,900 MAGI: raw reduction leaves only $50 of room — the IRS worksheet's $200 floor
        # (whenever the raw result is > $0) applies instead of the bare rounded $50.
        assert roth_ira_phase_out_max(2026, "single", 30, 167900, bracket_table) == pytest.approx(200.0)

    def test_hoh_shares_singles_range(self, bracket_table):
        assert roth_ira_phase_out_max(2026, "hoh", 30, 160000, bracket_table) == pytest.approx(4000.0)

    def test_mfj_uses_its_own_wider_range(self, bracket_table):
        assert roth_ira_phase_out_max(2026, "mfj", 30, 200000, bracket_table) == pytest.approx(7500.0)

    def test_catchup_eligible_gets_higher_statutory_limit_below_floor(self, bracket_table):
        assert roth_ira_phase_out_max(2026, "single", 55, 100000, bracket_table) == pytest.approx(8600.0)

    def test_mfs_raises_not_silently_wrong(self, bracket_table):
        with pytest.raises(ValueError, match="mfs"):
            roth_ira_phase_out_max(2026, "mfs", 30, 5000, bracket_table)

    def test_missing_year_raises(self, bracket_table):
        with pytest.raises(ValueError, match="1999"):
            roth_ira_phase_out_max(1999, "single", 30, 100000, bracket_table)


# ---- Contribution-limit warning functions ----


class TestCheck402gLimit:
    def test_under_pool_no_warning(self):
        assert check_402g_limit(10000, 0, 5000, 24500) is None

    def test_over_pool_warns_with_amount(self):
        warning = check_402g_limit(15000, 0, 10000, 24500)
        assert warning is not None
        assert "500" in warning


class TestCheck415cLimit:
    def test_under_ceiling_no_warning(self):
        assert check_415c_limit(30000, 30000, 72000) is None

    def test_over_ceiling_warns(self):
        warning = check_415c_limit(40000, 35000, 72000)
        assert warning is not None
        assert "3,000" in warning

    def test_does_not_combine_w2_and_se_plans(self):
        # A maxed W-2 plan's deferral is simply not a parameter here at all — confirms the per-plan
        # scope described in check_415c_limit's own docstring (see NEXT.md for the correction this
        # represents relative to an optimization spec's literal, incorrect combined-total wording).
        assert check_415c_limit(0, 0, 72000) is None

    def test_plan_label_defaults_to_se_solo_401k(self):
        warning = check_415c_limit(40000, 35000, 72000)
        assert warning.startswith("SE solo 401(k) annual additions")

    def test_plan_label_customizable_for_the_w2_plan(self):
        # MODEL_WIRING.md §3.1/§4.2 (2026-08-10) — the same function, called for the W-2 plan's own
        # separate §415(c) ceiling (employee deferral + employer match), with its own label.
        warning = check_415c_limit(40000, 35000, 72000, plan_label="W-2 401(k)")
        assert warning.startswith("W-2 401(k) annual additions")
        assert "3,000" in warning


class TestCheckRothIraLimit:
    def test_under_max_no_warning(self):
        assert check_roth_ira_limit(3000, 4000) is None

    def test_over_max_warns(self):
        warning = check_roth_ira_limit(5000, 4000)
        assert warning is not None
        assert "1,000" in warning


class TestCheckIraCombinedLimit:
    def test_under_combined_limit_no_warning(self):
        assert check_ira_combined_limit(3000, 3000, 7500) is None

    def test_over_combined_limit_warns_even_if_roth_alone_is_fine(self):
        # Roth alone ($4,000) could be under its own phased max, but Traditional + Roth together
        # ($9,000) still exceeds the single combined §219(b) limit ($7,500).
        warning = check_ira_combined_limit(5000, 4000, 7500)
        assert warning is not None
        assert "1,500" in warning


# ---- MODULE_G2_STEP7_BRACKET_AWARE_WITHDRAWALS.md Part 1 (2026-08-23): bracket-aware draw + RMDs ----


class TestOrdinaryBracketCeiling:
    def test_matches_known_2026_single_filer_boundary(self, bracket_table):
        single = bracket_table[2026]["federal_brackets"]["single"]
        assert ordinary_bracket_ceiling(single, 0.22) == pytest.approx(105700)
        assert ordinary_bracket_ceiling(single, 0.10) == pytest.approx(12400)
        assert ordinary_bracket_ceiling(single, 0.12) == pytest.approx(50400)

    def test_top_bracket_returns_infinity(self, bracket_table):
        single = bracket_table[2026]["federal_brackets"]["single"]
        assert ordinary_bracket_ceiling(single, 0.37) == float("inf")

    def test_rate_not_in_table_raises(self, bracket_table):
        single = bracket_table[2026]["federal_brackets"]["single"]
        with pytest.raises(ValueError):
            ordinary_bracket_ceiling(single, 0.99)


class TestRmdStartAge:
    def test_born_before_1960_starts_at_73(self):
        assert rmd_start_age(1951) == 73
        assert rmd_start_age(1959) == 73

    def test_born_1960_or_later_starts_at_75(self):
        assert rmd_start_age(1960) == 75
        assert rmd_start_age(1990) == 75


class TestRmdDivisor:
    @pytest.fixture
    def rmd_table(self):
        return load_rmd_table()

    def test_matches_irs_published_values_for_a_few_ages(self, rmd_table):
        # IRS Pub 590-B Table III (Uniform Lifetime Table), effective 2022+.
        assert rmd_divisor(73, rmd_table) == pytest.approx(26.5)
        assert rmd_divisor(75, rmd_table) == pytest.approx(24.6)
        assert rmd_divisor(90, rmd_table) == pytest.approx(12.2)
        assert rmd_divisor(100, rmd_table) == pytest.approx(6.4)

    def test_age_past_the_tables_last_entry_reuses_the_last_divisor(self, rmd_table):
        assert rmd_divisor(130, rmd_table) == pytest.approx(rmd_divisor(120, rmd_table))

    def test_age_below_the_tables_first_entry_raises(self, rmd_table):
        with pytest.raises(ValueError):
            rmd_divisor(50, rmd_table)


# ---- compute_taxes: validation ----


class TestComputeTaxesValidation:
    def _base_kwargs(self, **overrides):
        kwargs = dict(
            tax_year=2026,
            filing_status="single",
            age_at_year_end=35,
            w2_gross=80000,
            pretax_401k=0,
            roth_401k=0,
            pretax_health_dental=0,
            se_net_profit=0,
            se_solo_employee_deferral=0,
            se_solo_employer_contribution=0,
            taxable_retirement_withdrawal=0,
            ltcg=0,
            qualified_dividends=0,
            ordinary_dividends=0,
            interest_income=0,
            ss_benefit_gross=0,
        )
        kwargs.update(overrides)
        return kwargs

    def test_invalid_filing_status_raises(self, bracket_table):
        with pytest.raises(ValueError, match="filing_status"):
            compute_taxes(**self._base_kwargs(filing_status="mfs"), bracket_table=bracket_table)

    def test_unknown_tax_year_raises(self, bracket_table):
        with pytest.raises(ValueError, match="tax year"):
            compute_taxes(**self._base_kwargs(tax_year=1999), bracket_table=bracket_table)

    def test_se_solo_employer_contribution_over_max_raises(self, bracket_table):
        with pytest.raises(ValueError, match="se_solo_employer_contribution"):
            compute_taxes(
                **self._base_kwargs(se_net_profit=10000, se_solo_employer_contribution=999999),
                bracket_table=bracket_table,
            )

    def test_w2_deferral_over_pool_raises(self, bracket_table):
        # 2026 pool at age 35 is 24,500 — pretax+roth combined here is 30,000.
        with pytest.raises(ValueError, match="pretax_401k \\+ roth_401k"):
            compute_taxes(
                **self._base_kwargs(pretax_401k=15000, roth_401k=15000),
                bracket_table=bracket_table,
            )

    def test_se_employee_deferral_over_pool_raises(self, bracket_table):
        with pytest.raises(ValueError, match="se_solo_employee_deferral"):
            compute_taxes(
                **self._base_kwargs(se_net_profit=100000, se_solo_employee_deferral=30000),
                bracket_table=bracket_table,
            )

    def test_all_filing_statuses_accepted(self, bracket_table):
        for status in FILING_STATUSES:
            result = compute_taxes(**self._base_kwargs(filing_status=status), bracket_table=bracket_table)
            assert result["total_tax"] >= 0


# ---- compute_taxes: 401(k) integration (the core of this session's change) ----


class TestComputeTaxes401k:
    def _base_kwargs(self, **overrides):
        kwargs = dict(
            tax_year=2026,
            filing_status="single",
            age_at_year_end=35,
            w2_gross=100000,
            pretax_401k=0,
            roth_401k=0,
            pretax_health_dental=0,
            se_net_profit=300000,
            se_solo_employee_deferral=0,
            se_solo_employer_contribution=0,
            taxable_retirement_withdrawal=0,
            ltcg=0,
            qualified_dividends=0,
            ordinary_dividends=0,
            interest_income=0,
            ss_benefit_gross=0,
        )
        kwargs.update(overrides)
        return kwargs

    def test_large_se_employer_contribution_does_not_reduce_w2_deferral_room(self, bracket_table):
        # The critical correction from the build spec: employer (profit-sharing) contributions
        # must never touch the shared §402(g) pool that W-2 deferral room is drawn from.
        low = compute_taxes(**self._base_kwargs(se_solo_employer_contribution=0), bracket_table=bracket_table)
        high = compute_taxes(**self._base_kwargs(se_solo_employer_contribution=50000), bracket_table=bracket_table)
        assert low["max_w2_employee_deferral"] == pytest.approx(high["max_w2_employee_deferral"])
        assert high["max_w2_employee_deferral"] == pytest.approx(24500.0)

    def test_roth_401k_counts_toward_shared_w2_deferral_pool(self, bracket_table):
        common = self._base_kwargs(se_net_profit=0, pretax_401k=0, roth_401k=0)
        del common["pretax_401k"], common["roth_401k"]
        # 24,500 is the full 2026 pool at age 35 — splitting it pretax/Roth should still be fine...
        ok = compute_taxes(pretax_401k=12000, roth_401k=12000, **common, bracket_table=bracket_table)
        assert ok["combined_employee_deferral_pool"] == pytest.approx(24500.0)
        # ...but exceeding the combined pool, even split across pretax+Roth, must still raise.
        with pytest.raises(ValueError, match="pretax_401k \\+ roth_401k"):
            compute_taxes(pretax_401k=15000, roth_401k=15000, **common, bracket_table=bracket_table)

    def test_se_retirement_contributions_reduce_federal_and_ca_agi(self, bracket_table):
        # Real bug found and fixed alongside the 401(k) module: solo 401(k) contributions (either
        # side) are their own above-the-line deduction (Schedule 1), separate from — and in
        # addition to — their effect on the QBI base. Before this fix, compute_taxes only ever
        # subtracted them from the QBI base, never from AGI/CA taxable income themselves.
        # se_net_profit is large enough that qbi_base stays fully above the phase-out ceiling
        # (276,750 for 2026 single) even after subtracting the contribution, so qbi_deduction is
        # 0 either way — isolating the AGI effect from the (separate, already-tested) QBI effect.
        common = self._base_kwargs(w2_gross=0, se_net_profit=500000, se_solo_employee_deferral=0)
        no_contribution = compute_taxes(
            **{**common, "se_solo_employer_contribution": 0}, bracket_table=bracket_table
        )
        with_contribution = compute_taxes(
            **{**common, "se_solo_employer_contribution": 3000}, bracket_table=bracket_table
        )
        assert no_contribution["qbi_deduction"] == with_contribution["qbi_deduction"] == pytest.approx(0.0)
        assert with_contribution["federal_taxable_income"] == pytest.approx(
            no_contribution["federal_taxable_income"] - 3000, abs=0.01
        )
        assert with_contribution["ca_taxable_income"] == pytest.approx(
            no_contribution["ca_taxable_income"] - 3000, abs=0.01
        )

    def test_se_employee_deferral_also_reduces_qbi_base(self, bracket_table):
        # Moderate SE profit, comfortably in the full-20%-deduction region (below the 201,750
        # full-deduction ceiling) both before and after the deferral, so the QBI delta is a clean,
        # checkable 20% of the deferral amount.
        common = self._base_kwargs(w2_gross=0, se_net_profit=50000, se_solo_employer_contribution=0)
        no_deferral = compute_taxes(**{**common, "se_solo_employee_deferral": 0}, bracket_table=bracket_table)
        with_deferral = compute_taxes(**{**common, "se_solo_employee_deferral": 3000}, bracket_table=bracket_table)
        assert no_deferral["qbi_deduction"] - with_deferral["qbi_deduction"] == pytest.approx(0.20 * 3000, abs=0.01)

    def test_max_fields_present_and_consistent(self, bracket_table):
        r = compute_taxes(**self._base_kwargs(), bracket_table=bracket_table)
        assert r["combined_employee_deferral_pool"] == pytest.approx(24500.0)
        assert r["max_w2_employee_deferral"] + r["max_se_employee_deferral"] <= r[
            "combined_employee_deferral_pool"
        ] + 1e-6
        assert r["se_solo_employer_contribution_max"] > 0


# ---- compute_taxes: the build-spec fixtures ----


class TestComputeTaxesFixtures:
    def test_1_w2_only_low_income(self, bracket_table):
        r = compute_taxes(
            tax_year=2026, filing_status="single", age_at_year_end=35, w2_gross=80000, pretax_401k=0,
            roth_401k=0, pretax_health_dental=0, se_net_profit=0, se_solo_employee_deferral=0,
            se_solo_employer_contribution=0, taxable_retirement_withdrawal=0, ltcg=0,
            qualified_dividends=0, ordinary_dividends=0, interest_income=0, ss_benefit_gross=0,
            bracket_table=bracket_table,
        )
        assert r["federal_taxable_income"] == pytest.approx(63900.0)
        assert r["federal_ordinary_tax"] == pytest.approx(8770.0)
        assert r["niit"] == pytest.approx(0.0)
        assert r["additional_medicare_tax"] == pytest.approx(0.0)
        assert r["qbi_deduction"] == pytest.approx(0.0)
        assert r["ca_tax_total"] == pytest.approx(3347.98, abs=0.01)
        assert r["social_security_tax"] == pytest.approx(4960.0)
        assert r["medicare_tax"] == pytest.approx(1160.0)
        assert r["casdi"] == pytest.approx(1040.0)
        assert r["total_tax"] == pytest.approx(19277.98, abs=0.01)
        assert r["marginal_federal_rate"] == pytest.approx(0.22)

    def test_2_w2_only_high_income_mfj(self, bracket_table):
        r = compute_taxes(
            tax_year=2026, filing_status="mfj", age_at_year_end=35, w2_gross=450000, pretax_401k=0,
            roth_401k=0, pretax_health_dental=0, se_net_profit=0, se_solo_employee_deferral=0,
            se_solo_employer_contribution=0, taxable_retirement_withdrawal=0, ltcg=0,
            qualified_dividends=0, ordinary_dividends=0, interest_income=0, ss_benefit_gross=0,
            bracket_table=bracket_table,
        )
        assert r["federal_taxable_income"] == pytest.approx(417800.0)
        assert r["federal_ordinary_tax"] == pytest.approx(86608.0)
        assert r["additional_medicare_tax"] == pytest.approx(1800.0)
        assert r["niit"] == pytest.approx(0.0)  # no investment income at all
        assert r["ca_tax_total"] == pytest.approx(33665.96, abs=0.01)
        assert r["total_tax"] == pytest.approx(145887.96, abs=0.01)

    def test_3_w2_plus_se_mixed(self, bracket_table):
        r = compute_taxes(
            tax_year=2026, filing_status="single", age_at_year_end=35, w2_gross=90000, pretax_401k=0,
            roth_401k=0, pretax_health_dental=0, se_net_profit=60000, se_solo_employee_deferral=0,
            se_solo_employer_contribution=0, taxable_retirement_withdrawal=0, ltcg=0,
            qualified_dividends=0, ordinary_dividends=0, interest_income=0, ss_benefit_gross=0,
            bracket_table=bracket_table,
        )
        assert r["se_tax"] == pytest.approx(8477.73, abs=0.01)
        assert r["se_solo_employer_contribution_max"] == pytest.approx(11152.23, abs=0.01)
        assert r["qbi_deduction"] == pytest.approx(11152.23, abs=0.01)  # below the full-deduction ceiling
        assert r["federal_ordinary_tax"] == pytest.approx(21040.14, abs=0.01)
        assert r["total_tax"] == pytest.approx(47036.63, abs=0.01)

    def test_4_w2_plus_ltcg_stacking_and_niit(self, bracket_table):
        r = compute_taxes(
            tax_year=2026, filing_status="single", age_at_year_end=35, w2_gross=150000, pretax_401k=0,
            roth_401k=0, pretax_health_dental=0, se_net_profit=0, se_solo_employee_deferral=0,
            se_solo_employer_contribution=0, taxable_retirement_withdrawal=0, ltcg=80000,
            qualified_dividends=0, ordinary_dividends=0, interest_income=0, ss_benefit_gross=0,
            bracket_table=bracket_table,
        )
        # Hand-verified: ordinary_taxable=133,900 all in the 22%/24% brackets; LTCG of 80,000
        # stacks entirely in the 15% tier (133,900 to 213,900, both above the 49,450 0%-bracket
        # top and below the 545,500 20%-bracket floor).
        assert r["federal_ordinary_tax"] == pytest.approx(24734.0)
        assert r["federal_ltcg_tax"] == pytest.approx(12000.0)  # 80,000 * 15%
        assert r["niit"] == pytest.approx(1140.0)  # 3.8% * min(80,000, 230,000-200,000)
        assert r["total_tax"] == pytest.approx(68596.98, abs=0.01)

    def test_5_retirement_year_ss_taxability(self, bracket_table):
        r = compute_taxes(
            tax_year=2026, filing_status="single", age_at_year_end=65, w2_gross=0, pretax_401k=0,
            roth_401k=0, pretax_health_dental=0, se_net_profit=0, se_solo_employee_deferral=0,
            se_solo_employer_contribution=0, taxable_retirement_withdrawal=30000, ltcg=0,
            qualified_dividends=0, ordinary_dividends=0, interest_income=0, ss_benefit_gross=40000,
            bracket_table=bracket_table,
        )
        assert r["ss_taxable_amount"] == pytest.approx(18100.0)
        assert r["federal_taxable_income"] == pytest.approx(32000.0)
        assert r["federal_ordinary_tax"] == pytest.approx(3592.0)
        assert r["niit"] == pytest.approx(0.0)  # withdrawal correctly excluded from NIIT base
        assert r["ca_taxable_income"] == pytest.approx(24294.0)  # CA never taxes SS at all
        assert r["social_security_tax"] == pytest.approx(0.0)  # no W-2/SE income
        assert r["total_tax"] == pytest.approx(3967.09, abs=0.01)

    def test_6_high_income_sstb_qbi_full_phaseout(self, bracket_table):
        r = compute_taxes(
            tax_year=2026, filing_status="single", age_at_year_end=35, w2_gross=0, pretax_401k=0,
            roth_401k=0, pretax_health_dental=0, se_net_profit=280000, se_solo_employee_deferral=0,
            se_solo_employer_contribution=0, taxable_retirement_withdrawal=0, ltcg=0,
            qualified_dividends=0, ordinary_dividends=0, interest_income=0, ss_benefit_gross=0,
            bracket_table=bracket_table,
        )
        assert r["se_tax"] == pytest.approx(30376.82, abs=0.01)
        assert r["additional_medicare_tax"] == pytest.approx(527.22, abs=0.01)
        # qbi_base ~264,811.59 sits between the 201,750 full-deduction ceiling and 276,750
        # phase-out ceiling -> partial (not full) phase-out, not the $0 a naive "just check
        # se_net_profit against the ceiling" implementation would produce.
        assert r["qbi_deduction"] == pytest.approx(8430.48, abs=0.01)
        assert 0 < r["qbi_deduction"] < 0.20 * 280000
        assert r["total_tax"] == pytest.approx(104785.45, abs=0.01)

    def test_7_large_withdrawal_excluded_from_niit(self, bracket_table):
        """
        Regression test for the NIIT leak fixed by adding taxable_retirement_withdrawal. Designed
        so the NIIT min(net_investment_income, AGI-threshold) cap does NOT bind in either the
        correct or a "leaky" computation (a large W-2 income pushes AGI well above the $200k single
        threshold either way) -- so the two calculations differ by exactly 3.8% of the withdrawal
        amount, a clean, checkable number. A version of compute_taxes that (re)folds
        taxable_retirement_withdrawal into the NIIT base would overstate niit and total_tax by
        exactly 0.038 * 180000 = $6,840 here.
        """
        r = compute_taxes(
            tax_year=2026, filing_status="single", age_at_year_end=35, w2_gross=500000, pretax_401k=0,
            roth_401k=0, pretax_health_dental=0, se_net_profit=0, se_solo_employee_deferral=0,
            se_solo_employer_contribution=0, taxable_retirement_withdrawal=180000, ltcg=0,
            qualified_dividends=0, ordinary_dividends=0, interest_income=50000, ss_benefit_gross=0,
            bracket_table=bracket_table,
        )
        # Correct: net_investment_income = interest_income only = 50,000; AGI-threshold headroom
        # (730,000-200,000=530,000) doesn't bind. niit = 3.8% * 50,000 = 1,900.
        assert r["niit"] == pytest.approx(1900.0, abs=0.01)
        # The withdrawal is still ordinary income for AGI/CA/bracket purposes.
        federal_agi_expected = 500000 + 50000 + 180000  # no SE, no ltcg/div, no SS taxable
        assert r["federal_taxable_income"] == pytest.approx(federal_agi_expected - 16100, abs=0.01)
        leaky_niit = 0.038 * (50000 + 180000)
        assert r["niit"] != pytest.approx(leaky_niit, abs=0.01)
        assert leaky_niit - r["niit"] == pytest.approx(6840.0, abs=0.01)


# ---- cross-cutting sanity checks ----


class TestSanity:
    def test_total_tax_never_negative_across_filing_statuses(self, bracket_table):
        for status in FILING_STATUSES:
            r = compute_taxes(
                tax_year=2025, filing_status=status, age_at_year_end=35, w2_gross=1000, pretax_401k=0,
                roth_401k=0, pretax_health_dental=0, se_net_profit=0, se_solo_employee_deferral=0,
                se_solo_employer_contribution=0, taxable_retirement_withdrawal=0, ltcg=0,
                qualified_dividends=0, ordinary_dividends=0, interest_income=0, ss_benefit_gross=0,
                bracket_table=bracket_table,
            )
            assert r["total_tax"] >= 0

    def test_pretax_401k_reduces_federal_taxable_income_but_not_fica(self, bracket_table):
        common = dict(
            tax_year=2026, filing_status="single", age_at_year_end=35, roth_401k=0,
            pretax_health_dental=0, se_net_profit=0, se_solo_employee_deferral=0,
            se_solo_employer_contribution=0, taxable_retirement_withdrawal=0, ltcg=0,
            qualified_dividends=0, ordinary_dividends=0, interest_income=0, ss_benefit_gross=0,
            bracket_table=bracket_table,
        )
        no_deferral = compute_taxes(w2_gross=100000, pretax_401k=0, **common)
        with_deferral = compute_taxes(w2_gross=100000, pretax_401k=10000, **common)
        assert with_deferral["federal_taxable_income"] == pytest.approx(
            no_deferral["federal_taxable_income"] - 10000
        )
        # Pretax 401(k) deferrals are still subject to FICA (step 2) — SS/Medicare tax unaffected.
        assert with_deferral["social_security_tax"] == pytest.approx(no_deferral["social_security_tax"])
        assert with_deferral["medicare_tax"] == pytest.approx(no_deferral["medicare_tax"])

    def test_cafeteria_plan_reduces_both_taxable_wages_and_fica(self, bracket_table):
        common = dict(
            tax_year=2026, filing_status="single", age_at_year_end=35, roth_401k=0, pretax_401k=0,
            se_net_profit=0, se_solo_employee_deferral=0, se_solo_employer_contribution=0,
            taxable_retirement_withdrawal=0, ltcg=0, qualified_dividends=0, ordinary_dividends=0,
            interest_income=0, ss_benefit_gross=0, bracket_table=bracket_table,
        )
        no_cafe = compute_taxes(w2_gross=100000, pretax_health_dental=0, **common)
        with_cafe = compute_taxes(w2_gross=100000, pretax_health_dental=5000, **common)
        assert with_cafe["federal_taxable_income"] == pytest.approx(no_cafe["federal_taxable_income"] - 5000)
        assert with_cafe["social_security_tax"] == pytest.approx(no_cafe["social_security_tax"] - 5000 * 0.062)
        assert with_cafe["casdi"] == pytest.approx(no_cafe["casdi"] - 5000 * 0.013)

    def test_ca_never_taxes_social_security(self, bracket_table):
        r = compute_taxes(
            tax_year=2026, filing_status="single", age_at_year_end=65, w2_gross=0, pretax_401k=0,
            roth_401k=0, pretax_health_dental=0, se_net_profit=0, se_solo_employee_deferral=0,
            se_solo_employer_contribution=0, taxable_retirement_withdrawal=0, ltcg=0,
            qualified_dividends=0, ordinary_dividends=0, interest_income=0, ss_benefit_gross=50000,
            bracket_table=bracket_table,
        )
        assert r["ca_taxable_income"] == pytest.approx(0.0)
        assert r["ca_tax_total"] == pytest.approx(0.0)

    def test_mhst_applies_above_one_million_ca_taxable_income(self, bracket_table):
        r = compute_taxes(
            tax_year=2026, filing_status="single", age_at_year_end=35, w2_gross=1200000, pretax_401k=0,
            roth_401k=0, pretax_health_dental=0, se_net_profit=0, se_solo_employee_deferral=0,
            se_solo_employer_contribution=0, taxable_retirement_withdrawal=0, ltcg=0,
            qualified_dividends=0, ordinary_dividends=0, interest_income=0, ss_benefit_gross=0,
            bracket_table=bracket_table,
        )
        ca_taxable = 1200000 - 5706
        assert r["mhst"] == pytest.approx(0.01 * (ca_taxable - 1_000_000), abs=0.01)
        assert r["mhst"] > 0

    def test_retirement_withdrawal_excluded_from_niit_and_additional_medicare(self, bracket_table):
        # A withdrawal alone (no wages, no investment income) should trigger neither NIIT nor
        # Additional Medicare Tax even when large, since it's excluded from both bases.
        r = compute_taxes(
            tax_year=2026, filing_status="single", age_at_year_end=65, w2_gross=0, pretax_401k=0,
            roth_401k=0, pretax_health_dental=0, se_net_profit=0, se_solo_employee_deferral=0,
            se_solo_employer_contribution=0, taxable_retirement_withdrawal=300000, ltcg=0,
            qualified_dividends=0, ordinary_dividends=0, interest_income=0, ss_benefit_gross=0,
            bracket_table=bracket_table,
        )
        assert r["niit"] == pytest.approx(0.0)
        assert r["additional_medicare_tax"] == pytest.approx(0.0)

    def test_ordinary_break_income_defaults_to_zero_and_is_backward_compatible(self, bracket_table):
        # New optional parameter (default 0.0) — every pre-existing call site/test that doesn't
        # pass it must behave identically to before it existed.
        common = dict(
            tax_year=2026, filing_status="single", age_at_year_end=35, w2_gross=80000, pretax_401k=0,
            roth_401k=0, pretax_health_dental=0, se_net_profit=0, se_solo_employee_deferral=0,
            se_solo_employer_contribution=0, taxable_retirement_withdrawal=0, ltcg=0,
            qualified_dividends=0, ordinary_dividends=0, interest_income=0, ss_benefit_gross=0,
            bracket_table=bracket_table,
        )
        omitted = compute_taxes(**common)
        explicit_zero = compute_taxes(ordinary_break_income=0.0, **common)
        assert omitted == explicit_zero

    def test_ordinary_break_income_taxed_as_ordinary_but_excluded_from_niit_and_additional_medicare(
        self, bracket_table
    ):
        # Same treatment as taxable_retirement_withdrawal, for the same structural reason: ordinary
        # income for federal/CA brackets and SS provisional income, but neither investment income
        # (no NIIT) nor wages/SE income (no Additional Medicare Tax, no payroll/SE tax at all).
        r = compute_taxes(
            tax_year=2026, filing_status="single", age_at_year_end=35, w2_gross=0, pretax_401k=0,
            roth_401k=0, pretax_health_dental=0, se_net_profit=0, se_solo_employee_deferral=0,
            se_solo_employer_contribution=0, taxable_retirement_withdrawal=0, ltcg=0,
            qualified_dividends=0, ordinary_dividends=0, interest_income=0, ss_benefit_gross=0,
            bracket_table=bracket_table, ordinary_break_income=300000,
        )
        assert r["niit"] == pytest.approx(0.0)
        assert r["additional_medicare_tax"] == pytest.approx(0.0)
        assert r["social_security_tax"] == pytest.approx(0.0)
        assert r["se_tax"] == pytest.approx(0.0)
        # But it IS taxed as ordinary federal/CA income:
        assert r["federal_taxable_income"] == pytest.approx(300000 - 16100, abs=0.01)
        assert r["total_tax"] > 0


class TestSsTaxableEarnings:
    """Module F (Social Security AIME, 2026-08-31) — `compute_taxes`'s new returned field, the
    one number a real SSA earnings record shows per year. 2026's ss_wage_base is $184,500."""

    def test_w2_only_below_wage_base(self, bracket_table):
        r = compute_taxes(
            tax_year=2026, filing_status="single", age_at_year_end=35, w2_gross=80000, pretax_401k=0,
            roth_401k=0, pretax_health_dental=0, se_net_profit=0, se_solo_employee_deferral=0,
            se_solo_employer_contribution=0, taxable_retirement_withdrawal=0, ltcg=0,
            qualified_dividends=0, ordinary_dividends=0, interest_income=0, ss_benefit_gross=0,
            bracket_table=bracket_table,
        )
        assert r["ss_taxable_earnings"] == pytest.approx(80000.0)

    def test_w2_only_above_wage_base_is_capped(self, bracket_table):
        r = compute_taxes(
            tax_year=2026, filing_status="single", age_at_year_end=35, w2_gross=250000, pretax_401k=0,
            roth_401k=0, pretax_health_dental=0, se_net_profit=0, se_solo_employee_deferral=0,
            se_solo_employer_contribution=0, taxable_retirement_withdrawal=0, ltcg=0,
            qualified_dividends=0, ordinary_dividends=0, interest_income=0, ss_benefit_gross=0,
            bracket_table=bracket_table,
        )
        assert r["ss_taxable_earnings"] == pytest.approx(184500.0)

    def test_se_only_uses_net_earnings_factor(self, bracket_table):
        r = compute_taxes(
            tax_year=2026, filing_status="single", age_at_year_end=35, w2_gross=0, pretax_401k=0,
            roth_401k=0, pretax_health_dental=0, se_net_profit=60000, se_solo_employee_deferral=0,
            se_solo_employer_contribution=0, taxable_retirement_withdrawal=0, ltcg=0,
            qualified_dividends=0, ordinary_dividends=0, interest_income=0, ss_benefit_gross=0,
            bracket_table=bracket_table,
        )
        assert r["ss_taxable_earnings"] == pytest.approx(60000 * 0.9235)

    def test_combined_w2_and_se_never_double_counts_the_wage_base(self, bracket_table):
        # $150,000 W-2 leaves only $34,500 of wage-base headroom -- the SE side's own $55,410 net
        # earnings (60,000 * 0.9235) must be capped at that headroom, not added in full, so the
        # combined total never exceeds the wage base itself.
        r = compute_taxes(
            tax_year=2026, filing_status="single", age_at_year_end=35, w2_gross=150000, pretax_401k=0,
            roth_401k=0, pretax_health_dental=0, se_net_profit=60000, se_solo_employee_deferral=0,
            se_solo_employer_contribution=0, taxable_retirement_withdrawal=0, ltcg=0,
            qualified_dividends=0, ordinary_dividends=0, interest_income=0, ss_benefit_gross=0,
            bracket_table=bracket_table,
        )
        assert r["ss_taxable_earnings"] == pytest.approx(184500.0)

    def test_zero_earnings_gives_zero(self, bracket_table):
        r = compute_taxes(
            tax_year=2026, filing_status="single", age_at_year_end=35, w2_gross=0, pretax_401k=0,
            roth_401k=0, pretax_health_dental=0, se_net_profit=0, se_solo_employee_deferral=0,
            se_solo_employer_contribution=0, taxable_retirement_withdrawal=0, ltcg=0,
            qualified_dividends=0, ordinary_dividends=0, interest_income=0, ss_benefit_gross=0,
            bracket_table=bracket_table,
        )
        assert r["ss_taxable_earnings"] == pytest.approx(0.0)


class TestComputeTaxesShortTermGains:
    """MODEL_WIRING.md §5.2 (Step 4, 2026-08-10) — realized gains on a lot held under a year, from
    selling a tax lot to fund dissaving or a retirement withdrawal. Taxed at ordinary rates (unlike
    `ltcg`) but genuinely investment income for NIIT (unlike `taxable_retirement_withdrawal`/
    `ordinary_break_income`) — the one income type that needs BOTH properties simultaneously, so it
    can't reuse any existing parameter without misrepresenting one or the other."""

    def _common(self, bracket_table, **overrides):
        kwargs = dict(
            tax_year=2026, filing_status="single", age_at_year_end=35, w2_gross=80000, pretax_401k=0,
            roth_401k=0, pretax_health_dental=0, se_net_profit=0, se_solo_employee_deferral=0,
            se_solo_employer_contribution=0, taxable_retirement_withdrawal=0, ltcg=0,
            qualified_dividends=0, ordinary_dividends=0, interest_income=0, ss_benefit_gross=0,
            bracket_table=bracket_table,
        )
        kwargs.update(overrides)
        return kwargs

    def test_defaults_to_zero_and_is_backward_compatible(self, bracket_table):
        common = self._common(bracket_table)
        omitted = compute_taxes(**common)
        explicit_zero = compute_taxes(short_term_gains=0.0, **common)
        assert omitted == explicit_zero

    def test_taxed_at_ordinary_rates_not_preferential_ltcg_rates(self, bracket_table):
        # Same dollar amount, same everything else: short_term_gains must produce MORE tax than
        # ltcg, since ltcg gets preferential stacked rates and short_term_gains doesn't.
        as_short_term = compute_taxes(**self._common(bracket_table, short_term_gains=50000))
        as_long_term = compute_taxes(**self._common(bracket_table, ltcg=50000))
        assert as_short_term["total_tax"] > as_long_term["total_tax"]

    def test_included_in_niit_base(self, bracket_table):
        # Large enough, with no other income, to clear the single-filer NIIT threshold on its own.
        r = compute_taxes(**self._common(bracket_table, w2_gross=0, short_term_gains=300000))
        assert r["niit"] > 0

    def test_excluded_from_fica_se_and_additional_medicare_tax(self, bracket_table):
        r = compute_taxes(**self._common(bracket_table, w2_gross=0, short_term_gains=300000))
        assert r["social_security_tax"] == pytest.approx(0.0)
        assert r["medicare_tax"] == pytest.approx(0.0)
        assert r["se_tax"] == pytest.approx(0.0)
        assert r["additional_medicare_tax"] == pytest.approx(0.0)

    def test_behaves_identically_to_ordinary_dividends_in_every_other_respect(self, bracket_table):
        # short_term_gains is documented to mirror ordinary_dividends everywhere except it must
        # never be folded into it as a shortcut (MODEL_WIRING.md §6) -- confirmed here by swapping
        # the same dollar amount between the two parameters and getting an identical result.
        via_short_term = compute_taxes(**self._common(bracket_table, short_term_gains=25000, ordinary_dividends=0))
        via_ordinary_div = compute_taxes(**self._common(bracket_table, short_term_gains=0, ordinary_dividends=25000))
        assert via_short_term == via_ordinary_div

    def test_included_in_provisional_income_and_agi(self, bracket_table):
        with_gain = compute_taxes(**self._common(bracket_table, short_term_gains=10000))
        without_gain = compute_taxes(**self._common(bracket_table, short_term_gains=0))
        assert with_gain["federal_agi"] == pytest.approx(without_gain["federal_agi"] + 10000)
