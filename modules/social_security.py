"""
Module F — Social Security (AIME, bend points, claiming-age adjustment), 2026-08-31.

Pure functions only: no Streamlit calls, no I/O beyond `load_bend_point_table`'s own single
boundary. See CLAUDE.md ground rule 4. Computes the ONE number `modules.tax.compute_taxes`'s
already-correct benefit-taxation math has always needed and never received: `ss_benefit_gross`.
Nothing in the tax engine itself changes here — see NEXT.md's own framing for this module ("the
good news: benefit TAXATION is already fully built").

**Real-dollar convention, stated once, applies throughout this module**: this app works in
inflation-adjusted (today's) dollars end to end. SSA's own AIME calculation indexes each historical
year's NOMINAL earnings to age-60-equivalent dollars via the National Average Wage Index before
averaging. Since every earnings figure feeding `compute_aime` here — historical (a new "Social
Security" tab's own manual entry) and future/projected (`compute_taxes`'s own new
`ss_taxable_earnings` field) alike — is ALREADY in today's real dollars, that indexing step is
already done implicitly; a second, separate indexing pass here would double-apply it. Bend points
(`data/ss_bend_points.json`) are the one piece of real published nominal-dollar data this module
reads from an external, year-keyed source — held flat in REAL terms for any year beyond the last
one configured (`bend_points_for_year`), the same convention `modules.tax`'s own bracket tables use
for tax years beyond their last configured entry.
"""

from __future__ import annotations

import calendar
import json
import math
from datetime import date
from pathlib import Path

from modules.demographics import months_between

_BEND_POINTS_PATH = Path(__file__).resolve().parent.parent / "data" / "ss_bend_points.json"

# 20 CFR § 404.410 — early retirement reduces the benefit by 5/9 of 1% per month for the first 36
# months claimed before Full Retirement Age (FRA), then 5/12 of 1% per month for each month beyond
# that first 36.
_EARLY_REDUCTION_RATE_FIRST_36_MONTHS = 5.0 / 9.0 / 100.0
_EARLY_REDUCTION_RATE_BEYOND_36_MONTHS = 5.0 / 12.0 / 100.0

# ssa.gov/benefits/retirement/planner/delayret.html — the delayed-retirement-credit rate has been
# 8%/year (2/3 of 1%/month) for anyone born 1943 or later since the credit reached its modern,
# fully-phased-in form; earlier birth years had a narrower, since-superseded step schedule. Anyone
# using this app in practice is 1943+ (verified live against the SSA page, 2026-08-31) — the
# pre-1943 table is included for completeness/correctness on an old birth date, not because it's
# expected to matter.
_DELAYED_RETIREMENT_CREDIT_RATES_PRE_1943 = {
    1933: 0.055, 1934: 0.055,
    1935: 0.060, 1936: 0.060,
    1937: 0.065, 1938: 0.065,
    1939: 0.070, 1940: 0.070,
    1941: 0.075, 1942: 0.075,
}


def load_bend_point_table(path: Path = _BEND_POINTS_PATH) -> dict:
    """
    Reads data/ss_bend_points.json -- the SSA-published PIA bend points, keyed by year of
    eligibility (see that file's own `_note` for the exact source/retrieval date) -- and returns
    `{year: {"bend_point_1": float, "bend_point_2": float}}` with year keys converted to `int`
    (the file stores them as JSON string keys). This is the module's one I/O boundary, mirroring
    `modules.tax.load_bracket_table`/`load_rmd_table`/`modules.health.load_mortality_table`'s own
    pattern -- every function below takes the already-loaded table as a plain argument.
    """
    with open(path, "r") as f:
        raw = json.load(f)
    return {int(year): bp for year, bp in raw.items() if year != "_note"}


