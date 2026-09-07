"""
Tax tab (QC surface for modules/tax.py — see PROJECT_PLAN.md Step 4, not Module E).

One plain numeric input per compute_taxes income source, plus every TaxResult field displayed as
its own labeled line, so the numbers can be checked line-by-line against an external calculator.
Not wired into Portfolio (still uses its own placeholder rates — see PROJECT_PLAN.md). Its inputs
DO round-trip through save/load (ui/sidebar.py, 2026-08-09) like every other tab's, even though it
remains a manual single-year QC tool rather than a yearly-loop caller — see PROJECT_PLAN.md Step 4.

`taxable_retirement_withdrawal` and `ss_benefit_gross` are plain inputs here on purpose, even though
they won't stay that way long-term: they'll eventually be computed by Module G2 (retirement
withdrawal strategy) and Module F (Social Security/AIME), not typed in. See PROJECT_PLAN.md Step 4.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from modules.tax import (
    FILING_STATUSES,
    check_ira_combined_limit,
    check_roth_ira_limit,
    compute_max_401k_contributions,
    compute_taxes,
    load_bracket_table,
    max_ira_contribution_limit,
    roth_ira_magi,
    roth_ira_phase_out_max,
)

_FILING_STATUS_LABELS = {"single": "Single", "mfj": "Married filing jointly", "hoh": "Head of household"}

# (TaxResult key, display label, "dollar" | "percent") — grouped to match the build spec's own
# TaxResult sections, so a line here maps 1:1 onto a line in the spec/an external calculator.
_FIELD_GROUPS = [
    (
        "Federal",
        [
            ("federal_agi", "Federal AGI", "dollar"),
            ("federal_taxable_income", "Federal taxable income", "dollar"),
            ("federal_ordinary_tax", "Ordinary income tax", "dollar"),
            ("federal_ltcg_tax", "LTCG / qualified dividend tax", "dollar"),
            ("niit", "Net Investment Income Tax (NIIT)", "dollar"),
            ("additional_medicare_tax", "Additional Medicare Tax", "dollar"),
            ("federal_tax_total", "Federal tax total", "dollar"),
            ("qbi_deduction", "QBI deduction", "dollar"),
            ("ss_taxable_amount", "Taxable Social Security amount", "dollar"),
        ],
    ),
    (
        "California",
        [
            ("ca_taxable_income", "CA taxable income", "dollar"),
            ("ca_tax_before_mhst", "CA tax before Mental Health Services Tax", "dollar"),
            ("mhst", "Mental Health Services Tax", "dollar"),
            ("ca_tax_total", "CA tax total", "dollar"),
        ],
    ),
    (
        "Payroll",
        [
            ("social_security_tax", "Social Security tax (employee share)", "dollar"),
            ("medicare_tax", "Medicare tax (employee share)", "dollar"),
            ("se_tax", "Self-employment tax (SECA)", "dollar"),
            ("casdi", "CA State Disability Insurance", "dollar"),
        ],
    ),
    (
        "401(k) / solo 401(k)",
        [
            ("combined_employee_deferral_pool", "Combined W-2 + SE employee deferral pool (§402(g))", "dollar"),
            ("max_w2_employee_deferral", "Max W-2 employee deferral", "dollar"),
            ("max_se_employee_deferral", "Max SE employee deferral", "dollar"),
            ("se_solo_employer_contribution_max", "Max SE employer (profit-sharing) contribution", "dollar"),
            ("net_se_earnings_for_retirement", "Net SE earnings for retirement purposes", "dollar"),
        ],
    ),
    (
        "Summary",
        [
            ("total_tax", "Total tax", "dollar"),
            ("effective_rate_on_gross", "Effective rate on gross income", "percent"),
            ("marginal_federal_rate", "Marginal federal rate", "percent"),
        ],
    ),
]


def render() -> None:
    st.header("Tax")
    st.caption(
        "Single-year QC tool for modules/tax.py's compute_taxes — enter one starting value per "
        "income source and check every output line against an external calculator. Not yet wired "
        "into the Portfolio tab's own calculations, but its inputs do save/load with the rest of "
        "the app."
    )

    bracket_table = load_bracket_table()
    years = sorted(bracket_table.keys())

    c1, c2 = st.columns(2)
    with c1:
        st.selectbox(
            "Filing status",
            options=FILING_STATUSES,
            format_func=lambda s: _FILING_STATUS_LABELS[s],
            key="tax_filing_status",
        )
    with c2:
        st.selectbox("Tax year", options=years, key="tax_year_select")

    st.subheader("Wages")
    w1, w2, w3, w4 = st.columns(4)
    with w1:
        st.number_input("W-2 gross", min_value=0.0, step=1000.0, format="%.2f", key="tax_w2_gross")
    with w2:
        st.number_input(
            "Pretax 401(k)",
            min_value=0.0,
            step=500.0,
            format="%.2f",
            key="tax_pretax_401k",
            help="Traditional 401(k) elective deferral.",
        )
    with w3:
        st.number_input(
            "Roth 401(k)",
            min_value=0.0,
            step=500.0,
            format="%.2f",
            key="tax_roth_401k",
            help="Informational only — does not reduce federal/CA AGI, but is still subject to FICA.",
        )
    with w4:
        st.number_input(
            "Pretax health/dental",
            min_value=0.0,
            step=100.0,
            format="%.2f",
            key="tax_pretax_health_dental",
            help="Cafeteria-plan premiums — reduce W-2 taxable wages AND are excluded from FICA wages.",
        )

    st.subheader("Self-employment")
    s1, s2, s3 = st.columns(3)
    with s1:
        st.number_input(
            "SE net profit",
            min_value=0.0,
            step=1000.0,
            format="%.2f",
            key="tax_se_net_profit",
            help="Schedule C net profit before the SE tax deduction.",
        )
    with s2:
        st.number_input(
            "SE solo 401(k) employee deferral",
            min_value=0.0,
            step=500.0,
            format="%.2f",
            key="tax_se_solo_employee_deferral",
            help="Shares ONE combined annual §402(g) limit with the W-2 deferrals above — validated "
            "against the computed pool in the results below, not against SE earnings alone.",
        )
    with s3:
        st.number_input(
            "SE solo 401(k) employer contribution",
            min_value=0.0,
            step=500.0,
            format="%.2f",
            key="tax_se_solo_employer_contribution",
            help="Profit-sharing — a separate, per-plan §415(c) limit, not part of the §402(g) pool "
            "above. Validated against the computed max in the results below.",
        )

    st.subheader("401(k) settings")
    k1, k2 = st.columns(2)
    with k1:
        st.number_input(
            "Age at year-end",
            min_value=0,
            max_value=120,
            step=1,
            key="tax_age_at_year_end",
            help="Computed from the Demographics tab's date of birth and the tax year selected above "
            "— override here if needed.",
        )
    with k2:
        st.selectbox(
            "Employee deferral priority",
            options=["w2-first", "se-first"],
            key="tax_deferral_priority",
            help="Which side (W-2 or SE) fills the shared §402(g) pool first when both individually "
            "could exceed it. No tax reason to prefer either — a configuration choice.",
        )

    st.subheader("Investment income")
    i1, i2, i3, i4 = st.columns(4)
    with i1:
        st.number_input("Interest income", min_value=0.0, step=100.0, format="%.2f", key="tax_interest_income")
    with i2:
        st.number_input(
            "Ordinary dividends", min_value=0.0, step=100.0, format="%.2f", key="tax_ordinary_dividends",
            help="Non-qualified dividends.",
        )
    with i3:
        st.number_input(
            "Qualified dividends", min_value=0.0, step=100.0, format="%.2f", key="tax_qualified_dividends"
        )
    with i4:
        st.number_input(
            "Long-term capital gains", min_value=0.0, step=1000.0, format="%.2f", key="tax_ltcg"
        )

    st.subheader("Retirement")
    st.caption(
        "Plain inputs for now — will eventually be computed by other modules (withdrawal strategy, "
        "Social Security/AIME) rather than typed in directly. See PROJECT_PLAN.md Step 4."
    )
    r1, r2 = st.columns(2)
    with r1:
        st.number_input(
            "Taxable retirement withdrawal",
            min_value=0.0,
            step=1000.0,
            format="%.2f",
            key="tax_retirement_withdrawal",
            help="Traditional 401(k)/IRA distributions, RMDs, Roth conversions — ordinary income, "
            "excluded from the NIIT base under IRC §1411.",
        )
    with r2:
        st.number_input(
            "Social Security benefit (gross)",
            min_value=0.0,
            step=1000.0,
            format="%.2f",
            key="tax_ss_benefit_gross",
        )

    st.subheader("IRA")
    st.caption(
        "Traditional and Roth IRA contributions are independent of the 401(k)-family limits above "
        "(no statutory limit combines them), but NOT of each other — Traditional + Roth together "
        "share ONE combined annual limit (IRC §219(b)). Traditional IRA deductibility isn't modeled "
        "(compute_taxes has no such parameter — see NEXT.md), so a Traditional IRA entry here does "
        "NOT reduce AGI/taxable income below."
    )
    ira1, ira2 = st.columns(2)
    with ira1:
        st.number_input(
            "Traditional IRA contribution",
            min_value=0.0,
            step=500.0,
            format="%.2f",
            key="tax_traditional_ira_contribution",
        )
    with ira2:
        st.number_input(
            "Roth IRA contribution",
            min_value=0.0,
            step=500.0,
            format="%.2f",
            key="tax_roth_ira_contribution",
            help="Checked below against the income-phased maximum (IRS Pub. 590-A Worksheet 2-2), "
            "not just the flat statutory limit.",
        )

    with st.expander("How contribution priority/limits work", icon=":material/info:"):
        st.markdown(
            "**401(k)-family** (this page's `deferral_priority` dropdown above controls which side "
            "— W-2 or SE — fills the shared §402(g) pool first for the amounts you actually enter; "
            "the 'Computed maximum contributions' reference block below always uses a FIXED "
            "W-2-first order regardless of that dropdown, matching the Projection tab's 'Use "
            "computed maximum' checkbox exactly):\n\n"
            "1. W-2 pretax + Roth 401(k) draws from the shared §402(g) pool first (or second, if "
            "`deferral_priority` is set to SE-first).\n"
            "2. SE solo 401(k) employee deferral draws from whatever remains of that SAME pool.\n"
            "3. SE solo 401(k) employer (profit-sharing) contribution is bounded by the lesser of "
            "the ~20%-of-net-SE-earnings formula, remaining §415(c) room in that plan (after the SE "
            "employee deferral), and 100% of compensation — always computed with the SE employee "
            "deferral already known, never before it.\n\n"
            "**IRA**: Traditional + Roth IRA share one combined annual limit (§219(b)), entirely "
            "separate from the 401(k)-family limits above — there is no statutory limit combining "
            "IRA and 401(k)-family contributions."
        )

    st.subheader("Results")

    try:
        result = compute_taxes(
            tax_year=st.session_state.tax_year_select,
            filing_status=st.session_state.tax_filing_status,
            age_at_year_end=st.session_state.tax_age_at_year_end,
            w2_gross=st.session_state.tax_w2_gross,
            pretax_401k=st.session_state.tax_pretax_401k,
            roth_401k=st.session_state.tax_roth_401k,
            pretax_health_dental=st.session_state.tax_pretax_health_dental,
            se_net_profit=st.session_state.tax_se_net_profit,
            se_solo_employee_deferral=st.session_state.tax_se_solo_employee_deferral,
            se_solo_employer_contribution=st.session_state.tax_se_solo_employer_contribution,
            taxable_retirement_withdrawal=st.session_state.tax_retirement_withdrawal,
            ltcg=st.session_state.tax_ltcg,
            qualified_dividends=st.session_state.tax_qualified_dividends,
            ordinary_dividends=st.session_state.tax_ordinary_dividends,
            interest_income=st.session_state.tax_interest_income,
            ss_benefit_gross=st.session_state.tax_ss_benefit_gross,
            bracket_table=bracket_table,
            deferral_priority=st.session_state.tax_deferral_priority,
        )
    except ValueError as exc:
        st.error(str(exc))
        return

    tax_year = st.session_state.tax_year_select
    age_at_year_end = st.session_state.tax_age_at_year_end
    traditional_ira = st.session_state.tax_traditional_ira_contribution
    roth_ira = st.session_state.tax_roth_ira_contribution

    combined_ira_limit = max_ira_contribution_limit(tax_year, age_at_year_end, bracket_table)
    roth_magi = roth_ira_magi(result["federal_agi"])
    roth_ira_max = (
        roth_ira_phase_out_max(tax_year, st.session_state.tax_filing_status, age_at_year_end, roth_magi, bracket_table)
        if st.session_state.tax_filing_status in ("single", "hoh", "mfj")
        else None
    )
    ira_warnings = [
        w
        for w in (
            check_ira_combined_limit(traditional_ira, roth_ira, combined_ira_limit),
            check_roth_ira_limit(roth_ira, roth_ira_max) if roth_ira_max is not None else None,
        )
        if w
    ]
    for w in ira_warnings:
        st.warning(w)

    st.subheader("Computed maximum contributions (reference)")
    st.caption(
        "What every 401(k)-family bucket would be if maxed using the SAME fixed W-2-first priority "
        "as the Projection tab's 'Use computed maximum' checkbox — independent of the "
        "`deferral_priority` dropdown above, which only affects the validation caps used against "
        "what you actually entered (shown in the '401(k) / solo 401(k)' results group below)."
    )
    computed_max = compute_max_401k_contributions(
        tax_year, age_at_year_end, st.session_state.tax_w2_gross, result["net_se_earnings_for_retirement"], bracket_table
    )
    cm1, cm2, cm3 = st.columns(3)
    cm1.metric("Max W-2 pretax 401(k)", f"${computed_max['max_w2_employee_deferral']:,.2f}")
    cm2.metric("Max SE 401(k) employee", f"${computed_max['max_se_employee_deferral']:,.2f}")
    cm3.metric("Max SE 401(k) employer", f"${computed_max['max_se_employer_contribution']:,.2f}")

    st.subheader("IRA limits")
    im1, im2 = st.columns(2)
    im1.metric("Combined Traditional + Roth IRA limit", f"${combined_ira_limit:,.2f}", help="IRC §219(b).")
    im2.metric(
        "Roth IRA income-phased maximum",
        f"${roth_ira_max:,.2f}" if roth_ira_max is not None else "—",
        help=f"MAGI used: ${roth_magi:,.2f} (= federal AGI — see modules.tax.roth_ira_magi's own "
        "documented gaps).",
    )
    if st.button(
        "Fill Roth IRA at this year's income-phased maximum",
        icon=":material/auto_awesome:",
        disabled=roth_ira_max is None,
    ):
        st.session_state.tax_roth_ira_contribution = max(0.0, min(roth_ira_max, combined_ira_limit - traditional_ira))
        st.rerun()

    for group_name, fields in _FIELD_GROUPS:
        st.markdown(f"**{group_name}**")
        rows = [
            {
                "Line item": label,
                "Value": f"${result[key]:,.2f}" if kind == "dollar" else f"{result[key]:.2%}",
            }
            for key, label, kind in fields
        ]
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
