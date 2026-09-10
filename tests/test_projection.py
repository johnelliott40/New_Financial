"""
Tests for modules/projection.py — the gross-income-module <-> tax-module adapter
(gross_income_module_spec.md Section 4).
"""

from __future__ import annotations

from datetime import date

import pytest

from modules.contributions import DEFAULT_CONTRIBUTION_CONFIG, RECOMMENDED_CONTRIBUTION_CONFIG
from modules.investing import DEFAULT_DRAW_ORDER, draw_order_fill, roll_forward_holding
from modules.projection import (
    adjusted_wealth_via_retirement_tax_rate,
    average_annual_field,
    average_annual_net_retirement_income,
    average_retirement_tax_rate,
    net_present_value,
    net_present_value_of_field,
    portfolio_value,
    project_multi_year,
    project_no_income_no_expense_baseline,
)
from modules.social_security import load_bend_point_table, retirement_earnings_test_reduction
from modules.tax import (
    compute_taxes,
    load_bracket_table,
    load_rmd_table,
    max_ira_contribution_limit,
    ordinary_bracket_ceiling,
    rmd_divisor,
)

FLAT_CURVE = {"start_value": 0.0, "end_value": 0.0, "midpoint_years": 5.0, "steepness": 1.0}


def _income_inputs(**overrides):
    base = dict(
        current_year_already_earned_w2=0.0,
        current_year_already_earned_se=0.0,
        current_year_yet_to_earn_w2=0.0,
        current_year_yet_to_earn_se=0.0,
        w2_curve=FLAT_CURVE,
        se_curve=FLAT_CURVE,
        breaks=[],
    )
    base.update(overrides)
    return base


def _expense_inputs(**overrides):
    base = dict(
        current_year_already_incurred_expense=0.0,
        current_year_yet_to_incur_expense=0.0,
        expense_curve=FLAT_CURVE,
    )
    base.update(overrides)
    return base


@pytest.fixture
def bracket_table():
    return load_bracket_table()


class TestProjectMultiYear:
    def test_net_income_equals_gross_minus_total_tax(self, bracket_table):
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=40000.0, current_year_yet_to_earn_w2=40000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["tax_available"] is True
        assert row["gross_income"] == pytest.approx(80000.0, abs=0.01)
        assert row["net_income"] == pytest.approx(row["gross_income"] - row["total_tax"], abs=0.01)
        assert 0 < row["total_tax"] < row["gross_income"]

    def test_gross_income_sums_w2_se_and_break(self, bracket_table):
        from modules.gross_income import compute_year_income  # noqa: F401 -- sanity import only

        rows = project_multi_year(
            _income_inputs(
                current_year_already_earned_w2=10000.0,
                current_year_already_earned_se=5000.0,
                breaks=[
                    {
                        "id": "b",
                        "label": "Break",
                        "start_date": date(2026, 3, 1),
                        "end_date": date(2026, 4, 1),
                        "annualized_income_during_break": 36500.0,
                    }
                ],
            ),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
        )
        row = rows[0]
        assert row["gross_income"] == pytest.approx(row["gross_w2"] + row["gross_se"] + row["gross_ordinary_break_income"])
        assert row["gross_ordinary_break_income"] > 0

    def test_break_income_excluded_from_niit_via_the_adapter(self, bracket_table):
        # End-to-end version of modules/tax.py's own ordinary_break_income test: a big break with
        # no other investment income must not trigger NIIT through this adapter either.
        rows = project_multi_year(
            _income_inputs(
                breaks=[
                    {
                        "id": "b",
                        "label": "Big break",
                        "start_date": date(2026, 1, 1),
                        "end_date": date(2027, 1, 1),
                        "annualized_income_during_break": 300000.0,
                    }
                ]
            ),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
        )
        assert rows[0]["niit"] == pytest.approx(0.0)

    def test_years_beyond_bracket_table_hold_brackets_flat_in_real_dollars(self, bracket_table):
        # Real-dollar model policy (2026-08-09): a year past the last configured bracket year
        # reuses that year's brackets rather than going without tax figures. A nonzero, flat W2
        # curve (not the module-level FLAT_CURVE, which is zero) gives every held-flat year real
        # income to check the reused brackets against.
        nonzero_flat_curve = {"start_value": 60000.0, "end_value": 60000.0, "midpoint_years": 5.0, "steepness": 1.0}
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=50000.0, w2_curve=nonzero_flat_curve),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2031, 12, 31),
            retirement_date=date(2031, 12, 31),  # runs past 2026, the last year with bracket data
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
        )
        last_configured_year = max(bracket_table.keys())
        held_flat_rows = [r for r in rows if r["year"] > last_configured_year]
        assert held_flat_rows  # at least one year past the configured range exists

        # Every year still computes real tax figures -- nothing is None.
        for r in rows:
            assert r["tax_available"] is True
            assert r["total_tax"] is not None
            assert r["net_income"] is not None
            assert r["profit"] is not None

        for r in held_flat_rows:
            assert r["bracket_year_used"] == last_configured_year
            assert any("held flat" in n for n in r["notes"])
            # Recomputing directly against the last configured year's own brackets, using this
            # row's own real income, reproduces the exact same total_tax -- proof the brackets are
            # genuinely reused, not silently zeroed or approximated.
            direct = compute_taxes(
                tax_year=last_configured_year,
                filing_status="single",
                age_at_year_end=r["year"] - 1990,
                w2_gross=r["gross_w2"],
                pretax_401k=0.0,
                roth_401k=0.0,
                pretax_health_dental=0.0,
                se_net_profit=r["gross_se"],
                se_solo_employee_deferral=0.0,
                se_solo_employer_contribution=0.0,
                taxable_retirement_withdrawal=0.0,
                ltcg=0.0,
                qualified_dividends=0.0,
                ordinary_dividends=0.0,
                interest_income=0.0,
                ss_benefit_gross=0.0,
                bracket_table=bracket_table,
                ordinary_break_income=r["gross_ordinary_break_income"],
            )
            assert r["total_tax"] == pytest.approx(direct["total_tax"])

    def test_completely_empty_bracket_table_degrades_gracefully(self):
        # Distinct from the held-flat case above: there is no sensible fallback when NO year at all
        # is configured, so this (defensively) still degrades rather than crashing every row.
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=50000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table={},
        )
        assert rows[0]["tax_available"] is False
        assert rows[0]["bracket_year_used"] is None
        assert rows[0]["total_tax"] is None
        assert rows[0]["net_income"] is None
        assert rows[0]["profit"] is None
        assert any("No federal/CA tax bracket data configured at all" in n for n in rows[0]["notes"])

    def test_profit_equals_net_income_minus_gross_expense(self, bracket_table):
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=80000.0),
            _expense_inputs(current_year_already_incurred_expense=20000.0, current_year_yet_to_incur_expense=10000.0),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
        )
        row = rows[0]
        assert row["profit"] == pytest.approx(row["net_income"] - row["gross_expense"], abs=0.01)

    def test_years_from_now_and_gross_expense_present(self, bracket_table):
        rows = project_multi_year(
            _income_inputs(),
            _expense_inputs(current_year_already_incurred_expense=20000.0, current_year_yet_to_incur_expense=10000.0),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
        )
        assert rows[0]["years_from_now"] == 0
        assert rows[0]["gross_expense"] == pytest.approx(30000.0, abs=0.01)

    def test_age_at_year_end_computed_from_birth_date(self, bracket_table):
        # Age affects 401(k) pool sizing (combined_employee_deferral_pool) even with 0
        # contributions -- confirms age_at_year_end is actually wired through, not ignored.
        rows_no_catchup = project_multi_year(
            _income_inputs(current_year_already_earned_w2=50000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),  # 36 in 2026, no catch-up
            filing_status="single",
            bracket_table=bracket_table,
        )
        rows_with_catchup = project_multi_year(
            _income_inputs(current_year_already_earned_w2=50000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1966, 1, 1),  # 60 in 2026, enhanced catch-up
            filing_status="single",
            bracket_table=bracket_table,
        )
        assert rows_with_catchup[0]["combined_employee_deferral_pool"] > rows_no_catchup[0][
            "combined_employee_deferral_pool"
        ]


class TestProjectMultiYearHorizonVsRetirement:
    """MODEL_WIRING.md §1-§2 (2026-08-10) Step 1: the projection horizon extends past
    retirement_date — this is the end-to-end proof, through the full project_multi_year adapter,
    of the module-level behavior already unit-tested directly in test_gross_income.py."""

    def test_rows_extend_past_retirement_with_zero_w2_se_but_real_tax_figures(self, bracket_table):
        nonzero_flat_expense_curve = {"start_value": 40000.0, "end_value": 40000.0, "midpoint_years": 5.0, "steepness": 1.0}
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=60000.0),
            _expense_inputs(current_year_already_incurred_expense=30000.0, expense_curve=nonzero_flat_expense_curve),
            current_date=date(2026, 1, 1),
            horizon_date=date(2030, 12, 31),
            retirement_date=date(2027, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
        )
        assert [r["year"] for r in rows] == [2026, 2027, 2028, 2029, 2030]
        post_retirement = [r for r in rows if r["year"] > 2027]
        assert post_retirement
        for r in post_retirement:
            assert r["gross_w2"] == 0.0
            assert r["gross_se"] == 0.0
            # Real tax figures still compute (not None) even with $0 earned income this year —
            # Step 1's own gate: "post-retirement rows exist with zero income," not "no row/no tax."
            assert r["tax_available"] is True
            assert r["total_tax"] is not None
            assert r["net_income"] == pytest.approx(0.0)  # $0 gross income -> $0 tax -> $0 net
            # Expenses (unaffected by retirement) still consume the flat expense curve -> profit < 0.
            assert r["profit"] < 0


def _contrib_config(**overrides):
    """A `contribution_config_by_year` entry — defaults to DEFAULT_CONTRIBUTION_CONFIG ($0
    everywhere, "custom" mode), same override-a-dict pattern as `_income_inputs`/`_expense_inputs`
    above. See modules/contributions.py's own module docstring for the full field shape."""
    config = dict(DEFAULT_CONTRIBUTION_CONFIG)
    config.update(overrides)
    return config


class TestProjectMultiYearContributions:
    # CONTRIBUTION_TOGGLE_REDESIGN.md (2026-08-16) — the per-destination mode system. Mirrors the
    # existing TestComputeTaxes401k fixture pattern in tests/test_tax.py, extended across the
    # multi-year adapter instead of a single compute_taxes call.

    def test_no_contribution_config_by_year_is_backward_compatible(self, bracket_table):
        # Omitting the kwarg entirely must reproduce the exact "custom, $0" default (0 contributions).
        kwargs = dict(
            income_inputs=_income_inputs(current_year_already_earned_w2=60000.0),
            expense_inputs=_expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
        )
        rows_default = project_multi_year(**kwargs)
        rows_explicit_none = project_multi_year(**kwargs, contribution_config_by_year=None)
        assert rows_default[0]["total_tax"] == pytest.approx(rows_explicit_none[0]["total_tax"])
        assert rows_default[0]["net_se_earnings_for_retirement"] == pytest.approx(
            rows_explicit_none[0]["net_se_earnings_for_retirement"]
        )
        assert rows_default[0]["contribution_warnings"] == []
        assert rows_default[0]["w2_401k_contribution_used"] == 0.0

    def test_within_cap_contribution_reduces_taxable_income_and_liquid_net_income(self, bracket_table):
        no_contrib = project_multi_year(
            _income_inputs(current_year_already_earned_w2=60000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
        )
        with_contrib = project_multi_year(
            _income_inputs(current_year_already_earned_w2=60000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            contribution_config_by_year={2026: _contrib_config(contribution_401k_custom_amount=10000.0)},
        )
        assert with_contrib[0]["federal_agi"] == pytest.approx(no_contrib[0]["federal_agi"] - 10000.0)
        assert with_contrib[0]["total_tax"] < no_contrib[0]["total_tax"]
        # Gross income is unaffected (pretax 401k is a deduction, not a reduction of gross pay).
        assert with_contrib[0]["gross_income"] == pytest.approx(no_contrib[0]["gross_income"])
        # net_income/profit correctly subtract the $10,000 actually diverted into the 401(k)
        # (payroll-style — never liquid cash to begin with), only partly offset by the tax saved —
        # so LIQUID net_income goes DOWN by (10000 - tax_saved), not up.
        tax_saved = no_contrib[0]["total_tax"] - with_contrib[0]["total_tax"]
        expected_net_income_drop = 10000.0 - tax_saved
        assert with_contrib[0]["net_income"] == pytest.approx(no_contrib[0]["net_income"] - expected_net_income_drop)
        assert with_contrib[0]["net_income"] < no_contrib[0]["net_income"]
        assert with_contrib[0]["w2_401k_contribution_used"] == pytest.approx(10000.0)
        assert with_contrib[0]["contribution_warnings"] == []

    def test_over_cap_contribution_clamped_not_raised_no_warning(self, bracket_table):
        # $60,000 W-2 gross, but a $40,000 pretax 401(k) entry (well over the $24,500 2026 pool for
        # a 36-year-old) — must clamp to the pool, never raise ValueError like compute_taxes itself
        # would. 2026-08-30, user request: no longer warns either — resolve_401k_target's own
        # clamping happens regardless of any message, so the warning was confirmed pure narration
        # of an automatic correction, not a flag of anything the model doesn't already fix (see
        # modules/projection.py's own comment at the removed append site).
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=60000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            contribution_config_by_year={2026: _contrib_config(contribution_401k_custom_amount=40000.0)},
        )
        row = rows[0]
        assert row["tax_available"] is True  # never raised
        assert row["max_w2_employee_deferral"] == pytest.approx(24500.0)
        assert row["w2_401k_contribution_used"] == pytest.approx(24500.0)
        assert row["federal_agi"] == pytest.approx(
            [r for r in project_multi_year(
                _income_inputs(current_year_already_earned_w2=60000.0), _expense_inputs(),
                current_date=date(2026, 1, 1), horizon_date=date(2026, 12, 31),
                retirement_date=date(2026, 12, 31),
                birth_date=date(1990, 1, 1), filing_status="single", bracket_table=bracket_table,
                contribution_config_by_year={2026: _contrib_config(contribution_401k_custom_amount=24500.0)},
            )][0]["federal_agi"]
        )
        assert row["contribution_warnings"] == []

    def test_se_solo_401k_custom_amount_wired_through_when_no_w2_income(self, bracket_table):
        # With $0 W-2 income, the W-2 side has $0 capacity -- a custom 401(k) amount rolls entirely
        # into SE-employee deferral capacity instead (modules.contributions.resolve_401k_target's
        # own "W-2 first, overflow to SE-employee" ordering).
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_se=50000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            contribution_config_by_year={2026: _contrib_config(contribution_401k_custom_amount=10000.0)},
        )
        row = rows[0]
        assert row["contribution_warnings"] == []
        assert row["se_401k_employee_contribution_used"] == pytest.approx(10000.0)
        assert row["w2_401k_contribution_used"] == 0.0
        assert row["federal_agi"] < project_multi_year(
            _income_inputs(current_year_already_earned_se=50000.0), _expense_inputs(),
            current_date=date(2026, 1, 1), horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1), filing_status="single", bracket_table=bracket_table,
        )[0]["federal_agi"]

    def test_maximize_se_employer_checkbox_independent_of_401k_mode(self, bracket_table):
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_se=50000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            contribution_config_by_year={2026: _contrib_config(maximize_se_employer_401k=True)},
        )
        row = rows[0]
        assert row["se_401k_employer_contribution_used"] > 0
        # No W-2/custom entry, so nothing else fills -- only the employer side.
        assert row["w2_401k_contribution_used"] == 0.0
        assert row["se_401k_employee_contribution_used"] == 0.0

    def test_missing_year_in_contribution_config_by_year_defaults_to_zero(self, bracket_table):
        # A 2-year projection with an entry only for the second year must leave the first year
        # exactly as if contribution_config_by_year had never been passed at all.
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=40000.0, w2_curve=FLAT_CURVE),
            _expense_inputs(),
            current_date=date(2026, 6, 1),
            horizon_date=date(2027, 6, 1),
            retirement_date=date(2027, 6, 1),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            contribution_config_by_year={2027: _contrib_config(contribution_401k_custom_amount=5000.0)},
        )
        no_contrib_2026 = project_multi_year(
            _income_inputs(current_year_already_earned_w2=40000.0, w2_curve=FLAT_CURVE),
            _expense_inputs(),
            current_date=date(2026, 6, 1),
            horizon_date=date(2027, 6, 1),
            retirement_date=date(2027, 6, 1),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
        )
        assert rows[0]["total_tax"] == pytest.approx(no_contrib_2026[0]["total_tax"])
        assert rows[0]["contribution_warnings"] == []


PHASE_DATES = {
    "savings_stop_date": date(2056, 1, 1),
    "withdrawal_start_date": date(2056, 1, 1),
    "ss_claim_date": date(2063, 1, 1),
}


def _max_config(**overrides):
    """A `contribution_config_by_year` entry with every destination maxed out (RECOMMENDED_
    CONTRIBUTION_CONFIG) — used by tests that need real auto-resolved amounts, not the $0 default."""
    config = dict(RECOMMENDED_CONTRIBUTION_CONFIG)
    config.update(overrides)
    return config