def _bend_point_entry_for_year(year: int, bend_point_table: dict) -> dict:
    """
    Which `bend_point_table` year's own entry to actually use for `year`: the latest configured
    year at or before it (held flat in REAL terms beyond that, per this module's own top-of-file
    docstring), or the earliest configured year if `year` predates every entry (defensive -- SSA's
    own table starts in 1979, long before anyone using this app today could have a real eligibility
    year that early). Mirrors `modules.projection._bracket_year_for`'s exact "latest at-or-before,
    else earliest" policy. Raises if `bend_point_table` is empty -- there is no sensible fallback
    for that. The shared lookup behind both `bend_points_for_year` and `qc_threshold_for_year`
    below (each year's entry carries both `bend_point_1`/`bend_point_2` and `qc_threshold`, sourced
    together in `data/ss_bend_points.json`) -- one lookup, not two independently-duplicated ones.
    """
    if not bend_point_table:
        raise ValueError("bend_point_table is empty -- no bend-point data configured at all")
    available_years = sorted(bend_point_table.keys())
    candidates = [y for y in available_years if y <= year]
    resolved_year = candidates[-1] if candidates else available_years[0]
    return bend_point_table[resolved_year]


def bend_points_for_year(eligibility_year: int, bend_point_table: dict) -> tuple[float, float]:
    """The (bend_point_1, bend_point_2) pair for `eligibility_year` -- see
    `_bend_point_entry_for_year` for the lookup policy."""
    entry = _bend_point_entry_for_year(eligibility_year, bend_point_table)
    return entry["bend_point_1"], entry["bend_point_2"]


def qc_threshold_for_year(year: int, bend_point_table: dict) -> float:
    """
    The earnings needed for one quarter of coverage ("QC," a.k.a. Social Security credit) in
    `year` -- see `_bend_point_entry_for_year` for the lookup policy (same table, same held-flat-
    beyond-the-last-configured-year convention). Used by `quarters_of_coverage`/`is_fully_insured`
    below (Module F's "fully insured" eligibility gate, 2026-08-31) -- a SINGLE resolved threshold
    applied flat across every year of a real earnings record, not a per-historical-year lookup
    (this module's own real-dollar convention already makes that the correct treatment, same
    simplification already accepted for the SS wage base itself).
    """
    return _bend_point_entry_for_year(year, bend_point_table)["qc_threshold"]


def earnings_test_exempt_amounts_for_year(year: int, bend_point_table: dict) -> tuple[float, float]:
    """
    `(lower_exempt, higher_exempt)` for calendar YEAR `year` DIRECTLY -- see `data/ss_bend_points.
    json`'s own `_note` for why this is a real, important semantic difference from `bend_points_
    for_year`/`qc_threshold_for_year` just below (both looked up by ELIGIBILITY year, i.e.
    `ssa_effective_birth_year(birth_date) + 62`): the Retirement Earnings Test's exempt amount
    applies to the SPECIFIC calendar year earnings are counted in, not the year someone became
    eligible to claim.

    `earnings_test_lower_exempt`/`earnings_test_higher_exempt` only exist starting 2026 (the year
    this feature shipped) -- most of `data/ss_bend_points.json`'s own 1979-2026 range predates it,
    since a real caller only ever evaluates a current-or-future projected year, never a historical
    one. Held flat beyond the LAST YEAR THAT ACTUALLY DEFINES these two fields (reusing
    `_bend_point_entry_for_year`'s own "latest at or before, else earliest" policy, scoped to just
    that subset of years) -- not `_bend_point_entry_for_year(year, bend_point_table)` directly,
    which would return an earlier year's entry lacking these keys entirely for any `year` before
    the first one that defines them.
    """
    years_with_earnings_test_data = {
        y: entry for y, entry in bend_point_table.items() if "earnings_test_lower_exempt" in entry
    }
    entry = _bend_point_entry_for_year(year, years_with_earnings_test_data)
    return entry["earnings_test_lower_exempt"], entry["earnings_test_higher_exempt"]


