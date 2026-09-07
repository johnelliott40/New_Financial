"""
Social Security tab (Module F, NEXT.md's own queued 2026-08-31 spec): historical earnings entry
plus a full AIME / bend-point / claiming-age benefit calculation.

Calculation logic lives in modules/social_security.py; this file gathers inputs (historical
earnings entered here, future earnings reusing the SAME income assumptions the Projection tab's
own "Wages" section already collects — nothing re-entered), calls it, and renders results —
CLAUDE.md ground rule 4. Publishes the final computed annual benefit to
`st.session_state.computed_ss_annual_benefit` — a cross-tab bridge (same pattern as
`portfolio_universe`/`resolved_ticker_prices` on the Portfolio tab) the Projection tab reads to
feed `project_multi_year`'s new `ss_annual_benefit` parameter. This tab renders BEFORE Projection
in app.py's own tab order, so that bridge is fresh every rerun, not stale-by-one.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from modules.demographics import date_at_age
from modules.gross_income import project_gross_income
from modules.social_security import (
    REQUIRED_CREDITS_FOR_FULLY_INSURED,
    bend_point_pia,
    bend_points_for_year,
    claim_age_adjustment_factor,
    compute_aime,
    earnings_test_exempt_amounts_for_year,
    full_retirement_date,
    is_fully_insured,
    load_bend_point_table,
    qc_threshold_for_year,
    quarters_of_coverage,
    retirement_earnings_test_reduction,
    ssa_effective_birth_year,
)
from modules.tax import compute_taxes, load_bracket_table


def _bracket_year_for(year: int, bracket_table: dict) -> int:
    """Same 'latest configured year at or before, else earliest' policy as
    modules.projection._bracket_year_for — duplicated here (not imported across modules) since
    both this file and modules.social_security.bend_points_for_year already keep this small,
    stable lookup self-contained rather than sharing a cross-module private helper."""
    available = sorted(bracket_table.keys())
    candidates = [y for y in available if y <= year]
    return candidates[-1] if candidates else available[0]


def _w2_se_curves_from_session_state() -> tuple[dict, dict]:
    """Reads the SAME w2/se S-curve session-state keys ui/projection_tab.py's own `_curve_inputs`
    widgets already populate — never re-rendered here, just reused, so income assumptions are
    entered exactly once for the whole app."""

    def curve(prefix: str) -> dict:
        return {
            "start_value": st.session_state[f"proj_{prefix}_start_value"],
            "end_value": st.session_state[f"proj_{prefix}_end_value"],
            "midpoint_years": st.session_state[f"proj_{prefix}_midpoint_years"],
            "steepness": st.session_state[f"proj_{prefix}_steepness"],
        }

    return curve("w2"), curve("se")


def _future_ss_taxable_earnings(
    current_date: date, horizon_date: date, retirement_date: date, filing_status: str, bracket_table: dict
) -> dict[int, float]:
    """
    This year through `horizon_date`'s own `ss_taxable_earnings` (the wage-base-capped W-2+SE
    figure `modules.tax.compute_taxes` already computes correctly — see that field's own
    docstring), one throwaway `compute_taxes` call per year against ONLY that year's `gross_w2`/
    `gross_se` (every other input zeroed — 401(k)/IRA contributions, investment income, etc. don't
    affect this one field at all, so there's nothing to gain by threading them through here).
    """
    w2_curve, se_curve = _w2_se_curves_from_session_state()
    income_inputs = {
        "current_year_already_earned_w2": st.session_state.proj_already_earned_w2,
        "current_year_already_earned_se": st.session_state.proj_already_earned_se,
        "current_year_yet_to_earn_w2": st.session_state.proj_yet_to_earn_w2,
        "current_year_yet_to_earn_se": st.session_state.proj_yet_to_earn_se,
        "w2_curve": w2_curve,
        "se_curve": se_curve,
        "breaks": [],  # break/pension income isn't SS-creditable wages -- deliberately excluded
    }
    income_rows = project_gross_income(income_inputs, current_date, horizon_date, retirement_date)

    earnings_by_year: dict[int, float] = {}
    for r in income_rows:
        bracket_year = _bracket_year_for(r["year"], bracket_table)
        tax_result = compute_taxes(
            tax_year=bracket_year,
            filing_status=filing_status,
            age_at_year_end=30,  # doesn't affect ss_taxable_earnings at all -- any valid age works
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
        )
        earnings_by_year[r["year"]] = tax_result["ss_taxable_earnings"]
    return earnings_by_year


def _current_year_gross_w2_se(current_date: date, horizon_date: date, retirement_date: date) -> tuple[float, float]:
    """
    This year's own RAW `gross_w2`/`gross_se` (NOT the wage-base-capped `ss_taxable_earnings`
    `_future_ss_taxable_earnings` above computes for AIME purposes) — the Retirement Earnings
    Test's own "countable earnings" definition is the raw figure, matching exactly what
    `modules.projection`'s real per-year application reads (2026-09-06, NEXT.md item B3).
    """
    w2_curve, se_curve = _w2_se_curves_from_session_state()
    income_inputs = {
        "current_year_already_earned_w2": st.session_state.proj_already_earned_w2,
        "current_year_already_earned_se": st.session_state.proj_already_earned_se,
        "current_year_yet_to_earn_w2": st.session_state.proj_yet_to_earn_w2,
        "current_year_yet_to_earn_se": st.session_state.proj_yet_to_earn_se,
        "w2_curve": w2_curve,
        "se_curve": se_curve,
        "breaks": [],
    }
    income_rows = project_gross_income(income_inputs, current_date, horizon_date, retirement_date)
    row = next((r for r in income_rows if r["year"] == current_date.year), None)
    return (row["gross_w2"], row["gross_se"]) if row else (0.0, 0.0)


def render() -> None:
    st.header("Social Security")
    st.caption(
        "A full benefit calculation — Average Indexed Monthly Earnings (AIME), the bend-point "
        "Primary Insurance Amount formula, and your own claiming-age adjustment — not a "
        "placeholder. Benefit TAXATION was already fully built (see the Tax tab); this tab's only "
        "job is computing the one number that feeds it: the annual gross benefit."
    )

    birth_date = st.session_state.birth_date
    ss_claim_date = st.session_state.ss_claim_date
    filing_status = st.session_state.proj_filing_status
    current_date = date.today()
    horizon_date = date_at_age(birth_date, st.session_state.planning_horizon_age)
    retirement_date = st.session_state.retirement_date
    bracket_table = load_bracket_table()
    bend_point_table = load_bend_point_table()

    st.subheader("Historical earnings")
    fica_this_year = bracket_table[_bracket_year_for(current_date.year, bracket_table)]["fica"]
    wage_base = fica_this_year["ss_wage_base"]
    st.caption(
        f"One blended figure per year (W-2 + SE combined, already capped at that year's Social "
        f"Security wage base) — matches how a real SSA earnings record itself works. Simplification, "
        f"confirmed acceptable as a starting build: every historical year below is capped at "
        f"TODAY's real wage base (**${wage_base:,.0f}**), not each year's own historical wage base "
        "(which has drifted somewhat over decades, since it's wage-indexed, not CPI-indexed) — "
        "revisit with a real historical wage-base table later if it matters for a specific case."
    )

    default_start_year = birth_date.year + 22
    last_historical_year = current_date.year - 1
    start_year = st.number_input(
        "First year to track",
        min_value=birth_date.year,
        max_value=max(last_historical_year, birth_date.year),
        value=min(default_start_year, max(last_historical_year, birth_date.year)),
        step=1,
        help="Defaults to the year you turned 22 (a reasonable 'started working' default) — "
        "override if you started earning SS-creditable income earlier or later.",
    )

    if start_year > last_historical_year:
        st.info("No prior tax year to enter yet — historical earnings will appear here starting next year.")
        historical_ss_earnings: dict[int, float] = {}
    else:
        stored = st.session_state.historical_ss_earnings
        editor_records = [
            {"Year": year, "SS-taxable earnings ($)": stored.get(year, 0.0)}
            for year in range(start_year, last_historical_year + 1)
        ]
        edited = st.data_editor(
            pd.DataFrame(editor_records),
            # Versioned + range-keyed, same "force a full remount rather than fight a stale
            # cached edit-diff" pattern ui/projection_tab.py's own contribution editor uses —
            # here also remounting cleanly whenever `start_year` itself changes.
            key=f"ss_earnings_editor_v{st.session_state.form_version}_{start_year}_{last_historical_year}",
            num_rows="fixed",
            hide_index=True,
            disabled=["Year"],
            column_config={
                "Year": st.column_config.NumberColumn("Year", format="%d", pinned=True),
                "SS-taxable earnings ($)": st.column_config.NumberColumn(
                    "SS-taxable earnings ($)", format="$%.0f", min_value=0.0, max_value=wage_base
                ),
            },
            width="stretch",
        )
        historical_ss_earnings = {
            int(row["Year"]): float(row["SS-taxable earnings ($)"]) for row in edited.to_dict("records")
        }
        st.session_state.historical_ss_earnings = historical_ss_earnings

    st.subheader("Full earnings record & benefit calculation")
    future_ss_taxable_earnings = _future_ss_taxable_earnings(
        current_date, horizon_date, retirement_date, filing_status, bracket_table
    )
    merged_earnings = {**historical_ss_earnings, **future_ss_taxable_earnings}

    if not merged_earnings or not any(v > 0 for v in merged_earnings.values()):
        st.info("Enter some historical earnings above, or configure future income on the Projection tab, to see a benefit calculation.")
        st.session_state.computed_ss_annual_benefit = 0.0
        st.session_state.computed_ss_annual_benefit_baseline = 0.0
        return

    aime = compute_aime(merged_earnings)
    top_35_years = {
        year for year, _ in sorted(merged_earnings.items(), key=lambda kv: kv[1], reverse=True)[:35]
    }
    # Built from the smaller pieces directly (bend_points_for_year + bend_point_pia), not a single
    # modules.social_security.annual_ss_benefit call, since this tab needs monthly_pia and
    # eligibility_year/bend points as their OWN displayed metrics/help text, and needs to catch an
    # out-of-range claim date gracefully (still showing PIA) rather than letting the whole tab
    # raise -- annual_ss_benefit's own composed form doesn't expose those intermediate values.
    eligibility_year = ssa_effective_birth_year(birth_date) + 62
    bend_point_1, bend_point_2 = bend_points_for_year(eligibility_year, bend_point_table)
    monthly_pia = bend_point_pia(aime, bend_point_1, bend_point_2)
    # Module F "fully insured" gate (2026-08-31, a real user-caught bug): same held-flat-in-real-
    # terms lookup year as the bend points above, since both come from the same per-year table
    # entry (data/ss_bend_points.json).
    qc_threshold = qc_threshold_for_year(eligibility_year, bend_point_table)

    fra_date = full_retirement_date(birth_date)
    try:
        claim_factor = claim_age_adjustment_factor(birth_date, ss_claim_date)
        claim_factor_error = None
    except ValueError as exc:
        claim_factor = 1.0
        claim_factor_error = str(exc)

    if claim_factor_error:
        st.error(
            f"Social Security claim date is out of the valid claiming range: {claim_factor_error} "
            "(Demographics tab). Benefit shown below uses an un-adjusted (1.0x) factor until "
            "this is corrected."
        )

    annual_benefit = monthly_pia * claim_factor * 12.0

    # ---- "Fully insured" eligibility gate (Module F, 2026-08-31 — a real, user-caught bug) ----
    # A worker short of REQUIRED_CREDITS_FOR_FULLY_INSURED (40) credits receives $0 — the benefit
    # FORMULA above never even applies; this is not "a smaller benefit," it's no benefit at all
    # (ssa.gov/OACT/ProgData/insured.html — a single high-earning year, the user's own worked
    # example, buys at most 4 credits, nowhere near 40). Applied here rather than via modules.
    # social_security.insured_annual_ss_benefit's own composed form, since that one raises on an
    # out-of-range claim date (already handled gracefully above) rather than degrading to a 1.0x
    # factor — this tab's own established pattern for that specific failure mode.
    credits_real = quarters_of_coverage(merged_earnings, qc_threshold)
    insured_real = is_fully_insured(merged_earnings, qc_threshold)
    if not insured_real:
        annual_benefit = 0.0
    st.session_state.computed_ss_annual_benefit = annual_benefit

    # ---- "No further earnings" baseline benefit (2026-08-31, user request — a real bug fix) ----
    # The no-income/no-expense baseline scenario (Projection tab) was claiming this SAME
    # real-scenario benefit, built from merged_earnings (historical + every FUTURE projected
    # year) — but that baseline's own premise is "I essentially quit today," so future projected
    # earnings that scenario explicitly assumes never happen can't also feed its own AIME. Only
    # ALREADY-EARNED income can have generated Social Security credit under that hypothesis:
    # historical years, PLUS this year's own already-earned (not yet-to-earn) piece specifically
    # (confirmed with the user: counts as this year's FULL entry for this baseline, no further
    # proration — whatever was actually earned before "quitting" is real, already-earned income).
    current_year_earned_so_far = compute_taxes(
        tax_year=_bracket_year_for(current_date.year, bracket_table),
        filing_status=filing_status,
        age_at_year_end=30,  # doesn't affect ss_taxable_earnings at all -- any valid age works
        w2_gross=st.session_state.proj_already_earned_w2,
        pretax_401k=0.0,
        roth_401k=0.0,
        pretax_health_dental=0.0,
        se_net_profit=st.session_state.proj_already_earned_se,
        se_solo_employee_deferral=0.0,
        se_solo_employer_contribution=0.0,
        taxable_retirement_withdrawal=0.0,
        ltcg=0.0,
        qualified_dividends=0.0,
        ordinary_dividends=0.0,
        interest_income=0.0,
        ss_benefit_gross=0.0,
        bracket_table=bracket_table,
    )["ss_taxable_earnings"]
    baseline_earnings = {**historical_ss_earnings, current_date.year: current_year_earned_so_far}
    aime_baseline = compute_aime(baseline_earnings)
    # Same bend points / claim-age factor as the real scenario -- neither depends on AIME, only
    # this year's own earnings record differs between the two scenarios.
    monthly_pia_baseline = bend_point_pia(aime_baseline, bend_point_1, bend_point_2)
    annual_benefit_baseline = monthly_pia_baseline * claim_factor * 12.0
    # Same "fully insured" gate as the real scenario above, applied to THIS scenario's own
    # (smaller) earnings record — the baseline is exactly where "not insured" is most likely to be
    # the CORRECT real answer (a short earnings record is often the whole point of this scenario),
    # not an edge case to hide.
    credits_baseline = quarters_of_coverage(baseline_earnings, qc_threshold)
    insured_baseline = is_fully_insured(baseline_earnings, qc_threshold)
    if not insured_baseline:
        annual_benefit_baseline = 0.0
    st.session_state.computed_ss_annual_benefit_baseline = annual_benefit_baseline

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(
        "AIME (monthly)",
        f"${aime:,.0f}",
        help=f"Sum of the 35 highest years of SS-taxable earnings ÷ 420. {len(top_35_years)} "
        f"year(s) currently counted toward it, out of {len(merged_earnings)} entered/projected.",
    )
    c2.metric(
        "PIA (monthly)",
        f"${monthly_pia:,.0f}",
        help=f"90% of the first ${bend_point_1:,.0f}, 32% of the amount up to ${bend_point_2:,.0f}, "
        f"15% above that — bend points for eligibility year {eligibility_year} (held flat in real "
        "terms if beyond the last configured year).",
    )
    c3.metric(
        "Claim-age adjustment",
        f"{claim_factor:.0%}",
        help=f"Full Retirement Age: {fra_date.isoformat()}. Claiming on {ss_claim_date.isoformat()} "
        f"({'before' if ss_claim_date < fra_date else 'after' if ss_claim_date > fra_date else 'exactly at'} "
        "FRA) scales the PIA by this factor.",
    )
    c4.metric(
        "Annual gross benefit",
        f"${annual_benefit:,.0f}",
        help="PIA × claim-age adjustment × 12, ONLY if fully insured (see below) — this is the "
        "exact figure fed into `ss_benefit_gross` on the Projection tab's REAL (as-planned) "
        "scenario (and modules.tax.compute_taxes's own already-correct provisional-income "
        "taxability formula), phased in from your claim date. A year claimed before Full "
        "Retirement Age while still earning is further reduced there by the Retirement Earnings "
        "Test (2026-09-06) — see the note below for this year's own figure.",
    )

    # ---- "Fully insured" status, made explicit rather than a silently-zeroed number (2026-08-31,
    # a real user-caught bug: nothing gated the benefit formula before this) ----
    if insured_real:
        st.success(
            f"✓ Fully insured: {credits_real} of {REQUIRED_CREDITS_FOR_FULLY_INSURED} credits "
            "earned (this scenario's own full earnings record below)."
        )
    else:
        st.error(
            f"✗ NOT fully insured: only {credits_real} of {REQUIRED_CREDITS_FOR_FULLY_INSURED} "
            "credits earned (1 credit per "
            f"${qc_threshold:,.0f} of SS-taxable earnings in a year, capped at 4/year no matter "
            "how high that year's earnings are) — **no retired-worker benefit is payable**, "
            "regardless of AIME/PIA above. This is the real SSA eligibility rule "
            "(ssa.gov/OACT/ProgData/insured.html), not a smaller benefit — 'Annual gross benefit' "
            "above is correctly $0."
        )

    # ---- Retirement Earnings Test transparency (2026-09-06, NEXT.md item B3) ----
    # Made visible as its own caption, not a silently-smaller per-year benefit on the Projection
    # tab with no explanation — same "surface the real mechanism explicitly" convention this tab
    # already follows for the insured-status gate and claim-age adjustment above. Uses THIS
    # calendar year's own raw earnings (not the wage-base-capped AIME figures above) since the RET
    # applies year by year, on the same "countable earnings" definition modules.projection's real
    # per-year application reads.
    current_year_gross_w2, current_year_gross_se = _current_year_gross_w2_se(current_date, horizon_date, retirement_date)
    earnings_test_reduction_this_year = retirement_earnings_test_reduction(
        current_year_gross_w2, current_year_gross_se, birth_date, current_date.year, bend_point_table
    )
    fra_year = fra_date.year
    if earnings_test_reduction_this_year > 0:
        exempt_lower, exempt_higher = earnings_test_exempt_amounts_for_year(current_date.year, bend_point_table)
        exempt_amount = exempt_higher if current_date.year == fra_year else exempt_lower
        countable = current_year_gross_w2 + current_year_gross_se
        st.warning(
            f"⚠️ **Retirement Earnings Test**: claiming before Full Retirement Age "
            f"({fra_date.isoformat()}) while still earning withholds part of the benefit. This "
            f"year ({current_date.year}), projected countable earnings (W-2 + SE, from the "
            f"Projection tab's Wages section) are **\\${countable:,.0f}**, "
            f"\\${countable - exempt_amount:,.0f} over the \\${exempt_amount:,.0f} exempt amount — "
            f"**\\${earnings_test_reduction_this_year:,.0f} withheld** from this year's benefit "
            "(this is a per-year effect on the Projection tab's actual figures, not reflected in "
            "the static 'Annual gross benefit' metric above). SSA credits withheld amounts back "
            "once FRA is reached — this model does not (yet) model that credit-back, so this reads "
            "as a permanent loss here, not the delayed payment it really is."
        )
    elif fra_year > current_date.year:
        st.caption(
            f"Retirement Earnings Test: \\$0 withheld this year ({current_date.year}) — projected "
            f"countable earnings are at or under the exempt amount. Full Retirement Age is "
            f"{fra_date.isoformat()}; this test applies to every year before it while still earning."
        )

    st.metric(
        "Benefit under the \"no further earnings\" baseline",
        f"${annual_benefit_baseline:,.0f}",
        help=f"The Projection tab's OTHER scenario — 'no income or expenses from today' — assumes "
        f"you stop earning entirely today, so only ALREADY-EARNED income can have generated Social "
        f"Security credit: historical years above, plus this year's own already-earned piece only "
        f"(${current_year_earned_so_far:,.0f} — not the full-year projected amount). Same bend "
        f"points, qc_threshold, and claim-age adjustment as above, just a smaller earnings record "
        f"(AIME ${aime_baseline:,.0f}/mo vs. ${aime:,.0f}/mo) — this is the figure that scenario "
        "actually uses, not the real scenario's own (larger) benefit above.",
    )
    # Same "fully insured" transparency as the real scenario above — this is exactly where "not
    # insured" is most likely to be the CORRECT answer (a short earnings record is often this
    # scenario's whole point), not an edge case to bury.
    if insured_baseline:
        st.success(
            f"✓ Fully insured under this baseline: {credits_baseline} of "
            f"{REQUIRED_CREDITS_FOR_FULLY_INSURED} credits earned."
        )
    else:
        st.error(
            f"✗ NOT fully insured under this baseline: only {credits_baseline} of "
            f"{REQUIRED_CREDITS_FOR_FULLY_INSURED} credits earned from historical years + "
            f"{current_date.year}'s already-earned income — **no benefit is payable under this "
            "scenario**, correctly shown as $0 above. A short earnings record is often exactly "
            "why this baseline comes out uninsured, even when the real (as-planned) scenario above "
            "is fully insured."
        )
    # 2026-08-31, user request — made visible as its own caption, not just a hover tooltip: the
    # "no further earnings" baseline can come out nonzero even with an all-$0 historical table
    # above, purely from this year's own already-earned W-2/SE income (Projection tab's Wages
    # section) — genuinely correct per the confirmed design, but easy to miss without this line.
    if current_year_earned_so_far > 0 and not any(historical_ss_earnings.values()):
        st.caption(
            f"⚠️ This baseline benefit is driven entirely by {current_date.year}'s own "
            f"already-earned income (**${current_year_earned_so_far:,.0f}**, from the Projection "
            "tab's Wages section — 'W-2 already earned this year' / 'SE already earned this "
            "year') — every historical year above is currently $0."
        )
    elif current_year_earned_so_far > 0:
        st.caption(
            f"Includes {current_date.year}'s own already-earned income "
            f"(**${current_year_earned_so_far:,.0f}**, from the Projection tab's Wages section) "
            "alongside the historical years above."
        )

    with st.expander("Full earnings record (QC — check this against your own SSA statement)"):
        record_df = pd.DataFrame(
            [
                {
                    "Year": year,
                    "SS-taxable earnings": earnings,
                    "Source": "Historical (entered above)" if year in historical_ss_earnings else "Projected (Projection tab income)",
                    "Counts toward AIME (top 35)": year in top_35_years,
                }
                for year, earnings in sorted(merged_earnings.items())
            ]
        )
        st.dataframe(
            record_df,
            column_config={"SS-taxable earnings": st.column_config.NumberColumn(format="$%.0f")},
            width="stretch",
            hide_index=True,
        )

    st.caption(
        "No spousal, survivor, or divorced-spouse benefits — this is your OWN worker retirement "
        "benefit only. Real-dollar model: this annual figure is held flat in real terms for every "
        "year from your claim date onward (no separate COLA modeling needed — Social Security's "
        "own COLA is designed to preserve purchasing power, which a real-dollar model already "
        "represents as a flat real amount)."
    )