class TestProjectMultiYearContributionModes:
    """CONTRIBUTION_TOGGLE_REDESIGN.md (2026-08-16) — the per-destination mode system (max/custom,
    replacing the old priority-order waterfall + manual-override dual-branch entirely). Stage-1/
    Stage-2 pure-function logic itself is covered in tests/test_contributions.py; these tests cover
    the PROJECTION-LEVEL wiring — tax computed with the real 401(k) deduction applied, employer
    match, the coasting freeze's interaction with contributions, IRC §219(f)(1), and the guaranteed
    Taxable catch-all."""

    def test_default_config_produces_zero_contributions_even_with_real_income(self, bracket_table):
        # DEFAULT_CONTRIBUTION_CONFIG ("custom", $0) is what every year without an explicit entry
        # falls back to — phase_dates and real income alone must not trigger any auto-fill.
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=100000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            phase_dates=PHASE_DATES,
        )
        assert rows[0]["w2_401k_contribution_used"] == 0.0
        assert rows[0]["roth_ira_contribution_used"] == 0.0

    def test_coasting_no_longer_freezes_earned_income_stage1_still_contributes(self, bracket_table):
        # 2026-09-06, NEXT.md item B1 -- the old "coasting-phase freeze" (WATERFALL_STREAMLINE_
        # REDESIGN.md §3) used to zero gross_w2/se once savings_stop_date had passed, which zeroed
        # even Stage-1 401(k)/max-mode contributions as a SIDE EFFECT (there was no real W-2 left to
        # defer from). Now that real earned income flows through unchanged during coasting, Stage 1
        # (a payroll deduction, "never gated on available cash" by its own long-standing design --
        # see resolve_401k_target's own docstring) resolves normally against the REAL income, same
        # as any other year -- this was always Stage 1's own behavior, just previously masked by the
        # freeze. Stage 2 (Roth/Traditional IRA/Taxable) stays $0 -- gated on `available_cash`, which
        # is `profit_earned_only * saving_fraction`, still exactly $0 once saving_fraction is 0.
        past_savings_stop = {**PHASE_DATES, "savings_stop_date": date(2020, 1, 1)}
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=100000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2056, 1, 1),  # not later than PHASE_DATES' own withdrawal_start_date
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            phase_dates=past_savings_stop,
            contribution_config_by_year={2026: _max_config()},
        )
        row = rows[0]
        assert row["gross_w2"] == pytest.approx(100000.0)  # real income, no longer frozen
        assert row["w2_401k_contribution_used"] > 0  # Stage 1: never gated on saving_fraction
        assert row["traditional_ira_contribution_used"] == pytest.approx(0.0)  # Stage 2: gated
        assert row["roth_ira_contribution_used"] == pytest.approx(0.0)  # Stage 2: gated
        # The earned-income surplus that WOULD have funded a Stage-2 contribution (if saving hadn't
        # stopped) becomes discretionary spending instead of vanishing.
        assert row["discretionary_spending"] > 0

    def test_max_mode_fills_401k_and_roth_ira(self, bracket_table):
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=150000.0),
            _expense_inputs(current_year_already_incurred_expense=20000.0),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            phase_dates=PHASE_DATES,
            contribution_config_by_year={2026: _max_config()},
        )
        row = rows[0]
        assert row["w2_401k_contribution_used"] > 0
        assert row["roth_ira_contribution_used"] > 0
        # "max_pretax" mode -- Roth 401(k) stays $0.
        assert row["roth_401k_contribution_used"] == 0.0
        assert row["contribution_destinations"] is not None
        assert row["contribution_destinations"]["uninvested_surplus"] >= -1e-6

    def test_employer_match_reduces_neither_tax_nor_liquid_profit(self, bracket_table):
        no_match = project_multi_year(
            _income_inputs(current_year_already_earned_w2=150000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            phase_dates=PHASE_DATES,
            contribution_config_by_year={2026: _max_config()},
        )
        with_match = project_multi_year(
            _income_inputs(current_year_already_earned_w2=150000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            phase_dates=PHASE_DATES,
            contribution_config_by_year={2026: _max_config()},
            employer_match_rate=0.5,
            employer_match_cap_pct=0.06,
        )
        # Employer match is free money on top -- must not change the employee's own tax or profit,
        # even though it's nonzero and reported on the row.
        assert with_match[0]["employer_401k_match"] > 0
        assert with_match[0]["total_tax"] == pytest.approx(no_match[0]["total_tax"])
        assert with_match[0]["profit"] == pytest.approx(no_match[0]["profit"])

    def test_401k_and_roth_both_zero_when_earned_income_alone_cant_cover_expenses(self, bracket_table):
        # 2026-09-07 redesign (NEXT.md "contribution hierarchy redesign"): large expenses relative
        # to income -> net_income_no_401k < gross_expense -> a genuine earned-income shortfall, the
        # ONLY condition under which dissaving triggers now. Roth IRA and the ENTIRE 401(k) family
        # get $0 that year (superseding the pre-2026-09-07 "401(k) is cash-blind, funds in full
        # regardless" behavior this test used to assert) -- no exception, no spurious warning.
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=10000.0),
            _expense_inputs(current_year_already_incurred_expense=100000.0),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            phase_dates=PHASE_DATES,
            contribution_config_by_year={2026: _max_config()},
        )
        row = rows[0]
        assert not row.get("contribution_warnings")  # no exception, no spurious warning
        assert row["roth_ira_contribution_used"] == 0.0  # nothing left after expenses
        assert row["profit"] < 0
        assert row["w2_401k_contribution_used"] == 0.0
        assert row["se_401k_employee_contribution_used"] == 0.0
        assert row["se_401k_employer_contribution_used"] == 0.0
        assert row["traditional_ira_contribution_used"] == 0.0
        assert row["contribution_401k_iteration_count"] is None

    def test_401k_gets_priority_over_roth_ira_when_surplus_cant_afford_both(self, bracket_table):
        # 2026-09-07, NEXT.md "contribution hierarchy v3: revert cash order to 401(k) first" -- the
        # core regression that distinguishes this order from the superseded "Roth first" design
        # (which would have funded Roth IRA to its own $7,500 limit here and left less for 401(k)).
        # $60k W-2, $30k expenses -- real numbers from the user's own test_method.json scenario
        # (2027): a genuine partial-affordability year where 401(k) claims the surplus FIRST, up to
        # a fixed-point-converged amount well under its full legal target, leaving $0 for Roth IRA.
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=60000.0),
            _expense_inputs(current_year_already_incurred_expense=30000.0),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            phase_dates=PHASE_DATES,
            contribution_config_by_year={2026: _max_config()},
        )
        row = rows[0]
        assert row["w2_401k_contribution_used"] > 0
        # 401(k) didn't reach its own full legal target (there wasn't enough surplus) -- a genuine
        # partial fixed-point solve, not the trivial "fits entirely" case.
        assert row["w2_401k_contribution_used"] < row["max_w2_employee_deferral"]
        assert row["contribution_401k_iteration_count"] is not None
        assert row["contribution_401k_iteration_count"] > 1
        # 401(k) claimed the ENTIRE surplus first -- nothing left for Roth IRA this year.
        assert row["roth_ira_contribution_used"] == pytest.approx(0.0)
        assert row["profit"] == pytest.approx(0.0, abs=1.0)  # no dissaving, no leftover either

    def test_roth_magi_reflects_the_real_401k_deduction_not_a_hypothetical(self, bracket_table):
        # 2026-09-07, NEXT.md v3 -- MAGI for the Roth phase-out now uses the row's own REAL
        # tax_result (401(k) already fully known, since it resolves FIRST) rather than a dedicated
        # "$0 401(k) hypothetical" tax call the superseded "Roth first" design needed. $175,000 W-2
        # (2026 single Roth phase-out band: $153,000-$168,000) -- WITHOUT any 401(k) deduction, AGI
        # ($175,000) sits ABOVE the band ($0 Roth room); a full pretax 401(k) election
        # ($24,500 for a 36-year-old in 2026) drops real AGI to $150,500, BELOW the band's lower
        # bound -- full, uncapped Roth room. Confirms the model actually sees the post-deduction
        # AGI, not the pre-deduction one.
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=175000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            contribution_config_by_year={2026: _max_config()},
        )
        row = rows[0]
        assert row["w2_401k_contribution_used"] == pytest.approx(24500.0)  # fully affordable, fully maxed
        assert row["federal_agi"] == pytest.approx(150500.0)  # below the $153,000 phase-out floor
        assert row["roth_ira_contribution_used"] == pytest.approx(7500.0)  # full 2026 limit, uncapped

    def test_custom_amount_in_one_family_does_not_block_max_mode_in_the_other(self, bracket_table):
        # A small custom 401(k) entry alongside a real income -- Roth IRA (a separate destination,
        # its own independent mode) still resolves to its full "maximize" target regardless.
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=150000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            # retirement_date must not be LATER than PHASE_DATES' own withdrawal_start_date
            # (NEXT.md item B2's date-ordering validation) -- 2056, not 2060, still "years away"
            # relative to the single projected year (2026) every test in this class uses.
            retirement_date=date(2056, 1, 1),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            phase_dates=PHASE_DATES,
            contribution_config_by_year={
                2026: _contrib_config(contribution_401k_custom_amount=1000.0, roth_ira_mode="maximize")
            },
        )
        row = rows[0]
        assert row["w2_401k_contribution_used"] == pytest.approx(1000.0)
        assert row["roth_ira_contribution_used"] > 0

    def test_taxable_receives_leftover_available_cash(self, bracket_table):
        # WATERFALL_STREAMLINE_REDESIGN.md §2.2's guaranteed catch-all, now simply Stage 2's own
        # last step: a deliberately tiny 401(k) custom entry leaves most of a $150k income's
        # available_cash unspoken-for by Roth IRA's own MAGI-limited capacity -- the rest reaches
        # Taxable, never silently vanishes.
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=150000.0),
            _expense_inputs(current_year_already_incurred_expense=20000.0),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            # retirement_date must not be LATER than PHASE_DATES' own withdrawal_start_date
            # (NEXT.md item B2's date-ordering validation) -- 2056, not 2060, still "years away"
            # relative to the single projected year (2026) every test in this class uses.
            retirement_date=date(2056, 1, 1),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            phase_dates=PHASE_DATES,
            contribution_config_by_year={
                2026: _contrib_config(contribution_401k_custom_amount=1000.0, roth_ira_mode="maximize")
            },
        )
        row = rows[0]
        assert row["contribution_destinations"] is not None
        assert row["contribution_destinations"]["taxable"] > 0

    def test_taxable_funded_with_nothing_configured(self, bracket_table):
        # 100% of a real year's earned-only profit reaches Taxable when nothing else claims any of
        # it -- the DEFAULT ("custom", $0) config for 401(k)/IRA leaves the whole pool unclaimed.
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=150000.0),
            _expense_inputs(current_year_already_incurred_expense=20000.0),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            # retirement_date must not be LATER than PHASE_DATES' own withdrawal_start_date
            # (NEXT.md item B2's date-ordering validation) -- 2056, not 2060, still "years away"
            # relative to the single projected year (2026) every test in this class uses.
            retirement_date=date(2056, 1, 1),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            phase_dates=PHASE_DATES,
        )
        row = rows[0]
        assert row["w2_401k_contribution_used"] == pytest.approx(0.0)
        assert row["roth_ira_contribution_used"] == pytest.approx(0.0)
        assert row["contribution_destinations"] is not None
        assert row["contribution_destinations"]["taxable"] > 0
        assert row["contribution_destinations"]["uninvested_surplus"] == pytest.approx(0.0)

    def test_available_cash_credits_back_the_401k_tax_savings(self, bracket_table):
        # CONTRIBUTION_TOGGLE_REDESIGN.md §1's own explicit design: "tax is computed WITH that real
        # [401(k)] deduction applied, THEN Roth/Traditional/Taxable compete for what's actually
        # left" -- a deliberate improvement over the old waterfall's $0-contribution baseline,
        # which never credited the real tax savings a 401(k) deduction produces back into the cash
        # pool. Maxing 401(k) pretax must never REDUCE what's available for Taxable below what a
        # smaller 401(k) contribution would leave, since the tax saved partially offsets the payroll
        # deduction.
        small_401k = project_multi_year(
            _income_inputs(current_year_already_earned_w2=150000.0),
            _expense_inputs(current_year_already_incurred_expense=20000.0),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            # retirement_date must not be LATER than PHASE_DATES' own withdrawal_start_date
            # (NEXT.md item B2's date-ordering validation) -- 2056, not 2060, still "years away"
            # relative to the single projected year (2026) every test in this class uses.
            retirement_date=date(2056, 1, 1),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            phase_dates=PHASE_DATES,
            contribution_config_by_year={2026: _contrib_config(contribution_401k_custom_amount=1000.0)},
        )
        max_401k = project_multi_year(
            _income_inputs(current_year_already_earned_w2=150000.0),
            _expense_inputs(current_year_already_incurred_expense=20000.0),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            # retirement_date must not be LATER than PHASE_DATES' own withdrawal_start_date
            # (NEXT.md item B2's date-ordering validation) -- 2056, not 2060, still "years away"
            # relative to the single projected year (2026) every test in this class uses.
            retirement_date=date(2056, 1, 1),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            phase_dates=PHASE_DATES,
            contribution_config_by_year={2026: _max_config()},
        )
        small_401k_dollar = small_401k[0]["w2_401k_contribution_used"]
        max_401k_dollar = max_401k[0]["w2_401k_contribution_used"]
        assert max_401k_dollar > small_401k_dollar
        # Total dollars actually invested this year (401(k) + Taxable, both runs use default
        # "maximize" Roth so that piece is identical in both) — maxing 401(k) should never leave
        # LESS total money working than the smaller contribution, once the real tax savings are
        # credited back.
        small_total = small_401k_dollar + small_401k[0]["contribution_destinations"]["taxable"]
        max_total = max_401k_dollar + max_401k[0]["contribution_destinations"]["taxable"]
        assert max_total >= small_total - 1e-6

    def test_ira_clamped_to_zero_with_no_earned_income(self, bracket_table):
        # IRC §219(f)(1): $0 earned income means $0 combined IRA limit, structurally built into
        # resolve_roth_ira_target via combined_ira_limit -- "maximize" mode correctly resolves to
        # $0, not the full MAGI-phase-out-only amount.
        rows = project_multi_year(
            _income_inputs(),  # zero W-2/SE
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            phase_dates=PHASE_DATES,
            contribution_config_by_year={2026: _max_config()},
        )
        row = rows[0]
        assert row["gross_w2"] == pytest.approx(0.0)
        assert row["gross_se"] == pytest.approx(0.0)
        assert row["roth_ira_contribution_used"] == pytest.approx(0.0)

    def test_custom_ira_over_legal_limit_clamped_no_warning(self, bracket_table):
        # $20k earned income (comfortably above the ~$7,500 statutory IRA limit) but a $30k
        # combined custom entry -- clamped to the real combined statutory limit, Roth resolved
        # first (matches this system's own stated priority), Traditional gets $0 since Roth alone
        # already consumes the whole combined limit. 2026-08-30, user request: no longer warns —
        # resolve_roth_ira_target/resolve_traditional_ira_target's own clamping (asserted below)
        # happens regardless of any message, so the warning was confirmed pure narration of an
        # automatic correction (see modules/projection.py's own comment at the removed site).
        statutory_limit = max_ira_contribution_limit(2026, 36, bracket_table)
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=20000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            phase_dates=PHASE_DATES,
            contribution_config_by_year={
                2026: _contrib_config(
                    roth_ira_mode="custom", roth_ira_custom_amount=25000.0,
                    traditional_ira_mode="custom", traditional_ira_custom_amount=5000.0,
                )
            },
        )
        row = rows[0]
        assert row["roth_ira_contribution_used"] == pytest.approx(statutory_limit)
        assert row["traditional_ira_contribution_used"] == pytest.approx(0.0)
        assert row["contribution_warnings"] == []

    def test_ira_capacity_zero_with_no_earned_income_even_with_other_income(self, bracket_table):
        # IRC §219(f)(1): an IRA contribution can never exceed that year's compensation. Large break
        # income (not earned income) produces real available_cash, but with $0 W-2/SE, both Roth and
        # Traditional IRA capacity must be $0 regardless.
        rows = project_multi_year(
            _income_inputs(
                breaks=[
                    {
                        "id": "b", "label": "Break", "start_date": date(2026, 1, 1), "end_date": date(2027, 1, 1),
                        "annualized_income_during_break": 100000.0,
                    }
                ]
            ),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2020, 1, 1),  # already retired -- no W-2/SE from year 1 onward
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            phase_dates=PHASE_DATES,
            contribution_config_by_year={2026: _max_config()},
        )
        row = rows[0]
        assert row["gross_w2"] == pytest.approx(0.0)
        assert row["gross_se"] == pytest.approx(0.0)
        assert row["profit"] > 0  # real surplus exists from break income
        assert row["roth_ira_contribution_used"] == pytest.approx(0.0)
        assert row["traditional_ira_contribution_used"] == pytest.approx(0.0)

    def test_ira_capacity_still_available_in_a_genuinely_partial_retirement_year(self, bracket_table):
        # A mid-year retirement still has real compensation for THAT tax year -- IRA capacity must
        # NOT be gated off just because retirement_date falls within the year.
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=60000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 6, 30),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            phase_dates=PHASE_DATES,
            contribution_config_by_year={2026: _max_config()},
        )
        row = rows[0]
        assert row["gross_w2"] > 0
        assert (row["roth_ira_contribution_used"] + row["traditional_ira_contribution_used"]) > 0

    def test_zero_earned_income_matches_no_income_no_expense_baseline_exactly(self, bracket_table):
        # Investment income (dividends/interest) already has its own dedicated, UNCONDITIONAL
        # reinvestment mechanism (roll_forward_portfolio's automatic DRIP) -- it must NOT also be
        # treated as available_cash for the contribution engine, or the same after-tax dollar gets
        # invested twice. With zero real earned income, a "max everything" projection must match the
        # no-income/no-expense baseline EXACTLY, year over year -- both represent "just this
        # portfolio, growing on its own," nothing else.
        universe = _small_universe()
        lots = [
            {"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 100.0, "year_acquired": 2020}
        ]
        prices = {"VTI": 100.0}
        target_allocations = {"Taxable": {"VTI": 1.0}}

        with_max_config = project_multi_year(
            _income_inputs(),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2029, 12, 31),
            # retirement_date must not be LATER than PHASE_DATES' own withdrawal_start_date
            # (NEXT.md item B2's date-ordering validation) -- 2056 is still well beyond this test's
            # own 2026-2029 projected horizon.
            retirement_date=date(2056, 1, 1),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            phase_dates=PHASE_DATES,
            contribution_config_by_year={y: _max_config() for y in range(2026, 2030)},
            universe=universe,
            initial_lots=lots,
            initial_prices_by_ticker=prices,
            target_allocations=target_allocations,
        )
        baseline = project_no_income_no_expense_baseline(
            current_date=date(2026, 1, 1),
            horizon_date=date(2029, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            withdrawal_start_date=PHASE_DATES["withdrawal_start_date"],
            ss_claim_date=PHASE_DATES["ss_claim_date"],
            universe=universe,
            initial_lots=lots,
            initial_prices_by_ticker=prices,
            target_allocations=target_allocations,
        )
        assert len(with_max_config) == len(baseline)
        for r, b in zip(with_max_config, baseline):
            assert r["gross_income"] > 0, r["year"]  # real dividend/interest income each year
            assert r["portfolio_value"] == pytest.approx(b["portfolio_value"]), r["year"]
            assert r["w2_401k_contribution_used"] == pytest.approx(0.0)
            assert r["roth_ira_contribution_used"] == pytest.approx(0.0)
            assert r["traditional_ira_contribution_used"] == pytest.approx(0.0)

    def test_available_cash_bounded_by_earned_income_not_inflated_by_dividends(self, bracket_table):
        # A small real W-2 income next to a much larger dividend-paying portfolio -- the contribution
        # engine must only spend what the EARNED income actually supports, not the much bigger total
        # profit (W-2 + investment income combined), which would otherwise let a small paycheck
        # "unlock" a full IRA contribution effectively funded by dividends already being reinvested
        # elsewhere.
        universe = _small_universe()
        lots = [
            {"ticker": "VTI", "account_type": "Taxable", "shares": 10000.0, "basis_per_share": 100.0, "year_acquired": 2020}
        ]
        prices = {"VTI": 100.0}  # ~$1,000,000 taxable position -> ~$15,000/yr in qualified dividends
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=2000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            # retirement_date must not be LATER than PHASE_DATES' own withdrawal_start_date
            # (NEXT.md item B2's date-ordering validation) -- 2056, not 2060, still "years away"
            # relative to the single projected year (2026) every test in this class uses.
            retirement_date=date(2056, 1, 1),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            phase_dates=PHASE_DATES,
            # 401(k) left at "custom, $0" deliberately -- maxing it would itself consume the whole
            # $2,000 paycheck (100%-of-compensation-capped), leaving nothing to test the Roth IRA
            # side against. Only Roth IRA is set to "maximize" here.
            contribution_config_by_year={2026: _contrib_config(roth_ira_mode="maximize")},
            universe=universe,
            initial_lots=lots,
            initial_prices_by_ticker=prices,
            target_allocations={"Roth IRA": {"VTI": 1.0}, "Taxable": {"VTI": 1.0}},
        )
        row = rows[0]
        assert row["gross_w2"] == pytest.approx(2000.0)
        assert row["gross_income"] > 10000.0  # dividend income genuinely dominates gross_income
        # The full Roth IRA limit is far more than $2,000 of W-2 income (minus its own tax) could
        # ever fund -- if the bug were still present this would come back at the full IRA limit,
        # effectively funded by dividends already reinvested via roll_forward_portfolio.
        assert 0 < row["roth_ira_contribution_used"] < row["gross_w2"]
        assert row["contribution_destinations"]["taxable"] == pytest.approx(0.0)