def retirement_earnings_test_reduction(
    gross_w2: float, gross_se: float, birth_date: date, year: int, bend_point_table: dict
) -> float:
    """
    The Retirement Earnings Test (RET, ssa.gov/benefits/retirement/planner/whileworking.html,
    verified live 2026-09-06) — the dollar amount to WITHHOLD from a benefit actually being claimed
    in `year`, for claiming before Full Retirement Age (FRA) while still earning. `$0` once `year`
    is STRICTLY AFTER `full_retirement_date(birth_date).year` — from the year after FRA is reached
    onward there is no earnings test at all, regardless of earnings. The FRA year itself still
    applies a reduction (see the higher-threshold tier below) — "no earnings test... from the MONTH
    FRA is reached onward" (the real, month-precise rule) still leaves the months before FRA within
    that same year subject to it, which this model approximates across the whole FRA year — see the
    documented simplification below.

    Countable earnings are `gross_w2 + gross_se` ONLY — the RET explicitly does NOT count
    investment income, interest, pensions, annuities, or other government/military retirement
    benefits, i.e. never `gross_investment_income`.

    Two tiers, both already resolved for `year` by `earnings_test_exempt_amounts_for_year` above:
    - Every full year STRICTLY BEFORE the FRA year: the LOWER exempt amount, $1 withheld per $2
      earned above it.
    - The FRA year itself: the HIGHER exempt amount, $1 withheld per $3 earned above it.

    **A documented, flagged simplification, not silently wrong**: the real rule only counts
    earnings BEFORE THE MONTH FRA is reached against the higher threshold in the FRA year itself —
    this model has no sub-annual earnings attribution today (`gross_w2`/`gross_se` are annual
    totals), so applying the higher threshold to the FULL year's earnings UNDERSTATES the true
    reduction in the FRA year specifically (the real SSA rule would apply an even smaller effective
    earnings base, since only pre-FRA-month earnings count against it at all). A future precise fix
    would day-weight `gross_w2 + gross_se` up to `full_retirement_date` the same way other phase
    boundaries in this app already day-weight income — not attempted here.

    **Also a documented, flagged gap, recommended out of scope for this build**: SSA later
    recalculates the benefit to credit back whatever was withheld once FRA is reached — this
    withholding is a delayed-payment mechanism, not a permanent loss. Modeling the credit-back
    correctly requires tracking cumulative withheld dollars per claimant and increasing the benefit
    from FRA onward, a materially larger feature than the withholding rule itself — see
    `ui/social_security_tab.py`'s own transparency caption, which states this caveat plainly
    wherever this reduction is shown.
    """
    fra_year = full_retirement_date(birth_date).year
    if year > fra_year:
        return 0.0

    countable_earnings = gross_w2 + gross_se
    lower_exempt, higher_exempt = earnings_test_exempt_amounts_for_year(year, bend_point_table)
    if year == fra_year:
        exempt_amount, withhold_ratio = higher_exempt, 3.0
    else:
        exempt_amount, withhold_ratio = lower_exempt, 2.0
    excess = max(0.0, countable_earnings - exempt_amount)
    return excess / withhold_ratio


def compute_aime(earnings_by_year: dict[int, float]) -> float:
    """
    Average Indexed Monthly Earnings: the 35 HIGHEST values in `earnings_by_year` (a merged
    `{year: ss_taxable_earnings}` record -- historical years from a new "Social Security" tab's own
    manual entry, future/projected years from `modules.tax.compute_taxes`'s own `ss_taxable_
    earnings` field), summed, divided by 420 (35 years x 12 months).

    **Fewer than 35 real working years still divides by 420, never by a smaller count** -- the
    single most common real-world AIME mistake to avoid: the "missing" years are $0 SS-creditable
    earnings, not years that don't count at all. `sorted(...)[:35]` on a dict with fewer than 35
    entries already produces exactly this behavior (just returns every value there is, no error),
    so no special-casing is needed here -- but it's exactly why this comment, and this function's
    own dedicated test for it, both exist.

    No separate wage-indexing step (see this module's own top-of-file docstring for why this app's
    real-dollar convention already does what SSA's National Average Wage Index indexing does).
    """
    top_35 = sorted(earnings_by_year.values(), reverse=True)[:35]
    return sum(top_35) / 420.0


