"""
Demographics tab (Module 2): plain inputs plus derived durations. See PROJECT_PLAN.md Step 2.

Extended (MODEL_WIRING.md §1-§2, 2026-08-10) with the model's four independent life-phase dates
(`retirement_date` already existed; `savings_stop_date`, `withdrawal_start_date`, `ss_claim_date`
are new) and the planning-horizon age that now extends the projection past retirement. These are
plain inputs here — the future contribution/investing/withdrawal engines (Module D and beyond) are
what actually consume them; `modules.gross_income.project_gross_income` already consumes
`retirement_date` (Step 1 of that build).

Further extended (Module G1 — health index / expected lifespan, MODULE_G1_HEALTH_LIFESPAN.md,
2026-08-30) with a sex input, editable health-age-rating years, and the resulting expected/
percentile-lifespan stats — see `modules.health`'s own module docstring for the full methodology
and the two design decisions confirmed with the user before building this (`planning_horizon_age`
above stays the actual funding horizon, untouched; these are new, separate, informational figures
only). Per that spec's own explicit step 5, NOT wired into `project_multi_year`'s horizon or any
withdrawal-sizing logic yet — this tab computes and displays the figures, nothing more.
"""

from __future__ import annotations

from datetime import date

import streamlit as st

from modules.demographics import HEALTH_STATUS_OPTIONS, current_age, months_between
from modules.health import (
    DEFAULT_HEALTH_AGE_ADJUSTMENT_YEARS,
    coverage_percentile_at_age,
    expected_lifespan_age,
    load_mortality_table,
    percentile_lifespan_age,
    survival_curve,
)

_MAX_FUTURE_DATE = date(date.today().year + 100, date.today().month, date.today().day)
_SEX_OPTIONS = ["Female", "Male"]