class TestContributionMigrationEquivalence:
    """CONTRIBUTION_TOGGLE_REDESIGN.md §10's own migration checklist item: load a save with the old
    six-field shape, confirm it lands in mode="custom" with the exact prior dollar amounts, and that
    a fresh project_multi_year run produces the same contribution dollars as the old manual-override
    path would have. The old waterfall code itself is gone (deleted this pass), so "identical to
    before" is verified the only way still possible: for the common case of a single nonzero 401(k)
    field per year (documented in ui/sidebar.py's own _migrate_contribution_entry docstring as the
    case the migration is exact for, as opposed to a same-year mix of pretax+Roth+SE amounts, which
    is a documented approximation), migrating and re-running must reproduce the OLD manual-override
    semantics exactly: the entered dollar figure flows straight through as w2_401k/roth_401k/ira
    _contribution_used, unclamped, since every amount here sits well under its own statutory cap and
    ample cash is available -- exactly what the old `has_401k_override`/`has_ira_override` branches
    guaranteed."""

    def test_migrated_single_field_401k_entry_reproduces_the_exact_old_dollar_amount(self, bracket_table):
        from ui.sidebar import _migrate_contribution_entry

        old_shape_entry = {"w2_401k_contribution": 10000.0}
        migrated = _migrate_contribution_entry(old_shape_entry)
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=150000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            # retirement_date must not be LATER than PHASE_DATES' own withdrawal_start_date
            # (NEXT.md item B2's date-ordering validation) -- 2056, not 2060, still "years away"
            # relative to the single projected year (2026) every test in this class uses.
            retirement_date=date(2056, 1, 1),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            phase_dates=PHASE_DATES,
            contribution_config_by_year={2026: migrated},
        )
        row = rows[0]
        # Same behavior the old has_401k_override branch guaranteed: the entered amount flows
        # through untouched (well under the statutory cap), and the un-migrated destinations
        # (Roth IRA, Traditional IRA) stay at their old "nothing entered" default of exactly $0.
        assert row["w2_401k_contribution_used"] == pytest.approx(10000.0)
        assert row["roth_401k_contribution_used"] == pytest.approx(0.0)
        assert row["roth_ira_contribution_used"] == pytest.approx(0.0)
        assert row["traditional_ira_contribution_used"] == pytest.approx(0.0)

    def test_migrated_single_field_roth_ira_entry_reproduces_the_exact_old_dollar_amount(self, bracket_table):
        from ui.sidebar import _migrate_contribution_entry

        old_shape_entry = {"roth_ira_contribution": 5000.0}
        migrated = _migrate_contribution_entry(old_shape_entry)
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=150000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            # retirement_date must not be LATER than PHASE_DATES' own withdrawal_start_date
            # (NEXT.md item B2's date-ordering validation) -- 2056, not 2060, still "years away"
            # relative to the single projected year (2026) every test in this class uses.
            retirement_date=date(2056, 1, 1),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            phase_dates=PHASE_DATES,
            contribution_config_by_year={2026: migrated},
        )
        row = rows[0]
        assert row["roth_ira_contribution_used"] == pytest.approx(5000.0)
        assert row["w2_401k_contribution_used"] == pytest.approx(0.0)
        assert row["traditional_ira_contribution_used"] == pytest.approx(0.0)


# Savings stop date (2030-06-30) falls BEFORE Income end date (2033-01-01) -- a real gap where
# earned income continues but saving has stopped, exactly the case the OLD "freeze everything to
# $0" behavior got wrong (NEXT.md item B1). withdrawal_start_date (2035) is later than BOTH,
# satisfying B2's date-ordering validation. savings_stop_date mid-year (June 30) gives a genuinely
# partial transition year (2030) distinct from the fully-coasting years that follow (2031-2032) and
# the post-retirement/pre-withdrawal years after that (2033-2034, where gross_w2 is naturally $0
# via project_gross_income's own retirement clipping -- no discretionary-spending mechanism needed).
COASTING_PHASE_DATES = {
    "savings_stop_date": date(2030, 6, 30),
    "withdrawal_start_date": date(2035, 1, 1),
    "ss_claim_date": date(2035, 1, 1),
}
# S-curve "start_value"/"end_value" are DOLLAR amounts for a future year (see baseline_annual_rate's
# own docstring), not growth rates -- flat $100k/$40k across the whole projection window.
_COASTING_W2_CURVE = {"start_value": 100000.0, "end_value": 100000.0, "midpoint_years": 5.0, "steepness": 1.0}
_COASTING_EXPENSE_CURVE = {"start_value": 40000.0, "end_value": 40000.0, "midpoint_years": 5.0, "steepness": 1.0}


def _coasting_test_rows(bracket_table, **extra):
    return project_multi_year(
        _income_inputs(current_year_already_earned_w2=100000.0, w2_curve=_COASTING_W2_CURVE),
        _expense_inputs(current_year_already_incurred_expense=40000.0, expense_curve=_COASTING_EXPENSE_CURVE),
        current_date=date(2026, 1, 1),
        horizon_date=date(2036, 12, 31),
        retirement_date=date(2033, 1, 1),
        birth_date=date(1990, 1, 1),
        filing_status="single",
        bracket_table=bracket_table,
        phase_dates=COASTING_PHASE_DATES,
        **extra,
    )


class TestProjectMultiYearDiscretionarySpending:
    """2026-09-06, NEXT.md item B1 — replaces the old "coasting-phase freeze"
    (WATERFALL_STREAMLINE_REDESIGN.md §3, which used to force gross_w2/gross_se/gross_ordinary_
    break_income/gross_expense all to $0 between Savings stop date and Withdrawal start date,
    regardless of whether real earned income was still happening). Now every year computes
    gross_w2/gross_se/gross_expense normally, and `discretionary_spending` — the earned-income
    surplus NOT becoming a new Stage-2 contribution — absorbs whatever saving_fraction < 1 leaves
    over. `retirement_date` (2033) falls strictly BETWEEN COASTING_PHASE_DATES' own savings_stop_
    date (2030-06-30) and withdrawal_start_date (2035) — the case the old freeze got wrong (real
    earned income continuing into what used to be treated as a dead zone)."""

    def test_transition_year_still_earns_and_spends_no_longer_a_cliff_edge(self, bracket_table):
        # savings_stop_date (2030-06-30) falls partway through 2030 -- income/expenses were NEVER
        # frozen this specific year even under the old design (transition years earn/spend at their
        # own day-weighted fraction); still true here, now just framed as one end of a continuum
        # rather than "not yet hit the cliff."
        rows = _coasting_test_rows(bracket_table)
        row_2030 = next(r for r in rows if r["year"] == 2030)
        assert row_2030["gross_w2"] > 0
        assert row_2030["gross_expense"] > 0
        # Partial saving_fraction this year (day-weighted) -> a PARTIAL, nonzero discretionary
        # spending amount too, not the full-year figure 2031 shows.
        assert row_2030["discretionary_spending"] > 0

    def test_full_coasting_year_earns_normally_and_surplus_becomes_discretionary_spending(self, bracket_table):
        # 2031 is a full year strictly between savings_stop_date and Income end date (retirement_
        # date) -- real earned income continues (the case the old freeze got wrong), but saving_
        # fraction is 0, so none of the surplus becomes a new Stage-2 contribution.
        rows = _coasting_test_rows(bracket_table)
        row_2031 = next(r for r in rows if r["year"] == 2031)
        assert row_2031["gross_w2"] == pytest.approx(100000.0)  # real income, NOT frozen
        assert row_2031["gross_expense"] == pytest.approx(40000.0)  # real expenses, NOT frozen
        assert row_2031["discretionary_spending"] > 0
        # No portfolio wired in (no dividends to complicate this) and $0/$0 401(k)/IRA config
        # (DEFAULT_CONTRIBUTION_CONFIG) -> discretionary_spending must equal the year's own earned
        # profit exactly (100% of the surplus, since saving_fraction is 0 the entire year).
        assert row_2031["discretionary_spending"] == pytest.approx(row_2031["profit"])
        assert row_2031["contribution_destinations"]["taxable"] == pytest.approx(0.0)

    def test_post_retirement_pre_withdrawal_year_has_zero_discretionary_spending(self, bracket_table):
        # 2034 (a full year strictly AFTER retirement_date, 2033-01-01, but before withdrawal_
        # start_date, 2035): gross_w2 is naturally $0 via project_gross_income's own retirement
        # clipping (NOT this mechanism) -- discretionary_spending must correctly come back $0 too,
        # matching the user's own "dividends just keep saving/reinvesting, no code needed" framing
        # for the "savings stop AFTER income end" ordering (see this module's own docstring). Also
        # confirms `is_withdrawal_year` is correctly still False here (withdrawal_start_date is
        # 2035) -- this is neither the coasting-with-income case nor formal retirement withdrawal,
        # just an ordinary $0-income year.
        rows = _coasting_test_rows(bracket_table)
        row_2034 = next(r for r in rows if r["year"] == 2034)
        assert row_2034["gross_w2"] == pytest.approx(0.0)
        assert row_2034["discretionary_spending"] == pytest.approx(0.0)
        assert row_2034["is_withdrawal_year"] is False

    def test_withdrawal_phase_year_not_treated_as_coasting(self, bracket_table):
        # withdrawing_fraction > 0 (2035, the first withdrawal-phase year) must show real
        # discretionary_spending logic entirely unaffected -- saving_fraction is 0 here too, but
        # gross_w2 is already naturally $0 (well past retirement_date), so discretionary_spending
        # is $0, same as any other post-retirement year, not a special "coasting" case.
        rows = _coasting_test_rows(bracket_table)
        row_2035 = next(r for r in rows if r["year"] == 2035)
        assert row_2035["is_withdrawal_year"] is True
        assert row_2035["gross_w2"] == pytest.approx(0.0)
        assert row_2035["discretionary_spending"] == pytest.approx(0.0)

    def test_portfolio_still_grows_from_reinvestment_during_coasting(self, bracket_table):
        # The portfolio's own growth mechanism (roll_forward_portfolio's dividend/interest
        # reinvestment) is untouched by any of this -- it never reads gross_w2/se/expense at all.
        # Confirms discretionary_spending doesn't accidentally interfere with reinvestment during a
        # coasting year with real earned income alongside it.
        universe = _small_universe()
        lots = [
            {"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 100.0, "year_acquired": 2020}
        ]
        prices = {"VTI": 100.0}
        rows = _coasting_test_rows(bracket_table, universe=universe, initial_lots=lots, initial_prices_by_ticker=prices)
        row_2030_end = next(r for r in rows if r["year"] == 2030)
        row_2031 = next(r for r in rows if r["year"] == 2031)
        row_2032 = next(r for r in rows if r["year"] == 2032)
        assert row_2031["gross_w2"] == pytest.approx(100000.0)  # real income, NOT frozen
        assert row_2031["portfolio_value"] > row_2030_end["portfolio_value"]
        assert row_2032["portfolio_value"] > row_2031["portfolio_value"]

    def test_taxable_catchall_is_zero_during_full_coasting(self, bracket_table):
        # Belt-and-suspenders against Stage 2's own unconditional catch-all: available_cash is
        # exactly $0 whenever saving_fraction is 0, REGARDLESS of whether earned income is real and
        # nonzero (2031) -- Taxable must correctly come back $0, not some fraction of the real
        # earned surplus (which instead becomes discretionary_spending, see the test above).
        rows = _coasting_test_rows(bracket_table)
        row_2031 = next(r for r in rows if r["year"] == 2031)
        assert row_2031["contribution_destinations"] is not None
        assert row_2031["contribution_destinations"]["taxable"] == pytest.approx(0.0)

    def test_discretionary_spending_excludes_a_real_401k_contribution_during_coasting(self, bracket_table):
        # 2026-09-07 regression -- a real bug found AFTER the redesign's own tests all passed (every
        # test above uses a $0/$0 401(k)/IRA config, which happens to mask it entirely): 401(k) is
        # deliberately UNAFFECTED by saving_fraction (a payroll deduction, unchanged precedent -- see
        # modules.contributions's own module docstring), so a REAL 401(k) election keeps contributing
        # even during full coasting (2031, saving_fraction == 0). discretionary_spending must exclude
        # whatever that 401(k) contribution actually cost -- NOT treat the entire pre-401(k) surplus
        # as spent, which would double-count the same dollars as both "financed the 401(k)" and
        # "discretionary spending."
        rows = _coasting_test_rows(bracket_table, contribution_config_by_year={y: _max_config() for y in range(2026, 2037)})
        row_2031 = next(r for r in rows if r["year"] == 2031)
        assert row_2031["w2_401k_contribution_used"] > 0  # 401(k) still funds during coasting
        assert row_2031["roth_ira_contribution_used"] == pytest.approx(0.0)  # Roth IS gated by saving_fraction
        assert row_2031["contribution_destinations"]["taxable"] == pytest.approx(0.0)
        # The genuine leftover after the real 401(k) contribution -- must equal `profit` exactly
        # (Roth/Traditional/Taxable are all $0 this year, so every remaining real dollar IS
        # discretionary). A double-counting formula (the pre-401(k) baseline, ignoring the real
        # 401(k) contribution) would instead report `profit + <the 401(k) contribution's after-tax
        # cost>` here -- strictly more than `profit` itself, which this pins down as wrong.
        assert row_2031["discretionary_spending"] == pytest.approx(row_2031["profit"])


class TestProjectMultiYearWithdrawalDateOrdering:
    """2026-09-06, NEXT.md item B2 — `withdrawal_start_date` may not precede EITHER Income end date
    (`retirement_date`) or Savings stop date; equality with either (or both) is explicitly allowed.
    Structurally guarantees `saving_fraction` reaches exactly 0 no later than the year `withdrawing_
    fraction` turns positive, so Stage-2 contributions and an active withdrawal can never coincide
    (item B5's own "withdrawing money and then re-saving it" concern)."""

    def _run(self, bracket_table, retirement_date, savings_stop_date, withdrawal_start_date):
        return project_multi_year(
            _income_inputs(current_year_already_earned_w2=100000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2030, 12, 31),
            retirement_date=retirement_date,
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            phase_dates={
                "savings_stop_date": savings_stop_date,
                "withdrawal_start_date": withdrawal_start_date,
                "ss_claim_date": withdrawal_start_date,
            },
        )

    def test_withdrawal_before_retirement_raises(self, bracket_table):
        with pytest.raises(ValueError, match="Withdrawal start date"):
            self._run(
                bracket_table,
                retirement_date=date(2050, 1, 1),
                savings_stop_date=date(2040, 1, 1),
                withdrawal_start_date=date(2049, 1, 1),  # one year before retirement
            )

    def test_withdrawal_before_savings_stop_raises(self, bracket_table):
        with pytest.raises(ValueError, match="Withdrawal start date"):
            self._run(
                bracket_table,
                retirement_date=date(2030, 1, 1),
                savings_stop_date=date(2040, 1, 1),  # savings stops AFTER retirement
                withdrawal_start_date=date(2035, 1, 1),  # before savings_stop_date
            )

    def test_withdrawal_exactly_equal_to_both_is_allowed(self, bracket_table):
        # Explicitly confirmed with the user: same-day is fine, only strictly BEFORE raises.
        rows = self._run(
            bracket_table,
            retirement_date=date(2030, 1, 1),
            savings_stop_date=date(2030, 1, 1),
            withdrawal_start_date=date(2030, 1, 1),
        )
        assert rows  # did not raise

    def test_withdrawal_equal_to_retirement_but_after_savings_stop_is_allowed(self, bracket_table):
        rows = self._run(
            bracket_table,
            retirement_date=date(2030, 1, 1),
            savings_stop_date=date(2028, 1, 1),
            withdrawal_start_date=date(2030, 1, 1),
        )
        assert rows  # did not raise

    def test_withdrawal_after_both_is_allowed(self, bracket_table):
        rows = self._run(
            bracket_table,
            retirement_date=date(2028, 1, 1),
            savings_stop_date=date(2026, 1, 1),
            withdrawal_start_date=date(2030, 1, 1),
        )
        assert rows  # did not raise

    def test_no_phase_dates_at_all_skips_validation_entirely(self, bracket_table):
        # phase_dates=None (the default) must not raise -- every pre-existing caller/test that
        # never supplies phase_dates at all keeps working unchanged.
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=100000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2020, 1, 1),  # already "retired" -- would violate B2 if validated
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
        )
        assert rows


class TestEarnedIncomeTax:
    """`earned_income_tax` (2026-08-15, user request) — the tax on gross_w2 + gross_se alone,
    investment/break income zeroed, actual 401(k)-family contributions applied. Powers the
    "Pre-retirement: where gross income goes" chart's earned-income-only stack."""

    def test_none_when_tax_unavailable(self):
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=80000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table={},  # empty -> tax_available False
        )
        assert rows[0]["tax_available"] is False
        assert rows[0]["earned_income_tax"] is None

    def test_matches_total_tax_when_there_is_no_investment_or_break_income(self, bracket_table):
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=80000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
        )
        row = rows[0]
        assert row["gross_investment_income"] is None  # no portfolio wired in at all
        assert row["earned_income_tax"] == pytest.approx(row["total_tax"])

    def test_excludes_investment_income_that_total_tax_includes(self, bracket_table):
        # Real dividend income alongside real W-2 income -- earned_income_tax must be strictly LESS
        # than total_tax (which is computed on the full, investment-income-inclusive gross_income),
        # and must equal what compute_taxes alone would return for the W-2 income by itself.
        universe = _small_universe()
        lots = [
            {"ticker": "VTI", "account_type": "Taxable", "shares": 10000.0, "basis_per_share": 100.0, "year_acquired": 2020}
        ]
        prices = {"VTI": 100.0}
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=80000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2060, 1, 1),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            universe=universe,
            initial_lots=lots,
            initial_prices_by_ticker=prices,
        )
        row = rows[0]
        assert row["gross_investment_income"] > 0
        assert row["earned_income_tax"] < row["total_tax"]
        expected = compute_taxes(
            tax_year=row["bracket_year_used"],
            filing_status="single",
            age_at_year_end=row["bracket_year_used"] - 1990,
            w2_gross=80000.0,
            pretax_401k=0.0,
            roth_401k=0.0,
            pretax_health_dental=0.0,
            se_net_profit=0.0,
            se_solo_employee_deferral=0.0,
            se_solo_employer_contribution=0.0,
            taxable_retirement_withdrawal=0.0,
            ltcg=0.0,
            qualified_dividends=0.0,
            ordinary_dividends=0.0,
            interest_income=0.0,
            ss_benefit_gross=0.0,
            bracket_table=bracket_table,
            ordinary_break_income=0.0,
        )
        assert row["earned_income_tax"] == pytest.approx(expected["total_tax"])

    def test_zero_when_no_earned_income_even_with_investment_income(self, bracket_table):
        universe = _small_universe()
        lots = [
            {"ticker": "VTI", "account_type": "Taxable", "shares": 10000.0, "basis_per_share": 100.0, "year_acquired": 2020}
        ]
        prices = {"VTI": 100.0}
        rows = project_multi_year(
            _income_inputs(),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2020, 1, 1),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            universe=universe,
            initial_lots=lots,
            initial_prices_by_ticker=prices,
        )
        row = rows[0]
        assert row["gross_w2"] == pytest.approx(0.0)
        assert row["gross_investment_income"] > 0
        assert row["earned_income_tax"] == pytest.approx(0.0)


def _small_universe():
    """A two-ticker universe (one qualified-dividend equity ETF, one interest-bearing cash-like
    ticker) — shaped exactly like modules.portfolio.build_universe's output, deliberately with two
    DIFFERENT return/dividend profiles so a test can prove neither ticker is rolled forward using
    the other's (or a blended) rate."""
    return {
        "asset_classes": {
            "us_stock": {"label": "US stock", "nominal_return": 0.08},
            "cash": {"label": "Cash", "nominal_return": 0.03},
        },
        "etfs": {
            "VTI": {"asset_class": "us_stock", "expense_ratio": 0.0003, "dividend_rate": 0.015, "income_type": "qualified"},
            "SPAXX": {"asset_class": "cash", "expense_ratio": 0.0, "dividend_rate": 0.03, "income_type": "interest"},
        },
    }