def bend_point_pia(aime: float, bend_point_1: float, bend_point_2: float) -> float:
    """
    The standard SSA Primary Insurance Amount (PIA) formula: 90% of AIME up to the first bend
    point, 32% of AIME between the two bend points, 15% of AIME above the second bend point.
    `aime` and both bend points are MONTHLY dollar figures (matching `compute_aime`'s own /420
    convention and `data/ss_bend_points.json`'s own monthly-dollar values) -- this returns a
    MONTHLY PIA; `annual_ss_benefit` below multiplies by 12 for the annual figure `compute_taxes`
    actually needs.
    """
    return (
        0.90 * min(aime, bend_point_1)
        + 0.32 * max(0.0, min(aime, bend_point_2) - bend_point_1)
        + 0.15 * max(0.0, aime - bend_point_2)
    )


def full_retirement_age(birth_year: int) -> tuple[int, int]:
    """
    SSA's own Normal ("Full") Retirement Age table (ssa.gov/oact/progdata/nra.html, verified live
    2026-08-31), as `(years, months)`: 65 for 1937 and earlier; rising 2 months per birth year
    through 1942 (65y10m); flat at 66 for 1943-1954; rising 2 months per birth year again through
    1959 (66y10m); 67 for 1960 and later.

    `birth_year` here should already be the SSA-EFFECTIVE birth year (see `ssa_effective_birth_year`
    below) -- a person born January 1 is administratively treated as having been born in the PRIOR
    calendar year for this table's own purposes (the table's own published note 1).
    """
    if birth_year <= 1937:
        return (65, 0)
    if birth_year <= 1942:
        return (65, 2 * (birth_year - 1937))
    if birth_year <= 1954:
        return (66, 0)
    if birth_year <= 1959:
        return (66, 2 * (birth_year - 1954))
    return (67, 0)


def ssa_effective_birth_year(birth_date: date) -> int:
    """
    SSA's own administrative convention (ssa.gov/oact/progdata/nra.html note 1): a person is
    considered to attain any given age on the day BEFORE their actual birthday, so someone born
    January 1 attains that age on December 31 of the prior year -- pushing them, for every
    age/year-based SSA lookup in this module (the FRA table here, and the bend-point "year of
    eligibility" in `_eligibility_year` below), into the PRIOR birth year's row instead of their
    literal one. A no-op for every other birth date.
    """
    if birth_date.month == 1 and birth_date.day == 1:
        return birth_date.year - 1
    return birth_date.year


def _add_years_months(d: date, years: int, months: int) -> date:
    """Calendar-correct date arithmetic for a (years, months) offset -- clamps to the target
    month's own last day if the original day-of-month doesn't exist there (e.g. Jan 31 + 1 month)."""
    total_months = d.month - 1 + months
    year = d.year + years + total_months // 12
    month = total_months % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def full_retirement_date(birth_date: date) -> date:
    """The calendar date this person reaches Full Retirement Age -- `birth_date` offset by
    `full_retirement_age`'s own (years, months), looked up at the SSA-effective birth year."""
    years, months = full_retirement_age(ssa_effective_birth_year(birth_date))
    return _add_years_months(birth_date, years, months)


def _delayed_retirement_credit_annual_rate(birth_year: int) -> float:
    """See this module's own top-of-file rate-table comment for the source and the (practically
    immaterial) pre-1943 legacy schedule."""
    if birth_year >= 1943:
        return 0.08
    if birth_year in _DELAYED_RETIREMENT_CREDIT_RATES_PRE_1943:
        return _DELAYED_RETIREMENT_CREDIT_RATES_PRE_1943[birth_year]
    return _DELAYED_RETIREMENT_CREDIT_RATES_PRE_1943[1933]  # defensive floor for an even older birth year