def render() -> None:
    st.header("Demographics")

    d1, d2 = st.columns(2)
    with d1:
        st.date_input(
            "Date of birth",
            min_value=date(1900, 1, 1),
            max_value=date.today(),
            key="birth_date",
        )
    with d2:
        st.number_input(
            "Planning horizon (age)",
            min_value=1,
            max_value=130,
            step=1,
            key="planning_horizon_age",
            help="The model projects through this age (see MODEL_WIRING.md §1.2) — a deliberate, "
            "conservative PLANNING horizon, not a life-expectancy estimate. Module G1 (health "
            "index / projected lifespan) will later replace this with a computed figure.",
        )

    h1, h2 = st.columns(2)
    with h1:
        st.selectbox("Health status", options=HEALTH_STATUS_OPTIONS, key="health_status")
    with h2:
        st.selectbox(
            "Biological sex (for life-expectancy table lookup)",
            options=_SEX_OPTIONS,
            key="sex_for_mortality",
            help="SSA Period Life Tables (the mortality data behind 'Expected lifespan'/"
            "'percentile lifespan' below) are published separately by sex — the two cohorts' "
            "mortality differs by multiple years of life expectancy, enough that a single blended "
            "table would be a real accuracy loss, not a rounding difference.",
        )

    with st.expander("Health-adjustment years (advanced)"):
        st.caption(
            "Approximate — adjusts which table AGE is used to look up your mortality risk for "
            "each health-status tier (age rating, the standard actuarial/insurance-underwriting "
            "technique for turning a self-rated health status into a mortality adjustment); it "
            "never changes your real age used everywhere else in the model. 'Good' is the "
            "reference tier (SSA tables are population averages) — override any of these if you "
            "have a more specific estimate of your own mortality risk, e.g. from a life-insurance "
            "underwriting quote."
        )
        ha1, ha2, ha3, ha4 = st.columns(4)
        for col, tier in zip((ha1, ha2, ha3, ha4), HEALTH_STATUS_OPTIONS):
            with col:
                st.number_input(
                    tier,
                    min_value=-20,
                    max_value=30,
                    step=1,
                    key=f"health_adjustment_{tier.lower()}",
                    help=f"Years added to (or, if negative, subtracted from) your real age when "
                    f"looking up mortality risk for the '{tier}' tier. Default: "
                    f"{DEFAULT_HEALTH_AGE_ADJUSTMENT_YEARS[tier]:+d}.",
                )

    st.subheader("Life-phase dates")
    st.caption(
        "Four independent dates the model reads for the year-by-year projection — a real plan "
        "can separate them arbitrarily (e.g. stop saving at 55, retire at 60, start withdrawals "
        "at 62, claim Social Security at 70). No ordering between them is assumed or enforced, "
        "beyond the warning below."
    )
    p1, p2, p3, p4 = st.columns(4)
    with p1:
        # 2026-08-30, user request — renamed from "Planned retirement date" to "Income end date"
        # (session-state key retirement_date itself is UNCHANGED, cosmetic only — see this
        # function's own docstring / NEXT.md): a more accurate name for what this date actually
        # does (stops earned W-2/SE income), removing the ambiguity against "Withdrawal start
        # date" below that prompted this rename. Moved into this row, alongside the other three
        # phase-relevant dates, for the same reason.
        st.date_input(
            "Income end date",
            min_value=date(1900, 1, 1),
            max_value=_MAX_FUTURE_DATE,
            key="retirement_date",
            help="The date earned income (W-2/SE) stops, per this module's own curve. Independent "
            "of the other phase dates here — no particular ordering is assumed between this and "
            "Savings stop date, but Withdrawal start date may not be earlier than this one (same "
            "day is fine) — see that date's own help text.",
        )
    with p2:
        st.date_input(
            "Savings stop date",
            min_value=date(1900, 1, 1),
            max_value=_MAX_FUTURE_DATE,
            key="savings_stop_date",
            help="The date voluntary saving (Roth/Traditional IRA, Taxable) stops. Earned income "
            "and expenses are NOT frozen after this date (2026-09-06 — real income between this "
            "date and Income end date, if any, keeps flowing through normally): instead, whatever "
            "earned-income surplus would have been saved becomes 'Discretionary spending' on the "
            "Projection tab's income chart — money genuinely spent, not invested, not tracked "
            "further. 401(k) payroll contributions are UNAFFECTED by this date (never gated on "
            "saving having stopped, by this model's own long-standing design). Dividend/interest "
            "reinvestment on existing holdings also continues unaffected regardless of this date's "
            "position relative to Income end date.",
        )
    with p3:
        st.date_input(
            "Withdrawal start date",
            min_value=date(1900, 1, 1),
            max_value=_MAX_FUTURE_DATE,
            key="withdrawal_start_date",
            help="The date retirement withdrawals begin drawing down the portfolio. Must not be "
            "earlier than EITHER Income end date or Savings stop date (same day as either, or "
            "both, is fine) — the Projection tab shows an error if it is. This guarantees saving "
            "has genuinely stopped before withdrawals begin, so the two can never overlap.",
        )
    with p4:
        st.date_input(
            "Social Security claim date",
            min_value=date(1900, 1, 1),
            max_value=_MAX_FUTURE_DATE,
            key="ss_claim_date",
            help="The date Social Security benefits begin. Benefit amount comes from Module F "
            "(not yet built) — this date only controls when a configured benefit starts.",
        )

    birth_date = st.session_state.birth_date
    retirement_date = st.session_state.retirement_date
    savings_stop_date = st.session_state.savings_stop_date
    today = date.today()

    if savings_stop_date > retirement_date:
        st.warning(
            "Savings stop date is after the Income end date — no earned income remains to save "
            "from at that point. This is allowed (e.g. modeling severance or deferred "
            "compensation continuing past retirement) but double-check it's intentional."
        )

    age = current_age(birth_date, today)
    months_to_retirement = months_between(today, retirement_date)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Current age", f"{age}")
    m2.metric(
        "Years to retirement",
        f"{months_to_retirement / 12:.1f}",
        help=f"{months_to_retirement} months, to {retirement_date.isoformat()}.",
    )
    # 2026-08-30, user request: replaced "Years to end of accumulation"/"Years to planning
    # horizon" with these two age-at-date figures — current_age()'s own "completed years as of a
    # given date" math, just applied to a FUTURE date instead of today (same function already used
    # for "Current age" above).
    m3.metric(
        "Age of Retirement",
        f"{current_age(birth_date, retirement_date)}",
        help=f"Age on {retirement_date.isoformat()} (Income end date above).",
    )
    m4.metric(
        "Age that Savings is Discontinued",
        f"{current_age(birth_date, savings_stop_date)}",
        help=f"Age on {savings_stop_date.isoformat()} (Savings stop date above).",
    )

    # Module G1 (2026-08-30) — a SEPARATE, informational pair of stats, deliberately never
    # substituted for planning_horizon_age's own funding-horizon role above (see this file's own
    # top-of-file docstring for the confirmed design decision).
    adjustment_years = {
        tier: st.session_state[f"health_adjustment_{tier.lower()}"] for tier in HEALTH_STATUS_OPTIONS
    }
    mortality_table = load_mortality_table()
    curve = survival_curve(
        # modules.health/data/mortality_table.json key their sexes lowercase ("male"/"female");
        # the selectbox above displays the capitalized form for readability -- lowercased here at
        # the one point of use, not stored that way in session state.
        birth_date, today, st.session_state.sex_for_mortality.lower(), st.session_state.health_status,
        mortality_table, adjustment_years,
    )
    expected_lifespan = expected_lifespan_age(curve)
    percentile_90_age = percentile_lifespan_age(curve, 0.90)

    st.subheader("Expected lifespan (Module G1)")
    st.caption(
        "Informational only — a consumption/tax-timing planning figure, NOT a replacement for "
        "'Planning horizon (age)' above, which stays the actual conservative funding horizon the "
        "full projection runs through. Computed from the SSA Period Life Table, your sex, and "
        "your health status's age-rating adjustment above."
    )
    g1_col1, g1_col2 = st.columns(2)
    g1_col1.metric(
        "Expected lifespan",
        f"{expected_lifespan:.1f}" if expected_lifespan is not None else "—",
        help="The mean (probability-weighted average) age at death across your projected survival "
        "curve — a realistic planning assumption for smoothing consumption/taxes over your actual "
        "likely years, NOT a funding-safety figure (funding to the mean means a real, roughly 50% "
        "chance of outliving it).",
    )
    g1_col2.metric(
        "90th-percentile lifespan",
        f"{percentile_90_age}" if percentile_90_age is not None else "—",
        help="The age by which there is a 90% cumulative chance of having died (equivalently, only "
        "a 10% chance of living longer than this) — the conservative, funding-safety reading of "
        "your survival curve, for comparison against 'Planning horizon (age)' above.",
    )

    if percentile_90_age is not None:
        planning_horizon_age = st.session_state.planning_horizon_age
        if planning_horizon_age > curve[-1]["age"]:
            # Beyond the mortality table's own terminal age (119) — nothing left to look up;
            # by construction this horizon already exceeds every age the table can represent.
            st.caption(
                f"Your current planning horizon (age {planning_horizon_age}) is beyond this "
                "survival curve's own table range — it covers essentially all projected outcomes."
            )
        else:
            coverage = coverage_percentile_at_age(curve, planning_horizon_age)
            if coverage is not None:
                st.caption(
                    f"Your current planning horizon (age {planning_horizon_age}) covers the "
                    f"**{coverage:.0%}** percentile of your projected survival curve."
                )