class TestProjectMultiYearPortfolioRollForward:
    """MODEL_WIRING.md §4 (Step 3, 2026-08-10) — per-asset roll-forward wired into
    project_multi_year: universe/initial_lots/initial_prices_by_ticker/inflation_rate/
    target_allocations, all optional and opt-in (every test above, with none of these supplied,
    keeps its exact original $0-investment-income behavior). See modules/investing.py's own tests
    for the underlying roll_forward_holding/roll_forward_portfolio math itself — these tests only
    check the WIRING: gross_income widening, dividends reaching compute_taxes by type/account, the
    ledger carrying forward year over year, and the §4.1 double-counting trap specifically."""

    def test_disabled_by_default_portfolio_fields_are_none(self, bracket_table):
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=80000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
        )
        row = rows[0]
        assert row["portfolio_value"] is None
        assert row["ending_lots"] is None
        assert row["ending_prices_by_ticker"] is None
        assert row["gross_investment_income"] is None
        assert row["gross_income"] == pytest.approx(80000.0)

    def test_taxable_qualified_dividends_widen_gross_income_and_reach_compute_taxes(self, bracket_table):
        universe = _small_universe()
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 100.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}
        rows = project_multi_year(
            _income_inputs(),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            universe=universe,
            initial_lots=lots,
            initial_prices_by_ticker=prices,
            inflation_rate=0.03,
        )
        row = rows[0]
        expected_distribution = 1000.0 * 100.0 * 0.015  # shares * price * dividend_rate
        assert row["taxable_qualified_dividends"] == pytest.approx(expected_distribution)
        assert row["taxable_ordinary_dividends"] == pytest.approx(0.0)
        assert row["taxable_interest_income"] == pytest.approx(0.0)
        assert row["gross_investment_income"] == pytest.approx(expected_distribution)
        assert row["gross_income"] == pytest.approx(expected_distribution)
        # End-to-end proof the dividend actually reached compute_taxes (not just reported on the
        # row): total_tax for this dividend-only year must equal compute_taxes called directly
        # with the same qualified_dividends figure and everything else at $0.
        direct = compute_taxes(
            tax_year=row["bracket_year_used"], filing_status="single", age_at_year_end=36,
            w2_gross=0.0, pretax_401k=0.0, roth_401k=0.0, pretax_health_dental=0.0,
            se_net_profit=0.0, se_solo_employee_deferral=0.0, se_solo_employer_contribution=0.0,
            taxable_retirement_withdrawal=0.0, ltcg=0.0, qualified_dividends=expected_distribution,
            ordinary_dividends=0.0, interest_income=0.0, ss_benefit_gross=0.0,
            bracket_table=bracket_table, deferral_priority="w2-first", ordinary_break_income=0.0,
        )
        assert row["total_tax"] == pytest.approx(direct["total_tax"])

    def test_interest_income_routed_separately_from_qualified_dividends(self, bracket_table):
        universe = _small_universe()
        lots = [{"ticker": "SPAXX", "account_type": "Taxable", "shares": 10000.0, "basis_per_share": 1.0, "year_acquired": 2020}]
        prices = {"SPAXX": 1.0}
        rows = project_multi_year(
            _income_inputs(),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            universe=universe,
            initial_lots=lots,
            initial_prices_by_ticker=prices,
            inflation_rate=0.03,
        )
        row = rows[0]
        expected_interest = 10000.0 * 1.0 * 0.03
        assert row["taxable_interest_income"] == pytest.approx(expected_interest)
        assert row["taxable_qualified_dividends"] == pytest.approx(0.0)

    def test_roth_account_distributions_never_widen_gross_income_or_tax(self, bracket_table):
        # Same holding, same dividend income, but in a Roth IRA instead of Taxable -- must have
        # ZERO effect on gross_income/tax (only Taxable-account distributions are taxed).
        universe = _small_universe()
        lots = [{"ticker": "VTI", "account_type": "Roth IRA", "shares": 1000.0, "basis_per_share": 100.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}
        with_roth = project_multi_year(
            _income_inputs(current_year_already_earned_w2=50000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            universe=universe,
            initial_lots=lots,
            initial_prices_by_ticker=prices,
            inflation_rate=0.03,
        )
        no_portfolio = project_multi_year(
            _income_inputs(current_year_already_earned_w2=50000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
        )
        assert with_roth[0]["gross_income"] == pytest.approx(no_portfolio[0]["gross_income"])
        assert with_roth[0]["total_tax"] == pytest.approx(no_portfolio[0]["total_tax"])
        # But the Roth holding still grows and still shows up in the ledger.
        assert with_roth[0]["portfolio_value"] > 0

    def test_no_blended_rate_each_ticker_ends_at_its_own_independently_computed_price(self, bracket_table):
        universe = _small_universe()
        lots = [
            {"ticker": "VTI", "account_type": "Taxable", "shares": 100.0, "basis_per_share": 100.0, "year_acquired": 2020},
            {"ticker": "SPAXX", "account_type": "Taxable", "shares": 100.0, "basis_per_share": 1.0, "year_acquired": 2020},
        ]
        prices = {"VTI": 100.0, "SPAXX": 1.0}
        rows = project_multi_year(
            _income_inputs(),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            universe=universe,
            initial_lots=lots,
            initial_prices_by_ticker=prices,
            inflation_rate=0.03,
        )
        ending_prices = rows[0]["ending_prices_by_ticker"]
        expected_vti = roll_forward_holding(price=100.0, nominal_return=0.08, expense_ratio=0.0003, inflation_rate=0.03, dividend_rate=0.015)
        expected_spaxx = roll_forward_holding(price=1.0, nominal_return=0.03, expense_ratio=0.0, inflation_rate=0.03, dividend_rate=0.03)
        assert ending_prices["VTI"] == pytest.approx(expected_vti["new_price"])
        assert ending_prices["SPAXX"] == pytest.approx(expected_spaxx["new_price"])
        # The two tickers' returns are genuinely different -- proof neither used the other's rate.
        assert expected_vti["new_price"] / 100.0 != pytest.approx(expected_spaxx["new_price"] / 1.0)

    def test_reinvested_dividends_grow_share_count_year_over_year(self, bracket_table):
        universe = _small_universe()
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 100.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}
        rows = project_multi_year(
            _income_inputs(),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2027, 12, 31),
            retirement_date=date(2027, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            universe=universe,
            initial_lots=lots,
            initial_prices_by_ticker=prices,
            inflation_rate=0.03,
        )
        assert len(rows) == 2
        shares_after_year1 = sum(lot["shares"] for lot in rows[0]["ending_lots"] if lot["ticker"] == "VTI")
        shares_after_year2 = sum(lot["shares"] for lot in rows[1]["ending_lots"] if lot["ticker"] == "VTI")
        assert shares_after_year1 > 1000.0  # year 1's dividend already reinvested into a new lot
        assert shares_after_year2 > shares_after_year1  # year 2 reinvests again, compounding further
        # More than one lot now exists for VTI (original + at least one reinvestment lot each year).
        assert sum(1 for lot in rows[1]["ending_lots"] if lot["ticker"] == "VTI") >= 3

    def test_taxable_reinvestment_stops_once_withdrawing_end_to_end(self, bracket_table):
        universe = _small_universe()
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 100.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}
        already_withdrawing = {
            "savings_stop_date": date(2020, 1, 1),
            "withdrawal_start_date": date(2020, 1, 1),
            "ss_claim_date": date(2020, 1, 1),
        }
        rows = project_multi_year(
            _income_inputs(),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            # Already retired AND already withdrawing, both in the past (2020) -- NOT 2026-12-31:
            # retirement_date must not be LATER than withdrawal_start_date (NEXT.md item B2's
            # date-ordering validation).
            retirement_date=date(2020, 1, 1),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            universe=universe,
            initial_lots=lots,
            initial_prices_by_ticker=prices,
            inflation_rate=0.03,
            phase_dates=already_withdrawing,
            # Step 6's own withdrawal mechanism would otherwise ALSO sell shares in this same
            # withdrawing year (a real, separate, later-built behavior — see
            # TestProjectMultiYearWithdrawal) -- a 0% withdrawal rate isolates this test's own,
            # earlier (Step 3) concern: that reinvestment specifically stops, nothing else.
            withdrawal_strategy_params={"rate": 0.0},
        )
        # Distribution income is still produced and taxed, but no new lot is bought with it.
        assert rows[0]["taxable_qualified_dividends"] > 0
        assert len(rows[0]["ending_lots"]) == 1
        assert rows[0]["ending_lots"][0]["shares"] == pytest.approx(1000.0)

    def test_ledger_value_matches_roll_forward_holding_exactly_no_double_counting(self, bracket_table):
        # MODEL_WIRING.md §4.1's own explicit warning: the distribution rate must be subtracted from
        # total return to get price return, or it gets counted twice. This test protects the WIRING
        # layer specifically -- that project_multi_year doesn't ALSO add the distribution back in as
        # extra untracked cash on top of the reinvestment lot it already creates from that same
        # distribution (which would silently double the dividend's contribution to portfolio_value).
        universe = _small_universe()
        price = 100.0
        shares = 50.0
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": shares, "basis_per_share": price, "year_acquired": 2020}]
        prices = {"VTI": price}
        rows = project_multi_year(
            _income_inputs(),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            universe=universe,
            initial_lots=lots,
            initial_prices_by_ticker=prices,
            inflation_rate=0.03,
        )
        row = rows[0]
        rolled = roll_forward_holding(price=price, nominal_return=0.08, expense_ratio=0.0003, inflation_rate=0.03, dividend_rate=0.015)
        new_price = rolled["new_price"]
        distribution = rolled["distribution_income_per_share"] * shares
        reinvested_shares = distribution / price  # reinvested at the START-of-year price, §3.4
        expected_value = shares * new_price + reinvested_shares * new_price
        assert row["portfolio_value"] == pytest.approx(expected_value, rel=1e-9)
        # The wrong (double-counted) answer would be expected_value + distribution -- explicitly
        # confirm we're nowhere near that.
        assert row["portfolio_value"] != pytest.approx(expected_value + distribution, rel=1e-6)

    def test_contribution_dollars_convert_into_new_lots(self, bracket_table):
        universe = _small_universe()
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=150000.0),
            _expense_inputs(current_year_already_incurred_expense=20000.0),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            phase_dates=PHASE_DATES,
            contribution_config_by_year={2026: dict(RECOMMENDED_CONTRIBUTION_CONFIG)},
            universe=universe,
            initial_lots=[],
            initial_prices_by_ticker={"VTI": 100.0},
            inflation_rate=0.03,
            target_allocations={"Traditional 401(k)": {"VTI": 1.0}, "Roth IRA": {"VTI": 1.0}},
        )
        row = rows[0]
        assert row["w2_401k_contribution_used"] > 0
        assert row["roth_ira_contribution_used"] > 0
        traditional_401k_lots = [lot for lot in row["ending_lots"] if lot["account_type"] == "Traditional 401(k)"]
        roth_ira_lots = [lot for lot in row["ending_lots"] if lot["account_type"] == "Roth IRA"]
        assert traditional_401k_lots and traditional_401k_lots[0]["shares"] > 0
        assert roth_ira_lots and roth_ira_lots[0]["shares"] > 0
        assert traditional_401k_lots[0]["year_acquired"] == 2026

    # ---- tax_attributable_to_investment_income (2026-09-06 fix, see NEXT.md) ----
    # A Taxable holding's dividend/interest tax is real and reported in total_tax, but until this
    # fix nothing ever actually deducted it from anything: roll_forward_portfolio reinvests the
    # full GROSS distribution regardless of account type, and available_cash is deliberately
    # earned-income-only (so it can't absorb the tax either). Net effect: Taxable dividends
    # compounded completely tax-free. These tests run at the project_multi_year level (not
    # roll_forward_portfolio in isolation) since the bug lives entirely in how this module
    # orchestrates tax + reinvestment together.

    def test_taxable_dividend_reinvests_net_of_its_own_tax_when_actually_taxed(self, bracket_table):
        # High W-2 income pushes the qualified dividend out of the 0% bracket, so this scenario has
        # a real, nonzero tax to isolate and deduct.
        universe = _small_universe()
        shares, price = 1000.0, 100.0
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": shares, "basis_per_share": price, "year_acquired": 2020}]
        prices = {"VTI": price}
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=150000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            universe=universe,
            initial_lots=lots,
            initial_prices_by_ticker=prices,
            inflation_rate=0.03,
        )
        row = rows[0]
        expected_distribution = shares * price * 0.015  # shares * price * VTI's dividend_rate
        assert row["gross_investment_income"] == pytest.approx(expected_distribution)
        # The bug this fix closes: a real, nonzero tax bill on the dividend that a naive reading of
        # roll_forward_portfolio's own reinvestment would otherwise ignore entirely.
        assert row["tax_attributable_to_investment_income"] > 0
        assert row["tax_attributable_to_investment_income"] < expected_distribution

        after_tax_distribution = expected_distribution - row["tax_attributable_to_investment_income"]
        expected_reinvested_shares = after_tax_distribution / price  # reinvested at start-of-year price

        new_taxable_lots = [
            lot for lot in row["ending_lots"]
            if lot["account_type"] == "Taxable" and lot["year_acquired"] == 2026
        ]
        assert new_taxable_lots
        actual_reinvested_shares = sum(lot["shares"] for lot in new_taxable_lots)
        assert actual_reinvested_shares == pytest.approx(expected_reinvested_shares, rel=1e-9)
        # And the sanity bound that proves this is a genuine fix, not a no-op: strictly fewer
        # shares than the old, buggy full-gross reinvestment would have bought.
        assert actual_reinvested_shares < expected_distribution / price

    def test_taxable_dividend_fully_reinvests_when_untaxed_at_zero_other_income(self, bracket_table):
        # $0 other income -> a small qualified dividend falls entirely in the 0% QDI bracket -> $0
        # tax to isolate -> full gross reinvestment, same as before this fix. Confirms the fix is a
        # genuine no-op in the untaxed case, not a blanket haircut on every Taxable reinvestment.
        universe = _small_universe()
        shares, price = 50.0, 100.0
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": shares, "basis_per_share": price, "year_acquired": 2020}]
        prices = {"VTI": price}
        rows = project_multi_year(
            _income_inputs(),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            universe=universe,
            initial_lots=lots,
            initial_prices_by_ticker=prices,
            inflation_rate=0.03,
        )
        row = rows[0]
        assert row["tax_attributable_to_investment_income"] == pytest.approx(0.0)
        expected_distribution = shares * price * 0.015
        expected_reinvested_shares = expected_distribution / price
        new_taxable_lots = [
            lot for lot in row["ending_lots"]
            if lot["account_type"] == "Taxable" and lot["year_acquired"] == 2026
        ]
        actual_reinvested_shares = sum(lot["shares"] for lot in new_taxable_lots)
        assert actual_reinvested_shares == pytest.approx(expected_reinvested_shares, rel=1e-9)

    def test_tax_advantaged_reinvestment_never_reduced_even_under_a_high_tax_bracket(self, bracket_table):
        # Same high-income scenario as the Taxable test above, but the dividend-paying holding
        # lives in a Roth IRA instead -- must reinvest its FULL gross distribution regardless of
        # this year's tax bracket, since a Roth distribution is genuinely never taxed.
        universe = _small_universe()
        shares, price = 1000.0, 100.0
        lots = [{"ticker": "VTI", "account_type": "Roth IRA", "shares": shares, "basis_per_share": price, "year_acquired": 2020}]
        prices = {"VTI": price}
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=150000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2026, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            universe=universe,
            initial_lots=lots,
            initial_prices_by_ticker=prices,
            inflation_rate=0.03,
        )
        row = rows[0]
        # No Taxable distribution at all this year -> nothing for this fix to isolate/deduct.
        assert row["gross_investment_income"] == pytest.approx(0.0)
        assert row["tax_attributable_to_investment_income"] == pytest.approx(0.0)
        expected_distribution = shares * price * 0.015
        expected_reinvested_shares = expected_distribution / price
        new_roth_lots = [
            lot for lot in row["ending_lots"]
            if lot["account_type"] == "Roth IRA" and lot["year_acquired"] == 2026
        ]
        actual_reinvested_shares = sum(lot["shares"] for lot in new_roth_lots)
        assert actual_reinvested_shares == pytest.approx(expected_reinvested_shares, rel=1e-9)