def claim_age_adjustment_factor(birth_date: date, claim_date: date) -> float:
    """
    The multiplier applied to the monthly PIA for claiming at `claim_date` instead of exactly at
    Full Retirement Age (FRA): `1.0` at FRA; below `1.0` for claiming early (20 CFR § 404.410: 5/9
    of 1% per month for the first 36 months early, 5/12 of 1% per month for each month beyond
    that); above `1.0` for claiming late (this module's own delayed-retirement-credit rate,
    prorated per month, capped at age 70 since credits stop accumulating there regardless of
    further delay).

    Raises `ValueError` if `claim_date` is outside the legally valid claiming window, age 62
    through age 70 inclusive -- claiming earlier or later than that isn't a real, meaningful input
    (per this module's own spec: "raise... rather than silently extrapolating the formula past its
    real domain"), so a caller passing one is a real bug, not an edge case to paper over.
    """
    earliest_claim_date = _add_years_months(birth_date, 62, 0)
    latest_claim_date = _add_years_months(birth_date, 70, 0)
    if claim_date < earliest_claim_date or claim_date > latest_claim_date:
        raise ValueError(
            f"claim_date {claim_date} is outside the valid claiming window for this birth date -- "
            f"must be between age 62 ({earliest_claim_date}) and age 70 ({latest_claim_date})"
        )

    fra_date = full_retirement_date(birth_date)
    if claim_date < fra_date:
        months_early = months_between(claim_date, fra_date)
        first_36 = min(months_early, 36)
        beyond_36 = max(0, months_early - 36)
        reduction = first_36 * _EARLY_REDUCTION_RATE_FIRST_36_MONTHS + beyond_36 * _EARLY_REDUCTION_RATE_BEYOND_36_MONTHS
        return 1.0 - reduction
    if claim_date > fra_date:
        months_late = months_between(fra_date, claim_date)
        annual_rate = _delayed_retirement_credit_annual_rate(ssa_effective_birth_year(birth_date))
        return 1.0 + months_late * (annual_rate / 12.0)
    return 1.0


def annual_ss_benefit(aime: float, bend_point_table: dict, birth_date: date, claim_date: date) -> float:
    """
    Composes the above into the one number `modules.tax.compute_taxes`'s `ss_benefit_gross`
    parameter actually needs: this person's OWN eligibility-year bend points (`bend_points_for_
    year`, looked up at the SSA-effective year they attain age 62) resolve `aime` into a monthly
    PIA (`bend_point_pia`), the claim-age adjustment (`claim_age_adjustment_factor`) scales it for
    early/late claiming, and `x 12` converts the monthly figure to the annual gross benefit.

    No spousal, survivor, or divorced-spouse benefit calculations -- this is the WORKER's own
    retirement benefit only (a materially different, separate calculation with its own claiming
    rules, out of scope here).
    """
    eligibility_year = ssa_effective_birth_year(birth_date) + 62
    bend_point_1, bend_point_2 = bend_points_for_year(eligibility_year, bend_point_table)
    monthly_pia = bend_point_pia(aime, bend_point_1, bend_point_2)
    factor = claim_age_adjustment_factor(birth_date, claim_date)
    return monthly_pia * factor * 12.0


# The number of quarters of coverage (QC, a.k.a. Social Security "credits") required to be "fully
# insured" and receive a retired-worker benefit AT ALL (ssa.gov/OACT/ProgData/insured.html,
# verified live 2026-08-31): generally one QC per calendar year after turning 21 through the
# earlier of turning 62 / dying / becoming disabled, minimum 6, maximum 40. For anyone at or past
# typical retirement-planning age -- i.e. nearly every real user of this app -- that ceiling means
# 40, full stop; the true "elapsed years since 21" rule is a documented, deliberate simplification
# this module doesn't implement (worth a follow-up only for someone who turned 21 recently enough
# that 40 wouldn't actually apply to them yet).
REQUIRED_CREDITS_FOR_FULLY_INSURED = 40

