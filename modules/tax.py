"""
Tax module — federal, California, and payroll tax for a single simulation-year.

Pure calculation function (`compute_taxes`): no Streamlit, no I/O, no globals besides the
`bracket_table` argument the caller injects — see CLAUDE.md ground rule 4 and PROJECT_PLAN.md's
architecture principles ("Tax is computed on realized flows, never on balances"; "Assumptions live
in data/ or in user input, never inline in logic"). `load_bracket_table()` is the one I/O boundary,
matching the same pattern as `modules.portfolio.load_asset_classes`.

Model convention: every dollar figure in/out is real (inflation-adjusted). Two groups of federal
thresholds are fixed by statute and NOT inflation-indexed (NIIT/Additional Medicare Tax since 2013;
Social Security taxability since 1984) — see the comments at their use sites below. Because this
model runs in real dollars, those thresholds effectively decline in real terms every year of a
multi-year projection. That is a real modeling consequence of currently-real-dollar-only inputs,
not a bug; it is called out here rather than silently absorbed.

Scope (v1) — see the build spec this was implemented from for the full list. Notably out of scope:
AMT, itemized deductions, multi-state, Married Filing Separately, credits, estimated-tax penalties,
and the full QBI wage/UBIA limitation (replaced by the simplified linear phase-out in
`_qbi_deduction`, per the spec's explicit instruction to skip the wage/property test in v1).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

_TAX_BRACKETS_PATH = Path(__file__).resolve().parent.parent / "data" / "tax_brackets.json"

FILING_STATUSES = ["single", "mfj", "hoh"]


def load_bracket_table(path: Path = _TAX_BRACKETS_PATH) -> dict:
    """
    Reads data/tax_brackets.json and returns it keyed by tax year (as int). This is the module's
    one I/O boundary — `compute_taxes` itself takes the already-loaded table as a plain argument,
    keeping it pure and independently testable against fixture tables that don't touch disk.
    """
    with open(path, "r") as f:
        raw = json.load(f)
    return {int(year): table for year, table in raw.items() if year != "_note"}


def _stacked_bracket_tax(floor: float, amount: float, brackets: list[list[float]]) -> float:
    """
    Tax on `amount` of income that stacks on top of `floor` (which may be negative — see
    `compute_taxes` step 9/10 for why a negative ordinary-income floor is correct, not a bug: it
    lets leftover deduction capacity absorb some LTCG into the 0% bracket for free, matching the
    real Schedule D Tax Worksheet). `brackets` is `[[lower_bound, rate], ...]` ascending; each
    bracket's upper bound is implied by the next entry's lower bound (the last is unbounded).
    With `floor=0` this is an ordinary progressive-tax calculator.
    """
    top = floor + amount
    total = 0.0
    for i, (lower, rate) in enumerate(brackets):
        upper = brackets[i + 1][0] if i + 1 < len(brackets) else float("inf")
        seg_lower = max(lower, floor)
        seg_upper = min(upper, top)
        if seg_upper > seg_lower:
            total += (seg_upper - seg_lower) * rate
    return total


def _progressive_tax(amount: float, brackets: list[list[float]]) -> float:
    """Plain progressive tax on a non-negative amount — `_stacked_bracket_tax` with floor 0."""
    return _stacked_bracket_tax(0.0, max(0.0, amount), brackets)


def _marginal_rate(ordinary_taxable: float, brackets: list[list[float]]) -> float:
    """The rate that would apply to the next dollar of ordinary taxable income."""
    amount = max(0.0, ordinary_taxable)
    rate = brackets[0][1]
    for lower, r in brackets:
        if amount >= lower:
            rate = r
        else:
            break
    return rate


def _qbi_deduction(qbi_base: float, filing_status: str, qbi_thresholds: dict) -> float:
    """
    Simplified Section 199A phase-in (per the build spec's explicit v1 simplification — skips the
    full wage/UBIA limitation test): full 20% deduction below `full_deduction_ceiling`, linearly
    interpolated to $0 between the ceiling and `phaseout_ceiling` (this models the SSTB case; a
    non-SSTB business would instead bind on the wage/property test above the ceiling, which v1
    does not implement), $0 at or above `phaseout_ceiling`.

    Both the deduction base and the phase-in test use the same income figure (`qbi_base`), a
    deliberate v1 simplification — the real 199A phase-in test uses taxable income before the QBI
    deduction, not the QBI base itself, but this model doesn't otherwise need to sequence a full
    taxable-income computation ahead of this step. Document, don't silently absorb (CLAUDE.md
    ground rule 1).
    """
    if qbi_base <= 0:
        return 0.0
    thresholds = qbi_thresholds[filing_status]
    full_ceiling = thresholds["full_deduction_ceiling"]
    phaseout_ceiling = thresholds["phaseout_ceiling"]
    full_deduction = 0.20 * qbi_base
    if qbi_base <= full_ceiling:
        return full_deduction
    if qbi_base >= phaseout_ceiling:
        return 0.0
    fraction_remaining = (phaseout_ceiling - qbi_base) / (phaseout_ceiling - full_ceiling)
    return full_deduction * fraction_remaining


DEFERRAL_PRIORITIES = ["w2-first", "se-first"]


def max_combined_employee_deferral(tax_year: int, age_at_year_end: int, bracket_table: dict) -> float:
    """
    IRC §402(g): ONE combined annual dollar limit on employee elective deferrals (traditional or
    Roth) across every plan a person contributes to — a W-2 job's 401(k) and a self-employed solo
    401(k) draw from the same pool, not separate ones. Employer contributions (profit-sharing,
    match) are NOT part of this limit — see max_se_employer_contribution, governed by §415(c)
    instead, a genuinely separate, per-plan limit.
    """
    if tax_year not in bracket_table:
        raise ValueError(f"No contribution limits configured for {tax_year}")
    limits = bracket_table[tax_year]["k401_limits"]
    limit = limits["elective_deferral_limit"]
    if 60 <= age_at_year_end <= 63:
        limit += limits["catchup_60_to_63"]  # SECURE 2.0 enhanced catch-up, ages 60-63 only
    elif age_at_year_end >= 50:
        limit += limits["catchup_50_to_59"]
    return limit


def allocate_employee_deferral(
    tax_year: int,
    age_at_year_end: int,
    year_gross_w2: float,
    year_net_se_earnings_for_retirement: float,
    priority: str,
    bracket_table: dict,
) -> dict:
    """
    Splits the shared §402(g) pool (max_combined_employee_deferral) between a W-2 job's employee
    deferral and a self-employed solo 401(k)'s employee deferral. Each source is also capped by
    its own comp — can't defer more than you earned from that source.
    `year_net_se_earnings_for_retirement` is net SE profit *after* the half-SE-tax-deduction
    adjustment (the same "net SE earnings for retirement purposes" figure used by
    max_se_employer_contribution below), not raw Schedule C net profit.

    `priority` picks which source fills first when both individually could exceed the pool —
    there's no tax reason to prefer one order over the other, so it's a caller-supplied choice,
    not hardcoded. "w2-first" is the more common real-world default: payroll deferral elections
    usually lock in earlier in the year than SE solo 401(k) elections, which can often be made
    retroactively up to the filing deadline.

    Returns {"max_w2_deferral": float, "max_se_employee_deferral": float}; the two never sum to
    more than the pool (see tests/test_tax.py).
    """
    if priority not in DEFERRAL_PRIORITIES:
        raise ValueError(f"priority must be one of {DEFERRAL_PRIORITIES}, got {priority!r}")
    pool = max_combined_employee_deferral(tax_year, age_at_year_end, bracket_table)

    w2_cap = min(pool, max(0.0, year_gross_w2))
    se_cap = min(pool, max(0.0, year_net_se_earnings_for_retirement))

    if priority == "w2-first":
        max_w2_deferral = w2_cap
        max_se_employee_deferral = min(se_cap, pool - max_w2_deferral)
    else:
        max_se_employee_deferral = se_cap
        max_w2_deferral = min(w2_cap, pool - max_se_employee_deferral)

    return {"max_w2_deferral": max_w2_deferral, "max_se_employee_deferral": max_se_employee_deferral}


def annual_additions_ceiling(tax_year: int, age_at_year_end: int, bracket_table: dict) -> float:
    """
    IRC §415(c)'s per-participant-per-plan annual-additions dollar ceiling (catch-up-adjusted) —
    the SAME statutory figure (`k401_limits.annual_additions_limit` + age-based catch-up) applies
    to any 401(k)-type plan a person has, whether that's a self-employed solo 401(k) or a W-2 job's
    401(k); §415(c) itself is plan-agnostic, it's the model's own bookkeeping (which plan's
    contributions to test against which ceiling) that's plan-specific, done by the caller. Shared
    here so `max_se_employer_contribution` and the W-2-side §415(c) check (`check_415c_limit`)
    never let this formula drift out of sync with each other.
    """
    limits = bracket_table[tax_year]["k401_limits"]
    ceiling = limits["annual_additions_limit"]
    if 60 <= age_at_year_end <= 63:
        ceiling += limits["catchup_60_to_63"]
    elif age_at_year_end >= 50:
        ceiling += limits["catchup_50_to_59"]
    return ceiling


def max_se_employer_contribution(
    tax_year: int,
    age_at_year_end: int,
    net_se_earnings_for_retirement: float,
    se_employee_deferral_used: float,
    bracket_table: dict,
) -> float:
    """
    The solo 401(k) employer (profit-sharing) contribution cap — the minimum of three separate
    constraints:

    1. The standard ~20%-of-net-SE-earnings sole-prop/LLC formula.
    2. Whatever §415(c) annual-additions room remains in that same plan after the SE employee
       deferral (if any) already used some of it — an employee deferral competes with the employer
       contribution for THIS per-plan limit, even though it does NOT compete with W-2 deferral for
       the separate, shared §402(g) pool (see allocate_employee_deferral). Catch-up (age-based) is
       added on top of the §415(c) limit before computing remaining room, not counted within it —
       `age_at_year_end` is required for exactly this reason.
    3. The ordinary "100% of compensation" rule: total employee + employer contributions to a solo
       401(k) can never exceed net SE earnings itself. This is the constraint that actually binds
       at low SE income — e.g. at $16,000 net SE profit (≈$14,869.64 net SE earnings for
       retirement), an employee deferral maxed at $14,869.64 leaves $0 of compensation room for an
       employer contribution, even though the 20%-formula and §415(c) constraints alone would both
       allow a nonzero figure. This constraint was missing from an early draft of the build spec —
       omitting it lets total modeled contributions exceed the person's actual net income, a real
       bug (see NEXT.md), not a rare edge case: it binds at exactly the income levels (part-time SE
       income, early-retirement years, side income) this model is used for most.
    """
    if tax_year not in bracket_table:
        raise ValueError(f"No contribution limits configured for {tax_year}")

    profit_sharing_formula_max = max(0.0, net_se_earnings_for_retirement) * 0.20
    ceiling = annual_additions_ceiling(tax_year, age_at_year_end, bracket_table)
    remaining_415c_room = ceiling - se_employee_deferral_used
    remaining_comp_room = net_se_earnings_for_retirement - se_employee_deferral_used

    return max(0.0, min(profit_sharing_formula_max, remaining_415c_room, remaining_comp_room))


def net_se_earnings_for_retirement(
    tax_year: int, w2_gross: float, se_net_profit: float, bracket_table: dict, pretax_health_dental: float = 0.0
) -> float:
    """
    "Net SE earnings for retirement purposes" — net SE profit minus half the resulting SE tax
    deduction — the comp basis used throughout the 401(k)-family functions (allocate_employee_
    deferral, max_se_employer_contribution) and by compute_taxes itself (steps 3-4), computed the
    exact same way here. Extracted as its own pure function so a caller that needs this figure
    BEFORE calling compute_taxes — e.g. to resolve/clamp a year's contribution inputs against caps
    up front, see modules/projection.py — gets the exact same number compute_taxes would derive
    internally, rather than approximating with raw se_net_profit (which would let a clamp based on
    this figure disagree with compute_taxes's own stricter internal check and still raise).
    compute_taxes's own returned `net_se_earnings_for_retirement` field is the authoritative value
    for a year that's actually been run through compute_taxes; this function exists only for the
    "need it before that" case.
    """
    if tax_year not in bracket_table:
        raise ValueError(f"No bracket data for tax year {tax_year} in bracket_table")
    fica = bracket_table[tax_year]["fica"]
    fica_wages = w2_gross - pretax_health_dental
    se_tax_base = se_net_profit * fica["se_net_earnings_factor"]
    ss_headroom = max(0.0, fica["ss_wage_base"] - fica_wages)
    ss_portion_taxed = min(se_tax_base, ss_headroom)
    se_tax = ss_portion_taxed * fica["se_ss_rate"] + se_tax_base * fica["se_medicare_rate"]
    se_tax_deduction = se_tax / 2
    return se_net_profit - se_tax_deduction


def compute_max_401k_contributions(
    tax_year: int,
    age_at_year_end: int,
    year_gross_w2: float,
    year_net_se_earnings_for_retirement: float,
    bracket_table: dict,
) -> dict:
    """
    The fixed priority order for "fill every 401(k)-family bucket to its legal maximum" — built for
    an optimization spec (a Downloads-folder doc, not checked into this repo — see NEXT.md) covering
    the Projection tab's "Use Computed Maximum" checkbox and the Tax tab's max-contribution display,
    so both surfaces show identically-computed numbers (the spec's own explicit instruction: reuse
    the existing priority logic, don't reimplement a parallel model).

    The spec's own prose lists the priority as (1) W-2 deferral maxed first, (2) SE employer
    contribution maxed SECOND, (3) SE employee deferral maxed THIRD. That literal ORDER is not used
    here — computing the employer contribution before the employee deferral is known would
    reintroduce the exact bug this project already found and fixed (see
    max_se_employer_contribution's own docstring and NEXT.md's "$16,000 net SE profit" entry): the
    employer cap must be computed with the employee deferral already known, or the two together can
    exceed §415(c)/the 100%-of-comp rule. This function computes the SE employee deferral BEFORE the
    SE employer contribution instead — every bucket still ends up at its true legal maximum ("W-2
    first" is still honored, nothing here reduces any bucket below what the spec intends), only the
    internal computation order changes, specifically to avoid recreating an already-fixed
    correctness bug. Flagged explicitly rather than silently reordered — see NEXT.md.

    Roth 401(k) is deliberately NOT allocated any amount here — the spec's own text says a future
    pretax/Roth split within the shared §402(g) bucket is a "within-bucket allocation choice, not an
    additional limit," and defaulting entirely to pretax is "acceptable for now." A caller that wants
    a Roth split makes it themselves, downstream of this function's `max_w2_employee_deferral`.

    Always uses "w2-first" priority — the spec's checkbox behavior is a FIXED order, not the Tax
    tab's freely-choosable `deferral_priority` dropdown (see allocate_employee_deferral).
    """
    combined_pool = max_combined_employee_deferral(tax_year, age_at_year_end, bracket_table)
    allocation = allocate_employee_deferral(
        tax_year, age_at_year_end, year_gross_w2, year_net_se_earnings_for_retirement, "w2-first", bracket_table
    )
    max_w2_employee_deferral = allocation["max_w2_deferral"]
    max_se_employee_deferral = allocation["max_se_employee_deferral"]
    max_se_employer = max_se_employer_contribution(
        tax_year, age_at_year_end, year_net_se_earnings_for_retirement, max_se_employee_deferral, bracket_table
    )
    return {
        "combined_employee_deferral_pool": combined_pool,
        "max_w2_employee_deferral": max_w2_employee_deferral,
        "max_se_employee_deferral": max_se_employee_deferral,
        "max_se_employer_contribution": max_se_employer,
    }


def max_ira_contribution_limit(tax_year: int, age_at_year_end: int, bracket_table: dict) -> float:
    """
    IRC §219(b)/§408A(c)(2): ONE combined annual dollar limit shared by Traditional AND Roth IRA
    contributions together — not two separate limits (see check_ira_combined_limit below). Catch-up
    (age 50+) is one flat add-on, no further age tier (unlike §402(g)'s 50-59/60-63 split).
    """
    if tax_year not in bracket_table:
        raise ValueError(f"No contribution limits configured for {tax_year}")
    limits = bracket_table[tax_year]["ira_limits"]
    limit = limits["contribution_limit"]
    if age_at_year_end >= 50:
        limit += limits["catchup_50"]
    return limit


def roth_ira_magi(federal_agi: float) -> float:
    """
    MAGI for Roth IRA contribution-eligibility purposes, per the IRS worksheet, starts from federal
    AGI and adds back a handful of items (foreign earned income/housing exclusions, excluded
    savings-bond interest, excluded adoption benefits) — none of which this model computes as an
    input at all, so there is nothing to add back here: MAGI == federal AGI in this model's current
    scope. A named function (rather than every caller using `federal_agi` directly) so this
    simplification is documented in one place and is the obvious spot to extend if any add-back item
    is ever modeled.

    One real, DELIBERATE gap: this model also does not compute Traditional IRA deductibility or a
    Traditional-IRA-deduction reduction to AGI at all (`compute_taxes` has no such parameter) — so a
    real filer's actual MAGI could be lower than this figure if some of their Traditional IRA
    contribution would have been deductible. That makes this MAGI a conservative (high, i.e.
    understates the Roth room) estimate for phase-out purposes, not a rounding error — flagged in the
    UI, not silently absorbed.
    """
    return federal_agi


def roth_ira_phase_out_max(
    tax_year: int,
    filing_status: str,
    age_at_year_end: int,
    magi: float,
    bracket_table: dict,
) -> float:
    """
    IRS Roth IRA phase-out worksheet (Pub. 590-A Worksheet 2-2): full statutory limit (incl.
    catch-up) below the phase-out floor, $0 at or above the ceiling, linearly prorated between —
    rounded UP to the nearest $10, with a $200 floor whenever the raw (pre-rounding) result is > $0.
    `filing_status` must be 'single', 'hoh', or 'mfj' — MFS is out of scope (see FILING_STATUSES and
    data/tax_brackets.json's `ira_limits._note`: MFS's $0-$10,000 range is fixed by statute and this
    model has no 'mfs' filing status to hang it on, consistent with the rest of the tax module).
    """
    if tax_year not in bracket_table:
        raise ValueError(f"No contribution limits configured for {tax_year}")
    if filing_status not in ("single", "hoh", "mfj"):
        raise ValueError(
            f"roth_ira_phase_out_max does not support filing_status={filing_status!r} (MFS is out of scope)"
        )
    statutory_limit = max_ira_contribution_limit(tax_year, age_at_year_end, bracket_table)
    phase_out = bracket_table[tax_year]["ira_limits"]["roth_phase_out"][filing_status]
    lower, upper = phase_out["lower"], phase_out["upper"]

    if magi <= lower:
        return statutory_limit
    if magi >= upper:
        return 0.0

    reduction_ratio = (magi - lower) / (upper - lower)
    raw = statutory_limit * (1 - reduction_ratio)
    if raw <= 0:
        return 0.0
    rounded = math.ceil(raw / 10.0) * 10.0
    return max(rounded, 200.0)


def check_402g_limit(
    w2_401k_contribution: float,
    roth_401k_contribution: float,
    se_401k_employee_contribution: float,
    combined_employee_deferral_pool: float,
) -> str | None:
    """
    One pure function per limit type (per the optimization spec's own §7 "efficient validation"
    recommendation) — takes already-computed values, never recomputes limits internally, so it stays
    cheap enough to re-run per year on every edit without a full-projection recompute.
    """
    total = w2_401k_contribution + roth_401k_contribution + se_401k_employee_contribution
    if total > combined_employee_deferral_pool + 1e-6:
        over = total - combined_employee_deferral_pool
        return (
            f"Combined 401(k)-family elective deferrals (${total:,.0f}) exceed the §402(g) limit "
            f"(${combined_employee_deferral_pool:,.0f}) by ${over:,.0f}."
        )
    return None


def check_415c_limit(
    employee_contribution: float,
    employer_contribution: float,
    annual_additions_ceiling: float,
    plan_label: str = "SE solo 401(k)",
) -> str | None:
    """
    §415(c) is a PER-PLAN limit — one plan's own annual-additions ceiling, covering only THAT
    plan's employee + employer contributions together. Deliberately never combined across plans: a
    W-2 job's 401(k) and a self-employed solo 401(k) are different, unrelated plans, each with its
    OWN separate §415(c) ceiling — call this once per plan (`plan_label` distinguishes the warning
    text; the numeric ceiling itself, `annual_additions_ceiling`, is identical statutory logic for
    either plan). An optimization spec's own draft wording for this check ("all four 401(k)-family
    fields combined" against one shared ceiling) would incorrectly warn on a real, common,
    fully-compliant scenario — a maxed-out W-2 plan plus a separately near-maxed SE solo plan — so
    it was implemented per-plan instead; documented here rather than reproducing that error
    silently (see NEXT.md).

    `plan_label` defaults to "SE solo 401(k)" — this function originally only ever checked that one
    plan; the W-2 plan's own employer-match-vs-§415(c) check (MODEL_WIRING.md §3.1/§4.2,
    2026-08-10) is the first caller to pass `plan_label="W-2 401(k)"` instead.
    """
    total = employee_contribution + employer_contribution
    if total > annual_additions_ceiling + 1e-6:
        over = total - annual_additions_ceiling
        return (
            f"{plan_label} annual additions (${total:,.0f}) exceed the §415(c) limit for that plan "
            f"(${annual_additions_ceiling:,.0f}) by ${over:,.0f}."
        )
    return None


def employer_401k_match(
    w2_gross: float, w2_employee_deferral: float, employer_match_rate: float, employer_match_cap_pct: float
) -> float:
    """
    MODEL_WIRING.md §3.1/§3.2 (2026-08-10) — the W-2 employer 401(k) match:
    `matched_deferral = min(employee_deferral, employer_match_cap_pct * w2_gross)`,
    `employer_match = employer_match_rate * matched_deferral`. `employer_match_rate` is dollars of
    match per employee dollar deferred (e.g. 0.50 = 50 cents on the dollar);
    `employer_match_cap_pct` caps how much of W-2 gross compensation is eligible for matching
    (e.g. 0.06 = matched only on the first 6% of pay deferred). Both default to 0.0 in the UI, so a
    user with no employer match gets exactly $0 here — see the property test asserting this.

    Three properties this model relies on elsewhere (checked by tests, stated here so a future edit
    doesn't accidentally violate one):
    1. This is EMPLOYER money, not employee money — it is never subtracted from `profit`
       (modules.projection's per-year cash-flow figure) and never counted toward the §402(g)
       employee-deferral limit (see check_402g_limit — this function's output is deliberately not
       one of that check's inputs).
    2. It DOES count toward the W-2 plan's own §415(c) annual-additions ceiling, alongside the
       employee's own W-2-side deferral — see check_415c_limit, called with
       `plan_label="W-2 401(k)"` for this plan specifically (a different, unrelated ceiling from
       the SE solo plan's own).
    3. It is always Traditional (pre-tax) in this model, even when `w2_employee_deferral` is Roth —
       the common real-world default; Roth-match plans (permitted post-SECURE 2.0) are out of
       scope. Since this model doesn't yet feed contributions into a portfolio ledger (Module D),
       this function's output is informational/limit-checking only for now — it does not reduce
       AGI (employer contributions were never in taxable wages to begin with, so there is nothing
       to deduct) and is not otherwise wired into `compute_taxes`.
    """
    matched_deferral = min(max(0.0, w2_employee_deferral), max(0.0, employer_match_cap_pct) * max(0.0, w2_gross))
    return max(0.0, employer_match_rate) * matched_deferral


def check_roth_ira_limit(roth_ira_contribution: float, roth_ira_phase_out_max_value: float) -> str | None:
    """Roth IRA contribution vs. that year's income-phased maximum (see roth_ira_phase_out_max) —
    not just the flat statutory limit."""
    if roth_ira_contribution > roth_ira_phase_out_max_value + 1e-6:
        over = roth_ira_contribution - roth_ira_phase_out_max_value
        return (
            f"Roth IRA contribution (${roth_ira_contribution:,.0f}) exceeds the income-phased maximum "
            f"for this year (${roth_ira_phase_out_max_value:,.0f}) by ${over:,.0f}."
        )
    return None


def check_ira_combined_limit(
    traditional_ira_contribution: float, roth_ira_contribution: float, combined_ira_limit: float
) -> str | None:
    """
    IRC §219(b): Traditional + Roth IRA contributions share ONE combined annual limit — this is the
    only IRA-vs-401(k)-family-adjacent cross-check that actually exists in the tax code. There is NO
    statutory limit combining IRA contributions with 401(k)-family contributions (an optimization
    spec raised exactly this as an open question — "Traditional IRA + 401(k) too high" describes a
    limit that does not exist in IRS rules; building a warning for it would itself be a bug, per the
    spec's own caution — so no such function exists here; see NEXT.md).
    """
    total = traditional_ira_contribution + roth_ira_contribution
    if total > combined_ira_limit + 1e-6:
        over = total - combined_ira_limit
        return (
            f"Traditional + Roth IRA contributions (${total:,.0f}) exceed the combined annual IRA "
            f"limit (${combined_ira_limit:,.0f}) by ${over:,.0f}."
        )
    return None


# ---- Retirement withdrawals — bracket-aware draw + RMDs (Module G2 Step 7,
# MODULE_G2_STEP7_BRACKET_AWARE_WITHDRAWALS.md Part 1, 2026-08-23) ----


def ordinary_bracket_ceiling(brackets: list[list[float]], target_rate: float) -> float:
    """
    The taxable-income threshold at which `target_rate`'s bracket ENDS — the lower bound of the
    next bracket up. E.g. for federal single 2026 brackets, target_rate=0.22 -> $105,700 (the
    22%-bracket ceiling, where 24% begins). Returns `float('inf')` if `target_rate` is the top
    bracket (nothing to cap against). Raises `ValueError` if `target_rate` isn't one of `brackets`'
    own rates — a typo/mismatched-year bug should fail loudly here, not silently return an unrelated
    boundary.
    """
    for i, (_, rate) in enumerate(brackets):
        if abs(rate - target_rate) < 1e-9:
            return brackets[i + 1][0] if i + 1 < len(brackets) else float("inf")
    raise ValueError(f"target_rate {target_rate!r} is not one of this bracket table's own rates: {brackets}")


def rmd_start_age(birth_year: int) -> int:
    """SECURE 2.0's phased Required Minimum Distribution start age: 73 for anyone born 1951-1959,
    75 for 1960 and later (the 1960 cutoff is itself fixed by statute, not inflation-indexed —
    unrelated to this model's real-dollar convention)."""
    return 75 if birth_year >= 1960 else 73


_RMD_TABLE_PATH = Path(__file__).resolve().parent.parent / "data" / "rmd_table.json"


def load_rmd_table(path: Path = _RMD_TABLE_PATH) -> dict:
    """Reads data/rmd_table.json — the one I/O boundary for the RMD divisor table, mirroring
    `load_bracket_table`'s own pattern (one I/O boundary, pure functions consume the already-loaded
    dict)."""
    with open(path, "r") as f:
        return json.load(f)


def rmd_divisor(age: int, table: dict) -> float:
    """
    IRS Pub 590-B Table III (Uniform Lifetime Table) divisor for `age`. Ages past the table's last
    entry reuse that last divisor (the IRS table itself bottoms out and holds flat past age 120)
    rather than raising — an RMD must always be computable for any age this model might reach. Raises
    `ValueError` for an age below the table's first entry — nothing should ever call this for an age
    younger than `rmd_start_age` returns, so a below-table lookup is a real caller bug, not a case to
    paper over with a guessed divisor.
    """
    divisors = table["uniform_lifetime_table"]
    if str(age) in divisors:
        return divisors[str(age)]
    known_ages = [int(a) for a in divisors]
    last_age = max(known_ages)
    if age > last_age:
        return divisors[str(last_age)]
    raise ValueError(f"rmd_divisor: age {age} is below this table's first entry ({min(known_ages)})")


def _ss_taxability(provisional_income: float, ss_benefit_gross: float, tier1: float, tier2: float) -> float:
    """
    Two-tier Social Security taxability formula (IRS Pub. 915 Worksheet 1), fixed by statute since
    1984 — not inflation-indexed (see module docstring). `tier1`/`tier2` are the provisional-income
    thresholds where the 50%/85%-taxable tiers begin. The worksheet's fixed dollar caps ($4,500
    single/HoH, $6,000 MFJ) are mathematically identical to `0.5 * (tier2 - tier1)` for the
    thresholds in data/tax_brackets.json, so that's computed directly rather than hardcoded again.
    """
    if provisional_income <= tier1:
        return 0.0
    if provisional_income <= tier2:
        return min(0.5 * (provisional_income - tier1), 0.5 * ss_benefit_gross)
    tier1_amount = min(0.5 * (tier2 - tier1), 0.5 * ss_benefit_gross)
    taxable = tier1_amount + 0.85 * (provisional_income - tier2)
    return min(taxable, 0.85 * ss_benefit_gross)


def compute_taxes(
    tax_year: int,
    filing_status: str,
    age_at_year_end: int,
    w2_gross: float,
    pretax_401k: float,
    roth_401k: float,
    pretax_health_dental: float,
    se_net_profit: float,
    se_solo_employee_deferral: float,
    se_solo_employer_contribution: float,
    taxable_retirement_withdrawal: float,
    ltcg: float,
    qualified_dividends: float,
    ordinary_dividends: float,
    interest_income: float,
    ss_benefit_gross: float,
    bracket_table: dict,
    deferral_priority: str = "w2-first",
    ordinary_break_income: float = 0.0,
    short_term_gains: float = 0.0,
) -> dict:
    """
    Full federal + California + payroll tax breakdown for one simulation-year. Pure function: takes
    plain numbers and an already-loaded `bracket_table` (see `load_bracket_table`), returns a plain
    dict — no I/O, no globals. `roth_401k` is informational only for the payroll/AGI math (still
    subject to FICA, per step 2; never reduces federal/CA AGI) but — like `pretax_401k` — DOES count
    toward the shared §402(g) employee-deferral pool checked in step 4, since traditional and Roth
    elective deferrals are aggregated for that limit regardless of pretax/after-tax treatment.
    `taxable_retirement_withdrawal` (Traditional 401(k)/IRA distributions, RMDs, Roth conversions)
    is ordinary income for federal/CA brackets and for Social Security's provisional-income test,
    but is statutorily excluded from the NIIT base under IRC §1411 — see the NIIT step below; do not
    fold it into `interest_income` as a shortcut, that was a real bug in an earlier draft of this
    module (see NEXT.md). `ordinary_break_income` (e.g. unemployment insurance, a stipend — see the
    gross-income projection module) gets the exact same treatment for the exact same structural
    reason: ordinary income everywhere, but no payroll/SE tax (it's neither wages nor SE profit) and
    no NIIT (not investment income) — a distinct parameter, not folded into `interest_income` or
    reused from `taxable_retirement_withdrawal`, since those are conceptually different income
    sources a future UI may want to show broken out separately. See the module docstring and the
    build spec for scope and the 16-step calculation order this follows exactly.

    `short_term_gains` (MODEL_WIRING.md §5.2, 2026-08-10 — realized gains on a lot held under a
    year, from selling a tax lot to fund dissaving or a retirement withdrawal): taxed at full
    ORDINARY federal/CA rates (unlike `ltcg`, never stacked at preferential rates — it is never
    subtracted out of `ordinary_taxable` below, so it stays in the ordinary bracket fill by
    construction), but — like `ordinary_dividends`, which it otherwise mirrors in every other
    respect — IS genuinely investment income for NIIT purposes (unlike `taxable_retirement_
    withdrawal`/`ordinary_break_income`, which are ordinary income everywhere but statutorily
    excluded from NIIT) and is NOT wages or SE income, so it never touches FICA/SE tax or the
    Additional Medicare Tax base either. A distinct parameter rather than folding it into
    `ordinary_dividends` as a shortcut — same "streams stay separate" rule this module already
    follows for `taxable_retirement_withdrawal` and `ordinary_break_income` (MODEL_WIRING.md §6).

    401(k)/solo 401(k) contribution caps (step 4) — see max_combined_employee_deferral,
    allocate_employee_deferral, and max_se_employer_contribution, and 401k_contribution_module_spec
    (referenced in NEXT.md) for the full rules. In one sentence: `pretax_401k + roth_401k` (the W-2
    side) and `se_solo_employee_deferral` (the SE side) share ONE combined annual §402(g) dollar
    limit; `se_solo_employer_contribution` (profit-sharing) does NOT touch that shared limit at all
    — it's bounded separately, per-plan, by §415(c), which the SE employee deferral (but not the W-2
    deferral) also draws from.
    """
    if filing_status not in FILING_STATUSES:
        raise ValueError(f"filing_status must be one of {FILING_STATUSES}, got {filing_status!r}")
    if tax_year not in bracket_table:
        raise ValueError(f"No bracket data for tax year {tax_year} in bracket_table")
    t = bracket_table[tax_year]

    # 1. Wages for tax purposes (Roth 401(k) does NOT reduce this — only pretax deferrals do).
    w2_taxable_wages = w2_gross - pretax_401k - pretax_health_dental

    # 2. FICA wages (both traditional AND Roth 401(k) deferrals are still subject to FICA — only
    # cafeteria-plan premiums escape it).
    fica_wages = w2_gross - pretax_health_dental

    # Standard employee-side FICA withholding on W-2 wages. Distinct from the SE/SECA calculation
    # in step 3 below — both appear separately in the returned "Payroll" fields.
    fica = t["fica"]
    social_security_tax = min(fica_wages, fica["ss_wage_base"]) * fica["ss_rate"]
    medicare_tax = fica_wages * fica["medicare_rate"]

    # 3. SE tax (SECA). 12.4% applies only up to whatever Social Security wage-base headroom is
    # left after W-2 wages; 2.9% Medicare applies to the full SE tax base, uncapped.
    se_tax_base = se_net_profit * fica["se_net_earnings_factor"]
    ss_headroom = max(0.0, fica["ss_wage_base"] - fica_wages)
    ss_portion_taxed = min(se_tax_base, ss_headroom)
    se_tax = ss_portion_taxed * fica["se_ss_rate"] + se_tax_base * fica["se_medicare_rate"]
    se_tax_deduction = se_tax / 2  # above-the-line deduction, not the SE tax itself

    # 4. 401(k)/solo 401(k) contribution caps. "Net SE earnings for retirement purposes" — net
    # profit minus half the SE tax deduction — is the comp basis for both the SE side of the
    # shared employee-deferral pool and the employer contribution cap; see the module docstring.
    net_se_earnings_for_retirement = se_net_profit - se_tax_deduction
    w2_employee_deferral = pretax_401k + roth_401k  # both count toward the shared §402(g) pool
    combined_employee_deferral_pool = max_combined_employee_deferral(tax_year, age_at_year_end, bracket_table)
    deferral_allocation = allocate_employee_deferral(
        tax_year, age_at_year_end, w2_gross, net_se_earnings_for_retirement, deferral_priority, bracket_table
    )
    max_w2_employee_deferral = deferral_allocation["max_w2_deferral"]
    max_se_employee_deferral = deferral_allocation["max_se_employee_deferral"]
    if w2_employee_deferral > max_w2_employee_deferral + 1e-6:
        raise ValueError(
            f"pretax_401k + roth_401k ({w2_employee_deferral}) exceeds the max W-2 employee "
            f"deferral ({max_w2_employee_deferral}) allowed under the shared §402(g) pool for "
            f"age_at_year_end={age_at_year_end}, deferral_priority={deferral_priority!r}"
        )
    if se_solo_employee_deferral > max_se_employee_deferral + 1e-6:
        raise ValueError(
            f"se_solo_employee_deferral ({se_solo_employee_deferral}) exceeds the max SE employee "
            f"deferral ({max_se_employee_deferral}) allowed under the shared §402(g) pool for "
            f"age_at_year_end={age_at_year_end}, deferral_priority={deferral_priority!r}"
        )

    se_solo_employer_contribution_max = max_se_employer_contribution(
        tax_year, age_at_year_end, net_se_earnings_for_retirement, se_solo_employee_deferral, bracket_table
    )
    if se_solo_employer_contribution > se_solo_employer_contribution_max + 1e-6:
        raise ValueError(
            f"se_solo_employer_contribution ({se_solo_employer_contribution}) exceeds "
            f"se_solo_employer_contribution_max ({se_solo_employer_contribution_max}) for "
            f"net_se_earnings_for_retirement={net_se_earnings_for_retirement}"
        )

    # 5. QBI deduction (federal only — CA does not allow it). Both the SE employee deferral and the
    # employer contribution reduce the QBI base, matching real Schedule 1/Form 8995 treatment —
    # qualified-plan contributions attributable to the business reduce QBI regardless of which side
    # of the deferral/employer-contribution split they're on.
    qbi_base = se_net_profit - se_tax_deduction - se_solo_employee_deferral - se_solo_employer_contribution
    qbi_deduction = _qbi_deduction(qbi_base, filing_status, t["qbi_thresholds"])

    # The self-employed retirement plan deduction (Schedule 1, "Self-employed SEP, SIMPLE, and
    # qualified plans") is its OWN above-the-line deduction, separate from — and in addition to —
    # its effect on the QBI base just above. It reduces AGI directly (steps 6/7/13 below), on both
    # federal and CA returns: CA generally conforms to this deduction, unlike QBI, which is
    # federal-only and CA never adopted. `se_solo_employee_deferral` is assumed traditional/pretax
    # here (the build spec has no Roth-vs-traditional split for the SE side, unlike the W-2 side's
    # separate `pretax_401k`/`roth_401k`) — flagged as a simplification, not silently assumed.
    se_retirement_deduction = se_solo_employee_deferral + se_solo_employer_contribution

    # 6. Social Security taxability (federal only; CA never taxes SS benefits). Retirement
    # withdrawals and break income are both ordinary income and count toward provisional income.
    provisional_income = (
        w2_taxable_wages
        + se_net_profit
        - se_tax_deduction
        - se_retirement_deduction
        + taxable_retirement_withdrawal
        + ordinary_break_income
        + interest_income
        + ordinary_dividends
        + short_term_gains
        + qualified_dividends
        + ltcg
        + 0.5 * ss_benefit_gross
    )
    ss_tier = t["ss_taxability_thresholds"][filing_status]
    ss_taxable_amount = _ss_taxability(provisional_income, ss_benefit_gross, ss_tier["tier1"], ss_tier["tier2"])

    # 7. Federal AGI. Retirement withdrawals and break income are ordinary income here too.
    federal_agi = (
        w2_taxable_wages
        + se_net_profit
        - se_tax_deduction
        - se_retirement_deduction
        + taxable_retirement_withdrawal
        + ordinary_break_income
        + interest_income
        + ordinary_dividends
        + short_term_gains
        + qualified_dividends
        + ltcg
        + ss_taxable_amount
    )

    # 8. Federal taxable income.
    federal_taxable_income = max(0.0, federal_agi - t["standard_deduction"][filing_status] - qbi_deduction)

    # 9. Ordinary bracket fill: the non-preferential portion fills the brackets first. This can be
    # negative (deductions exceeding ordinary income) — see _stacked_bracket_tax's docstring for
    # why that's handled correctly (not just clamped away) in the LTCG stacking step below.
    ordinary_taxable = federal_taxable_income - qualified_dividends - ltcg
    federal_ordinary_tax = _progressive_tax(ordinary_taxable, t["federal_brackets"][filing_status])

    # 10. LTCG/QDI stacks on top, using federal_taxable_income as the top of the stack.
    ltcg_qdi_amount = qualified_dividends + ltcg
    federal_ltcg_tax = _stacked_bracket_tax(ordinary_taxable, ltcg_qdi_amount, t["ltcg_brackets"][filing_status])

    # 11. NIIT — fixed threshold since 2013, not inflation-indexed (see module docstring).
    # `taxable_retirement_withdrawal` and `ordinary_break_income` are deliberately NOT part of net
    # investment income: neither is statutorily investment income (retirement distributions are
    # excluded from the NIIT base under IRC §1411; break income like UI benefits or a stipend was
    # never investment income to begin with), even though both are ordinary income everywhere else
    # in this function. Do not add either to this line.
    net_investment_income = interest_income + ordinary_dividends + short_term_gains + qualified_dividends + ltcg
    niit_threshold = t["niit_threshold"][filing_status]
    niit = 0.038 * max(0.0, min(net_investment_income, federal_agi - niit_threshold))

    # 12. Additional Medicare Tax — wages and SE income share ONE threshold (fixed since 2013).
    # `taxable_retirement_withdrawal` and `ordinary_break_income` are excluded here too (neither is
    # wages or SE income).
    addl_medicare_threshold = t["addl_medicare_threshold"][filing_status]
    additional_medicare_tax = 0.009 * max(0.0, fica_wages + se_tax_base - addl_medicare_threshold)

    # 13. CA taxable income — no QBI, no LTCG preferential rate, no SS taxation. Retirement
    # withdrawals and break income are ordinary income here too (CA has no NIIT-style carve-out for
    # either). CA DOES conform to the self-employed retirement plan deduction (unlike QBI), so it's
    # subtracted here too — see se_retirement_deduction's definition above.
    ca_taxable_income = max(
        0.0,
        w2_taxable_wages
        + se_net_profit
        - se_tax_deduction
        - se_retirement_deduction
        + taxable_retirement_withdrawal
        + ordinary_break_income
        + interest_income
        + ordinary_dividends
        + short_term_gains
        + qualified_dividends
        + ltcg
        - t["ca_standard_deduction"][filing_status],
    )

    # 14. CA tax — capital gains taxed as ordinary income in CA, plus 1% Mental Health Services Tax
    # on CA taxable income over $1,000,000.
    ca_tax_before_mhst = _progressive_tax(ca_taxable_income, t["ca_brackets"][filing_status])
    mhst = t["ca_mhst_rate"] * max(0.0, ca_taxable_income - t["ca_mhst_threshold"])
    ca_tax_total = ca_tax_before_mhst + mhst

    # 15. CASDI — uncapped, no wage base.
    casdi = t["casdi_rate"] * fica_wages

    federal_tax_total = federal_ordinary_tax + federal_ltcg_tax + niit + additional_medicare_tax

    # 16. Total tax.
    total_tax = (
        federal_ordinary_tax
        + federal_ltcg_tax
        + niit
        + additional_medicare_tax
        + ca_tax_total
        + social_security_tax
        + medicare_tax
        + se_tax
        + casdi
    )

    gross_income = (
        w2_gross
        + se_net_profit
        + taxable_retirement_withdrawal
        + ordinary_break_income
        + ltcg
        + short_term_gains
        + qualified_dividends
        + ordinary_dividends
        + interest_income
        + ss_benefit_gross
    )
    effective_rate_on_gross = total_tax / gross_income if gross_income > 0 else 0.0
    marginal_federal_rate = _marginal_rate(ordinary_taxable, t["federal_brackets"][filing_status])

    return {
        # Federal
        "federal_agi": federal_agi,
        "federal_taxable_income": federal_taxable_income,
        "federal_ordinary_tax": federal_ordinary_tax,
        "federal_ltcg_tax": federal_ltcg_tax,
        "niit": niit,
        "additional_medicare_tax": additional_medicare_tax,
        "federal_tax_total": federal_tax_total,
        "qbi_deduction": qbi_deduction,
        "ss_taxable_amount": ss_taxable_amount,
        # California
        "ca_taxable_income": ca_taxable_income,
        "ca_tax_before_mhst": ca_tax_before_mhst,
        "mhst": mhst,
        "ca_tax_total": ca_tax_total,
        # Payroll
        "social_security_tax": social_security_tax,
        "medicare_tax": medicare_tax,
        "se_tax": se_tax,
        "casdi": casdi,
        # Module F (Social Security AIME, 2026-08-31) — this year's SS-taxable earnings (W-2 +
        # SE, wage-base-capped, W-2 counted first so the SE side's own headroom already avoids
        # double-counting the cap — exactly the same `fica_wages`/`ss_portion_taxed` this
        # function already computed above for payroll tax purposes, just returned rather than
        # only consumed internally). This is the one number a real SSA earnings record shows per
        # year; `modules.social_security.compute_aime` sums the 35 highest such years (this one
        # for future/projected years, a new "Social Security" tab's own manual entry for
        # historical ones) to build AIME. No separate wage-indexing step needed here — this
        # model's whole real-dollar convention already does what SSA's National Average Wage
        # Index indexing does in the nominal-dollar version of this calculation.
        "ss_taxable_earnings": min(fica_wages, fica["ss_wage_base"]) + ss_portion_taxed,
        # 401(k) / solo 401(k)
        "combined_employee_deferral_pool": combined_employee_deferral_pool,
        "max_w2_employee_deferral": max_w2_employee_deferral,
        "max_se_employee_deferral": max_se_employee_deferral,
        "se_solo_employer_contribution_max": se_solo_employer_contribution_max,
        "net_se_earnings_for_retirement": net_se_earnings_for_retirement,
        # Summary
        "total_tax": total_tax,
        "effective_rate_on_gross": effective_rate_on_gross,
        "marginal_federal_rate": marginal_federal_rate,
    }