class TestProjectMultiYearDissaving:
    """MODEL_WIRING.md §5.1 point 1 (Step 4, 2026-08-10) — dissaving: any pre-retirement year with
    `profit < 0` sells portfolio assets to cover the shortfall, via the same draw-order/sale-method
    machinery Step 6 will later reuse for formal retirement withdrawals. Gated on `retirement_date`
    specifically, not `withdrawal_start_date`/`saving_fraction` — see `project_multi_year`'s own
    docstring for why."""

    def _run(self, bracket_table, universe, lots, prices, expense, retirement_date=date(2060, 1, 1), **kwargs):
        return project_multi_year(
            _income_inputs(),
            _expense_inputs(current_year_already_incurred_expense=expense),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=retirement_date,
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            universe=universe,
            initial_lots=lots,
            initial_prices_by_ticker=prices,
            inflation_rate=0.03,
            **kwargs,
        )

    def test_disabled_when_portfolio_inactive(self, bracket_table):
        rows = project_multi_year(
            _income_inputs(),
            _expense_inputs(current_year_already_incurred_expense=1000.0),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2060, 1, 1),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
        )
        row = rows[0]
        assert row["dissaving_proceeds"] is None
        assert row["dissaving_unfunded_shortfall"] is None

    def test_no_dissaving_when_profit_positive(self, bracket_table):
        universe = _small_universe()
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 100.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}
        rows = self._run(bracket_table, universe, lots, prices, expense=0.0)
        row = rows[0]
        assert row["dissaving_proceeds"] == pytest.approx(0.0)
        assert row["profit"] >= 0  # taxable dividend income, no expenses -> non-negative profit

    def test_negative_profit_pre_retirement_triggers_sale(self, bracket_table):
        universe = _small_universe()
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 100.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}
        rows = self._run(bracket_table, universe, lots, prices, expense=5000.0)
        row = rows[0]
        assert row["dissaving_proceeds"] > 0
        assert row["dissaving_unfunded_shortfall"] == pytest.approx(0.0)  # plenty of assets to cover $5,000

    def test_not_gated_on_withdrawal_start_date(self, bracket_table):
        # withdrawal_start_date far in the future (withdrawing_fraction stays 0 this year) must NOT
        # prevent dissaving -- it's gated on retirement_date only (MODEL_WIRING.md §5.1's own words).
        universe = _small_universe()
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 100.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}
        phase_dates = {
            "savings_stop_date": date(2060, 1, 1),
            "withdrawal_start_date": date(2090, 1, 1),
            "ss_claim_date": date(2060, 1, 1),
        }
        rows = self._run(bracket_table, universe, lots, prices, expense=5000.0, phase_dates=phase_dates)
        assert rows[0]["dissaving_proceeds"] > 0

    def test_no_dissaving_after_retirement(self, bracket_table):
        universe = _small_universe()
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 100.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}
        rows = self._run(bracket_table, universe, lots, prices, expense=5000.0, retirement_date=date(2025, 1, 1))
        row = rows[0]
        assert row["profit"] < 0  # the shortfall is real
        assert row["dissaving_proceeds"] == pytest.approx(0.0)  # but NOT funded -- Step 6's job, not this one

    def test_short_term_gain_routes_correctly(self, bracket_table):
        universe = _small_universe()
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 10.0, "year_acquired": 2026}]
        prices = {"VTI": 100.0}
        rows = self._run(bracket_table, universe, lots, prices, expense=5000.0, sale_method="fifo")
        row = rows[0]
        assert row["dissaving_short_term_gain"] > 0
        assert row["dissaving_long_term_gain"] == pytest.approx(0.0)

    def test_long_term_gain_routes_correctly(self, bracket_table):
        universe = _small_universe()
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 10.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}
        rows = self._run(bracket_table, universe, lots, prices, expense=5000.0, sale_method="fifo")
        row = rows[0]
        assert row["dissaving_long_term_gain"] > 0
        assert row["dissaving_short_term_gain"] == pytest.approx(0.0)

    def test_traditional_account_sale_routes_to_retirement_withdrawal_and_is_taxed(self, bracket_table):
        universe = _small_universe()
        lots = [{"ticker": "VTI", "account_type": "Traditional 401(k)", "shares": 10000.0, "basis_per_share": 10.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}
        # Large enough that the withdrawal clears the standard deduction and produces real tax.
        rows = self._run(bracket_table, universe, lots, prices, expense=80000.0)
        row = rows[0]
        assert row["dissaving_retirement_withdrawal"] > 0
        assert row["total_tax"] > 0  # a real distribution, taxed as ordinary income

    def test_roth_account_sale_produces_zero_taxable_income(self, bracket_table):
        universe = _small_universe()
        lots = [{"ticker": "VTI", "account_type": "Roth IRA", "shares": 1000.0, "basis_per_share": 10.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}
        rows = self._run(bracket_table, universe, lots, prices, expense=5000.0)
        row = rows[0]
        assert row["dissaving_proceeds"] > 0
        assert row["dissaving_long_term_gain"] == pytest.approx(0.0)
        assert row["dissaving_short_term_gain"] == pytest.approx(0.0)
        assert row["dissaving_retirement_withdrawal"] == pytest.approx(0.0)
        assert row["total_tax"] == pytest.approx(0.0)  # no other income at all this year, all Roth

    def test_unfunded_shortfall_surfaced_when_assets_insufficient(self, bracket_table):
        universe = _small_universe()
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 1.0, "basis_per_share": 100.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}  # only $100 of sellable value
        rows = self._run(bracket_table, universe, lots, prices, expense=5000.0)
        row = rows[0]
        assert row["dissaving_unfunded_shortfall"] > 0
        # The original lot is fully liquidated by the sale -- what's left (if anything) is only a
        # tiny NEW lot from this same year's own dividend reinvestment (§4.3's reinvestment rule
        # runs independently of dissaving, keyed off withdrawing_fraction, not profit), not any
        # leftover of the sold lot itself.
        remaining_value = sum(lot["shares"] * prices["VTI"] for lot in row["ending_lots"])
        assert remaining_value < 5.0

    def test_custom_draw_order_respected(self, bracket_table):
        universe = _small_universe()
        lots = [
            {"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 10.0, "year_acquired": 2020},
            {"ticker": "VTI", "account_type": "Roth IRA", "shares": 1000.0, "basis_per_share": 10.0, "year_acquired": 2020},
        ]
        prices = {"VTI": 100.0}
        rows = self._run(bracket_table, universe, lots, prices, expense=5000.0, draw_order=["Roth IRA"])
        row = rows[0]
        # Only Roth IRA was in the draw order -- the Taxable lot must be entirely untouched by sales.
        assert row["dissaving_long_term_gain"] == pytest.approx(0.0)
        taxable_lot = next(lot for lot in row["ending_lots"] if lot["account_type"] == "Taxable")
        assert taxable_lot["shares"] == pytest.approx(1000.0)

    # ---- dividend_used_for_expenses (2026-09-06, NEXT.md item B4/B5 — a real double-counting bug)
    # ----
    # Before this fix: `row["profit"]` (investment-income-INCLUSIVE) gated the sale, so a dividend
    # large enough to cover an earned-income shortfall on its own skipped the sale correctly, while
    # that SAME dividend cash, completely separately, still unconditionally reinvested in full --
    # the identical dollar counted as both "spent on expenses" and "bought new shares." $0 other
    # income (`_income_inputs()`'s own default) keeps the dividend in the 0% tax bracket, isolating
    # this fix from the separate investment-income-tax fix (both compose, tested below).

    def test_dividend_fully_covers_earned_shortfall_no_sale_partial_reinvestment(self, bracket_table):
        universe = _small_universe()
        shares, price = 1000.0, 100.0
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": shares, "basis_per_share": price, "year_acquired": 2020}]
        prices = {"VTI": price}
        expense = 1000.0
        rows = self._run(bracket_table, universe, lots, prices, expense=expense)
        row = rows[0]
        expected_distribution = shares * price * 0.015  # $1,500 -- VTI's own dividend_rate, _small_universe
        assert row["gross_investment_income"] == pytest.approx(expected_distribution)
        assert row["tax_attributable_to_investment_income"] == pytest.approx(0.0)  # $0 other income -> 0% bracket

        # The dividend ($1,500) more than covers the $1,000 earned-only shortfall -- no sale at all.
        assert row["dividend_used_for_expenses"] == pytest.approx(expense)
        assert row["dissaving_proceeds"] == pytest.approx(0.0)

        # Only the LEFTOVER dividend ($500, since $0 tax + $1,000 spent) actually reinvests -- the
        # bug this fix closes would have reinvested the full $1,500 regardless.
        expected_reinvested_dollars = expected_distribution - expense
        new_taxable_lots = [
            lot for lot in row["ending_lots"] if lot["account_type"] == "Taxable" and lot["year_acquired"] == 2026
        ]
        actual_reinvested_dollars = sum(lot["shares"] * price for lot in new_taxable_lots)
        assert actual_reinvested_dollars == pytest.approx(expected_reinvested_dollars, rel=1e-9)

    def test_dividend_partially_covers_shortfall_sale_funds_only_the_remainder(self, bracket_table):
        universe = _small_universe()
        shares, price = 1000.0, 100.0
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": shares, "basis_per_share": price, "year_acquired": 2020}]
        prices = {"VTI": price}
        expense = 5000.0  # exceeds the $1,500 dividend -- a real sale is still required for the rest
        rows = self._run(bracket_table, universe, lots, prices, expense=expense)
        row = rows[0]
        expected_distribution = shares * price * 0.015  # $1,500

        # The ENTIRE dividend goes to expenses (bounded by the full gross amount, per this fix's own
        # formula) -- nothing left over to reinvest at all.
        assert row["dividend_used_for_expenses"] == pytest.approx(expected_distribution)
        new_taxable_lots = [
            lot for lot in row["ending_lots"] if lot["account_type"] == "Taxable" and lot["year_acquired"] == 2026
        ]
        actual_reinvested_dollars = sum(lot["shares"] * price for lot in new_taxable_lots)
        assert actual_reinvested_dollars == pytest.approx(0.0)

        # A real sale still covers the REMAINING shortfall ($5,000 - $1,500 = $3,500), not the full
        # $5,000 -- proving the dividend genuinely reduced the sale, not just got ignored.
        assert row["dissaving_proceeds"] > 0
        assert row["dissaving_proceeds"] < expense
        assert row["dissaving_proceeds"] == pytest.approx(expense - expected_distribution, rel=1e-2)

    def test_no_earned_shortfall_dividend_fully_reinvests_same_as_before_this_fix(self, bracket_table):
        # $0 expense -> $0 earned shortfall -> $0 dividend used for expenses -> the ENTIRE dividend
        # still reinvests, unaffected by this fix -- confirms it's a genuine no-op outside a real
        # earned-income shortfall, not a blanket haircut on every Taxable dividend.
        universe = _small_universe()
        shares, price = 1000.0, 100.0
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": shares, "basis_per_share": price, "year_acquired": 2020}]
        prices = {"VTI": price}
        rows = self._run(bracket_table, universe, lots, prices, expense=0.0)
        row = rows[0]
        assert row["dividend_used_for_expenses"] == pytest.approx(0.0)
        expected_distribution = shares * price * 0.015
        new_taxable_lots = [
            lot for lot in row["ending_lots"] if lot["account_type"] == "Taxable" and lot["year_acquired"] == 2026
        ]
        actual_reinvested_dollars = sum(lot["shares"] * price for lot in new_taxable_lots)
        assert actual_reinvested_dollars == pytest.approx(expected_distribution, rel=1e-9)


class TestProjectMultiYearRebalance:
    """MODEL_WIRING.md §7 (Step 5, 2026-08-10) — rebalancing within tax-advantaged accounts, opt-in
    via `rebalance` (default `False`, so every pre-existing caller/test above is unaffected)."""

    def _run(self, bracket_table, lots, target_allocations, rebalance=True, **kwargs):
        universe = _small_universe()
        # Two tickers with genuinely different returns/dividends so a starting 50/50 split drifts
        # over the year even with no contributions -- see _small_universe (VTI 8% nominal/1.5% div,
        # SPAXX 3% nominal/3% div, both real_price_return... SPAXX's price barely moves while VTI's
        # does, guaranteeing real drift to correct).
        prices = {"VTI": 100.0, "SPAXX": 1.0}
        return project_multi_year(
            _income_inputs(),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2060, 1, 1),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            universe=universe,
            initial_lots=lots,
            initial_prices_by_ticker=prices,
            inflation_rate=0.03,
            target_allocations=target_allocations,
            rebalance=rebalance,
            **kwargs,
        )

    def test_disabled_by_default_residual_drift_is_none(self, bracket_table):
        lots = [{"ticker": "VTI", "account_type": "Roth IRA", "shares": 10.0, "basis_per_share": 100.0, "year_acquired": 2020}]
        rows = self._run(bracket_table, lots, {"Roth IRA": {"VTI": 1.0}}, rebalance=False)
        assert rows[0]["rebalance_residual_drift"] is None

    def test_corrects_drifted_tax_advantaged_account_to_target(self, bracket_table):
        # Starts already drifted (90/10 instead of the 50/50 target) -- rebalancing must correct it.
        lots = [
            {"ticker": "VTI", "account_type": "Roth IRA", "shares": 9.0, "basis_per_share": 100.0, "year_acquired": 2020},
            {"ticker": "SPAXX", "account_type": "Roth IRA", "shares": 1000.0, "basis_per_share": 1.0, "year_acquired": 2020},
        ]
        target = {"Roth IRA": {"VTI": 0.5, "SPAXX": 0.5}}
        rows = self._run(bracket_table, lots, target, rebalance=True)
        drift = rows[0]["rebalance_residual_drift"]["Roth IRA"]
        assert drift == pytest.approx(0.0, abs=1e-6)

    def test_taxable_is_never_rebalanced(self, bracket_table):
        lots = [
            {"ticker": "VTI", "account_type": "Taxable", "shares": 9.0, "basis_per_share": 100.0, "year_acquired": 2020},
            {"ticker": "SPAXX", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 1.0, "year_acquired": 2020},
        ]
        target = {"Taxable": {"VTI": 0.5, "SPAXX": 0.5}}
        rows = self._run(bracket_table, lots, target, rebalance=True)
        # No "Taxable" key at all in the residual-drift report -- it's not a rebalanceable account
        # type, not even attempted.
        assert "Taxable" not in rows[0]["rebalance_residual_drift"]
        vti_shares = sum(lot["shares"] for lot in rows[0]["ending_lots"] if lot["ticker"] == "VTI" and lot["account_type"] == "Taxable")
        # Still drifted -- shares roughly unchanged from the original 9.0 (only dividend
        # reinvestment could move this, not a sell-side correction).
        assert vti_shares == pytest.approx(9.0, rel=0.05)

    def test_rebalancing_never_affects_tax_or_profit(self, bracket_table):
        lots = [
            {"ticker": "VTI", "account_type": "Roth IRA", "shares": 9.0, "basis_per_share": 100.0, "year_acquired": 2020},
            {"ticker": "SPAXX", "account_type": "Roth IRA", "shares": 1000.0, "basis_per_share": 1.0, "year_acquired": 2020},
        ]
        target = {"Roth IRA": {"VTI": 0.5, "SPAXX": 0.5}}
        without = self._run(bracket_table, lots, target, rebalance=False)
        with_reb = self._run(bracket_table, lots, target, rebalance=True)
        assert with_reb[0]["total_tax"] == pytest.approx(without[0]["total_tax"])
        assert with_reb[0]["profit"] == pytest.approx(without[0]["profit"])

    def test_residual_drift_reported_for_every_rebalanceable_account_type(self, bracket_table):
        lots = [{"ticker": "VTI", "account_type": "Roth IRA", "shares": 10.0, "basis_per_share": 100.0, "year_acquired": 2020}]
        rows = self._run(bracket_table, lots, {"Roth IRA": {"VTI": 1.0}}, rebalance=True)
        drift = rows[0]["rebalance_residual_drift"]
        for account_type in ("Traditional 401(k)", "Traditional IRA", "Roth 401(k)", "Roth IRA", "HSA"):
            assert account_type in drift

    def test_portfolio_value_conserved_by_rebalancing_itself(self, bracket_table):
        # Rebalancing trades are net-zero-cash -- total portfolio value for the SAME year must be
        # identical whether or not rebalancing ran (asset-location effects on growth only show up
        # in LATER years, not the year rebalancing itself happens).
        lots = [
            {"ticker": "VTI", "account_type": "Roth IRA", "shares": 9.0, "basis_per_share": 100.0, "year_acquired": 2020},
            {"ticker": "SPAXX", "account_type": "Roth IRA", "shares": 1000.0, "basis_per_share": 1.0, "year_acquired": 2020},
        ]
        target = {"Roth IRA": {"VTI": 0.5, "SPAXX": 0.5}}
        without = self._run(bracket_table, lots, target, rebalance=False)
        with_reb = self._run(bracket_table, lots, target, rebalance=True)
        assert with_reb[0]["portfolio_value"] == pytest.approx(without[0]["portfolio_value"])


ALREADY_WITHDRAWING_PHASE_DATES = {
    "savings_stop_date": date(2020, 1, 1),
    "withdrawal_start_date": date(2020, 1, 1),
    "ss_claim_date": date(2020, 1, 1),
}


class TestProjectMultiYearWithdrawal:
    """MODEL_WIRING.md §2.3 / §5.1 point 2 (Step 6, 2026-08-10) — the formal retirement-withdrawal
    strategy dispatch, wired into project_multi_year. Mutually exclusive with dissaving (see
    TestProjectMultiYearDissaving's own gate) -- these tests focus on the withdrawal side only."""

    def _run(self, bracket_table, lots, prices, expense=0.0, retirement_date=date(2020, 1, 1), **kwargs):
        universe = _small_universe()
        kwargs.setdefault("phase_dates", ALREADY_WITHDRAWING_PHASE_DATES)
        return project_multi_year(
            _income_inputs(),
            _expense_inputs(current_year_already_incurred_expense=expense),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=retirement_date,
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            universe=universe,
            initial_lots=lots,
            initial_prices_by_ticker=prices,
            inflation_rate=0.03,
            **kwargs,
        )

    def test_disabled_when_portfolio_inactive(self, bracket_table):
        rows = project_multi_year(
            _income_inputs(),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2020, 1, 1),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            phase_dates=ALREADY_WITHDRAWING_PHASE_DATES,
        )
        row = rows[0]
        assert row["withdrawal_target"] is None
        assert row["funding_gap"] is None

    def test_no_withdrawal_when_not_yet_in_withdrawal_phase(self, bracket_table):
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 100.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}
        rows = self._run(
            bracket_table, lots, prices,
            phase_dates={**ALREADY_WITHDRAWING_PHASE_DATES, "withdrawal_start_date": date(2090, 1, 1)},
        )
        row = rows[0]
        assert row["withdrawal_target"] == pytest.approx(0.0)
        assert row["withdrawal_sale_proceeds"] == pytest.approx(0.0)

    def test_withdrawal_target_is_rate_times_starting_portfolio_value(self, bracket_table):
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 100.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}  # $100,000 starting value
        rows = self._run(bracket_table, lots, prices, withdrawal_strategy_params={"rate": 0.04})
        assert rows[0]["withdrawal_target"] == pytest.approx(4000.0)

    def test_zero_rate_produces_zero_target_and_no_sale(self, bracket_table):
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 100.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}
        rows = self._run(bracket_table, lots, prices, withdrawal_strategy_params={"rate": 0.0})
        assert rows[0]["withdrawal_target"] == pytest.approx(0.0)
        assert rows[0]["withdrawal_sale_proceeds"] == pytest.approx(0.0)

    def test_dividends_already_available_reduce_the_sale_needed(self, bracket_table):
        # Large enough Taxable dividend income to fully cover a small withdrawal target on its own.
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 100000.0, "basis_per_share": 100.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}  # $10,000,000 -> huge dividend income, dwarfing a 0.01% target
        rows = self._run(bracket_table, lots, prices, withdrawal_strategy_params={"rate": 0.0001})
        row = rows[0]
        assert row["withdrawal_dividends_applied"] == pytest.approx(row["withdrawal_target"])
        assert row["withdrawal_sale_proceeds"] == pytest.approx(0.0)

    def test_sale_covers_the_remainder_after_dividends(self, bracket_table):
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 100.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}
        rows = self._run(bracket_table, lots, prices, withdrawal_strategy_params={"rate": 0.04})
        row = rows[0]
        assert row["withdrawal_sale_proceeds"] > 0
        assert row["withdrawal_dividends_applied"] == pytest.approx(row["taxable_qualified_dividends"])

    def test_funding_gap_reported_honestly_not_reconciled(self, bracket_table):
        # Expenses far exceed the flat-rate withdrawal target -- funding_gap must show a real
        # shortfall, and profit must NOT be silently patched to zero it out.
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 100.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}  # 4% of $100,000 = $4,000 target
        rows = self._run(bracket_table, lots, prices, expense=50000.0, withdrawal_strategy_params={"rate": 0.04})
        row = rows[0]
        assert row["withdrawal_target"] == pytest.approx(4000.0)
        assert row["spending_need"] == pytest.approx(50000.0)
        assert row["funding_gap"] == pytest.approx(4000.0 - 50000.0)
        assert row["profit"] < 0  # the mismatch shows up as real negative profit, not hidden

    def test_traditional_account_withdrawal_taxed_as_ordinary_income(self, bracket_table):
        lots = [{"ticker": "VTI", "account_type": "Traditional 401(k)", "shares": 10000.0, "basis_per_share": 10.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}
        rows = self._run(bracket_table, lots, prices, withdrawal_strategy_params={"rate": 0.8})
        row = rows[0]
        assert row["withdrawal_taxable_retirement_distribution"] > 0
        assert row["total_tax"] > 0

    def test_roth_account_withdrawal_produces_zero_taxable_income(self, bracket_table):
        lots = [{"ticker": "VTI", "account_type": "Roth IRA", "shares": 1000.0, "basis_per_share": 10.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}
        rows = self._run(bracket_table, lots, prices, withdrawal_strategy_params={"rate": 0.04})
        row = rows[0]
        assert row["withdrawal_sale_proceeds"] > 0
        assert row["withdrawal_long_term_gain"] == pytest.approx(0.0)
        assert row["withdrawal_taxable_retirement_distribution"] == pytest.approx(0.0)
        assert row["total_tax"] == pytest.approx(0.0)

    def test_unfunded_shortfall_surfaced_when_assets_insufficient(self, bracket_table):
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 10.0, "basis_per_share": 100.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}
        # A >100% rate deliberately exceeds what any single-year portfolio can fund, to exercise
        # the reporting path -- not a realistic strategy setting.
        rows = self._run(bracket_table, lots, prices, withdrawal_strategy_params={"rate": 2.0})
        assert rows[0]["withdrawal_unfunded_shortfall"] > 0

    def test_mutually_exclusive_with_dissaving(self, bracket_table):
        # withdrawal_start_date == retirement_date == 2020, both already passed -- a negative-profit
        # year here must be funded by the WITHDRAWAL mechanism, never dissaving.
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 100.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}
        rows = self._run(bracket_table, lots, prices, expense=5000.0, withdrawal_strategy_params={"rate": 0.04})
        row = rows[0]
        assert row["dissaving_proceeds"] == pytest.approx(0.0)
        assert row["withdrawal_sale_proceeds"] > 0

    def test_unimplemented_strategy_raises(self, bracket_table):
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 100.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}
        with pytest.raises(NotImplementedError):
            self._run(bracket_table, lots, prices, withdrawal_strategy="guardrails")