# A single year's earnings can never buy more than this many QCs, no matter how far above
# qc_threshold they are (ssa.gov/oact/cola/QC.html, verified live 2026-08-31).
_MAX_CREDITS_PER_YEAR = 4


def quarters_of_coverage(earnings_by_year: dict[int, float], qc_threshold: float) -> int:
    """
    Total Social Security "credits" (quarters of coverage) earned across `earnings_by_year`: each
    year contributes `min(4, floor(earnings_that_year / qc_threshold))` -- one credit per
    `qc_threshold` of SS-taxable earnings that year, capped at 4/year regardless of how far above
    the threshold that year's earnings are (a single $100,000 year buys exactly 4 credits, not 4 +
    however-many-multiples-over). `qc_threshold` is a single resolved value (see
    `qc_threshold_for_year`), applied flat across every year -- see this module's own top-of-file
    docstring for why that's the correct treatment under its real-dollar convention.
    """
    return sum(min(_MAX_CREDITS_PER_YEAR, math.floor(earnings / qc_threshold)) for earnings in earnings_by_year.values())


def is_fully_insured(
    earnings_by_year: dict[int, float], qc_threshold: float, required_credits: int = REQUIRED_CREDITS_FOR_FULLY_INSURED
) -> bool:
    """
    Whether this earnings record clears the "fully insured" bar for a retired-worker benefit at
    all -- `quarters_of_coverage(...) >= required_credits`. A worker short of the required count
    receives `$0`: the benefit FORMULA never even applies (`bend_point_pia`/`annual_ss_benefit`
    have no eligibility gate of their own -- see `insured_annual_ss_benefit` below, the intended
    caller-facing entry point that actually enforces this), this isn't "a smaller benefit," it's
    no benefit. `required_credits` defaults to `REQUIRED_CREDITS_FOR_FULLY_INSURED` (40) -- see
    that constant's own docstring for the simplification it carries.
    """
    return quarters_of_coverage(earnings_by_year, qc_threshold) >= required_credits


def insured_annual_ss_benefit(
    earnings_by_year: dict[int, float],
    bend_point_table: dict,
    qc_threshold: float,
    birth_date: date,
    claim_date: date,
) -> tuple[float, bool, int]:
    """
    The intended top-level entry point for a real annual benefit figure — wraps `compute_aime` +
    `is_fully_insured` + `annual_ss_benefit` together so the "fully insured" gate can never be
    forgotten at one call site while being applied at another (Module F, 2026-08-31 — a real,
    user-caught bug: nothing gated the benefit formula at all before this, so even a single
    high-earning year could show a small-but-nonzero benefit instead of the correct `$0`).

    Returns `(annual_benefit, is_insured, credits_earned)`: `(0.0, False, credits_earned)` when
    not fully insured — the formula is never even evaluated, matching the real rule that an
    under-credited worker receives no benefit at all, not a reduced one — or
    `(annual_ss_benefit(...), True, credits_earned)` otherwise. `credits_earned` is always
    returned, insured or not, so a caller can show "X of `required_credits` credits" either way.

    Still raises `ValueError` (via `annual_ss_benefit` -> `claim_age_adjustment_factor`) for a
    `claim_date` outside the valid 62-70 window — that's a separate, genuine input error, not an
    insurance-status question, and this function doesn't paper over it.
    """
    credits = quarters_of_coverage(earnings_by_year, qc_threshold)
    if not is_fully_insured(earnings_by_year, qc_threshold):
        return 0.0, False, credits
    aime = compute_aime(earnings_by_year)
    return annual_ss_benefit(aime, bend_point_table, birth_date, claim_date), True, credits