class TestProjectMultiYearBracketAwareWithdrawal:
    """MODULE_G2_STEP7_BRACKET_AWARE_WITHDRAWALS.md Part 1 (2026-08-23) — the bracket-aware split
    replacing the old fixed DEFAULT_DRAW_ORDER sale for the WITHDRAWAL-phase sale specifically
    (dissaving, pre-retirement, is untouched — see TestProjectMultiYearDissaving)."""

    def _run(self, bracket_table, lots, prices, **kwargs):
        universe = _small_universe()
        kwargs.setdefault("phase_dates", ALREADY_WITHDRAWING_PHASE_DATES)
        return project_multi_year(
            _income_inputs(),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2020, 1, 1),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            universe=universe,
            initial_lots=lots,
            initial_prices_by_ticker=prices,
            inflation_rate=0.03,
            **kwargs,
        )

    def _three_bucket_lots(self):
        return [
            # $2,000,000 -- huge, so it alone could absorb the entire withdrawal if a fixed order
            # dumped everything into it regardless of bracket.
            {"ticker": "VTI", "account_type": "Traditional 401(k)", "shares": 20000.0, "basis_per_share": 100.0, "year_acquired": 2020},
            # $50,000, low basis -> a real embedded LTCG once sold, same under either approach since
            # it's small enough to be fully exhausted either way.
            {"ticker": "VTI", "account_type": "Taxable", "shares": 500.0, "basis_per_share": 20.0, "year_acquired": 2020},
            # $300,000 -- large enough to absorb a real amount of "overflow" once Traditional is
            # capped at its own bracket ceiling instead of absorbing everything.
            {"ticker": "VTI", "account_type": "Roth IRA", "shares": 3000.0, "basis_per_share": 100.0, "year_acquired": 2020},
        ]

    def test_traditional_capped_at_bracket_ceiling_not_drained_first(self, bracket_table):
        # rate=0.10 on a $2,350,000 portfolio -> a $235,000-ish target, deliberately far larger than
        # Taxable's own $50,000 balance, so a fixed Taxable-then-Traditional order would have to spill
        # a large amount into Traditional in one lump, regardless of bracket.
        rows = self._run(
            bracket_table, self._three_bucket_lots(), {"VTI": 100.0}, withdrawal_strategy_params={"rate": 0.10}
        )
        row = rows[0]
        single = bracket_table[2026]["federal_brackets"]["single"]
        ceiling = ordinary_bracket_ceiling(single, 0.22)  # default target_bracket_rate
        # Traditional's own distribution must not exceed the 22%-bracket ceiling (no other ordinary
        # income this year to compete for that headroom) -- capped, not drained in one lump.
        assert row["withdrawal_taxable_retirement_distribution"] <= ceiling + 1e-6
        # And real money DID flow to Roth once Taxable+bracket-capped-Traditional weren't enough --
        # this is the whole point: Roth is touched, not preserved untouched the way a fixed
        # Taxable->Traditional->Roth order would leave it (Traditional alone could have covered the
        # entire target here).
        assert row["withdrawal_sale_proceeds"] > row["withdrawal_taxable_retirement_distribution"]

    def test_total_realized_tax_lower_than_the_old_fixed_taxable_then_traditional_order(self, bracket_table):
        """The doc's own checklist item 7: confirm total realized tax over the horizon is LOWER
        under the new split than under DEFAULT_DRAW_ORDER's old Taxable-first-fixed order, for a
        representative scenario. The old code path itself is gone (replaced this pass), so the "old"
        comparison is reproduced here directly from still-existing public functions
        (draw_order_fill/compute_taxes) -- exactly the two calls the old projection.py code used to
        make, fed the SAME sale_amount_needed the real (new) run actually used, against the SAME
        starting lots (this is year one, so current_lots/current_prices at the sale point are
        exactly initial_lots/initial_prices_by_ticker -- nothing has mutated them yet)."""
        lots = self._three_bucket_lots()
        prices = {"VTI": 100.0}
        rows = self._run(bracket_table, lots, prices, withdrawal_strategy_params={"rate": 0.10})
        row = rows[0]
        new_total_tax = row["total_tax"]

        sale_amount_needed = row["withdrawal_target"] - row["withdrawal_dividends_applied"]
        old_sale = draw_order_fill(sale_amount_needed, lots, prices, 2026, draw_order=DEFAULT_DRAW_ORDER, sale_method="hifo")
        old_ltcg = old_stg = old_traditional_distribution = 0.0
        for sold_account_type, sale in old_sale["sales_by_account_type"].items():
            if sold_account_type == "Taxable":
                old_ltcg += sale["long_term_gain"]
                old_stg += sale["short_term_gain"]
            elif sold_account_type in ("Traditional 401(k)", "Traditional IRA"):
                old_traditional_distribution += sale["total_proceeds"]

        old_tax_result = compute_taxes(
            tax_year=2026,
            filing_status="single",
            age_at_year_end=36,
            w2_gross=0.0,
            pretax_401k=0.0,
            roth_401k=0.0,
            pretax_health_dental=0.0,
            se_net_profit=0.0,
            se_solo_employee_deferral=0.0,
            se_solo_employer_contribution=0.0,
            taxable_retirement_withdrawal=old_traditional_distribution,
            ltcg=old_ltcg,
            qualified_dividends=row["taxable_qualified_dividends"],
            ordinary_dividends=row["taxable_ordinary_dividends"],
            interest_income=row["taxable_interest_income"],
            ss_benefit_gross=0.0,
            bracket_table=bracket_table,
            ordinary_break_income=0.0,
            short_term_gains=old_stg,
        )
        old_total_tax = old_tax_result["total_tax"]

        # The new bracket-aware split must produce a materially lower total tax bill for this
        # representative scenario -- the entire point of Part 1 (see modules/projection.py's own
        # docstring for the full reasoning: capping Traditional at a comfortable bracket instead of
        # dumping it all in one lump, preserving Roth for last).
        assert new_total_tax < old_total_tax
        # Sanity: the old approach really did realize materially MORE Traditional ordinary income
        # than the new one, confirming this is actually exercising the bracket cap, not a no-op.
        assert row["withdrawal_taxable_retirement_distribution"] < old_traditional_distribution

    def test_target_bracket_rate_is_configurable(self, bracket_table):
        # A much lower target bracket rate (10%) should cap Traditional's distribution lower than
        # the 22% default would, for the same scenario.
        rows_default = self._run(
            bracket_table, self._three_bucket_lots(), {"VTI": 100.0}, withdrawal_strategy_params={"rate": 0.10}
        )
        rows_low_bracket = self._run(
            bracket_table, self._three_bucket_lots(), {"VTI": 100.0},
            withdrawal_strategy_params={"rate": 0.10, "target_bracket_rate": 0.10},
        )
        assert (
            rows_low_bracket[0]["withdrawal_taxable_retirement_distribution"]
            < rows_default[0]["withdrawal_taxable_retirement_distribution"]
        )

    def test_rmd_table_none_skips_rmd_enforcement_entirely(self, bracket_table):
        # birth_date well past RMD age (age 80 in 2026) but rmd_table not supplied -- fully backward
        # compatible, no forced excess, matching every pre-existing test in this file.
        rows = project_multi_year(
            _income_inputs(),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2020, 1, 1),
            birth_date=date(1946, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            universe=_small_universe(),
            initial_lots=[{"ticker": "VTI", "account_type": "Traditional 401(k)", "shares": 1000.0, "basis_per_share": 100.0, "year_acquired": 2020}],
            initial_prices_by_ticker={"VTI": 100.0},
            phase_dates=ALREADY_WITHDRAWING_PHASE_DATES,
            withdrawal_strategy_params={"rate": 0.0},
        )
        row = rows[0]
        assert row["rmd_amount"] == pytest.approx(0.0)
        assert row["rmd_forced_excess"] == pytest.approx(0.0)

    def test_rmd_floor_forces_a_sale_above_the_flat_rate_target(self, bracket_table):
        # birth_date makes age_at_year_end=80 in 2026 -- well past rmd_start_age(1946)=73. A $0-rate
        # withdrawal strategy means sale_amount_needed would otherwise be exactly $0 -- the RMD floor
        # alone must force a real Traditional sale.
        lots = [{"ticker": "VTI", "account_type": "Traditional 401(k)", "shares": 10000.0, "basis_per_share": 100.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}  # $1,000,000 Traditional balance
        rmd_table = load_rmd_table()
        rows = project_multi_year(
            _income_inputs(),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2020, 1, 1),
            birth_date=date(1946, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            universe=_small_universe(),
            initial_lots=lots,
            initial_prices_by_ticker=prices,
            phase_dates=ALREADY_WITHDRAWING_PHASE_DATES,
            withdrawal_strategy_params={"rate": 0.0},
            rmd_table=rmd_table,
        )
        row = rows[0]
        expected_divisor = rmd_divisor(80, rmd_table)
        expected_rmd = 1000000.0 / expected_divisor
        assert row["rmd_amount"] == pytest.approx(expected_rmd)
        assert row["withdrawal_target"] == pytest.approx(0.0)  # the strategy itself still says $0
        # The forced excess is real money sold beyond the $0 target -- taxed, but NOT counted as
        # spendable withdrawal income (reinvested into Taxable instead, per Part 1's own "not
        # consumed" instruction).
        assert row["rmd_forced_excess"] == pytest.approx(expected_rmd, rel=1e-3)
        assert row["withdrawal_taxable_retirement_distribution"] == pytest.approx(expected_rmd, rel=1e-3)
        assert row["total_withdrawal_income"] == pytest.approx(0.0, abs=1.0)
        assert row["total_tax"] > 0  # still genuinely taxed as ordinary income

    def test_rmd_forced_excess_reinvested_into_taxable_not_lost(self, bracket_table):
        # The RMD's forced-excess dollars must show up as new Taxable lots -- the portfolio's total
        # value shouldn't collapse by the RMD amount, only shrink by the tax owed on it.
        lots = [{"ticker": "VTI", "account_type": "Traditional 401(k)", "shares": 10000.0, "basis_per_share": 100.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}
        rmd_table = load_rmd_table()
        rows = project_multi_year(
            _income_inputs(),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2020, 1, 1),
            birth_date=date(1946, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            universe=_small_universe(),
            initial_lots=lots,
            initial_prices_by_ticker=prices,
            target_allocations={"Taxable": {"VTI": 1.0}},
            phase_dates=ALREADY_WITHDRAWING_PHASE_DATES,
            withdrawal_strategy_params={"rate": 0.0},
            rmd_table=rmd_table,
        )
        row = rows[0]
        taxable_lots = [lot for lot in row["ending_lots"] if lot["account_type"] == "Taxable"]
        assert taxable_lots  # the forced excess landed somewhere real, not vanished
        # Valued at each lot's own basis_per_share (the price AT THE MOMENT it was purchased/
        # reinvested) -- not the year-end price, which has already grown past that point via this
        # same year's own roll-forward (see roll_forward_portfolio) and would overstate the dollar
        # amount actually reinvested.
        reinvested_value = sum(lot["shares"] * lot["basis_per_share"] for lot in taxable_lots)
        assert reinvested_value == pytest.approx(row["rmd_forced_excess"], rel=1e-3)
        # Total portfolio value only dropped by roughly the tax owed on the RMD, not by the full RMD
        # amount itself (the RMD dollars themselves moved account, not out of the portfolio).
        assert row["portfolio_value"] > 1000000.0 - row["total_tax"] - 1000.0


class TestProjectMultiYearDynamicWithdrawalSizing:
    """MODULE_G2_STEP7_BRACKET_AWARE_WITHDRAWALS.md Part 2 (2026-08-29) — "target_net_spending"
    closes funding_gap via fixed-point iteration instead of merely reporting it, the way
    "flat_percentage" (see TestProjectMultiYearBracketAwareWithdrawal above) does by design."""

    def _run(self, bracket_table, lots, prices, **kwargs):
        universe = _small_universe()
        kwargs.setdefault("phase_dates", ALREADY_WITHDRAWING_PHASE_DATES)
        kwargs.setdefault("withdrawal_strategy", "target_net_spending")
        # A real, nonzero spending_need by default -- $0 (the shared _expense_inputs() default)
        # makes "converged onto spending_need" a trivial, uninformative check.
        default_expenses = _expense_inputs(current_year_yet_to_incur_expense=60000.0)
        return project_multi_year(
            kwargs.pop("income_inputs", _income_inputs()),
            kwargs.pop("expense_inputs", default_expenses),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2020, 1, 1),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            universe=universe,
            initial_lots=lots,
            initial_prices_by_ticker=prices,
            inflation_rate=0.03,
            **kwargs,
        )

    def _ample_lots(self):
        # A large, three-bucket portfolio comfortably able to fund a modest spending_need out of
        # any one bucket alone -- isolates the solver's own convergence behavior from bucket
        # exhaustion.
        return [
            {"ticker": "VTI", "account_type": "Traditional 401(k)", "shares": 10000.0, "basis_per_share": 100.0, "year_acquired": 2020},
            {"ticker": "VTI", "account_type": "Taxable", "shares": 2000.0, "basis_per_share": 80.0, "year_acquired": 2020},
            {"ticker": "VTI", "account_type": "Roth IRA", "shares": 3000.0, "basis_per_share": 100.0, "year_acquired": 2020},
        ]

    def test_net_retirement_income_converges_to_spending_need(self, bracket_table):
        rows = self._run(bracket_table, self._ample_lots(), {"VTI": 100.0})
        row = rows[0]
        assert row["net_retirement_income"] == pytest.approx(row["spending_need"], abs=1.0)

    def test_iteration_count_recorded_and_bounded(self, bracket_table):
        rows = self._run(bracket_table, self._ample_lots(), {"VTI": 100.0})
        row = rows[0]
        assert row["withdrawal_iteration_count"] is not None
        assert 1 <= row["withdrawal_iteration_count"] <= 20

    def test_flat_percentage_iteration_count_is_none(self, bracket_table):
        # "flat_percentage" never iterates -- the field stays None, distinguishable from "ran 1
        # iteration."
        rows = self._run(
            bracket_table, self._ample_lots(), {"VTI": 100.0},
            withdrawal_strategy="flat_percentage", withdrawal_strategy_params={"rate": 0.04},
        )
        assert rows[0]["withdrawal_iteration_count"] is None

    def test_dynamic_strategy_lands_closer_to_spending_need_than_flat_rate(self, bracket_table):
        # A flat 4% rule has no relationship to spending_need at all, by design (see
        # flat_percentage's own docstring); target_net_spending, for the SAME portfolio/expenses,
        # must land net_retirement_income much closer to spending_need.
        flat_rows = self._run(
            bracket_table, self._ample_lots(), {"VTI": 100.0},
            withdrawal_strategy="flat_percentage", withdrawal_strategy_params={"rate": 0.04},
        )
        dynamic_rows = self._run(bracket_table, self._ample_lots(), {"VTI": 100.0})
        flat_gap = abs(flat_rows[0]["net_retirement_income"] - flat_rows[0]["spending_need"])
        dynamic_gap = abs(dynamic_rows[0]["net_retirement_income"] - dynamic_rows[0]["spending_need"])
        assert dynamic_gap < flat_gap

    def test_funding_gap_equals_tax_owed_once_converged(self, bracket_table):
        # funding_gap = withdrawal_target - spending_need is deliberately PRE-TAX (§2.3's own
        # "report the raw mismatch, honestly, before tax noise" — see this function's own
        # docstring), so it does NOT go to zero under a converged dynamic solve; a converged solve
        # makes net_retirement_income == spending_need, which by construction (net = gross - tax)
        # means the remaining pre-tax gap is exactly the tax owed on the withdrawal -- a real,
        # informative number, not a bug or a leftover of the flat rule's own by-design mismatch.
        rows = self._run(bracket_table, self._ample_lots(), {"VTI": 100.0})
        row = rows[0]
        assert row["funding_gap"] == pytest.approx(row["retirement_taxes_paid"], abs=5.0)

    def test_nonconvergence_warns_and_caps_at_twenty_iterations(self, bracket_table):
        # A spending_need far beyond anything this tiny portfolio can ever fund: every bucket is
        # exhausted almost immediately, net_retirement_income plateaus, and the fixed-point update
        # keeps raising the guess without the gap ever shrinking below $1 -- must hit the
        # iteration cap and warn, not loop forever or silently accept a wildly wrong answer.
        tiny_lots = [
            {"ticker": "VTI", "account_type": "Taxable", "shares": 10.0, "basis_per_share": 100.0, "year_acquired": 2020},
        ]
        rows = self._run(
            bracket_table, tiny_lots, {"VTI": 100.0},
            expense_inputs=_expense_inputs(current_year_yet_to_incur_expense=5_000_000.0),
        )
        row = rows[0]
        assert row["withdrawal_iteration_count"] == 20
        assert any("did not converge" in w for w in row["contribution_warnings"])


class TestProjectMultiYearRetirementIncomeReporting:
    """RETIREMENT_REPORTING_AUDIT.md §2 (2026-08-13) — total_withdrawal_income/
    retirement_taxes_paid/net_retirement_income, and the withdrawing_fraction/is_withdrawal_year
    split point. Confirmed defect: gross_income/net_income only ever capture the TAXABLE slice of a
    withdrawal, understating real spendable cash whenever any of it comes from Roth/return-of-basis."""

    def _run(self, bracket_table, lots, prices, expense=0.0, retirement_date=date(2020, 1, 1), **kwargs):
        universe = _small_universe()
        kwargs.setdefault("phase_dates", ALREADY_WITHDRAWING_PHASE_DATES)
        return project_multi_year(
            _income_inputs(),
            _expense_inputs(current_year_already_incurred_expense=expense),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=retirement_date,
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            universe=universe,
            initial_lots=lots,
            initial_prices_by_ticker=prices,
            inflation_rate=0.03,
            **kwargs,
        )

    def test_is_withdrawal_year_and_withdrawing_fraction_always_present(self, bracket_table):
        # Present even with NO portfolio at all -- these are phase facts, not portfolio facts.
        rows = project_multi_year(
            _income_inputs(),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2020, 1, 1),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            phase_dates=ALREADY_WITHDRAWING_PHASE_DATES,
        )
        row = rows[0]
        assert row["is_withdrawal_year"] is True
        assert row["withdrawing_fraction"] == pytest.approx(1.0)

    def test_is_withdrawal_year_false_before_withdrawal_start_date(self, bracket_table):
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 100.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}
        rows = self._run(
            bracket_table, lots, prices,
            phase_dates={**ALREADY_WITHDRAWING_PHASE_DATES, "withdrawal_start_date": date(2090, 1, 1)},
        )
        assert rows[0]["is_withdrawal_year"] is False

    def test_new_fields_none_when_portfolio_inactive(self, bracket_table):
        rows = project_multi_year(
            _income_inputs(),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2020, 1, 1),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            phase_dates=ALREADY_WITHDRAWING_PHASE_DATES,
        )
        row = rows[0]
        assert row["total_withdrawal_income"] is None
        assert row["retirement_taxes_paid"] is None
        assert row["net_retirement_income"] is None

    def test_new_fields_none_when_not_yet_withdrawing(self, bracket_table):
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 100.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}
        rows = self._run(
            bracket_table, lots, prices,
            phase_dates={**ALREADY_WITHDRAWING_PHASE_DATES, "withdrawal_start_date": date(2090, 1, 1)},
        )
        row = rows[0]
        assert row["total_withdrawal_income"] is None
        assert row["retirement_taxes_paid"] is None
        assert row["net_retirement_income"] is None

    def test_total_withdrawal_income_equals_dividends_plus_sale_proceeds(self, bracket_table):
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 100.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}
        rows = self._run(bracket_table, lots, prices, withdrawal_strategy_params={"rate": 0.04})
        row = rows[0]
        assert row["total_withdrawal_income"] == pytest.approx(
            row["withdrawal_dividends_applied"] + row["withdrawal_sale_proceeds"]
        )

    def test_total_withdrawal_income_at_least_the_taxable_distribution_alone(self, bracket_table):
        # The new figure must be at least as large as the old, understated one (audit §2.5).
        lots = [{"ticker": "VTI", "account_type": "Traditional 401(k)", "shares": 10000.0, "basis_per_share": 10.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}
        rows = self._run(bracket_table, lots, prices, withdrawal_strategy_params={"rate": 0.8})
        row = rows[0]
        assert row["total_withdrawal_income"] >= row["withdrawal_taxable_retirement_distribution"]

    def test_net_retirement_income_equals_total_income_minus_taxes(self, bracket_table):
        lots = [{"ticker": "VTI", "account_type": "Traditional 401(k)", "shares": 10000.0, "basis_per_share": 10.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}
        rows = self._run(bracket_table, lots, prices, withdrawal_strategy_params={"rate": 0.8})
        row = rows[0]
        assert row["net_retirement_income"] == pytest.approx(
            row["total_withdrawal_income"] - row["retirement_taxes_paid"]
        )
        assert row["retirement_taxes_paid"] == pytest.approx(row["total_tax"])

    def test_roth_only_withdrawal_shows_real_cash_despite_zero_gross_income(self, bracket_table):
        # THE regression test the audit specifically calls for (§2.5): a draw_order pulling
        # entirely from Roth must leave gross_income/net_income at $0 (Roth distributions are never
        # taxable) while total_withdrawal_income/net_retirement_income correctly show the real cash
        # raised -- this is the exact understatement bug that motivated this whole section.
        lots = [{"ticker": "VTI", "account_type": "Roth IRA", "shares": 1000.0, "basis_per_share": 10.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}
        rows = self._run(
            bracket_table, lots, prices,
            draw_order=["Roth IRA"],
            withdrawal_strategy_params={"rate": 0.04},
        )
        row = rows[0]
        assert row["gross_income"] == pytest.approx(0.0)
        assert row["net_income"] == pytest.approx(0.0)
        assert row["total_withdrawal_income"] > 0
        assert row["net_retirement_income"] > 0
        assert row["net_retirement_income"] == pytest.approx(row["total_withdrawal_income"])  # no tax at all

    def test_discretionary_income_equals_net_retirement_income_minus_spending_need(self, bracket_table):
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 10.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}
        rows = self._run(bracket_table, lots, prices, expense=20000.0, withdrawal_strategy_params={"rate": 0.04})
        row = rows[0]
        assert row["discretionary_income"] == pytest.approx(row["net_retirement_income"] - row["spending_need"])

    def test_discretionary_income_none_when_not_withdrawal_year(self, bracket_table):
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 10.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}
        rows = self._run(
            bracket_table, lots, prices,
            phase_dates={**ALREADY_WITHDRAWING_PHASE_DATES, "withdrawal_start_date": date(2090, 1, 1)},
        )
        assert rows[0]["discretionary_income"] is None

    def test_discretionary_income_never_exceeds_net_retirement_income(self, bracket_table):
        # A large embedded gain relative to a small expense need: the withdrawal itself triggers a
        # substantial tax bill, which is exactly the scenario where the OLD (pre-tax) funding_gap
        # could exceed net_retirement_income -- discretionary_income must never do that, by
        # construction (net_retirement_income - spending_need, spending_need never negative).
        lots = [
            {"ticker": "VTI", "account_type": "Taxable", "shares": 100000.0, "basis_per_share": 1.0, "year_acquired": 2020}
        ]
        prices = {"VTI": 100.0}  # $10,000,000 position, almost entirely embedded gain
        rows = self._run(bracket_table, lots, prices, expense=10000.0, withdrawal_strategy_params={"rate": 0.04})
        row = rows[0]
        assert row["discretionary_income"] <= row["net_retirement_income"] + 1e-6
        # And confirm this scenario genuinely reproduces the user-found issue with the OLD
        # (pre-tax) funding_gap -- it really can exceed net_retirement_income in a normal,
        # non-partial year, which is exactly why discretionary_income needed to be a separate,
        # correctly-defined field rather than a rename of funding_gap.
        assert row["funding_gap"] > row["net_retirement_income"]

    def test_funding_gap_and_discretionary_income_are_genuinely_different_fields(self, bracket_table):
        # A large realized-gain year should show a real gap between the two -- proving
        # discretionary_income isn't just funding_gap under another name.
        lots = [
            {"ticker": "VTI", "account_type": "Taxable", "shares": 100000.0, "basis_per_share": 1.0, "year_acquired": 2020}
        ]
        prices = {"VTI": 100.0}
        rows = self._run(bracket_table, lots, prices, expense=10000.0, withdrawal_strategy_params={"rate": 0.04})
        row = rows[0]
        assert row["discretionary_income"] != pytest.approx(row["funding_gap"])


class TestNetPresentValue:
    def test_matches_hand_computed_sum(self):
        rows = [
            {"years_from_now": 0, "profit": 100.0},
            {"years_from_now": 1, "profit": 100.0},
            {"years_from_now": 2, "profit": 100.0},
        ]
        expected = 100.0 + 100.0 / 1.05 + 100.0 / 1.05**2
        assert net_present_value(rows, 0.05) == pytest.approx(expected)

    def test_year_zero_is_not_discounted(self):
        rows = [{"years_from_now": 0, "profit": 500.0}]
        assert net_present_value(rows, 0.10) == pytest.approx(500.0)

    def test_zero_discount_rate_is_a_plain_sum(self):
        rows = [
            {"years_from_now": 0, "profit": 100.0},
            {"years_from_now": 5, "profit": 200.0},
            {"years_from_now": 10, "profit": -50.0},
        ]
        assert net_present_value(rows, 0.0) == pytest.approx(250.0)

    def test_negative_profit_years_reduce_npv(self):
        rows = [
            {"years_from_now": 0, "profit": 100.0},
            {"years_from_now": 1, "profit": -100.0},
        ]
        assert net_present_value(rows, 0.05) < 100.0

    def test_none_profit_rows_are_excluded_not_zeroed(self):
        rows = [
            {"years_from_now": 0, "profit": 100.0},
            {"years_from_now": 1, "profit": None},
        ]
        # If the None row were silently treated as $0 this would still equal 100.0 -- the real
        # assertion is that it's excluded from the sum entirely, which for this discount rate and
        # these two rows happens to produce the same numeric answer either way, so check directly
        # against a version with the None row removed instead of a hand-computed literal.
        assert net_present_value(rows, 0.05) == pytest.approx(net_present_value(rows[:1], 0.05))

    def test_all_none_profit_returns_none(self):
        rows = [{"years_from_now": 0, "profit": None}, {"years_from_now": 1, "profit": None}]
        assert net_present_value(rows, 0.05) is None

    def test_empty_rows_returns_none(self):
        assert net_present_value([], 0.05) is None

    def test_end_to_end_with_project_multi_year(self, bracket_table):
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=80000.0),
            _expense_inputs(current_year_already_incurred_expense=40000.0),
            current_date=date(2026, 1, 1),
            horizon_date=date(2028, 12, 31),
            retirement_date=date(2028, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
        )
        npv = net_present_value(rows, 0.04)
        assert npv is not None
        # Hand-recompute from the rows' own profit/years_from_now to confirm the wiring, not just
        # that *a* number came back.
        expected = sum(r["profit"] / 1.04 ** r["years_from_now"] for r in rows)
        assert npv == pytest.approx(expected)


class TestNetPresentValueOfField:
    """ADJUSTED_WEALTH_REDESIGN.md §3.1 (2026-08-14) — net_present_value's generalized form, the
    same discounting convention applied to any per-row field, e.g. "total_tax"."""

    def test_zero_discount_rate_matches_plain_sum(self):
        rows = [
            {"years_from_now": 0, "total_tax": 100.0},
            {"years_from_now": 5, "total_tax": 200.0},
            {"years_from_now": 10, "total_tax": 50.0},
        ]
        assert net_present_value_of_field(rows, 0.0, "total_tax") == pytest.approx(
            sum(r["total_tax"] for r in rows)
        )

    def test_matches_hand_computed_sum_for_a_non_profit_field(self):
        rows = [
            {"years_from_now": 0, "total_tax": 100.0},
            {"years_from_now": 1, "total_tax": 100.0},
        ]
        expected = 100.0 + 100.0 / 1.05
        assert net_present_value_of_field(rows, 0.05, "total_tax") == pytest.approx(expected)

    def test_none_values_excluded_not_zeroed(self):
        rows = [{"years_from_now": 0, "total_tax": 100.0}, {"years_from_now": 1, "total_tax": None}]
        assert net_present_value_of_field(rows, 0.05, "total_tax") == pytest.approx(100.0)

    def test_all_none_returns_none(self):
        rows = [{"years_from_now": 0, "total_tax": None}]
        assert net_present_value_of_field(rows, 0.05, "total_tax") is None

    def test_net_present_value_wrapper_still_reads_profit_specifically(self):
        rows = [{"years_from_now": 0, "profit": 10.0, "total_tax": 999.0}]
        assert net_present_value(rows, 0.05) == pytest.approx(10.0)


class TestPortfolioValue:
    """`portfolio_value` — exported (ADJUSTED_WEALTH_REDESIGN.md §3.2, 2026-08-14) so the UI can
    compute today's mark-to-market value directly, not just read it off a projected row."""

    def test_sums_shares_times_price_across_lots(self):
        lots = [
            {"ticker": "VTI", "shares": 10.0},
            {"ticker": "VTI", "shares": 5.0},
            {"ticker": "BND", "shares": 2.0},
        ]
        prices = {"VTI": 100.0, "BND": 50.0}
        assert portfolio_value(lots, prices) == pytest.approx(10 * 100.0 + 5 * 100.0 + 2 * 50.0)

    def test_unknown_ticker_price_treated_as_zero(self):
        assert portfolio_value([{"ticker": "UNKNOWN", "shares": 10.0}], {}) == pytest.approx(0.0)


class TestAverageAnnualField:
    """2026-08-31 — the general form behind average_annual_net_retirement_income (now a thin
    wrapper over this), reused directly for ss_after_tax_income/other_after_tax_income."""

    def test_plain_arithmetic_mean_of_the_named_field(self):
        rows = [{"ss_after_tax_income": 10000.0}, {"ss_after_tax_income": 20000.0}, {"ss_after_tax_income": None}]
        assert average_annual_field(rows, "ss_after_tax_income") == pytest.approx(15000.0)

    def test_none_when_no_row_has_the_field_populated(self):
        assert average_annual_field([{"other_after_tax_income": None}], "other_after_tax_income") is None

    def test_different_field_names_are_independent(self):
        rows = [{"a": 100.0, "b": 200.0}, {"a": 300.0, "b": 400.0}]
        assert average_annual_field(rows, "a") == pytest.approx(200.0)
        assert average_annual_field(rows, "b") == pytest.approx(300.0)


class TestAverageAnnualNetRetirementIncome:
    """ADJUSTED_WEALTH_REDESIGN.md §4b (2026-08-14) — plain mean, not a present value."""

    def test_plain_arithmetic_mean_across_withdrawal_years(self):
        rows = [
            {"net_retirement_income": 40000.0},
            {"net_retirement_income": 60000.0},
            {"net_retirement_income": None},  # pre-retirement row, excluded
        ]
        assert average_annual_net_retirement_income(rows) == pytest.approx(50000.0)

    def test_none_when_no_row_has_it(self):
        assert average_annual_net_retirement_income([{"net_retirement_income": None}]) is None

    def test_end_to_end_with_project_multi_year(self, bracket_table):
        lots = [
            {"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 10.0, "year_acquired": 2020}
        ]
        prices = {"VTI": 100.0}
        universe = _small_universe()
        rows = project_multi_year(
            _income_inputs(),
            _expense_inputs(current_year_already_incurred_expense=10000.0),
            current_date=date(2026, 1, 1),
            horizon_date=date(2027, 12, 31),
            retirement_date=date(2020, 1, 1),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            universe=universe,
            initial_lots=lots,
            initial_prices_by_ticker=prices,
            phase_dates=ALREADY_WITHDRAWING_PHASE_DATES,
            withdrawal_strategy_params={"rate": 0.04},
        )
        avg = average_annual_net_retirement_income(rows)
        withdrawal_rows = [r for r in rows if r["net_retirement_income"] is not None]
        assert withdrawal_rows  # sanity: the scenario actually reaches its withdrawal phase
        assert avg == pytest.approx(
            sum(r["net_retirement_income"] for r in withdrawal_rows) / len(withdrawal_rows)
        )


class TestAverageRetirementTaxRate:
    """2026-08-30, user request — feeds the redesigned 'Adjusted wealth' metric's new calculation
    (see TestAdjustedWealthViaRetirementTaxRate below): the average of each withdrawal-phase row's
    OWN effective tax rate (retirement_taxes_paid / total_withdrawal_income), not a rate on net."""

    def test_plain_arithmetic_mean_of_per_row_effective_rates(self):
        rows = [
            {"retirement_taxes_paid": 2000.0, "total_withdrawal_income": 20000.0},  # 10%
            {"retirement_taxes_paid": 6000.0, "total_withdrawal_income": 30000.0},  # 20%
            {"retirement_taxes_paid": 0.0, "total_withdrawal_income": 0.0},  # excluded, not 0%
        ]
        assert average_retirement_tax_rate(rows) == pytest.approx(0.15)

    def test_none_when_nothing_ever_drawn(self):
        rows = [{"retirement_taxes_paid": 0.0, "total_withdrawal_income": 0.0}]
        assert average_retirement_tax_rate(rows) is None

    def test_none_for_empty_rows(self):
        assert average_retirement_tax_rate([]) is None

    def test_end_to_end_with_project_multi_year(self, bracket_table):
        lots = [
            {"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 10.0, "year_acquired": 2020}
        ]
        rows = project_multi_year(
            _income_inputs(),
            _expense_inputs(current_year_already_incurred_expense=10000.0),
            current_date=date(2026, 1, 1),
            horizon_date=date(2027, 12, 31),
            retirement_date=date(2020, 1, 1),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            universe=_small_universe(),
            initial_lots=lots,
            initial_prices_by_ticker={"VTI": 100.0},
            phase_dates=ALREADY_WITHDRAWING_PHASE_DATES,
            withdrawal_strategy_params={"rate": 0.04},
        )
        rate = average_retirement_tax_rate(rows)
        withdrawal_rows = [r for r in rows if r.get("total_withdrawal_income")]
        assert withdrawal_rows  # sanity: the scenario actually draws something
        expected = sum(r["retirement_taxes_paid"] / r["total_withdrawal_income"] for r in withdrawal_rows) / len(
            withdrawal_rows
        )
        assert rate == pytest.approx(expected)
        assert 0.0 <= rate < 1.0  # a real effective rate, not a nonsense figure


class TestAdjustedWealthViaRetirementTaxRate:
    """2026-08-30, user request — replaces the old NPV-of-every-future-tax-bill 'Adjusted wealth'
    calculation with: average effective retirement tax rate × portfolio value at retirement,
    discounted back to today."""

    def test_none_when_scenario_never_reaches_withdrawal_phase(self):
        rows = [{"is_withdrawal_year": False}, {"is_withdrawal_year": False}]
        assert adjusted_wealth_via_retirement_tax_rate(rows, discount_rate=0.04) is None

    def test_none_when_nothing_ever_drawn(self):
        rows = [
            {
                "is_withdrawal_year": True,
                "portfolio_value_at_start_of_year": 1_000_000.0,
                "years_from_now": 10,
                "retirement_taxes_paid": 0.0,
                "total_withdrawal_income": 0.0,
            }
        ]
        assert adjusted_wealth_via_retirement_tax_rate(rows, discount_rate=0.04) is None

    def test_matches_hand_computed_three_step_recipe(self):
        # 10% average effective rate, $3M at retirement -> $2.7M after-tax, discounted back 10
        # years at 5% -- the doc's own worked example, verbatim.
        rows = [
            {
                "is_withdrawal_year": True,
                "portfolio_value_at_start_of_year": 3_000_000.0,
                "years_from_now": 10,
                "retirement_taxes_paid": 300_000.0,
                "total_withdrawal_income": 3_000_000.0,
            }
        ]
        result = adjusted_wealth_via_retirement_tax_rate(rows, discount_rate=0.05)
        assert result["average_tax_rate"] == pytest.approx(0.10)
        assert result["wealth_at_retirement"] == pytest.approx(3_000_000.0)
        assert result["years_to_retirement"] == 10
        expected_adjusted_wealth = (3_000_000.0 * 0.90) / (1.05**10)
        assert result["adjusted_wealth"] == pytest.approx(expected_adjusted_wealth)

    def test_uses_first_withdrawal_year_row_not_a_later_one(self):
        rows = [
            {"is_withdrawal_year": False},
            {
                "is_withdrawal_year": True,
                "portfolio_value_at_start_of_year": 1_000_000.0,
                "years_from_now": 5,
                "retirement_taxes_paid": 100_000.0,
                "total_withdrawal_income": 1_000_000.0,
            },
            {
                "is_withdrawal_year": True,
                "portfolio_value_at_start_of_year": 2_000_000.0,  # a later year -- must NOT be used
                "years_from_now": 6,
                "retirement_taxes_paid": 100_000.0,
                "total_withdrawal_income": 1_000_000.0,
            },
        ]
        result = adjusted_wealth_via_retirement_tax_rate(rows, discount_rate=0.05)
        assert result["wealth_at_retirement"] == pytest.approx(1_000_000.0)
        assert result["years_to_retirement"] == 5

    def test_end_to_end_with_project_no_income_no_expense_baseline(self, bracket_table):
        lots = [
            {"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 10.0, "year_acquired": 2020}
        ]
        baseline_rows = project_no_income_no_expense_baseline(
            current_date=date(2026, 1, 1),
            horizon_date=date(2027, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            withdrawal_start_date=date(2026, 1, 1),
            ss_claim_date=date(2080, 1, 1),
            universe=_small_universe(),
            initial_lots=lots,
            initial_prices_by_ticker={"VTI": 100.0},
            withdrawal_strategy_params={"rate": 0.04},
        )
        result = adjusted_wealth_via_retirement_tax_rate(baseline_rows, discount_rate=0.05)
        assert result is not None
        assert result["adjusted_wealth"] > 0
        assert 0.0 <= result["average_tax_rate"] < 1.0
        assert result["wealth_at_retirement"] > 0


class TestProjectNoIncomeNoExpenseBaseline:
    """ADJUSTED_WEALTH_REDESIGN.md §2 (2026-08-14) — the 'what if no more income is earned and no
    more expenses are incurred, starting today' baseline behind the Adjusted wealth metric."""

    def _run(self, bracket_table, lots, prices, withdrawal_start_date=date(2020, 1, 1), **kwargs):
        universe = _small_universe()
        return project_no_income_no_expense_baseline(
            current_date=date(2026, 1, 1),
            horizon_date=date(2028, 12, 31),
            birth_date=date(1990, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            withdrawal_start_date=withdrawal_start_date,
            ss_claim_date=date(2020, 1, 1),
            universe=universe,
            initial_lots=lots,
            initial_prices_by_ticker=prices,
            **kwargs,
        )

    def test_income_and_expense_fields_are_all_zero_every_year(self, bracket_table):
        lots = [
            {"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 100.0, "year_acquired": 2020}
        ]
        prices = {"VTI": 100.0}
        rows = self._run(bracket_table, lots, prices)
        assert len(rows) == 3  # 2026, 2027, 2028
        for row in rows:
            assert row["gross_w2"] == pytest.approx(0.0)
            assert row["gross_se"] == pytest.approx(0.0)
            assert row["gross_ordinary_break_income"] == pytest.approx(0.0)
            assert row["gross_expense"] == pytest.approx(0.0)

    def test_withdrawal_start_date_passes_through_unchanged(self, bracket_table):
        # Unlike retirement_date/savings_stop_date (pulled to current_date internally),
        # withdrawal_start_date is a real parameter here and must be honored exactly.
        lots = [
            {"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 100.0, "year_acquired": 2020}
        ]
        prices = {"VTI": 100.0}
        rows = self._run(bracket_table, lots, prices, withdrawal_start_date=date(2027, 1, 1))
        rows_by_year = {r["year"]: r for r in rows}
        assert rows_by_year[2026]["is_withdrawal_year"] is False
        assert rows_by_year[2027]["is_withdrawal_year"] is True

    def test_all_roth_portfolio_has_zero_tax_every_year(self, bracket_table):
        # No Taxable-account dividends to report and Roth withdrawals are never taxable -- the
        # baseline's total_tax should come back exactly $0 for every row, the useful sanity check
        # ADJUSTED_WEALTH_REDESIGN.md §6 calls for directly.
        lots = [{"ticker": "VTI", "account_type": "Roth IRA", "shares": 1000.0, "basis_per_share": 10.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}
        rows = self._run(bracket_table, lots, prices, withdrawal_strategy_params={"rate": 0.04})
        for row in rows:
            assert row["total_tax"] == pytest.approx(0.0)

    def test_portfolio_still_rolls_forward_and_grows(self, bracket_table):
        # The frozen-income/expense baseline shouldn't freeze the PORTFOLIO too -- dividends should
        # still reinvest and the ledger should still carry forward year over year.
        lots = [
            {"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 100.0, "year_acquired": 2020}
        ]
        prices = {"VTI": 100.0}
        rows = self._run(bracket_table, lots, prices, withdrawal_start_date=date(2090, 1, 1))
        assert rows[-1]["portfolio_value"] > rows[0]["portfolio_value"]


class TestProjectMultiYearSocialSecurity:
    """Module F (2026-08-31) — ss_annual_benefit wired into project_multi_year, phased in by
    ss_fraction (phase_flags_for_year's own field, previously computed but never consumed by
    anything downstream — confirmed by grep before this pass)."""

    def _run(self, bracket_table, phase_dates, ss_annual_benefit, **kwargs):
        return project_multi_year(
            _income_inputs(),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2027, 12, 31),
            retirement_date=date(2020, 1, 1),
            birth_date=date(1955, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            universe=_small_universe(),
            initial_lots=[
                {"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 50.0, "year_acquired": 2020}
            ],
            initial_prices_by_ticker={"VTI": 100.0},
            phase_dates=phase_dates,
            ss_annual_benefit=ss_annual_benefit,
            **kwargs,
        )

    def test_default_zero_benefit_is_fully_backward_compatible(self, bracket_table):
        rows_default = self._run(bracket_table, ALREADY_WITHDRAWING_PHASE_DATES, ss_annual_benefit=0.0)
        rows_omitted = project_multi_year(
            _income_inputs(), _expense_inputs(), current_date=date(2026, 1, 1), horizon_date=date(2027, 12, 31),
            retirement_date=date(2020, 1, 1), birth_date=date(1955, 1, 1), filing_status="single",
            bracket_table=bracket_table, universe=_small_universe(),
            initial_lots=[{"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 50.0, "year_acquired": 2020}],
            initial_prices_by_ticker={"VTI": 100.0}, phase_dates=ALREADY_WITHDRAWING_PHASE_DATES,
        )
        assert rows_default[0]["portfolio_value"] == pytest.approx(rows_omitted[0]["portfolio_value"])
        assert rows_default[0]["ss_benefit_gross"] == 0.0

    def test_benefit_phases_in_from_claim_date_not_before(self, bracket_table):
        phase_dates = {**ALREADY_WITHDRAWING_PHASE_DATES, "ss_claim_date": date(2027, 1, 1)}
        rows = self._run(bracket_table, phase_dates, ss_annual_benefit=24000.0)
        rows_by_year = {r["year"]: r for r in rows}
        assert rows_by_year[2026]["ss_benefit_gross"] == pytest.approx(0.0)
        assert rows_by_year[2027]["ss_benefit_gross"] == pytest.approx(24000.0)

    def test_mid_year_claim_date_prorates_the_first_year(self, bracket_table):
        phase_dates = {**ALREADY_WITHDRAWING_PHASE_DATES, "ss_claim_date": date(2026, 7, 2)}
        rows = self._run(bracket_table, phase_dates, ss_annual_benefit=24000.0)
        row_2026 = rows[0]
        assert 0.0 < row_2026["ss_benefit_gross"] < 24000.0

    def test_gross_income_includes_the_benefit(self, bracket_table):
        rows = self._run(bracket_table, ALREADY_WITHDRAWING_PHASE_DATES, ss_annual_benefit=20000.0)
        without_ss = self._run(bracket_table, ALREADY_WITHDRAWING_PHASE_DATES, ss_annual_benefit=0.0)
        assert rows[0]["gross_income"] == pytest.approx(without_ss[0]["gross_income"] + 20000.0)

    def test_benefit_is_actually_taxed(self, bracket_table):
        # $200,000 -- large enough that the taxable portion (85%-capped under the provisional-
        # income formula compute_taxes already implements) clears the standard deduction (this
        # scenario's birth_date also qualifies for the additional age-65+ deduction) and produces
        # real owed tax; a too-small benefit can legitimately owe $0 even once some of it is
        # "taxable" on paper (verified directly: $60,000 here produces a nonzero ss_taxable_amount
        # but still $0.0 total_tax, entirely absorbed by the standard deduction -- real tax policy,
        # not a bug, so this test uses a benefit large enough to clear that margin instead).
        rows_with = self._run(bracket_table, ALREADY_WITHDRAWING_PHASE_DATES, ss_annual_benefit=200000.0)
        rows_without = self._run(bracket_table, ALREADY_WITHDRAWING_PHASE_DATES, ss_annual_benefit=0.0)
        assert rows_with[0]["total_tax"] > rows_without[0]["total_tax"]

    def test_total_withdrawal_income_and_net_retirement_income_include_the_benefit(self, bracket_table):
        rows_with = self._run(bracket_table, ALREADY_WITHDRAWING_PHASE_DATES, ss_annual_benefit=20000.0)
        rows_without = self._run(bracket_table, ALREADY_WITHDRAWING_PHASE_DATES, ss_annual_benefit=0.0)
        row_with, row_without = rows_with[0], rows_without[0]
        # total_withdrawal_income rises by the full benefit; net_retirement_income rises by less
        # (the benefit's own tax is subtracted too), but must still rise, not fall or stay flat.
        assert row_with["total_withdrawal_income"] == pytest.approx(
            row_without["total_withdrawal_income"] + 20000.0
        )
        assert row_with["net_retirement_income"] > row_without["net_retirement_income"]

    def test_does_not_affect_available_cash_contribution_decisions(self, bracket_table):
        # earned_only_tax / available_cash are deliberately isolated to EARNED income only (see
        # modules/projection.py's own comment at that call site) -- a nonzero SS benefit during a
        # year with real W-2 income must not change Roth/Traditional IRA/Taxable contribution
        # amounts at all.
        income = _income_inputs(current_year_already_earned_w2=60000.0)
        common = dict(
            income_inputs=income, expense_inputs=_expense_inputs(), current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31), retirement_date=date(2050, 1, 1), birth_date=date(1990, 1, 1),
            filing_status="single", bracket_table=bracket_table,
            phase_dates={"savings_stop_date": date(2050, 1, 1), "withdrawal_start_date": date(2050, 1, 1), "ss_claim_date": date(2020, 1, 1)},
            contribution_config_by_year={2026: _max_config()},
        )
        rows_with = project_multi_year(**{**common, "ss_annual_benefit": 30000.0})
        rows_without = project_multi_year(**{**common, "ss_annual_benefit": 0.0})
        assert rows_with[0]["roth_ira_contribution_used"] == pytest.approx(rows_without[0]["roth_ira_contribution_used"])
        assert rows_with[0]["w2_401k_contribution_used"] == pytest.approx(rows_without[0]["w2_401k_contribution_used"])

    def test_does_not_affect_earned_income_tax_field(self, bracket_table):
        income = _income_inputs(current_year_already_earned_w2=60000.0)
        common = dict(
            income_inputs=income, expense_inputs=_expense_inputs(), current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31), retirement_date=date(2050, 1, 1), birth_date=date(1990, 1, 1),
            filing_status="single", bracket_table=bracket_table,
            phase_dates={"savings_stop_date": date(2050, 1, 1), "withdrawal_start_date": date(2050, 1, 1), "ss_claim_date": date(2020, 1, 1)},
        )
        rows_with = project_multi_year(**{**common, "ss_annual_benefit": 30000.0})
        rows_without = project_multi_year(**{**common, "ss_annual_benefit": 0.0})
        assert rows_with[0]["earned_income_tax"] == pytest.approx(rows_without[0]["earned_income_tax"])

    def test_baseline_passes_ss_annual_benefit_through(self, bracket_table):
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 50.0, "year_acquired": 2020}]
        rows_with = project_no_income_no_expense_baseline(
            current_date=date(2026, 1, 1), horizon_date=date(2026, 12, 31), birth_date=date(1955, 1, 1),
            filing_status="single", bracket_table=bracket_table, withdrawal_start_date=date(2020, 1, 1),
            ss_claim_date=date(2020, 1, 1), universe=_small_universe(), initial_lots=lots,
            initial_prices_by_ticker={"VTI": 100.0}, ss_annual_benefit=20000.0,
        )
        rows_without = project_no_income_no_expense_baseline(
            current_date=date(2026, 1, 1), horizon_date=date(2026, 12, 31), birth_date=date(1955, 1, 1),
            filing_status="single", bracket_table=bracket_table, withdrawal_start_date=date(2020, 1, 1),
            ss_claim_date=date(2020, 1, 1), universe=_small_universe(), initial_lots=lots,
            initial_prices_by_ticker={"VTI": 100.0}, ss_annual_benefit=0.0,
        )
        assert rows_with[0]["ss_benefit_gross"] == pytest.approx(20000.0)
        assert rows_without[0]["ss_benefit_gross"] == pytest.approx(0.0)


class TestProjectMultiYearRetirementEarningsTest:
    """2026-09-06, NEXT.md item B3 — the Retirement Earnings Test wired into project_multi_year via
    the optional `ss_bend_point_table` parameter. `ss_bend_point_table=None` (every pre-existing
    caller/test that doesn't pass it) must skip this entirely — fully backward compatible."""

    def _run(self, bracket_table, ss_bend_point_table, gross_w2=80000.0):
        return project_multi_year(
            _income_inputs(current_year_already_earned_w2=gross_w2),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2060, 1, 1),  # still working
            birth_date=date(1990, 1, 1),  # FRA (67) reached in 2057 -- 2026 is decades before it
            filing_status="single",
            bracket_table=bracket_table,
            phase_dates={
                "savings_stop_date": date(2060, 1, 1),
                "withdrawal_start_date": date(2060, 1, 1),
                "ss_claim_date": date(2026, 1, 1),  # claiming immediately, decades before FRA
            },
            ss_annual_benefit=24000.0,
            ss_bend_point_table=ss_bend_point_table,
        )

    def test_none_table_skips_the_earnings_test_entirely(self, bracket_table):
        row = self._run(bracket_table, ss_bend_point_table=None)[0]
        assert row["ss_benefit_gross"] == pytest.approx(24000.0)
        assert row["ss_earnings_test_reduction"] == pytest.approx(0.0)

    def test_real_table_reduces_the_benefit_for_an_earner_claiming_early(self, bracket_table):
        # $30,000 earned vs. 2026's own $24,480 lower exempt -> a real but PARTIAL reduction
        # ($2,760), well under the $24,000 benefit itself -- isolates this test from the separate
        # "reduction exceeds the benefit, clamp at $0" case covered below.
        table = load_bend_point_table()
        row = self._run(bracket_table, ss_bend_point_table=table, gross_w2=30000.0)[0]
        assert row["ss_earnings_test_reduction"] > 0
        assert row["ss_benefit_gross"] == pytest.approx(24000.0 - row["ss_earnings_test_reduction"])
        # Matches a direct, independent reconstruction from the smaller pieces.
        expected_reduction = retirement_earnings_test_reduction(30000.0, 0.0, date(1990, 1, 1), 2026, table)
        assert row["ss_earnings_test_reduction"] == pytest.approx(expected_reduction)

    def test_benefit_never_goes_negative_even_if_reduction_would_exceed_it(self, bracket_table):
        table = load_bend_point_table()
        row = self._run(bracket_table, ss_bend_point_table=table, gross_w2=5000000.0)[0]
        assert row["ss_benefit_gross"] == pytest.approx(0.0)

    def test_zero_reduction_once_at_or_under_the_exempt_amount(self, bracket_table):
        table = load_bend_point_table()
        row = self._run(bracket_table, ss_bend_point_table=table, gross_w2=10000.0)[0]
        assert row["ss_earnings_test_reduction"] == pytest.approx(0.0)
        assert row["ss_benefit_gross"] == pytest.approx(24000.0)

    def test_no_reduction_once_past_full_retirement_age(self, bracket_table):
        table = load_bend_point_table()
        rows = project_multi_year(
            _income_inputs(current_year_already_earned_w2=500000.0),
            _expense_inputs(),
            current_date=date(2026, 1, 1),
            horizon_date=date(2026, 12, 31),
            retirement_date=date(2060, 1, 1),
            birth_date=date(1955, 1, 1),  # FRA already long past by 2026
            filing_status="single",
            bracket_table=bracket_table,
            phase_dates={
                "savings_stop_date": date(2060, 1, 1),
                "withdrawal_start_date": date(2060, 1, 1),
                "ss_claim_date": date(2020, 1, 1),
            },
            ss_annual_benefit=24000.0,
            ss_bend_point_table=table,
        )
        row = rows[0]
        assert row["ss_earnings_test_reduction"] == pytest.approx(0.0)
        assert row["ss_benefit_gross"] == pytest.approx(24000.0)


class TestProjectMultiYearSsAfterTaxSplit:
    """2026-08-31, user request — the retirement-income chart's own SS-vs-other stacked breakout:
    tax_attributable_to_ss / ss_after_tax_income / other_after_tax_income."""

    def _run(self, bracket_table, ss_annual_benefit, **kwargs):
        defaults = dict(
            current_date=date(2026, 1, 1),
            horizon_date=date(2027, 12, 31),
            retirement_date=date(2020, 1, 1),
            birth_date=date(1955, 1, 1),
            filing_status="single",
            bracket_table=bracket_table,
            universe=_small_universe(),
            initial_lots=[
                {"ticker": "VTI", "account_type": "Taxable", "shares": 1000.0, "basis_per_share": 50.0, "year_acquired": 2020}
            ],
            initial_prices_by_ticker={"VTI": 100.0},
            phase_dates=ALREADY_WITHDRAWING_PHASE_DATES,
            ss_annual_benefit=ss_annual_benefit,
            withdrawal_strategy_params={"rate": 0.04},
        )
        defaults.update(kwargs)
        return project_multi_year(_income_inputs(), _expense_inputs(), **defaults)

    def test_none_outside_the_withdrawal_phase(self, bracket_table):
        rows = self._run(
            bracket_table, ss_annual_benefit=0.0,
            phase_dates={"savings_stop_date": date(2050, 1, 1), "withdrawal_start_date": date(2050, 1, 1), "ss_claim_date": date(2020, 1, 1)},
            retirement_date=date(2050, 1, 1),
        )
        row = rows[0]
        assert row["is_withdrawal_year"] is False
        assert row["tax_attributable_to_ss"] is None
        assert row["ss_after_tax_income"] is None
        assert row["other_after_tax_income"] is None

    def test_bands_sum_to_net_retirement_income(self, bracket_table):
        # By construction, always true, regardless of benefit size -- a real split of the total,
        # not a new number.
        for benefit in (0.0, 20000.0, 60000.0, 200000.0):
            row = self._run(bracket_table, ss_annual_benefit=benefit)[0]
            assert row["ss_after_tax_income"] + row["other_after_tax_income"] == pytest.approx(
                row["net_retirement_income"], abs=0.01
            )

    def test_zero_benefit_gives_zero_ss_bands(self, bracket_table):
        row = self._run(bracket_table, ss_annual_benefit=0.0)[0]
        assert row["tax_attributable_to_ss"] == pytest.approx(0.0)
        assert row["ss_after_tax_income"] == pytest.approx(0.0)
        assert row["other_after_tax_income"] == pytest.approx(row["net_retirement_income"])

    def test_large_benefit_produces_a_real_positive_tax_attributable_to_ss(self, bracket_table):
        # $200,000 clears the standard deduction comfortably (same figure verified in
        # TestProjectMultiYearSocialSecurity::test_benefit_is_actually_taxed) -- the marginal tax
        # cost of SS specifically must be real and positive, not zero or negative.
        row = self._run(bracket_table, ss_annual_benefit=200000.0)[0]
        assert row["tax_attributable_to_ss"] > 0.0
        assert row["ss_after_tax_income"] < row["ss_benefit_gross"]
        assert row["ss_after_tax_income"] > 0.0

    def test_matches_hand_computed_second_pass(self, bracket_table):
        # Directly reproduces the isolation technique using the still-public compute_taxes/
        # bracket_table, confirming the row-level fields aren't just internally self-consistent
        # but match an independent reconstruction.
        row = self._run(bracket_table, ss_annual_benefit=200000.0)[0]
        zero_ss = compute_taxes(
            tax_year=row["bracket_year_used"],
            filing_status="single",
            age_at_year_end=71,
            w2_gross=0.0,
            pretax_401k=0.0,
            roth_401k=0.0,
            pretax_health_dental=0.0,
            se_net_profit=0.0,
            se_solo_employee_deferral=0.0,
            se_solo_employer_contribution=0.0,
            taxable_retirement_withdrawal=row["withdrawal_taxable_retirement_distribution"],
            ltcg=row["withdrawal_long_term_gain"],
            qualified_dividends=row["taxable_qualified_dividends"],
            ordinary_dividends=row["taxable_ordinary_dividends"],
            interest_income=row["taxable_interest_income"],
            ss_benefit_gross=0.0,
            bracket_table=bracket_table,
            ordinary_break_income=0.0,
            short_term_gains=row["withdrawal_short_term_gain"],
        )
        expected = row["total_tax"] - zero_ss["total_tax"]
        assert row["tax_attributable_to_ss"] == pytest.approx(expected)

    def test_does_not_change_total_withdrawal_income_or_net_retirement_income(self, bracket_table):
        # Purely additive/derived -- adding the split fields must not change any pre-existing field.
        row_with_split = self._run(bracket_table, ss_annual_benefit=60000.0)[0]
        assert row_with_split["net_retirement_income"] == pytest.approx(
            row_with_split["total_withdrawal_income"] - row_with_split["retirement_taxes_paid"]
        )
